from odoo import fields, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    cash_in_out_message_enabled = fields.Boolean(
        string="Enable Cash In/Out Message",
        default=False,
    )
    cash_in_out_message = fields.Text(
        string="Cash In/Out Message",
        help="Message displayed in the Cash In/Out popup.",
    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    pos_cash_in_out_message_enabled = fields.Boolean(
        related="pos_config_id.cash_in_out_message_enabled",
        readonly=False,
    )
    cash_in_out_message = fields.Text(
        related="pos_config_id.cash_in_out_message",
        readonly=False,
        string="Cash In/Out Message",
        help="Message displayed above the Reason field in the Cash In/Out popup.",
    )