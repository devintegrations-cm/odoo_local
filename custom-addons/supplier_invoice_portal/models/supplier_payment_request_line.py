# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.tools import float_is_zero


class SupplierPaymentRequestLine(models.Model):
    _name = "supplier.payment.request.line"
    _description = "Linea de solicitud de pago de proveedor"
    _order = "sequence, id"

    request_id = fields.Many2one(
        "supplier.payment.request",
        string="Solicitud",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(string="Secuencia", default=10)
    description = fields.Char(string="Descripcion", required=True)
    product_code = fields.Char(string="Codigo del proveedor")
    quantity = fields.Float(string="Cantidad", digits="Product Unit of Measure")
    price_unit = fields.Monetary(string="Precio unitario")
    tax_rate = fields.Float(string="% impuesto", digits=(16, 2))
    subtotal = fields.Monetary(string="Subtotal")
    unit_code = fields.Char(
        string="Unidad del documento", help="Codigo de unidad DIAN del XML (NIU, KGM, BG...).")

    # Conversion de unidades (DECISIONS.md #51): la factura puede cobrar en
    # bultos lo que la orden pide en kilos. El subtotal no cambia; la cantidad
    # y el precio se llevan a la unidad de la orden para compararlos y para
    # crear la factura.
    uom_factor = fields.Float(
        string="Factor de unidad", default=1.0, digits=(16, 6),
        help="Unidades de la orden de compra por cada unidad de la factura.",
    )
    uom_note = fields.Char(string="Nota de la conversion")
    po_quantity = fields.Float(
        string="Cantidad en unidad de la orden", compute="_compute_po_values",
        digits="Product Unit of Measure",
    )
    po_price_unit = fields.Monetary(
        string="Precio en unidad de la orden", compute="_compute_po_values")

    po_line_id = fields.Many2one(
        "purchase.order.line",
        string="Linea de la orden de compra",
        ondelete="set null",
        domain="[('order_id', 'in', parent.purchase_ids)]",
    )
    is_extra_charge = fields.Boolean(
        string="Cargo adicional",
        help="Flete, envio u otro cargo que la factura cobra y la orden de compra no "
             "tiene. Va a la factura con el producto de flete de Ajustes, sin orden.",
    )
    match_method = fields.Selection(
        [
            ("code", "Por codigo"),
            ("ai", "Por IA"),
            ("order", "Desde la orden"),
            ("charge", "Cargo adicional"),
            ("manual", "Manual"),
            ("none", "Sin emparejar"),
        ],
        string="Metodo de emparejamiento",
        default="none",
        required=True,
    )
    match_confidence = fields.Float(string="Confianza", digits=(3, 2))
    match_note = fields.Char(string="Nota del emparejamiento")

    # No almacenado: solo lo necesitan los campos Monetary para saber su moneda.
    currency_id = fields.Many2one(related="request_id.currency_id", string="Moneda")

    @api.depends("quantity", "price_unit", "uom_factor")
    def _compute_po_values(self):
        for line in self:
            factor = line.uom_factor if line.uom_factor > 0 else 1.0
            line.po_quantity = line.quantity * factor
            line.po_price_unit = line.price_unit / factor

    @api.onchange("po_line_id")
    def _onchange_po_line_id(self):
        """Si el validador cambia la linea de OC a mano, queda marcado como manual
        para que una revalidacion no lo pise."""
        for line in self:
            if line.po_line_id:
                line.is_extra_charge = False
                line.match_method = "manual"
                line.match_confidence = 1.0
                line.match_note = "Emparejada manualmente"
            elif line.match_method == "manual" and not line.is_extra_charge:
                line.match_method = "none"
                line.match_confidence = 0.0
                line.match_note = False

    @api.onchange("uom_factor")
    def _onchange_uom_factor(self):
        """Un factor puesto a mano tambien sobrevive a la revalidacion."""
        for line in self:
            if line.po_line_id:
                line.match_method = "manual"
                line.match_confidence = 1.0
                line.uom_note = "Factor puesto manualmente"

    @api.onchange("is_extra_charge")
    def _onchange_is_extra_charge(self):
        """Marcar a mano un flete tambien sobrevive a la revalidacion."""
        for line in self:
            if line.is_extra_charge:
                line.po_line_id = False
                line.match_method = "manual"
                line.match_confidence = 1.0
                line.match_note = "Marcada como cargo adicional"
            elif line.match_method in ("manual", "charge") and not line.po_line_id:
                line.match_method = "none"
                line.match_confidence = 0.0
                line.match_note = False

    def _implied_discount_pct(self):
        """Descuento porcentual implicito en la linea de la factura.

        Si ``subtotal < cantidad x precio`` la diferencia es un descuento del
        proveedor y se traslada a la factura como porcentaje.
        """
        self.ensure_one()
        gross = self.quantity * self.price_unit
        if float_is_zero(gross, precision_digits=2) or self.subtotal <= 0:
            return 0.0
        if self.subtotal >= gross:
            return 0.0
        return round((1.0 - self.subtotal / gross) * 100.0, 2)
