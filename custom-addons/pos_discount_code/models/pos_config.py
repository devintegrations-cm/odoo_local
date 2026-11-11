from odoo import fields, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    show_discount_code_button = fields.Boolean(
        string="Mostrar botón de código de descuento",
        default=True,
        help="Muestra un botón en la pantalla del POS para capturar tipo de "
             "documento, número de documento y código de descuento (bono).",
    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    pos_show_discount_code_button = fields.Boolean(
        related="pos_config_id.show_discount_code_button",
        readonly=False,
        string="Mostrar botón de código de descuento",
        help="Muestra un botón en la pantalla del POS para capturar tipo de "
             "documento, número de documento y código de descuento (bono).",
    )
