# -*- coding: utf-8 -*-
"""Documentos del proveedor: Camara de comercio, RUT y certificacion bancaria.

Cada documento apunta a un adjunto del contacto comercial, el mismo que se ve
en el chatter (DECISIONS.md #48): lo que el proveedor sube desde el portal
queda como adjunto del contacto, y en el backend se elige cualquier PDF ya
adjunto. La vista previa sale del adjunto (campo ``*_preview``).

Los campos son solo para el grupo Responsable (DECISIONS.md #49): un "leer todos
los campos" de otro usuario no debe traer los PDF. El portal los lee y escribe con
sudo sobre el contacto comercial del usuario conectado
(``controllers/portal_account.py``), nunca por ORM directo.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..services import pdf_check

# (clave, etiqueta). El adjunto va en "<clave>_id" y la vista previa en
# "<clave>_preview". La clave es tambien el nombre del input en el portal.
SPR_DOCUMENTS = [
    ("spr_doc_chamber", "Camara de comercio"),
    ("spr_doc_rut", "RUT"),
    ("spr_doc_bank_cert", "Certificacion de cuenta bancaria"),
]

# Solo los PDF adjuntos al propio contacto.
_ATTACHMENT_DOMAIN = (
    "[('res_model', '=', 'res.partner'), ('res_id', '=', id), "
    "('res_field', '=', False), ('mimetype', '=', 'application/pdf')]"
)


class ResPartner(models.Model):
    _inherit = "res.partner"

    spr_doc_chamber_id = fields.Many2one(
        "ir.attachment", string="Camara de comercio", domain=_ATTACHMENT_DOMAIN,
        copy=False, groups="supplier_invoice_portal.group_spr_manager", ondelete="set null",
    )
    spr_doc_chamber_preview = fields.Binary(
        related="spr_doc_chamber_id.datas", string="Vista previa de camara de comercio",
        groups="supplier_invoice_portal.group_spr_manager",
    )
    spr_doc_rut_id = fields.Many2one(
        "ir.attachment", string="RUT", domain=_ATTACHMENT_DOMAIN,
        copy=False, groups="supplier_invoice_portal.group_spr_manager", ondelete="set null",
    )
    spr_doc_rut_preview = fields.Binary(
        related="spr_doc_rut_id.datas", string="Vista previa del RUT",
        groups="supplier_invoice_portal.group_spr_manager",
    )
    spr_doc_bank_cert_id = fields.Many2one(
        "ir.attachment", string="Certificacion de cuenta bancaria", domain=_ATTACHMENT_DOMAIN,
        copy=False, groups="supplier_invoice_portal.group_spr_manager", ondelete="set null",
    )
    spr_doc_bank_cert_preview = fields.Binary(
        related="spr_doc_bank_cert_id.datas", string="Vista previa de certificacion bancaria",
        groups="supplier_invoice_portal.group_spr_manager",
    )

    @api.constrains("spr_doc_chamber_id", "spr_doc_rut_id", "spr_doc_bank_cert_id")
    def _check_spr_documents(self):
        """Un adjunto de este contacto, PDF liviano y sin contrasena (#47)."""
        for partner in self:
            for key, label in SPR_DOCUMENTS:
                attachment = partner[key + "_id"].sudo()
                if not attachment:
                    continue
                if attachment.res_model != "res.partner" or attachment.res_id != partner.id:
                    raise ValidationError(_(
                        "%s de %s: elija un archivo adjunto a este contacto."
                    ) % (label, partner.name))
                problem = pdf_check.pdf_problem(attachment.raw or b"")
                if problem:
                    raise ValidationError(_("%s de %s: %s.") % (label, partner.name, problem))

    def _spr_attach_document(self, key, filename, content):
        """Adjunta ``content`` al contacto (queda en el chatter) y lo deja como el
        documento ``key``. Devuelve el adjunto."""
        self.ensure_one()
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename,
            "raw": content,
            "mimetype": "application/pdf",
            "res_model": "res.partner",
            "res_id": self.id,
        })
        self.sudo()[key + "_id"] = attachment
        return attachment
