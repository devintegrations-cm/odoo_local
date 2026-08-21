from odoo import models, _
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

    def _cannot_close_session(self, bank_payment_method_diffs=None):
        """Odoo 17 extension point: block session close when validations fail."""
        result = super()._cannot_close_session(bank_payment_method_diffs)

        if result:
            return result

        error = self._check_cash_in_out_integrity()
        if error:
            return error

        return result

    def post_closing_cash_details(self, counted_cash):
        """Validate cash difference BEFORE Odoo stores the counted cash.

        Returns ``{'successful': False, 'message': ..., 'redirect': False}``
        if the difference exceeds the configured maximum, keeping the error
        inside the POS popup instead of redirecting to the backend.
        """
        self.ensure_one()

        error = self._check_authorized_cash_difference(counted_cash)
        if error:
            return error

        return super().post_closing_cash_details(counted_cash)

    def _check_authorized_cash_difference(self, counted_cash):
        """Return error dict if cash difference exceeds the configured maximum.

        Only enforced when ``set_maximum_difference`` is enabled in the POS.
        Returns ``None`` if the difference is acceptable.
        """
        self.ensure_one()

        config = self.config_id

        if not config.set_maximum_difference:
            return None

        difference = abs(counted_cash - self.cash_register_balance_end)
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
                    "Diferencia de efectivo: %(difference)s\n"
                    "Máximo autorizado: %(maximum)s",
                    body=body,
                    difference=self.currency_id.format(difference),
                    maximum=self.currency_id.format(maximum_difference),
                ),
                "redirect": False,
            }

        return None

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
