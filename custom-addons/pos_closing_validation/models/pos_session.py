from odoo import api, models, fields, _
from odoo.exceptions import UserError

DEFAULT_CASH_DIFFERENCE_BODY = _(
    "No puede cerrar esta sesión de Punto de Venta.\n\n"
    "La diferencia de efectivo supera la diferencia máxima autorizada.\n\n"
    "Debe contactar a un responsable del Punto de Venta."
)


class PosSession(models.Model):
    """Extend POS session with Cash In/Out movement limits and closing
    cash difference validation."""

    _inherit = "pos.session"

    rescue_parent_session_id = fields.Many2one(
        "pos.session",
        string="Sesión Padre",
        readonly=True,
        copy=False,
        help="Sesión original que originó esta sesión de rescate.",
    )
    rescue_session_ids = fields.One2many(
        "pos.session",
        "rescue_parent_session_id",
        string="Sesiones de Rescate",
        readonly=True,
    )

    # ------------------------------------------------------------------
    # Snapshot: single source of truth for closing validation
    # ------------------------------------------------------------------

    def _get_closing_cash_validation_data(self):
        """Produce the single source of truth for closing validation.

        Computes expected cash, counted cash, difference, and all
        supporting data from the same set of database records that
        Odoo uses internally.  Both backend validation and frontend
        display should consume this snapshot.
        """
        self.ensure_one()
        config = self.config_id

        opening = self.cash_register_balance_start

        # Cash payments from POS orders
        cash_pm = self.payment_method_ids.filtered(
            lambda pm: pm.type == "cash"
        )[:1]
        cash_sales = 0.0
        if cash_pm:
            cash_sales = sum(
                self.env["pos.payment"]
                .search([
                    ("session_id", "=", self.id),
                    ("payment_method_id", "=", cash_pm.id),
                ])
                .mapped("amount")
            )

        # Cash In / Cash Out (statement lines flagged as pos_cash_move)
        cash_in_lines = self.statement_line_ids.filtered(
            lambda l: l.pos_cash_move and l.amount > 0
        )
        cash_out_lines = self.statement_line_ids.filtered(
            lambda l: l.pos_cash_move and l.amount < 0
        )
        cash_in = sum(cash_in_lines.mapped("amount"))
        cash_out = abs(sum(cash_out_lines.mapped("amount")))

        expected = opening + cash_sales + cash_in - cash_out
        counted = self.cash_register_balance_end_real or 0.0

        state = self.state
        is_rescue = self.rescue
        must_block = is_rescue or state in ("closing_control", "closed")
        if is_rescue:
            reason = "rescue_session"
        elif state == "closed":
            reason = "session_closed"
        elif state == "closing_control":
            reason = "session_closing_control"
        else:
            reason = ""

        has_orders = bool(self.order_ids)

        return {
            "session_id": self.id,
            "session_name": self.name,
            "state": state,
            "is_rescue": is_rescue,
            "must_block": must_block,
            "reason": reason,
            "parent_session_name": (
                self.rescue_parent_session_id.name if self.rescue else False
            ),
            "opening_cash": opening,
            "cash_sales": cash_sales,
            "cash_in": cash_in,
            "cash_in_count": len(cash_in_lines),
            "cash_out": cash_out,
            "cash_out_count": len(cash_out_lines),
            "cash_move_count": len(cash_in_lines) + len(cash_out_lines),
            "expected_cash": expected,
            "counted_cash": counted,
            "difference": counted - expected,
            "statement_lines_count": len(self.statement_line_ids),
            "orders_count": len(self.order_ids),
            "has_orders": has_orders,
        }

    # ------------------------------------------------------------------
    # Cash In / Out helpers
    # ------------------------------------------------------------------

    def _get_cash_in_out_moves(self):
        """Return only statement lines flagged as Cash In/Out movements."""
        self.ensure_one()
        return self.statement_line_ids.filtered(
            lambda line: line.pos_cash_move
        )

    def _get_cash_in_out_move_count(self):
        """Return the number of Cash In/Out movements in this session."""
        self.ensure_one()
        return len(self._get_cash_in_out_moves())

    def get_cash_in_out_control_data(self):
        """RPC endpoint for the POS frontend: returns current count and limit."""
        self.ensure_one()
        return {
            "count": self._get_cash_in_out_move_count(),
            "limit": self.config_id.maximum_cash_in_out_moves,
        }

    def get_closing_validation_info(self):
        """RPC endpoint: returns the closing validation snapshot.

        The frontend uses this to display consistent expected/counted/difference
        data and to detect rescue sessions or sync issues.
        """
        self.ensure_one()
        return self._get_closing_cash_validation_data()

    def _check_cash_in_out_limit(self):
        """Raise UserError if the Cash In/Out limit is reached."""
        self.ensure_one()

        current_count = self._get_cash_in_out_move_count()
        maximum = self.config_id.maximum_cash_in_out_moves

        if current_count >= maximum:
            raise UserError(
                _(
                    "Se alcanzó el límite de movimientos de efectivo.\n\n"
                    "Este Punto de Venta permite un máximo de %(maximum)s "
                    "movimientos Cash In/Out por sesión.\n"
                    "Ya se han registrado %(current)s movimientos.",
                    maximum=maximum,
                    current=current_count,
                )
            )

    def try_cash_in_out(self, _type, amount, reason, extras):
        """Override to enforce the movement limit and tag new statement lines.

        Serialises the operation with ``FOR UPDATE`` to prevent concurrent
        terminals from exceeding the limit simultaneously.
        """
        self.ensure_one()

        self.env.cr.execute(
            "SELECT id FROM pos_session WHERE id = %s FOR UPDATE",
            (self.id,),
        )

        self._check_cash_in_out_limit()

        statement_lines_before = self.statement_line_ids

        result = super().try_cash_in_out(
            _type,
            amount,
            reason,
            extras,
        )

        self.invalidate_recordset(["statement_line_ids"])

        statement_lines_after = self.statement_line_ids

        new_lines = statement_lines_after - statement_lines_before

        if new_lines:
            new_lines.write({
                "pos_cash_move": True,
            })

        return result

    # ------------------------------------------------------------------
    # Closing validation: _cannot_close_session
    # ------------------------------------------------------------------

    def _cannot_close_session(self, bank_payment_method_diffs=None):
        """Odoo 17 extension point: block session close when validations fail."""
        result = super()._cannot_close_session(bank_payment_method_diffs)

        if result:
            return result

        # Check rescue sessions pending (only for non-rescue sessions)
        if not self.rescue and self.config_id.enable_rescue_session_validation:
            rescue_error = self._check_rescue_sessions_pending()
            if rescue_error:
                return rescue_error

        # Cash In/Out integrity
        error = self._check_cash_in_out_integrity()
        if error:
            return error

        # Data integrity
        integrity_error = self._check_session_data_integrity()
        if integrity_error:
            return integrity_error

        return result

    def _is_empty_rescue(self):
        """Return True if this rescue session has no meaningful data.

        A rescue session is considered empty when it has:
        - No POS orders (any state: draft, paid, done, invoice, cancel)
        - No payments
        - No bank statement lines
        """
        self.ensure_one()
        if not self.rescue:
            return False

        has_orders = self.env["pos.order"].search_count(
            [("session_id", "=", self.id)], limit=1
        )
        has_payments = self.env["pos.payment"].search_count(
            [("session_id", "=", self.id)], limit=1
        )
        has_statement_lines = self.env["account.bank.statement.line"].search_count(
            [("pos_session_id", "=", self.id)], limit=1
        )
        return not (has_orders or has_payments or has_statement_lines)

    def _check_rescue_sessions_pending(self):
        """Return error dict if there are open rescue sessions for this config.

        Blocks the session close and advises the user to review rescue
        sessions before proceeding.  Empty rescue sessions (no orders,
        no payments, no statement lines) are ignored.
        """
        self.ensure_one()

        if not self.config_id.enable_rescue_session_validation:
            return None

        rescue_sessions = self.search([
            ("config_id", "=", self.config_id.id),
            ("rescue", "=", True),
            ("state", "!=", "closed"),
        ]).filtered(lambda s: not s._is_empty_rescue())

        if rescue_sessions:
            return {
                "successful": False,
                "message": _(
                    "Existe(n) %(count)s sesión(es) de rescate pendiente(s) "
                    "para este Punto de Venta.\n\n"
                    "Se recomienda revisarlas antes de cerrar para asegurar "
                    "que todos los movimientos estén contabilizados.\n\n"
                    "Sesiones: %(names)s",
                    count=len(rescue_sessions),
                    names=", ".join(rescue_sessions.mapped("name")),
                ),
                "redirect": False,
            }
        return None

    # ------------------------------------------------------------------
    # Closing validation: post_closing_cash_details
    # ------------------------------------------------------------------

    def post_closing_cash_details(self, counted_cash):
        """Validate cash difference BEFORE Odoo stores the counted cash.

        Uses the snapshot to compute the expected value, ensuring
        consistent results between backend and frontend.
        """
        self.ensure_one()

        error = self._check_authorized_cash_difference(counted_cash)
        if error:
            return error

        return super().post_closing_cash_details(counted_cash)

    def _check_authorized_cash_difference(self, counted_cash):
        """Return error dict if cash difference exceeds the configured maximum.

        Uses ``_get_closing_cash_validation_data()`` as the single source
        of truth instead of ``cash_register_balance_end`` which can be
        stale for rescue sessions or sessions with unsynced data.
        """
        self.ensure_one()

        config = self.config_id

        if not config.set_maximum_difference:
            return None

        data = self._get_closing_cash_validation_data()
        difference = abs(counted_cash - data["expected_cash"])
        maximum_difference = config.amount_authorized_diff

        if difference > maximum_difference:
            body = (
                config.cash_difference_exceeded_message
                or DEFAULT_CASH_DIFFERENCE_BODY
            )
            return {
                "successful": False,
                "message": _(
                    "%(body)s\n\n"
                    "Diferencia: %(difference)s\n"
                    "Máximo autorizado: %(maximum)s",
                    body=body,
                    expected=self.currency_id.format(data["expected_cash"]),
                    counted=self.currency_id.format(counted_cash),
                    difference=self.currency_id.format(difference),
                    maximum=self.currency_id.format(maximum_difference),
                ),
                "redirect": False,
            }

        return None

    # ------------------------------------------------------------------
    # Closing validation: rescue session close
    # ------------------------------------------------------------------

    def action_pos_session_closing_control(
        self, balancing_account=False, amount_to_balance=0,
        bank_payment_method_diffs=None,
    ):
        """Override to recompute cash_register_balance_end_real for rescue
        sessions so that Cash In/Out movements are included in the
        closing balance.

        Odoo's default rescue close only considers order payments + opening,
        ignoring Cash In/Out statement lines.  This override fixes that.
        """
        for session in self:
            if session.rescue and session.config_id.cash_control:
                cash_pm = session.payment_method_ids.filtered(
                    lambda pm: pm.type == "cash"
                )[:1]

                cash_sales = 0.0
                if cash_pm:
                    cash_sales = sum(
                        self.env["pos.payment"]
                        .search([
                            ("session_id", "=", session.id),
                            ("payment_method_id", "=", cash_pm.id),
                        ])
                        .mapped("amount")
                    )

                cash_in_out = sum(
                    session.statement_line_ids.filtered(
                        lambda l: l.pos_cash_move
                    ).mapped("amount")
                )

                session.cash_register_balance_end_real = (
                    session.cash_register_balance_start
                    + cash_sales
                    + cash_in_out
                )

        return super().action_pos_session_closing_control(
            balancing_account, amount_to_balance, bank_payment_method_diffs
        )

    # ------------------------------------------------------------------
    # Cash In/Out integrity
    # ------------------------------------------------------------------

    def _check_cash_in_out_integrity(self):
        """Return error dict if movement count exceeds the configured limit.

        This is an integrity safety net: normally the limit prevents creating
        excess movements, but if data inconsistencies occur (e.g. 4/3), the
        session must not be closed until the issue is resolved.
        Returns ``None`` if the count is within bounds.
        """
        self.ensure_one()

        current_count = self._get_cash_in_out_move_count()
        maximum = self.config_id.maximum_cash_in_out_moves

        if current_count > maximum:
            rescue_note = ""
            if self.rescue:
                rescue_note = _(
                    "\n\nEsta es una sesión de rescate. Los movimientos "
                    "pueden haber sido heredados de la sesión original."
                )
            return {
                "successful": False,
                "message": _(
                    "Existe una inconsistencia en los movimientos de efectivo.\n\n"
                    "Se registraron %(current)s movimientos, pero el límite "
                    "configurado es de %(maximum)s."
                    "%(rescue_note)s\n\n"
                    "No puede cerrar la sesión hasta que se resuelva esta "
                    "situación. Contacte a un responsable del Punto de Venta.",
                    current=current_count,
                    maximum=maximum,
                    rescue_note=rescue_note,
                ),
                "redirect": False,
            }

        return None

    # ------------------------------------------------------------------
    # Data integrity
    # ------------------------------------------------------------------

    def _check_session_data_integrity(self):
        """Return error dict if session data has inconsistencies.

        Checks for:
        - Paid orders without associated payments
        - Orphan payments without an order
        - Cash statement lines missing the pos_cash_move flag
        """
        self.ensure_one()
        issues = []

        # Paid orders without payments
        paid_no_payments = self.env["pos.order"].search([
            ("session_id", "=", self.id),
            ("state", "=", "paid"),
            ("payment_ids", "=", False),
        ])
        if paid_no_payments:
            issues.append(
                _("Hay %(count)s órdenes pagadas sin pagos registrados.",
                  count=len(paid_no_payments))
            )

        # Orphan payments
        orphan_payments = self.env["pos.payment"].search([
            ("session_id", "=", self.id),
            ("pos_order_id", "=", False),
        ])
        if orphan_payments:
            issues.append(
                _("Hay %(count)s pagos huérfanos sin orden asociada.",
                  count=len(orphan_payments))
            )

        if issues:
            return {
                "successful": False,
                "message": _(
                    "Se detectaron inconsistencias en los datos de la sesión:\n\n"
                    "%(issues)s\n\n"
                    "Contacte a un responsable antes de cerrar.",
                    issues="\n".join(f"  • {i}" for i in issues),
                ),
                "redirect": False,
            }
        return None

    # ------------------------------------------------------------------
    # Rescue session linking
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        """Link rescue sessions to their parent (most recent normal session)."""
        for vals in vals_list:
            if vals.get("rescue") and vals.get("config_id"):
                last_normal = self.search([
                    ("config_id", "=", vals["config_id"]),
                    ("rescue", "=", False),
                ], order="id desc", limit=1)
                if last_normal:
                    vals["rescue_parent_session_id"] = last_normal.id
        return super().create(vals_list)
