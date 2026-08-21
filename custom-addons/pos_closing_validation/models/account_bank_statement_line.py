from odoo import fields, models


class AccountBankStatementLine(models.Model):
    """Flag statement lines created as POS Cash In/Out movements."""

    _inherit = "account.bank.statement.line"

    pos_cash_move = fields.Boolean(
        string="POS Cash In/Out",
        default=False,
        readonly=True,
        copy=False,
        index=True,
        help="Indicates that this statement line was created as a POS Cash In/Out movement.",
    )
