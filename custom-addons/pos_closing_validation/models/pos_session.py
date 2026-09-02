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
    expected_opening_balance = fields.Monetary(
        string="Saldo de apertura esperado",
        readonly=True,
        copy=False,
        help="Saldo que Odoo calculó automáticamente al abrir la sesión. "
             "Se compara con el saldo ingresado por el operador para detectar "
             "inconsistencias de continuidad de caja.",
        currency_field="currency_id",
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

    def _get_blocking_reasons(self):
        """Return list of strings explaining why this session cannot close.

        Aggregates all blocking conditions into a single list for the
        frontend to display.
        """
        self.ensure_one()
        reasons = []

        if self.rescue:
            reasons.append("Sesión de rescate no puede cerrarse desde aquí.")

        if self.state == "closed":
            reasons.append("La sesión ya está cerrada.")

        if self.state == "closing_control":
            reasons.append("La sesión ya está en proceso de cierre.")

        if (
            not self.rescue
            and self.config_id.enable_rescue_session_validation
        ):
            pending = self._get_pending_rescue_sessions()
            if pending:
                names = ", ".join(pending.mapped("name"))
                reasons.append(
                    f"Sesiones de rescate pendientes: {names}"
                )

        integrity = self._check_cash_in_out_integrity()
        if integrity:
            reasons.append(integrity["message"])

        data_integrity = self._check_session_data_integrity()
        if data_integrity:
            reasons.append(data_integrity["message"])

        return reasons

    def get_closing_control_data(self):
        """Override: enrich standard Odoo closing data with validation fields.

        This is the single source of truth for the closing popup.
        The frontend ``ClosePosPopup`` consumes this data directly.
        """
        data = super().get_closing_control_data()

        validation = self._get_closing_cash_validation_data()
        pending = self._get_pending_rescue_sessions()
        reasons = self._get_blocking_reasons()

        data.update({
            "pending_rescue": bool(pending),
            "pending_rescue_sessions": [
                {"id": s.id, "name": s.name, "state": s.state}
                for s in pending
            ],
            "can_close": not bool(reasons),
            "blocking_reasons": reasons,
            "session_id": self.id,
            "session_name": self.name,
            "is_rescue": self.rescue,
            "opening_cash": validation["opening_cash"],
            "cash_sales": validation["cash_sales"],
            "cash_in": validation["cash_in"],
            "cash_out": validation["cash_out"],
            "expected_cash": validation["expected_cash"],
            "difference": validation["difference"],
            "cash_move_count": validation["cash_move_count"],
            "cash_move_limit": self.config_id.maximum_cash_in_out_moves,
        })

        return data

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

        Cash In/Out movements are not permitted on rescue sessions. The
        user sees a friendly message (POS not synchronized) rather than
        a technical reference to "rescue" to avoid confusing store staff.
        """
        self.ensure_one()

        if self.rescue:
            raise UserError(_(
                "El Punto de Venta no está sincronizado con la sesión "
                "actual.\n\n"
                "Actualice la página del Punto de Venta y vuelva a "
                "intentarlo.\n\n"
                "Si el problema persiste, contacte a un administrador."
            ))

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

    def _filter_non_empty_rescues(self):
        """Return the subset of self with orders, payments, or statement lines.

        Batched equivalent of ``filtered(lambda s: not s._is_empty_rescue())``.
        Executes 3 database queries total regardless of how many rescue
        sessions are in the recordset, so cost stays constant as
        accumulated rescues grow across sedes.

        Non-rescue sessions in self are always excluded from the result
        (the method is designed for filtering rescue sessions).
        """
        rescue_ids = self.filtered(lambda s: s.rescue).ids
        if not rescue_ids:
            return self.env["pos.session"]

        with_orders = set(
            self.env["pos.order"].search([
                ("session_id", "in", rescue_ids),
            ]).mapped("session_id.id")
        )
        with_payments = set(
            self.env["pos.payment"].search([
                ("session_id", "in", rescue_ids),
            ]).mapped("session_id.id")
        )
        # statement.line access requires sudo for POS users; matches the
        # pattern used elsewhere in this module (see _get_closing_cash_validation_data)
        with_lines = set(
            self.env["account.bank.statement.line"].sudo().search([
                ("pos_session_id", "in", rescue_ids),
            ]).mapped("pos_session_id.id")
        )

        non_empty_ids = list(with_orders | with_payments | with_lines)
        return self.browse(non_empty_ids)

    def _check_rescue_sessions_pending(self):
        """Return error dict if non-empty rescue sessions are open.

        CLOSING validation: only blocks if the rescue has meaningful data
        (orders, payments, or statement lines).  Empty rescues are ignored
        because they do not affect the parent session's cash integrity.

        This is stricter for OPENING (see ``_check_pending_rescue_sessions``)
        where ANY open rescue blocks, regardless of emptiness.
        """
        self.ensure_one()

        if not self.config_id.enable_rescue_session_validation:
            return None

        rescue_sessions = self._get_pending_rescue_sessions()._filter_non_empty_rescues()

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
        """Validate and close with FOR UPDATE to prevent race conditions.

        The lock is acquired at the start to ensure that two terminals
        cannot simultaneously close the same session with stale snapshots.

        Rescue sessions cannot be closed through this endpoint — they must
        be closed from the backend view. This is consistent with the
        business rule that rescue sessions are backend-only.

        Only validations unique to this method run here (cash difference,
        which must be checked BEFORE super writes counted_cash).  All
        other validations (rescue pending, cash in/out integrity, data
        integrity) are handled by our ``_cannot_close_session`` override,
        which Odoo's standard ``post_closing_cash_details`` invokes
        internally before writing the counted balance.
        """
        self.ensure_one()

        # Rescue sessions are backend-only; refuse to close from POS flow.
        if self.rescue:
            return {
                "successful": False,
                "message": _(
                    "Las sesiones de rescate no pueden cerrarse desde el "
                    "Punto de Venta. Cierre la sesión desde la vista del "
                    "backend."
                ),
                "redirect": False,
            }

        # Step 1: Acquire row-level lock
        self.env.cr.execute(
            "SELECT id FROM pos_session WHERE id = %s FOR UPDATE",
            (self.id,),
        )

        # Step 2: Invalidate ORM cache — subsequent reads hit the database
        self.invalidate_recordset()

        # Step 3: Unique validation — must run before super because
        # super stores counted_cash in cash_register_balance_end_real.
        error = self._check_authorized_cash_difference(counted_cash)
        if error:
            return error

        # Step 4: Odoo's standard flow — calls _cannot_close_session
        # (our override) which re-checks rescue pending, integrity,
        # and data consistency with fresh (post-lock) data.
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
            return {
                "successful": False,
                "message": _(
                    "Existe una inconsistencia en los movimientos de efectivo.\n\n"
                    "Se registraron %(current)s movimientos, pero el límite "
                    "configurado es de %(maximum)s.\n\n"
                    "No puede cerrar la sesión hasta que se resuelva esta "
                    "situación. Contacte a un responsable del Punto de Venta.",
                    current=current_count,
                    maximum=maximum,
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

    # ------------------------------------------------------------------
    # Opening balance audit
    # ------------------------------------------------------------------

    def action_pos_session_open(self):
        """Override to capture the expected opening balance.

        When Odoo sets ``cash_register_balance_start`` from the previous
        session's closing balance, we store it in ``expected_opening_balance``
        so that ``set_cashbox_pos`` can later compare it with the value
        entered by the operator.
        """
        for session in self.filtered(lambda s: s.state == "opening_control"):
            if session.config_id.cash_control and not session.rescue:
                last_session = self.search([
                    ("config_id", "=", session.config_id.id),
                    ("id", "!=", session.id),
                ], limit=1)
                if last_session:
                    session.expected_opening_balance = (
                        last_session.cash_register_balance_end_real
                    )

        return super().action_pos_session_open()

    def set_cashbox_pos(self, cashbox_value, notes):
        """Override to validate opening balance continuity.

        Compares the value entered by the operator with the expected
        opening balance calculated by Odoo.  If the difference exceeds
        the configured maximum, blocks the operation.
        """
        self.ensure_one()

        if (
            self.config_id.set_maximum_difference
            and self.expected_opening_balance
        ):
            difference = abs(cashbox_value - self.expected_opening_balance)
            maximum = self.config_id.amount_authorized_diff

            if difference > maximum:
                raise UserError(_(
                    "Inconsistencia de continuidad de caja.\n\n"
                    "Saldo esperado (calculado por el sistema): %(expected)s\n"
                    "Saldo ingresado por el operador: %(entered)s\n"
                    "Diferencia: %(diff)s\n"
                    "Máximo autorizado: %(maximum)s\n\n"
                    "El saldo ingresado difiere significativamente del saldo "
                    "que el sistema esperaba basándose en la sesión anterior. "
                    "Contacte a un responsable del Punto de Venta.",
                    expected=self.currency_id.format(
                        self.expected_opening_balance
                    ),
                    entered=self.currency_id.format(cashbox_value),
                    diff=self.currency_id.format(difference),
                    maximum=self.currency_id.format(maximum),
                ))

        return super().set_cashbox_pos(cashbox_value, notes)

    # ------------------------------------------------------------------
    # Cycle validation: pending rescue sessions
    # ------------------------------------------------------------------

    def _get_pending_rescue_sessions_for_config(self, config_id):
        """Return open rescue sessions for a POS config.

        Any rescue session with state != 'closed' is considered pending,
        regardless of whether it has orders or not.  This is stricter than
        the closing validation which ignores empty rescues.
        """
        return self.search([
            ("config_id", "=", config_id),
            ("rescue", "=", True),
            ("state", "!=", "closed"),
        ])

    def _has_pending_rescue_sessions(self):
        """Return True if this session's config has open rescue sessions."""
        self.ensure_one()
        return bool(
            self._get_pending_rescue_sessions_for_config(self.config_id.id)
        )

    def _get_pending_rescue_sessions(self):
        """Return recordset of open rescue sessions for this config."""
        self.ensure_one()
        return self._get_pending_rescue_sessions_for_config(self.config_id.id)

    def _get_pending_rescue_validation_data(self):
        """Return structured data about pending rescue sessions.

        Used by both backend validation and frontend display.
        """
        self.ensure_one()
        rescues = self._get_pending_rescue_sessions()
        return [
            {"id": s.id, "name": s.name, "state": s.state}
            for s in rescues
        ]

    def _check_pending_rescue_sessions(self):
        """Return error dict if any rescue session is open for this config.

        For OPENING validation: any open rescue blocks, no exceptions.
        Empty rescues are NOT ignored here — the operator must close or
        resolve every rescue before opening a new normal session.

        Returns ``None`` if no pending rescues exist.
        """
        self.ensure_one()
        pending = self._get_pending_rescue_sessions()
        if pending:
            names = ", ".join(pending.mapped("name"))
            return {
                "blocked": True,
                "reason": "pending_rescue",
                "message": _(
                    "No puede abrir una nueva sesión porque existe(n) "
                    "%(count)s sesión(es) de rescate pendiente(s) "
                    "para este Punto de Venta.\n\n"
                    "Sesiones pendientes: %(names)s\n\n"
                    "Cierre las sesiones de rescate antes de continuar.",
                    count=len(pending),
                    names=names,
                ),
                "sessions": [
                    {"id": s.id, "name": s.name}
                    for s in pending
                ],
            }
        return {"blocked": False, "reason": "", "sessions": []}
