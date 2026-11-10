# -*- coding: utf-8 -*-
import base64
import json
import logging

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from ..services import (
    ai_adapter,
    cutoff,
    dian_catalog,
    dian_xml_parser,
    line_matcher,
    ocr_adapter,
    validation_rules,
)

_logger = logging.getLogger(__name__)


def base64_decode(value):
    """Decodifica el contenido de un campo Binary de Odoo a bytes."""
    return base64.b64decode(value or b"")


STATE_SELECTION = [
    ("draft", "Borrador"),
    ("validating", "Validando"),
    ("approved", "Aprobada"),
    ("warning", "Con observaciones"),
    ("rejected", "Rechazada"),
    ("invoiced", "Facturada"),
    ("cancelled", "Cancelada"),
]

DOCUMENT_TYPES = [
    ("invoice", "Factura electronica"),
    ("credit_note", "Nota credito"),
    ("debit_note", "Nota debito"),
    ("support_doc", "Cuenta de cobro (documento soporte)"),
]
NOTE_TYPES = ("credit_note", "debit_note")

LEVEL_LABELS = {
    validation_rules.LEVEL_ERROR: ("Error", "text-danger"),
    validation_rules.LEVEL_WARNING: ("Observacion", "text-warning"),
    validation_rules.LEVEL_OK: ("Correcto", "text-success"),
}


class SupplierPaymentRequest(models.Model):
    _name = "supplier.payment.request"
    _description = "Solicitud de pago de proveedor"
    _inherit = ["mail.thread.main.attachment", "mail.activity.mixin", "portal.mixin"]
    _order = "create_date desc, id desc"

    name = fields.Char(
        string="Numero", required=True, readonly=True, copy=False, default="/", index=True
    )
    document_type = fields.Selection(
        DOCUMENT_TYPES,
        string="Tipo de documento",
        required=True,
        default="invoice",
        tracking=True,
        index=True,
    )
    # Siempre el contacto comercial (empresa o persona natural sin padre): ver
    # create/write. Un contacto hijo se reemplaza por su empresa.
    partner_id = fields.Many2one(
        "res.partner",
        string="Proveedor",
        required=True,
        index=True,
        tracking=True,
        domain="['|', ('is_company', '=', True), ('parent_id', '=', False)]",
    )
    # Varias ordenes: hay proveedores que agrupan varias entregas en una sola
    # factura (DECISIONS.md #34).
    purchase_ids = fields.Many2many(
        "purchase.order",
        "supplier_payment_request_purchase_rel",
        "request_id",
        "purchase_id",
        string="Ordenes de compra",
        copy=False,
        domain="[('partner_id', 'child_of', partner_id),"
               " ('state', 'in', ['purchase', 'done'])]",
    )
    purchase_names = fields.Char(
        string="Ordenes", compute="_compute_purchase_names",
        help="Nombres de las ordenes de compra, para listas y correos.",
    )
    purchase_count = fields.Integer(string="Numero de ordenes", compute="_compute_purchase_names")
    # Pestana "Orden de compra": la orden al lado de la factura para validar
    # sin salir de la solicitud (DECISIONS.md #56).
    purchase_line_ids = fields.Many2many(
        "purchase.order.line", string="Lineas de la orden de compra",
        compute="_compute_purchase_lines",
    )
    purchase_amount_untaxed = fields.Monetary(
        string="Subtotal de las ordenes", compute="_compute_purchase_lines")
    purchase_amount_total = fields.Monetary(
        string="Total de las ordenes", compute="_compute_purchase_lines")
    origin_move_id = fields.Many2one(
        "account.move",
        string="Factura que afecta",
        copy=False,
        tracking=True,
        domain="[('move_type', '=', 'in_invoice'), ('state', '!=', 'cancel'),"
               " ('commercial_partner_id', '=', partner_id)]",
        help="Solo para notas credito y debito: la factura de proveedor que la nota corrige.",
    )
    note_accepted_by_id = fields.Many2one(
        "res.users", string="Nota debito aceptada por", readonly=True, copy=False
    )
    note_accepted_date = fields.Datetime(
        string="Fecha de aceptacion", readonly=True, copy=False
    )
    submitted_by_partner_id = fields.Many2one(
        "res.partner",
        string="Radicada por",
        readonly=True,
        copy=False,
        help="Contacto del portal que radico. Tambien recibe los correos.",
    )
    cufe = fields.Char(
        string="CUFE",
        size=96,
        index=True,
        copy=False,
        tracking=True,
        help="Codigo Unico de Factura Electronica: 96 caracteres hexadecimales.",
    )

    xml_file = fields.Binary(string="XML DIAN", attachment=True, copy=False)
    xml_filename = fields.Char(string="Nombre del XML")
    pdf_file = fields.Binary(string="PDF de la factura", attachment=True, copy=False)
    pdf_filename = fields.Char(string="Nombre del PDF")

    state = fields.Selection(
        STATE_SELECTION,
        string="Estado",
        default="draft",
        required=True,
        copy=False,
        tracking=True,
        index=True,
    )
    source = fields.Selection(
        [("xml", "XML DIAN"), ("ocr", "OCR del PDF"), ("manual", "Captura manual")],
        string="Origen de los datos",
        copy=False,
    )

    extracted_json = fields.Text(string="Datos extraidos (JSON)", readonly=True, copy=False)
    validation_json = fields.Text(string="Validacion (JSON)", readonly=True, copy=False)
    validation_summary = fields.Html(
        string="Resumen de validacion", readonly=True, sanitize=False, copy=False
    )

    party_check_enabled = fields.Boolean(
        compute="_compute_party_check_enabled",
        help="Refleja el ajuste 'Verificar emisor y receptor' para ocultar los campos.",
    )
    supplier_nit = fields.Char(string="NIT del emisor", readonly=True, copy=False)
    supplier_name = fields.Char(string="Razon social del emisor", readonly=True, copy=False)
    customer_nit = fields.Char(string="NIT del adquiriente", readonly=True, copy=False)
    customer_name = fields.Char(
        string="Razon social del adquiriente", readonly=True, copy=False
    )

    invoice_ref = fields.Char(string="Numero de factura", tracking=True, copy=False)
    invoice_date = fields.Date(string="Fecha de la factura", copy=False)

    amount_untaxed = fields.Monetary(string="Subtotal", copy=False)
    amount_tax = fields.Monetary(string="Impuestos", copy=False)
    amount_total = fields.Monetary(string="Total", tracking=True, copy=False)

    line_ids = fields.One2many(
        "supplier.payment.request.line", "request_id", string="Lineas", copy=False
    )

    move_id = fields.Many2one(
        "account.move", string="Factura de proveedor", readonly=True, copy=False
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Diario",
        domain="[('type', '=', 'purchase')]",
        # check_company aplica el filtro de compania sin exponer company_id en el
        # dominio: ese campo esta restringido al grupo multi-compania en la vista.
        check_company=True,
        default=lambda self: self._default_journal_id(),
    )

    company_id = fields.Many2one(
        "res.company", string="Compania", required=True, default=lambda self: self.env.company
    )
    currency_id = fields.Many2one(
        "res.currency",
        string="Moneda",
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    submitted_date = fields.Datetime(string="Fecha de envio", readonly=True, copy=False)
    rejection_reason = fields.Text(string="Motivo del rechazo", readonly=True, copy=False)

    supplier_note = fields.Text(
        string="Nota del proveedor",
        copy=False,
        help="Comentario que el proveedor escribio al radicar desde el portal.",
    )
    portal_submitted = fields.Boolean(
        string="Radicada desde el portal", default=False, copy=False, readonly=True
    )

    dian_cufe_check = fields.Selection(
        [
            ("skipped", "No consultado"),
            ("ok", "Encontrado en la DIAN"),
            ("not_found", "No encontrado en la DIAN"),
            ("error", "Error al consultar"),
        ],
        string="Consulta DIAN",
        default="skipped",
        readonly=True,
        copy=False,
    )

    # ------------------------------------------------------------------
    # Portal
    # ------------------------------------------------------------------

    @api.depends("purchase_ids")
    def _compute_purchase_names(self):
        # sudo: el usuario portal no lee purchase.order.
        for request in self:
            request.purchase_names = ", ".join(request.sudo().purchase_ids.mapped("name"))
            request.purchase_count = len(request.sudo().purchase_ids)

    @api.depends("purchase_ids")
    def _compute_purchase_lines(self):
        # Con el IVA como producto (#43) la linea de mayor valor IVA es IVA, no
        # mercancia: no va en el subtotal, si en el total.
        tax_product, _exempt = self._tax_as_product()
        for request in self:
            lines = request.purchase_ids.order_line.filtered(lambda line: not line.display_type)
            request.purchase_line_ids = lines
            request.purchase_amount_untaxed = sum(
                lines.filtered(lambda line: line.product_id != tax_product).mapped("price_subtotal"))
            request.purchase_amount_total = sum(request.purchase_ids.mapped("amount_total"))

    def spr_xml_preview(self):
        """XML con sangria para el boton de vista previa (DECISIONS.md #54 y #55)."""
        self.ensure_one()
        if not self.xml_file:
            return ""
        try:
            return dian_xml_parser.pretty_xml(base64_decode(self.with_context(bin_size=False).xml_file))
        except dian_xml_parser.DianXmlError as exc:
            return _("No se pudo leer el XML: %s") % exc

    def _compute_access_url(self):
        super()._compute_access_url()
        for request in self:
            request.access_url = "/my/payment-requests/%s" % request.id

    def _get_portal_return_action(self):
        self.ensure_one()
        return self.env.ref("supplier_invoice_portal.action_spr_request")

    def _portal_findings(self):
        """Hallazgos que se le muestran al proveedor: errores y observaciones.

        Los 'ok' y el detalle tecnico no salen del backend.
        """
        self.ensure_one()
        if not self.validation_json:
            return []
        try:
            findings = json.loads(self.validation_json).get("findings") or []
        except ValueError:
            return []
        return [
            f for f in findings
            if f.get("level") in (validation_rules.LEVEL_ERROR, validation_rules.LEVEL_WARNING)
        ]

    # ------------------------------------------------------------------
    # Defaults y CRUD
    # ------------------------------------------------------------------

    @api.model
    def _default_journal_id(self, document_type=None):
        """Diario con el que nace la factura. El documento soporte usa su propio
        diario: el de la resolucion DIAN de documento soporte (DECISIONS.md #36)."""
        params = self.env["ir.config_parameter"].sudo()
        if document_type == "support_doc":
            param = params.get_param("spr.support_journal_id")
            journal = self.env["account.journal"].browse(int(param)).exists() if param else False
            return journal or self.env["account.journal"]
        param = params.get_param("spr.default_journal_id")
        if param:
            journal = self.env["account.journal"].browse(int(param)).exists()
            if journal:
                return journal
        return self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)], limit=1
        )

    @api.model
    def _cutoff_settings(self):
        """(activo, hora) del cierre de radicacion de fin de mes. Activo por
        defecto; se guarda como "True"/"False" (ver res_config_settings)."""
        params = self.env["ir.config_parameter"].sudo()
        enabled = str(params.get_param("spr.cutoff_enabled", "True")).lower() in ("true", "1")
        hour = validation_rules._param_float(
            self.env, "spr.cutoff_hour", cutoff.DEFAULT_CUTOFF_HOUR
        )
        return enabled, hour

    @api.model
    def _radication_cutoff(self, now=None):
        """Estado del cierre de fin de mes para el portal (DECISIONS.md #38).

        :return: dict con ``closed`` (el portal no recibe), ``moment`` (fecha y
                 hora local del corte de este mes) y ``reopen`` (primer dia del
                 mes siguiente).
        """
        enabled, hour = self._cutoff_settings()
        tz_name = self.env.company.partner_id.tz or cutoff.DEFAULT_TZ
        closed, moment, reopen = cutoff.radication_closed(
            now or fields.Datetime.now(), tz_name, hour
        )
        return {"closed": enabled and closed, "moment": moment, "reopen": reopen,
                "enabled": enabled}

    def _compute_party_check_enabled(self):
        enabled = validation_rules.party_check_enabled(self.env)
        for request in self:
            request.party_check_enabled = enabled

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._normalize_partner_vals(vals)
            if vals.get("document_type") == "support_doc" and "journal_id" not in vals:
                journal = self._default_journal_id("support_doc")
                if journal:
                    vals["journal_id"] = journal.id
            if not vals.get("name") or vals["name"] == "/":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code("supplier.payment.request") or "/"
                )
            if vals.get("cufe"):
                vals["cufe"] = dian_xml_parser.normalize_cufe(vals["cufe"])
        requests = super().create(vals_list)
        requests.filtered("pdf_file")._spr_post_pdf()
        return requests

    def write(self, vals):
        self._normalize_partner_vals(vals)
        if vals.get("cufe"):
            vals["cufe"] = dian_xml_parser.normalize_cufe(vals["cufe"])
        res = super().write(vals)
        if vals.get("pdf_file"):
            self._spr_post_pdf()
        return res

    def _spr_post_pdf(self):
        """El PDF tambien va como adjunto del chatter, para verlo al lado del
        formulario sin abrir la pestana Adjuntos (DECISIONS.md #55)."""
        for request in self:
            attachment = self.env["ir.attachment"].create({
                "name": request.pdf_filename or "%s.pdf" % request.name.replace("/", "_"),
                "datas": request.with_context(bin_size=False).pdf_file,
                "mimetype": "application/pdf",
                "res_model": request._name,
                "res_id": request.id,
            })
            # Un PDF nuevo pasa a ser el que se muestra, aunque ya hubiera otro.
            request.with_context(tracking_disable=True).message_main_attachment_id = attachment
            request.message_post(
                body=_("PDF de la factura"),
                attachment_ids=attachment.ids,
                subtype_xmlid="mail.mt_note",
            )

    def _normalize_partner_vals(self, vals):
        """El proveedor de la solicitud es siempre el contacto comercial.

        Si se elige un contacto hijo (la persona de cuentas por pagar de la
        empresa), la solicitud queda a nombre de la empresa: es la que factura,
        la que tiene el NIT y la que el portal usa para filtrar.
        """
        if vals.get("partner_id"):
            partner = self.env["res.partner"].browse(vals["partner_id"])
            vals["partner_id"] = partner.commercial_partner_id.id

    @api.constrains("cufe")
    def _check_cufe_format(self):
        for request in self:
            if request.cufe and not dian_xml_parser.is_valid_cufe(request.cufe):
                raise UserError(
                    _("El CUFE debe tener exactamente 96 caracteres hexadecimales.")
                )

    @api.constrains("purchase_ids", "partner_id")
    def _check_purchase_partner(self):
        for request in self:
            for po in request.purchase_ids:
                if po.partner_id.commercial_partner_id != request.partner_id.commercial_partner_id:
                    raise UserError(
                        _("La orden de compra %s no pertenece al proveedor %s.")
                        % (po.name, request.partner_id.display_name)
                    )

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        """Si cambia el proveedor, las OC anteriores dejan de tener sentido."""
        commercial = self.partner_id.commercial_partner_id
        self.purchase_ids = self.purchase_ids.filtered(
            lambda po: po.partner_id.commercial_partner_id == commercial
        )
        if self.origin_move_id.commercial_partner_id != commercial:
            self.origin_move_id = False

    @api.onchange("origin_move_id")
    def _onchange_origin_move_id(self):
        if self.origin_move_id and self.document_type in NOTE_TYPES:
            self.purchase_ids = self._origin_orders(self.origin_move_id)

    @api.model
    def _origin_orders(self, move):
        """Ordenes de compra de la factura que una nota corrige.

        La nota se empareja contra las lineas de esas ordenes para saber a que
        producto, cuenta e impuesto va cada linea.
        """
        return move.invoice_line_ids.purchase_line_id.order_id

    # ------------------------------------------------------------------
    # Pipeline de validacion
    # ------------------------------------------------------------------

    def action_validate(self):
        """Extrae los datos de la factura y corre las reglas duras."""
        for request in self:
            request._run_pipeline()
        return True

    def action_validate_ai(self):
        """Validar con IA: boton y accion de servidor (DECISIONS.md #51).

        Corre la validacion completa, que ya pide a la IA emparejar lineas sin
        codigo y revisar unidades cuando cantidad y precio no cuadran con la
        orden. La diferencia con *Validar* es que aqui la IA es obligatoria y
        sirve para varias solicitudes a la vez desde la lista.
        """
        if isinstance(ai_adapter.get_ai_adapter(self.env), ai_adapter.NoopAiAdapter):
            raise UserError(_("La IA no esta configurada. Activela en Ajustes > Compras > "
                              "Portal de facturas de proveedor."))
        to_validate = self.filtered(
            lambda req: req.state not in ("invoiced", "cancelled")
            and not (req.state == "rejected" and req.rejection_reason)
        )
        if not to_validate:
            raise UserError(_("Ninguna de las solicitudes elegidas se puede validar: estan "
                              "facturadas, canceladas o rechazadas por contabilidad."))
        for request in to_validate:
            request._run_pipeline()
        return True

    def _run_pipeline(self):
        self.ensure_one()
        if self.state in ("invoiced", "cancelled"):
            raise UserError(
                _("No se puede validar una solicitud en estado '%s'.")
                % dict(STATE_SELECTION)[self.state]
            )
        if self.state == "rejected" and self.rejection_reason:
            # Rechazo manual del Responsable: revalidar lo revertiria en
            # silencio. Solo se reabre con "Volver a borrador".
            raise UserError(
                _("La solicitud fue rechazada por contabilidad (%s). Para reabrirla "
                  "use 'Volver a borrador'.") % self.rejection_reason
            )
        if not self.pdf_file:
            raise UserError(_("El PDF de la factura es obligatorio."))

        self.write({"state": "validating"})
        _logger.info("[SPR] %s: iniciando validacion", self.name)

        parsed = self._extract_data()
        self._apply_extracted_data(parsed)
        if self.document_type in NOTE_TYPES:
            self._resolve_note_origin(parsed)
        self._check_dian_cufe()
        if self.document_type != "support_doc":
            # Las lineas del documento soporte nacen de la orden, ya emparejadas.
            self._apply_line_matching(parsed)

        findings = validation_rules.run_rules(self, parsed)
        # Si cantidad y precio no cuadran con la orden, antes de dejarla con
        # observaciones se le pide a la IA revisar si es cuestion de unidades.
        if self._ai_review_units():
            findings = validation_rules.run_rules(self, parsed)
        verdict = validation_rules.verdict_from_findings(findings)
        self._apply_findings(findings, verdict)
        _logger.info("[SPR] %s: veredicto %s", self.name, verdict)
        return verdict

    def _ai_review_units(self):
        """Pide a la IA el factor de unidad de las lineas cuyo precio no cuadra.

        Caso tipico: la factura cobra 2 bultos a 980.000 y la orden pide 140 kg
        a 14.000; el total coincide pero cantidad y precio no. La IA propone el
        factor (70) y solo se aplica si, convertido, el precio queda dentro de
        la tolerancia del de la orden: la IA sugiere, la regla decide. Devuelve
        True si cambio algun factor.
        """
        self.ensure_one()
        adapter = ai_adapter.get_ai_adapter(self.env)
        if isinstance(adapter, ai_adapter.NoopAiAdapter):
            return False
        tolerance = validation_rules.Tolerance.from_env(self.env, self.currency_id)
        candidates = self.line_ids.sorted(lambda line: (line.sequence, line.id)).filtered(
            lambda line: line.po_line_id
            and not (line.match_method == line_matcher.MATCH_MANUAL and line.uom_factor != 1.0)
            and not tolerance.within_pct(line.po_price_unit, line.po_line_id.price_unit)
        )
        if not candidates:
            return False
        payload = [
            {
                "idx": idx,
                "description": line.description or "",
                "code": line.product_code or "",
                "unit_code": line.unit_code or "",
                "quantity": line.quantity,
                "price_unit": line.price_unit,
                "subtotal": line.subtotal,
                "po_description": line.po_line_id.name or "",
                "po_uom": line.po_line_id.product_uom.name or "",
                "po_quantity": line.po_line_id.product_qty - line.po_line_id.qty_invoiced,
                "po_price_unit": line.po_line_id.price_unit,
            }
            for idx, line in enumerate(candidates)
        ]
        try:
            answer = adapter.reconcile_units(payload, {"currency": self.currency_id.name}) or {}
        except Exception:  # noqa: BLE001 - la IA nunca tumba la validacion
            _logger.exception("[SPR] %s: la revision de unidades fallo", self.name)
            return False

        changed = False
        notes = []
        for item in answer.get("lines") or []:
            line = candidates[item["idx"]]
            factor = item["factor"]
            label = "%s: x %g (%.0f%%) %s" % (
                line.description, factor, item["confidence"] * 100, item["note"])
            if abs(factor - 1.0) < 1e-6:
                notes.append(_("%s - sin conversion") % label)
                continue
            if item["confidence"] < line_matcher.MIN_CONFIDENCE:
                notes.append(_("%s - confianza baja, no se aplico") % label)
                continue
            if not tolerance.within_pct(line.price_unit / factor, line.po_line_id.price_unit):
                notes.append(_("%s - el precio convertido no cuadra con la orden, no se aplico")
                             % label)
                continue
            line.write({
                "uom_factor": factor,
                "uom_note": (_("IA (%.0f%%): %s") % (item["confidence"] * 100, item["note"]))[:250],
            })
            notes.append(_("%s - aplicado") % label)
            changed = True
        summary = answer.get("summary_es") or ""
        self.message_post(body=Markup("<p>%s</p><ul>%s</ul>") % (
            _("Revision de unidades con IA: %s") % (summary or _("sin respuesta")),
            Markup("").join(Markup("<li>%s</li>") % note for note in notes),
        ))
        _logger.info("[SPR] %s: revision de unidades, %d lineas, cambio=%s",
                     self.name, len(candidates), changed)
        return changed

    def _extract_data(self):
        """Devuelve el dict normalizado de la factura. El XML manda sobre el OCR."""
        self.ensure_one()
        if self.document_type == "support_doc":
            return self._support_doc_data()
        if self.xml_file:
            xml_bytes = base64_decode(self.xml_file)
            parsed = dian_xml_parser.parse_dian_xml(xml_bytes)
            parsed["source"] = "xml"
            if self.cufe:
                ok, message = dian_xml_parser.check_cufe_matches(parsed, self.cufe)
                if not ok:
                    parsed.setdefault("raw_warnings", []).append(message)
            return parsed

        adapter = ocr_adapter.get_ocr_adapter(self.env)
        hint = {
            "expected_po": self.purchase_names or "",
            # El tronco del NIT sin DV: en Odoo el DV va tras el guion.
            "expected_supplier_nit": dian_xml_parser.normalize_nit(
                (self.partner_id.commercial_partner_id.vat or "").split("-")[0]
            ),
        }
        parsed = adapter.extract(
            base64_decode(self.pdf_file), self.pdf_filename or "factura.pdf", hint=hint
        )
        parsed["source"] = "ocr" if parsed.get("lines") else "manual"
        return parsed

    def _support_doc_data(self):
        """Datos de una cuenta de cobro de un proveedor no obligado a facturar.

        No hay XML ni CUFE: el documento electronico (documento soporte) lo
        emite la compania al registrar la factura. El proveedor declara numero,
        fecha y total en el portal; las lineas salen de lo pendiente de sus
        ordenes. Solo se crean la primera vez: despues son de contabilidad, que
        las ajusta si la cuenta de cobro no cubre todo lo pendiente.
        """
        self.ensure_one()
        lines = []
        if not self.line_ids:
            for po_line in self.purchase_ids.order_line.filtered(
                lambda line: not line.display_type and line.product_id
            ):
                qty = po_line.qty_to_invoice
                if qty <= 0:
                    qty = po_line.product_qty - po_line.qty_invoiced
                if qty <= 0:
                    continue
                lines.append({
                    "description": po_line.name or po_line.product_id.display_name,
                    "code": po_line.product_id.default_code or "",
                    "quantity": qty,
                    "price_unit": po_line.price_unit,
                    "tax_rate": validation_rules._po_line_tax_rate(po_line),
                    "subtotal": qty * po_line.price_unit * (1 - (po_line.discount or 0.0) / 100.0),
                    "po_line_id": po_line.id,
                })
        source_lines = lines or [
            {"subtotal": line.subtotal, "tax_rate": line.tax_rate} for line in self.line_ids
        ]
        untaxed = sum(line["subtotal"] for line in source_lines)
        tax = sum(line["subtotal"] * line["tax_rate"] / 100.0 for line in source_lines)
        return {
            "document_type": "support_doc",
            "source": "manual",
            "invoice_ref": self.invoice_ref or "",
            "issue_date": fields.Date.to_string(self.invoice_date) if self.invoice_date else "",
            "lines": lines,
            "amount_untaxed": self.currency_id.round(untaxed),
            "amount_tax": self.currency_id.round(tax),
            # El total lo declara el proveedor y no se pisa: la regla
            # AMOUNT_INCONSISTENT lo compara con lo que suman las lineas.
            "amount_total": self.amount_total,
            "currency": self.currency_id.name,
            "raw_warnings": [],
        }

    def _resolve_note_origin(self, parsed):
        """Ubica la factura que corrige la nota si el proveedor no la eligio.

        Se busca por el CUFE y luego por el numero que la nota trae en
        BillingReference, solo entre las facturas de este proveedor.
        """
        self.ensure_one()
        if not self.origin_move_id:
            reference = parsed.get("billing_reference") or {}
            base_domain = [
                ("move_type", "=", "in_invoice"),
                ("state", "!=", "cancel"),
                ("commercial_partner_id", "=", self.partner_id.commercial_partner_id.id),
                ("company_id", "=", self.company_id.id),
            ]
            Move = self.env["account.move"].sudo()
            move = Move.browse()
            if reference.get("cufe"):
                move = Move.search(base_domain + [("cufe", "=", reference["cufe"])], limit=1)
            if not move and reference.get("number"):
                move = Move.search(base_domain + [("ref", "=", reference["number"])], limit=1)
            if move:
                self.origin_move_id = move
        if self.origin_move_id and not self.purchase_ids:
            self.purchase_ids = self._origin_orders(self.origin_move_id)

    def _dian_check_enabled(self):
        # Apagado por defecto: el catalogo exige captcha desde 2026 y la
        # consulta automatica solo puede decir "no se pudo verificar"
        # (DECISIONS.md #31). Se activa desde Ajustes si la DIAN lo reabre.
        value = self.env["ir.config_parameter"].sudo().get_param("spr.dian_cufe_check", "False")
        return str(value).lower() in ("true", "1")

    def _check_dian_cufe(self):
        """Consulta el CUFE en el catalogo DIAN y guarda el resultado.

        Nunca lanza: cualquier problema queda como ``error`` y la regla
        ``DIAN_CUFE_CHECK`` no bloquea por eso.
        """
        self.ensure_one()
        if not self.cufe or not self._dian_check_enabled():
            self.dian_cufe_check = "skipped"
            return "skipped"
        params = self.env["ir.config_parameter"].sudo()
        status, message = dian_catalog.check_cufe(
            self.cufe,
            base_url=params.get_param("spr.dian_catalog_url") or None,
        )
        self.dian_cufe_check = status
        _logger.info("[SPR] %s: consulta DIAN %s (%s)", self.name, status, message)
        return status

    def action_check_dian(self):
        """Boton manual: vuelve a consultar el CUFE y actualiza el resumen."""
        for request in self:
            if not request.cufe:
                raise UserError(_("La solicitud no tiene CUFE que consultar."))
            status = request._check_dian_cufe()
            request._refresh_dian_finding()
            request.message_post(
                body=Markup("<p>%s</p>")
                % (_("Consulta DIAN: %s")
                   % dict(request._fields["dian_cufe_check"].selection)[status])
            )
        return True

    def _refresh_dian_finding(self):
        """Reemplaza el hallazgo DIAN_CUFE_CHECK en el resumen sin tocar el estado."""
        self.ensure_one()
        if not self.validation_json:
            return
        payload = json.loads(self.validation_json)
        findings = [
            validation_rules.Finding(f["level"], f["code"], f["message_es"], f.get("details"))
            for f in payload.get("findings") or [] if f.get("code") != "DIAN_CUFE_CHECK"
        ]
        findings.extend(validation_rules._rule_dian_cufe_check({"request": self, "env": self.env}))
        payload["findings"] = [finding.to_dict() for finding in findings]
        self.write({
            "validation_json": json.dumps(payload, ensure_ascii=False, indent=2),
            "validation_summary": self._render_validation_summary(findings, self.state),
        })

    def _apply_extracted_data(self, parsed):
        """Vuelca el dict normalizado en los campos del request."""
        self.ensure_one()
        supplier = parsed.get("supplier") or {}
        customer = parsed.get("customer") or {}
        values = {
            "source": parsed.get("source") or "manual",
            "extracted_json": json.dumps(parsed, ensure_ascii=False, indent=2, default=str),
            "supplier_nit": supplier.get("nit") or False,
            "supplier_name": supplier.get("name") or False,
            "customer_nit": customer.get("nit") or False,
            "customer_name": customer.get("name") or False,
            "submitted_date": self.submitted_date or fields.Datetime.now(),
        }
        if parsed.get("invoice_ref"):
            values["invoice_ref"] = parsed["invoice_ref"]
        if parsed.get("issue_date"):
            values["invoice_date"] = parsed["issue_date"]
        # Un CUFE malformado no se escribe: la constraint abortaria la
        # validacion. La regla CUFE_FORMAT lo reporta como hallazgo.
        if parsed.get("cufe") and not self.cufe and dian_xml_parser.is_valid_cufe(parsed["cufe"]):
            values["cufe"] = parsed["cufe"]
        for field_name in ("amount_untaxed", "amount_tax", "amount_total"):
            if parsed.get(field_name) is not None:
                values[field_name] = parsed[field_name]

        lines = parsed.get("lines") or []
        if lines:
            # Las lineas se recrean desde el documento, pero lo que el validador
            # emparejo a mano se conserva: revalidar no debe deshacer su
            # trabajo. Se reconoce cada linea por su contenido (codigo,
            # descripcion y precio), no por posicion: el orden en pantalla se
            # puede cambiar arrastrando y ya no coincide con el del documento.
            manual_by_key = {}
            for line in self.line_ids.sorted(lambda line: (line.sequence, line.id)):
                if line.match_method == line_matcher.MATCH_MANUAL and (
                    line.po_line_id or line.is_extra_charge
                ):
                    key = self._line_identity(line.product_code, line.description, line.price_unit)
                    manual_by_key.setdefault(key, []).append(line)
            commands = [fields.Command.clear()]
            for index, line in enumerate(lines):
                line_values = self._prepare_line_values(index, line)
                key = self._line_identity(
                    line_values["product_code"], line_values["description"], line_values["price_unit"]
                )
                if manual_by_key.get(key):
                    manual_line = manual_by_key[key].pop(0)
                    line_values.update({
                        "po_line_id": manual_line.po_line_id.id,
                        "is_extra_charge": manual_line.is_extra_charge,
                        "uom_factor": manual_line.uom_factor,
                        "uom_note": manual_line.uom_note,
                        "match_method": line_matcher.MATCH_MANUAL,
                        "match_confidence": 1.0,
                        "match_note": manual_line.match_note or _("Emparejada manualmente"),
                    })
                commands.append(fields.Command.create(line_values))
            values["line_ids"] = commands
        self.write(values)

    @staticmethod
    def _line_identity(product_code, description, price_unit):
        """Clave estable de una linea del documento entre validaciones."""
        return (
            (product_code or "").strip().lower(),
            (description or "").strip().lower(),
            round(price_unit or 0.0, 2),
        )

    def _prepare_line_values(self, index, line):
        try:
            sequence = int(line.get("sequence") or index + 1)
        except (TypeError, ValueError):
            sequence = index + 1
        values = {
            "sequence": sequence,
            "description": line.get("description") or _("Sin descripcion"),
            "product_code": line.get("code") or False,
            "quantity": line.get("quantity") or 0.0,
            "price_unit": line.get("price_unit") or 0.0,
            "tax_rate": line.get("tax_rate") or 0.0,
            "subtotal": line.get("subtotal") or 0.0,
            "unit_code": line.get("unit") or False,
            "match_method": "none",
        }
        if line.get("po_line_id"):
            # Documento soporte: la linea sale de la orden, ya emparejada.
            values.update({
                "po_line_id": line["po_line_id"],
                "match_method": line_matcher.MATCH_ORDER,
                "match_confidence": 1.0,
                "match_note": _("Tomada de la orden de compra"),
            })
        return values

    def _apply_line_matching(self, parsed):
        """Aplica al request los emparejamientos sugeridos por el matcher."""
        self.ensure_one()
        lines = self.line_ids.sorted(lambda line: (line.sequence, line.id))
        # Todo lo que no sea manual se recalcula desde cero.
        lines.filtered(lambda line: line.match_method != line_matcher.MATCH_MANUAL).write({
            "po_line_id": False,
            "is_extra_charge": False,
            "uom_factor": 1.0,
            "uom_note": False,
            "match_method": line_matcher.MATCH_NONE,
            "match_confidence": 0.0,
            "match_note": False,
        })
        for match in line_matcher.match_lines(self, parsed):
            index = match.get("invoice_idx")
            if index is None or index >= len(lines):
                continue
            lines[index].write({
                "po_line_id": match.get("po_line_id") or False,
                "is_extra_charge": match.get("method") == line_matcher.MATCH_CHARGE,
                "match_method": match.get("method") or line_matcher.MATCH_NONE,
                "match_confidence": match.get("confidence") or 0.0,
                "match_note": match.get("note") or False,
            })

    def _apply_findings(self, findings, verdict):
        """Guarda los hallazgos, fija el estado y avisa a quien corresponda."""
        self.ensure_one()
        payload = {
            "verdict": verdict,
            "findings": [finding.to_dict() for finding in findings],
        }
        first_validation = not self.validation_json
        self.write({
            "state": verdict,
            "validation_json": json.dumps(payload, ensure_ascii=False, indent=2),
            "validation_summary": self._render_validation_summary(findings, verdict),
        })
        self.message_post(
            body=Markup("<p>%s</p>")
            % (_("Validacion ejecutada: %s") % dict(STATE_SELECTION)[verdict])
        )
        if verdict in ("approved", "warning"):
            self._notify_validators()
        # Al proveedor se le avisa solo la primera vez: las revalidaciones del
        # equipo de contabilidad no le cambian nada hasta que haya decision.
        if first_validation:
            if verdict == "rejected":
                self._notify_supplier("supplier_invoice_portal.mail_template_spr_auto_rejected")
            else:
                self._notify_supplier("supplier_invoice_portal.mail_template_spr_received")

    def _render_validation_summary(self, findings, verdict):
        """HTML legible para contabilidad. Se escapa todo: hay datos de terceros."""
        rows = []
        for finding in findings:
            label, css = LEVEL_LABELS.get(finding.level, ("Info", ""))
            rows.append(
                "<tr><td class='%s'><strong>%s</strong></td><td><code>%s</code></td>"
                "<td>%s</td></tr>"
                % (css, html_escape(label), html_escape(finding.code),
                   html_escape(finding.message_es))
            )
        if not rows:
            rows.append("<tr><td colspan='3'>Sin observaciones.</td></tr>")
        return Markup(
            "<div><p><strong>Veredicto:</strong> %s</p>"
            "<table class='table table-sm'><thead><tr>"
            "<th>Nivel</th><th>Codigo</th><th>Detalle</th></tr></thead>"
            "<tbody>%s</tbody></table></div>"
            % (html_escape(dict(STATE_SELECTION)[verdict]), "".join(rows))
        )

    def _notify_validators(self):
        """Crea una actividad para el grupo validador configurado."""
        self.ensure_one()
        group = self._get_validator_group()
        if not group:
            _logger.warning("[SPR] %s: no hay grupo validador configurado", self.name)
            return
        users = group.users.filtered(lambda user: user.active)
        for user in users[:1] or self.env["res.users"]:
            self.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=user.id,
                summary=_("Revisar solicitud de pago %s") % self.name,
            )

    def _notify_supplier(self, template_xmlid):
        """Envia al proveedor la plantilla indicada. Nunca tumba la transaccion."""
        self.ensure_one()
        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            _logger.warning("[SPR] %s: no existe la plantilla %s", self.name, template_xmlid)
            return
        # La empresa y el contacto que radico: en proveedores con contactos
        # hijos, el correo de la empresa suele ser generico y quien espera la
        # respuesta es la persona que subio la factura.
        recipients = (self.partner_id | self.submitted_by_partner_id).filtered("email")
        if not recipients:
            _logger.info("[SPR] %s: el proveedor no tiene correo, no se notifica", self.name)
            return
        try:
            self.with_context(lang=self.partner_id.lang).message_post_with_source(
                template,
                subtype_xmlid="mail.mt_comment",
                partner_ids=recipients.ids,
            )
        except Exception:  # noqa: BLE001 - un correo no puede bloquear la validacion
            _logger.exception("[SPR] %s: fallo el correo al proveedor", self.name)

    @api.model
    def _get_validator_group(self):
        param = self.env["ir.config_parameter"].sudo().get_param("spr.validator_group_id")
        if param:
            group = self.env["res.groups"].browse(int(param)).exists()
            if group:
                return group
        return self.env.ref("supplier_invoice_portal.group_spr_validator", False)

    # ------------------------------------------------------------------
    # Acciones de estado
    # ------------------------------------------------------------------

    def action_reset_to_draft(self):
        for request in self:
            if request.move_id:
                raise UserError(
                    _("No se puede volver a borrador: ya existe la factura %s.")
                    % request.move_id.name
                )
        self.write({
            "state": "draft",
            "validation_json": False,
            "validation_summary": False,
            "rejection_reason": False,
            "note_accepted_by_id": False,
            "note_accepted_date": False,
        })
        return True

    def action_accept_debit_note(self):
        """El Responsable acepta el incremento de precio de una nota debito.

        Una nota debito sube lo que se le debe al proveedor por encima de lo
        pactado en la orden, asi que nunca pasa sola: sin esta aceptacion no se
        puede crear la factura (DECISIONS.md #33).
        """
        for request in self:
            if request.document_type != "debit_note":
                raise UserError(_("Solo las notas debito se aceptan."))
            if request.state not in ("approved", "warning"):
                raise UserError(
                    _("La nota debito debe estar validada para aceptarla (estado actual: %s).")
                    % dict(STATE_SELECTION)[request.state]
                )
            request.write({
                "note_accepted_by_id": self.env.user.id,
                "note_accepted_date": fields.Datetime.now(),
            })
            request.message_post(body=Markup("<p>%s</p>") % _(
                "Nota debito aceptada por %s."
            ) % self.env.user.name)
            request._run_pipeline()
        return True

    def action_reject(self, reason=None):
        """Rechaza la solicitud. El motivo es obligatorio."""
        reason = reason or self.env.context.get("spr_rejection_reason")
        if not reason:
            raise UserError(_("Debe indicar el motivo del rechazo."))
        for request in self:
            if request.state in ("invoiced", "cancelled") or request.move_id:
                raise UserError(
                    _("No se puede rechazar una solicitud en estado '%s'.")
                    % dict(STATE_SELECTION)[request.state]
                )
            request.write({"state": "rejected", "rejection_reason": reason})
            request.message_post(
                body=Markup("<p>%s</p>") % (_("Solicitud rechazada: %s") % reason),
            )
            request._notify_supplier("supplier_invoice_portal.mail_template_spr_rejected")
            _logger.info("[SPR] %s: rechazada (%s)", request.name, reason)
        return True

    def action_cancel(self):
        for request in self:
            if request.move_id:
                raise UserError(
                    _("No se puede cancelar: ya existe la factura %s.") % request.move_id.name
                )
        self.write({"state": "cancelled"})
        return True

    def action_open_reject_wizard(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Rechazar solicitud"),
            "res_model": "supplier.payment.request.reject",
            "view_mode": "form",
            "target": "new",
            "context": {"default_request_id": self.id},
        }

    # ------------------------------------------------------------------
    # Creacion de la factura de proveedor
    # ------------------------------------------------------------------

    def action_create_bill(self):
        """Crea la factura de proveedor en borrador a partir de la solicitud.

        Solo desde *Aprobada* o *Con observaciones*. Todas las lineas deben
        estar emparejadas: la factura nace ligada a las lineas de la orden de
        compra para que ``qty_invoiced`` y el estado de facturacion de la orden
        se actualicen solos.
        """
        moves = self.env["account.move"]
        for request in self:
            moves |= request._create_bill()
        if len(moves) == 1:
            return {
                "type": "ir.actions.act_window",
                "res_model": "account.move",
                "res_id": moves.id,
                "view_mode": "form",
                "views": [(self.env.ref("account.view_move_form").id, "form")],
            }
        return True

    def _check_can_create_bill(self):
        self.ensure_one()
        if self.move_id:
            raise UserError(
                _("La solicitud %s ya tiene la factura %s.") % (self.name, self.move_id.name)
            )
        if self.state not in ("approved", "warning"):
            raise UserError(
                _("Solo se puede crear la factura desde 'Aprobada' o 'Con observaciones' "
                  "(estado actual: %s).") % dict(STATE_SELECTION)[self.state]
            )
        if self.document_type in NOTE_TYPES and not self.origin_move_id:
            raise UserError(_("La nota no tiene la factura que afecta."))
        if self.document_type == "debit_note" and not self.note_accepted_by_id:
            raise UserError(
                _("La nota debito debe ser aceptada por el Responsable antes de crear la factura.")
            )
        if not self.purchase_ids:
            raise UserError(_("La solicitud no tiene orden de compra."))
        if not self.line_ids:
            raise UserError(_("La solicitud no tiene lineas."))
        tax_product, exempt_tax = self._tax_as_product()
        if tax_product is None:
            raise UserError(_(
                "El IVA como producto esta activo pero falta el producto de mayor valor IVA "
                "o el impuesto de las lineas en Ajustes -> Compras -> Portal de facturas de "
                "proveedor."
            ))
        if self.line_ids.filtered("is_extra_charge") and not self._freight_product():
            raise UserError(_(
                "La solicitud tiene cargos adicionales (flete) y no hay producto de flete "
                "configurado en Ajustes -> Compras -> Portal de facturas de proveedor."
            ))
        unmatched = self.line_ids.filtered(lambda line: not line.po_line_id and not line.is_extra_charge)
        if unmatched:
            raise UserError(
                _("Hay %d lineas sin emparejar con la orden de compra: %s. "
                  "Asignelas en la pestana 'Lineas y emparejamiento' antes de crear la factura.")
                % (len(unmatched),
                   ", ".join(line.product_code or line.description for line in unmatched))
            )
        wrong_order = self.line_ids.filtered(
            lambda line: line.po_line_id and line.po_line_id.order_id not in self.purchase_ids
        )
        if wrong_order:
            raise UserError(
                _("Hay lineas emparejadas con una orden de compra que no es de la solicitud (%s).")
                % self.purchase_names
            )
        if self.document_type == "support_doc":
            # Siempre el diario de documento soporte: en otro diario no se
            # emitiria el documento electronico ante la DIAN.
            journal = self._default_journal_id("support_doc")
            if not journal:
                raise UserError(_(
                    "No hay diario de documento soporte configurado (Ajustes -> Compras -> "
                    "Portal de facturas de proveedor)."
                ))
            return journal
        journal = self.journal_id or self._default_journal_id()
        if not journal:
            raise UserError(_("No hay diario de compras configurado."))
        return journal

    @api.model
    def _freight_product(self):
        param = self.env["ir.config_parameter"].sudo().get_param("spr.freight_product_id")
        return self.env["product.product"].browse(int(param)).exists() if param else False

    @api.model
    def _tax_as_product(self):
        """(producto de mayor valor IVA, impuesto 0 %) si el IVA va como producto
        (DECISIONS.md #43); (False, False) si no. (None, None) si esta activo pero
        le falta configuracion."""
        params = self.env["ir.config_parameter"].sudo()
        if str(params.get_param("spr.tax_as_product", "False")).lower() not in ("true", "1"):
            return False, False
        product_id = params.get_param("spr.tax_product_id")
        tax_id = params.get_param("spr.exempt_tax_id")
        product = self.env["product.product"].browse(int(product_id)).exists() if product_id else False
        tax = self.env["account.tax"].browse(int(tax_id)).exists() if tax_id else False
        if not product or not tax:
            return None, None
        return product, tax

    def _extra_po_line(self, product, quantity, price_unit, taxes, name, used):
        """Linea de la orden para un cargo que la factura cobra: flete o mayor valor IVA.

        Reusa una linea de ese producto que no se haya facturado; si no hay, la
        agrega a la primera orden de la solicitud (DECISIONS.md #43). ``used``
        evita que dos cargos de la misma factura caigan en la misma linea.
        """
        free = self.purchase_ids.order_line.filtered(
            lambda po_line: po_line.product_id == product and not po_line.display_type
            and not po_line.qty_invoiced and po_line not in used
        )
        if free:
            used |= free[:1]
            return free[:1], used
        order = self.purchase_ids.sorted("id")[:1]
        po_line = self.env["purchase.order.line"].create({
            "order_id": order.id,
            "product_id": product.id,
            "name": name,
            "product_qty": quantity,
            "product_uom": product.uom_po_id.id,
            "price_unit": price_unit,
            "taxes_id": [fields.Command.set(taxes.ids)],
            "date_planned": fields.Datetime.now(),
        })
        order.message_post(body=Markup("<p>%s</p>") % (
            _("Linea agregada desde la solicitud %s: %s por %s.")
            % (self.name, name, self.currency_id.format(quantity * price_unit))
        ))
        return po_line, used | po_line

    def _prepare_bill_values(self, journal):
        self.ensure_one()
        orders = self.purchase_ids.sorted("id")
        order = orders[:1]
        is_note = self.document_type in NOTE_TYPES
        fiscal_position = order.fiscal_position_id or self.origin_move_id.fiscal_position_id
        tax_product, exempt_tax = self._tax_as_product()
        used_po_lines = self.env["purchase.order.line"]
        line_commands = []
        for line in self.line_ids.sorted(lambda line: (line.sequence, line.id)):
            if line.is_extra_charge:
                values = self._prepare_extra_charge_line(line, fiscal_position)
                if exempt_tax:
                    values["tax_ids"] = [fields.Command.set(exempt_tax.ids)]
                if not is_note:
                    # El flete entra a la orden para que la factura quede ligada.
                    po_line, used_po_lines = self._extra_po_line(
                        self._freight_product(), values["quantity"], values["price_unit"],
                        self.env["account.tax"].browse(values["tax_ids"][0][2]),
                        line.description, used_po_lines,
                    )
                    if exempt_tax and po_line.taxes_id != exempt_tax:
                        po_line.taxes_id = exempt_tax
                    values["purchase_line_id"] = po_line.id
                line_commands.append(fields.Command.create(values))
                continue
            if exempt_tax and not is_note and line.po_line_id.taxes_id != exempt_tax:
                # Con el IVA como producto, la orden tambien va al 0 %.
                line.po_line_id.taxes_id = exempt_tax
            # _prepare_account_move_line trae producto, cuenta, impuestos, UdM,
            # analitica y el vinculo purchase_line_id. Encima se ponen la
            # cantidad y el precio que dice la factura del proveedor.
            values = line.po_line_id._prepare_account_move_line()
            if is_note:
                # Una nota corrige el valor, no las cantidades recibidas: sin
                # el vinculo, la orden no cambia su cantidad facturada
                # (DECISIONS.md #33).
                values.pop("purchase_line_id", None)
            values.update({
                # En la unidad de la orden: si la factura cobra en otra unidad,
                # el factor la convierte (DECISIONS.md #51).
                "quantity": line.po_quantity,
                "price_unit": line.po_price_unit,
                # El descuento de la OC no aplica: manda lo que dice la factura.
                # Si el subtotal facturado es menor que cantidad x precio, la
                # diferencia es un descuento de linea del proveedor.
                "discount": line._implied_discount_pct(),
                "name": line.description or values.get("name"),
                "sequence": line.sequence,
            })
            if exempt_tax:
                values["tax_ids"] = [fields.Command.set(exempt_tax.ids)]
            line_commands.append(fields.Command.create(values))
        if tax_product and self.amount_tax:
            line_commands.append(fields.Command.create(
                self._prepare_tax_product_line(tax_product, exempt_tax, is_note, used_po_lines)
            ))
        values = {
            "move_type": "in_refund" if self.document_type == "credit_note" else "in_invoice",
            "partner_id": (order.partner_id or self.origin_move_id.partner_id or self.partner_id).id,
            "company_id": self.company_id.id,
            "currency_id": self.currency_id.id,
            "journal_id": journal.id,
            "invoice_date": self.invoice_date or fields.Date.context_today(self),
            "ref": self.invoice_ref or False,
            "cufe": self.cufe or False,
            "invoice_origin": self.purchase_names or False,
            "payment_reference": self.invoice_ref or False,
            "invoice_payment_term_id": (
                order.payment_term_id or self.origin_move_id.invoice_payment_term_id
            ).id,
            "fiscal_position_id": fiscal_position.id,
            "invoice_line_ids": line_commands,
        }
        if self.document_type == "credit_note":
            values["reversed_entry_id"] = self.origin_move_id.id
        elif self.document_type == "debit_note" and "debit_origin_id" in self.env["account.move"]._fields:
            # Solo si esta instalado account_debit_note (Jorels lo usa para
            # reconocer la nota debito); no se agrega como dependencia.
            values["debit_origin_id"] = self.origin_move_id.id
        return values

    def _prepare_extra_charge_line(self, line, fiscal_position):
        """Linea de flete u otro cargo que no esta en la orden de compra.

        Va con el producto de flete de Ajustes, sin vinculo a la orden. Lleva
        los impuestos del producto solo si la factura le cobra impuesto.
        """
        product = self._freight_product()
        taxes = self.env["account.tax"]
        if line.tax_rate:
            taxes = product.supplier_taxes_id.filtered(
                lambda tax: tax.company_id == self.company_id
            )
            taxes = fiscal_position.map_tax(taxes) if fiscal_position else taxes
        return {
            "product_id": product.id,
            "name": line.description,
            "quantity": line.quantity or 1.0,
            "price_unit": line.price_unit,
            "discount": line._implied_discount_pct(),
            "tax_ids": [fields.Command.set(taxes.ids)],
            "sequence": line.sequence,
        }

    def _prepare_tax_product_line(self, tax_product, exempt_tax, is_note, used_po_lines):
        """El IVA total de la factura como una linea del producto de mayor valor IVA."""
        name = _("%s - IVA factura %s") % (tax_product.display_name, self.invoice_ref or self.name)
        values = {
            "product_id": tax_product.id,
            "name": name,
            "quantity": 1.0,
            "price_unit": self.amount_tax,
            "tax_ids": [fields.Command.set(exempt_tax.ids)],
            "sequence": max(self.line_ids.mapped("sequence") or [0]) + 1,
        }
        if not is_note:
            po_line, _used = self._extra_po_line(
                tax_product, 1.0, self.amount_tax, exempt_tax, name, used_po_lines
            )
            if po_line.taxes_id != exempt_tax:
                po_line.taxes_id = exempt_tax
            values["purchase_line_id"] = po_line.id
        return values

    def _create_bill(self):
        self.ensure_one()
        journal = self._check_can_create_bill()
        values = self._prepare_bill_values(journal)
        move = (
            self.env["account.move"]
            .with_company(self.company_id)
            .with_context(default_move_type="in_invoice")
            .create(values)
        )
        self._attach_documents_to(move)
        self.write({"move_id": move.id, "state": "invoiced"})

        tolerance = validation_rules.Tolerance.from_env(self.env, self.currency_id)
        note = _("Factura borrador %s creada desde la solicitud %s.") % (move.name, self.name)
        if not tolerance.within(move.amount_total, self.amount_total):
            note += " " + _(
                "OJO: el total calculado por Odoo (%s) difiere del de la factura del "
                "proveedor (%s). Revise impuestos y redondeos antes de confirmar."
            ) % (
                self.currency_id.format(move.amount_total),
                self.currency_id.format(self.amount_total),
            )
        self.message_post(body=Markup("<p>%s</p>") % note)
        move.message_post(
            body=Markup("<p>%s</p>")
            % (_("Creada desde la solicitud de pago %s (ordenes: %s).")
               % (self.name, self.purchase_names or "-"))
        )
        self._notify_supplier("supplier_invoice_portal.mail_template_spr_invoiced")
        _logger.info("[SPR] %s: factura %s creada (id %s)", self.name, move.name, move.id)
        return move

    def _attach_documents_to(self, move):
        """Copia el PDF y el XML del proveedor como adjuntos de la factura."""
        attachments = self.env["ir.attachment"]
        for field_name, filename, default_name, mimetype in (
            ("pdf_file", self.pdf_filename, "factura.pdf", "application/pdf"),
            ("xml_file", self.xml_filename, "factura.xml", "application/xml"),
        ):
            content = self[field_name]
            if not content:
                continue
            attachments |= attachments.create({
                "name": filename or default_name,
                "datas": content,
                "mimetype": mimetype,
                "res_model": move._name,
                "res_id": move.id,
            })
        return attachments

    def action_view_purchases(self):
        """Abre la orden de compra, o la lista si la solicitud tiene varias."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("purchase.purchase_form_action")
        if len(self.purchase_ids) == 1:
            action.update({"res_id": self.purchase_ids.id, "view_mode": "form",
                           "views": [(False, "form")]})
        else:
            action["domain"] = [("id", "in", self.purchase_ids.ids)]
        action["context"] = {"create": False}
        return action

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("Esta solicitud todavia no tiene factura asociada."))
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": self.move_id.id,
            "view_mode": "form",
        }
