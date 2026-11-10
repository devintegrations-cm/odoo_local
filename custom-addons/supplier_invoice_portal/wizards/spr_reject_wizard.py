# -*- coding: utf-8 -*-
from odoo import fields, models


class SupplierPaymentRequestReject(models.TransientModel):
    """Pide el motivo antes de rechazar: el proveedor lo va a leer en el portal."""

    _name = "supplier.payment.request.reject"
    _description = "Rechazar solicitud de pago"

    request_id = fields.Many2one(
        "supplier.payment.request", string="Solicitud", required=True, readonly=True
    )
    reason = fields.Text(string="Motivo del rechazo", required=True)

    def action_confirm(self):
        self.ensure_one()
        self.request_id.action_reject(reason=self.reason.strip())
        return {"type": "ir.actions.act_window_close"}
