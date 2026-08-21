from odoo import api, fields, models
from odoo.exceptions import ValidationError


class PosConfig(models.Model):
    """Extend POS configuration with a maximum Cash In/Out movement limit."""

    _inherit = "pos.config"

    maximum_cash_in_out_moves = fields.Integer(
        string="Máximo de movimientos de efectivo",
        default=2,
        required=True,
        help="Número máximo de movimientos Cash In/Out permitidos por sesión.",
    )

    cash_difference_exceeded_message = fields.Text(
        string="Mensaje de diferencia de efectivo",
        help="Texto que se muestra cuando la diferencia de efectivo supera el máximo "
             "autorizado al cerrar la sesión. Solo escriba el texto; los valores de la "
             "diferencia y del máximo autorizado se añaden automáticamente al final. "
             "Déjelo vacío para usar el mensaje por defecto.",
    )

    @api.constrains("maximum_cash_in_out_moves")
    def _check_maximum_cash_in_out_moves(self):
        """Ensure the configured limit is at least 1."""
        for config in self:
            if config.maximum_cash_in_out_moves < 1:
                raise ValidationError(
                    "El máximo de movimientos de efectivo debe ser mayor que cero."
                )


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
