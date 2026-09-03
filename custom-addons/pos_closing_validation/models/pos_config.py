from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class PosConfig(models.Model):
    """Extend POS configuration with a maximum Cash In/Out movement limit."""

    _inherit = "pos.config"

    maximum_cash_in_out_moves = fields.Integer(
        string="Máximo de movimientos de efectivo. ",
        default=2,
        required=True,
        help="Número máximo de movimientos Cash In/Out permitidos por sesión.",
    )

    cash_difference_exceeded_message = fields.Text(
        string="Mensaje de diferencia de efectivo: ",
        help="Texto que se muestra cuando la diferencia de efectivo supera el máximo "
             "autorizado al cerrar la sesión. Solo escriba el texto; los valores de la "
             "diferencia y del máximo autorizado se añaden automáticamente al final. "
             "Déjelo vacío para usar el mensaje por defecto.",
    )

    enable_rescue_session_validation = fields.Boolean(
        string="Validar sesiones de rescate",
        default=False,
        help="Mostrar advertencias cuando existan sesiones de rescate pendientes "
             "al cerrar la sesión normal.",
    )

    @api.constrains("maximum_cash_in_out_moves")
    def _check_maximum_cash_in_out_moves(self):
        """Ensure the configured limit is at least 1."""
        for config in self:
            if config.maximum_cash_in_out_moves < 1:
                raise ValidationError(
                    "El máximo de movimientos de efectivo debe ser mayor que cero."
                )

    def open_ui(self):
        """Validate pending rescue sessions before opening a new session.

        This runs on the backend when the operator clicks "Open" from the
        dashboard, BEFORE the session is created and BEFORE the browser
        is redirected to the POS UI.

        The check fires when:
        - ``enable_rescue_session_validation`` is enabled for this POS, AND
        - There is at least one pending rescue session for this config, AND
        - Either the user has no current session (about to create a new one),
          OR their current session IS a rescue (which the /pos/ui controller
          filters out anyway, causing a silent bounce to the dashboard).

        Blocking here gives operators a clear message instead of the Odoo
        controller's silent dashboard redirect.
        """
        self.ensure_one()

        if self.enable_rescue_session_validation:
            pending = self.env["pos.session"]._get_pending_rescue_sessions_for_config(
                self.id
            )
            if pending and (
                not self.current_session_id or self.current_session_id.rescue
            ):
                names = ", ".join(pending.mapped("name"))
                raise UserError(_(
                    "No puede abrir una nueva sesión porque existe(n) "
                    "%(count)s sesión(es) de rescate pendiente(s) "
                    "para este Punto de Venta.\n\n"
                    "Sesiones pendientes: %(names)s\n\n"
                    "Cierre las sesiones de rescate antes de continuar.",
                    count=len(pending),
                    names=names,
                ))

        return super().open_ui()


class ResConfigSettings(models.TransientModel):
    """Expose the Cash In/Out movement limit in Odoo Settings."""

    _inherit = "res.config.settings"

    pos_maximum_cash_in_out_moves = fields.Integer(
        related="pos_config_id.maximum_cash_in_out_moves",
        readonly=False,
        string="Máximo de movimientos de efectivo",
        help="Número máximo de movimientos Cash In/Out permitidos por sesión.",
    )

    pos_cash_difference_exceeded_message = fields.Text(
        related="pos_config_id.cash_difference_exceeded_message",
        readonly=False,
        string="Mensaje de diferencia de efectivo",
        help="Texto que se muestra cuando la diferencia de efectivo supera el máximo "
             "autorizado al cerrar la sesión. Solo escriba el texto; los valores de la "
             "diferencia y del máximo autorizado se añaden automáticamente al final. "
             "Déjelo vacío para usar el mensaje por defecto.",
    )

    pos_enable_rescue_session_validation = fields.Boolean(
        related="pos_config_id.enable_rescue_session_validation",
        readonly=False,
        string="Validar sesiones de rescate",
        help="Mostrar advertencias cuando existan sesiones de rescate pendientes.",
    )
