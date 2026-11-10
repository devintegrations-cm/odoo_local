# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services import dian_xml_parser


class AccountMove(models.Model):
    _inherit = "account.move"

    # OJO: no confundir con ei_uuid de l10n_co_edi_jorels, que guarda el CUFE de
    # las facturas que NOSOTROS emitimos. Este campo es el CUFE del documento que
    # el proveedor recibio de la DIAN.
    cufe = fields.Char(
        string="CUFE del proveedor",
        size=96,
        index=True,
        copy=False,
        help="Codigo Unico de Factura Electronica del documento emitido por el proveedor.",
    )
    spr_request_ids = fields.One2many(
        "supplier.payment.request", "move_id", string="Solicitudes de pago",
        groups="supplier_invoice_portal.group_spr_validator",  # DECISIONS.md #49
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("cufe"):
                vals["cufe"] = dian_xml_parser.normalize_cufe(vals["cufe"])
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("cufe"):
            vals = dict(vals, cufe=dian_xml_parser.normalize_cufe(vals["cufe"]))
        return super().write(vals)

    @api.constrains("cufe", "company_id", "state", "move_type")
    def _check_cufe_unique(self):
        """El CUFE identifica un documento DIAN: no puede repetirse en compras.

        Se acota a facturas de proveedor no canceladas para no chocar con
        modulos de facturacion electronica de salida.
        """
        for move in self:
            if not move.cufe or move.move_type not in ("in_invoice", "in_refund"):
                continue
            if move.state == "cancel":
                continue
            duplicate = self.search(
                [
                    ("id", "!=", move.id),
                    ("cufe", "=", move.cufe),
                    ("company_id", "=", move.company_id.id),
                    ("move_type", "in", ("in_invoice", "in_refund")),
                    ("state", "!=", "cancel"),
                ],
                limit=1,
            )
            if duplicate:
                raise ValidationError(
                    _("El CUFE %s ya esta registrado en la factura %s.")
                    % (move.cufe, duplicate.name or duplicate.id)
                )

    @api.constrains("cufe")
    def _check_cufe_format(self):
        for move in self:
            if move.cufe and not dian_xml_parser.is_valid_cufe(move.cufe):
                raise ValidationError(
                    _("El CUFE debe tener exactamente 96 caracteres hexadecimales.")
                )

    def unlink(self):
        """Si se borra la factura borrador, la solicitud vuelve a quedar lista
        para generar otra."""
        requests = self.env["supplier.payment.request"].sudo().search(
            [("move_id", "in", self.ids)]
        )
        result = super().unlink()
        if requests:
            requests.write({"move_id": False, "state": "approved"})
            for request in requests:
                request.message_post(body=_(
                    "La factura borrador fue eliminada; la solicitud vuelve a 'Aprobada'."
                ))
        return result
