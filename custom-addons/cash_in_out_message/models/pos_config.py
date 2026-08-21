from odoo import fields, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    cash_in_out_message = fields.Text(
        string="Cash In/Out Message",
        help="Message displayed in the Cash In/Out popup.",
    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    cash_in_out_message = fields.Text(
        related="pos_config_id.cash_in_out_message",
        readonly=False,
        string="Cash In/Out Message",
        help="Message displayed above the Reason field in the Cash In/Out popup.",
    )