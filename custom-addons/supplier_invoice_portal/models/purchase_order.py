# -*- coding: utf-8 -*-
from datetime import datetime

import pytz

from odoo import api, fields, models

from ..services import cutoff


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    spr_request_ids = fields.Many2many(
        "supplier.payment.request",
        "supplier_payment_request_purchase_rel",
        "purchase_id",
        "request_id",
        string="Solicitudes de pago",
        copy=False,
        groups="supplier_invoice_portal.group_spr_validator",  # DECISIONS.md #49
    )
    spr_request_count = fields.Integer(
        string="Numero de solicitudes", compute="_compute_spr_request_count",
        groups="supplier_invoice_portal.group_spr_validator",
    )

    @api.depends("spr_request_ids")
    def _compute_spr_request_count(self):
        for order in self:
            order.spr_request_count = len(order.spr_request_ids)

    @api.model
    def _spr_year_start(self):
        """1 de enero del ano en curso a las 00:00 de la compania, en UTC.

        El portal solo ofrece ordenes del ano en curso: las de anos anteriores
        ya no se radican por ahi (DECISIONS.md #50).
        """
        # sudo: el usuario portal no puede leer el contacto de la compania.
        tz = pytz.timezone(self.env.company.sudo().partner_id.tz or cutoff.DEFAULT_TZ)
        local_now = fields.Datetime.now().replace(tzinfo=pytz.UTC).astimezone(tz)
        start = tz.localize(datetime(local_now.year, 1, 1))
        return start.astimezone(pytz.UTC).replace(tzinfo=None)

    def _spr_vendor_bills(self):
        """Facturas de proveedor (no anuladas, borrador incluido) ligadas a la orden.

        Una orden con factura ya no acepta otra factura ni cuenta de cobro por el
        portal: lo que falte o sobre se corrige con nota credito o debito sobre
        esa factura (DECISIONS.md #40).
        """
        return self.invoice_ids.filtered(
            lambda move: move.move_type == "in_invoice" and move.state != "cancel"
        )

    def _spr_open_requests(self):
        """Radicaciones de factura o cuenta de cobro en curso sobre la orden.

        Todas menos las rechazadas y canceladas: mientras contabilidad no crea la
        factura, la orden todavia no tiene factura y sin esto se podria radicar
        otra vez (DECISIONS.md #46). Las notas no cuentan: corrigen una factura.
        """
        return self.spr_request_ids.filtered(
            lambda req: req.document_type in ("invoice", "support_doc")
            and req.state not in ("rejected", "cancelled")
        )

    def _spr_accepts_invoice(self):
        """True si el proveedor puede radicar por el portal una factura de esta orden.

        Las mismas condiciones que el formulario usa para ofrecerla
        (``_spr_open_orders`` del controlador): proveedor habilitado, orden
        confirmada, del ano en curso, no totalmente facturada y sin factura
        (DECISIONS.md #40 y #50).
        """
        self.ensure_one()
        return bool(
            self.partner_id.commercial_partner_id.portal_invoice_enabled
            and self.state in ("purchase", "done")
            and self.date_order >= self._spr_year_start()
            and self.invoice_status != "invoiced"
            and not self._spr_vendor_bills()
            and not self._spr_open_requests()
        )

    def action_view_spr_requests(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Solicitudes de pago",
            "res_model": "supplier.payment.request",
            "view_mode": "tree,form",
            "domain": [("purchase_ids", "in", self.id)],
            "context": {
                "default_purchase_ids": [(6, 0, self.ids)],
                "default_partner_id": self.partner_id.commercial_partner_id.id,
            },
        }
