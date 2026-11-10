# -*- coding: utf-8 -*-
import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import file_open

from ..services import dian_xml_parser

# Capturas del instructivo en static/src/img/instructivo (DECISIONS.md #52).
SPR_INSTRUCTION_IMAGES = (
    "c_01_login", "c_02_inicio", "c_03_ordenes", "c_04_orden", "c_05_facturas",
    "c_06_radicaciones", "c_07_formulario", "c_09_detalle_rechazada", "c_10_documentos",
)


class ResPartner(models.Model):
    _inherit = "res.partner"

    portal_invoice_enabled = fields.Boolean(
        string="Puede radicar facturas en el portal",
        default=False,
        help="Habilita a este proveedor para enviar solicitudes de pago desde el portal. "
             "Se marca en la empresa (o en la persona natural sin contacto padre): sus "
             "contactos hijos con usuario de portal radican a nombre de ella.",
    )
    spr_support_document = fields.Boolean(
        string="No obligado a facturar (documento soporte)",
        default=False,
        help="El proveedor no expide factura electronica: radica una cuenta de cobro y "
             "la compania emite el documento soporte al registrarla.",
    )
    # Solo para los grupos del portal: un usuario sin acceso a las solicitudes
    # (cajero, vendedor) que lea "todos los campos" del contacto, como hace el
    # POS de odooleal, recibiria AccessError al calcularlos (DECISIONS.md #49).
    spr_request_ids = fields.One2many(
        "supplier.payment.request", "partner_id", string="Solicitudes de pago",
        groups="supplier_invoice_portal.group_spr_validator",
    )
    spr_request_count = fields.Integer(
        string="Numero de solicitudes", compute="_compute_spr_request_count",
        groups="supplier_invoice_portal.group_spr_validator",
    )

    @api.depends("spr_request_ids")
    def _compute_spr_request_count(self):
        grouped = self.env["supplier.payment.request"]._read_group(
            [("partner_id", "in", self.ids)],
            groupby=["partner_id"],
            aggregates=["__count"],
        )
        counts = {partner.id: count for partner, count in grouped}
        for partner in self:
            partner.spr_request_count = counts.get(partner.id, 0)

    @api.constrains("portal_invoice_enabled", "vat", "parent_id", "is_company")
    def _check_portal_invoice_enabled(self):
        """Requisitos para habilitar a un proveedor en el portal (DECISIONS.md #37).

        1. Se habilita el contacto comercial, no un contacto hijo: el portal lee
           el indicador de ``commercial_partner_id``, en el hijo no tendria efecto.
        2. Tiene NIT: sin el no se puede verificar el emisor de sus facturas.

        Un NIT repetido en otro tercero no bloquea (antes si, DECISIONS.md #37):
        quien radica es el usuario portal que se le crea al proveedor, y ese
        usuario solo ve las ordenes del tercero al que pertenece.
        """
        for partner in self.filtered("portal_invoice_enabled"):
            if partner.commercial_partner_id != partner:
                raise ValidationError(_(
                    "%s es un contacto de %s. Habilite el portal en %s: sus contactos "
                    "radican a nombre de ella."
                ) % (partner.name, partner.commercial_partner_id.name,
                     partner.commercial_partner_id.name))
            if not dian_xml_parser.normalize_nit(partner.vat):
                raise ValidationError(_(
                    "Registre el NIT de %s antes de habilitarlo en el portal de facturas."
                ) % partner.name)

    def action_view_spr_requests(self):
        self.ensure_one()
        partner = self.commercial_partner_id
        return {
            "type": "ir.actions.act_window",
            "name": "Solicitudes de pago",
            "res_model": "supplier.payment.request",
            "view_mode": "tree,form",
            "domain": [("partner_id", "=", partner.id)],
            "context": {"default_partner_id": partner.id},
        }

    def action_spr_send_instructions(self):
        """Abre el correo con el instructivo del portal para el proveedor.

        Se abre el asistente en vez de enviar directo para que compras revise
        los destinatarios. La plantilla trae el instructivo en PDF como reporte,
        asi que el asistente ya lo muestra adjunto (DECISIONS.md #52).
        """
        self.ensure_one()
        if not self.portal_invoice_enabled:
            raise UserError(_("Habilite primero el portal de facturas para %s.") % self.name)
        template = self.env.ref("supplier_invoice_portal.mail_template_spr_instructions")
        return {
            "type": "ir.actions.act_window",
            "name": _("Enviar instructivo del portal"),
            "res_model": "mail.compose.message",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_model": "res.partner",
                "default_res_ids": self.ids,
                "default_template_id": template.id,
                "default_composition_mode": "comment",
                "default_email_layout_xmlid": "mail.mail_notification_light",
            },
        }

    def _spr_instructions_company(self):
        """Compania que firma el instructivo: la del contacto o la activa."""
        self.ensure_one()
        return (self.company_id or self.env.company).sudo()

    def _spr_instructions_image(self, name):
        """Captura del instructivo como data URI.

        Incrustada y no por URL: wkhtmltopdf no siempre alcanza el servidor
        (Docker, proxies), y asi el PDF sale igual en cualquier instancia.
        """
        if name not in SPR_INSTRUCTION_IMAGES:
            return ""
        path = "supplier_invoice_portal/static/src/img/instructivo/%s.png" % name
        with file_open(path, "rb") as handle:
            return "data:image/png;base64,%s" % base64.b64encode(handle.read()).decode()

    def _spr_cutoff_label(self):
        """Texto del corte de radicacion para el instructivo, p. ej. '12:00 m.'."""
        hour = self.env["supplier.payment.request"]._cutoff_settings()[1]
        hours, minutes = int(hour), int(round((hour - int(hour)) * 60))
        suffix = "m." if (hours, minutes) == (12, 0) else ("p. m." if hours >= 12 else "a. m.")
        return "%d:%02d %s" % (hours if hours <= 12 else hours - 12, minutes, suffix)
