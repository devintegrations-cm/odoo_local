# -*- coding: utf-8 -*-
"""Portal del proveedor: lista, detalle y radicacion de solicitudes de pago.

Todo lo que toca purchase.order, account.move o el pipeline corre con sudo,
porque el usuario portal no tiene (ni debe tener) acceso a esos modelos. El
filtro por proveedor se aplica siempre de forma explicita con
``_spr_partner()``: nunca se confia en un id que venga del formulario sin
comprobar que pertenece al proveedor conectado.
"""

import base64
import logging
import re

from odoo import _, fields, http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import request
from odoo.osv.expression import AND

from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager

from ..services import dian_xml_parser, pdf_check
from ..models.supplier_payment_request import DOCUMENT_TYPES, NOTE_TYPES, STATE_SELECTION

_logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB por archivo

STATE_BADGE = {
    "draft": "text-bg-secondary",
    "validating": "text-bg-info",
    "approved": "text-bg-success",
    "warning": "text-bg-warning",
    "rejected": "text-bg-danger",
    "invoiced": "text-bg-primary",
    "cancelled": "text-bg-dark",
}


class SupplierInvoicePortal(CustomerPortal):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _spr_partner(self):
        """Proveedor (empresa) del usuario conectado, o None si no esta habilitado."""
        partner = request.env.user.partner_id.commercial_partner_id
        if not partner.sudo().portal_invoice_enabled:
            return None
        return partner

    def _spr_domain(self, partner):
        return [("partner_id", "=", partner.id)]

    def _spr_open_orders(self, partner):
        orders = request.env["purchase.order"].sudo().search(
            [
                ("partner_id", "child_of", partner.id),
                ("state", "in", ("purchase", "done")),
                ("invoice_status", "!=", "invoiced"),
                # Solo las del ano en curso (DECISIONS.md #50).
                ("date_order", ">=", request.env["purchase.order"]._spr_year_start()),
            ],
            order="date_order desc, id desc",
        )
        # Con una factura ya registrada solo caben notas sobre esa factura; con
        # una radicacion en curso, hay que esperar a que se resuelva.
        return orders.filtered(
            lambda order: not order._spr_vendor_bills() and not order._spr_open_requests()
        )

    def _spr_common_values(self):
        return {
            "state_labels": dict(STATE_SELECTION),
            "state_badge": STATE_BADGE,
            "document_labels": dict(DOCUMENT_TYPES),
        }

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if "spr_count" in counters:
            partner = self._spr_partner()
            values["spr_count"] = (
                request.env["supplier.payment.request"].search_count(self._spr_domain(partner))
                if partner else 0
            )
        return values

    # ------------------------------------------------------------------
    # Lista
    # ------------------------------------------------------------------

    @http.route(
        ["/my/payment-requests", "/my/payment-requests/page/<int:page>"],
        type="http", auth="user", website=True,
    )
    def portal_my_payment_requests(self, page=1, sortby=None, filterby=None, **kw):
        partner = self._spr_partner()
        if not partner:
            return request.render("supplier_invoice_portal.portal_spr_not_enabled",
                                  {"page_name": "payment_request"})

        searchbar_sortings = {
            "date": {"label": _("Mas recientes"), "order": "create_date desc, id desc"},
            "state": {"label": _("Estado"), "order": "state, create_date desc"},
            "amount": {"label": _("Monto"), "order": "amount_total desc"},
        }
        searchbar_filters = {
            "all": {"label": _("Todas"), "domain": []},
            "pending": {"label": _("En revision"),
                        "domain": [("state", "in", ("approved", "warning", "validating"))]},
            "rejected": {"label": _("Rechazadas"), "domain": [("state", "=", "rejected")]},
            "invoiced": {"label": _("Registradas"), "domain": [("state", "=", "invoiced")]},
        }
        sortby = sortby if sortby in searchbar_sortings else "date"
        filterby = filterby if filterby in searchbar_filters else "all"

        Request = request.env["supplier.payment.request"]
        domain = AND([self._spr_domain(partner), searchbar_filters[filterby]["domain"]])
        total = Request.search_count(domain)
        pager = portal_pager(
            url="/my/payment-requests",
            url_args={"sortby": sortby, "filterby": filterby},
            total=total,
            page=page,
            step=self._items_per_page,
        )
        records = Request.search(
            domain, order=searchbar_sortings[sortby]["order"],
            limit=self._items_per_page, offset=pager["offset"],
        )
        values = self._prepare_portal_layout_values()
        values.update(self._spr_common_values())
        values.update({
            "requests": records,
            "page_name": "payment_request",
            "pager": pager,
            "default_url": "/my/payment-requests",
            "searchbar_sortings": searchbar_sortings,
            "sortby": sortby,
            "searchbar_filters": searchbar_filters,
            "filterby": filterby,
        })
        return request.render("supplier_invoice_portal.portal_my_payment_requests", values)

    # ------------------------------------------------------------------
    # Detalle
    # ------------------------------------------------------------------

    @http.route(["/my/payment-requests/<int:request_id>"], type="http", auth="public", website=True)
    def portal_payment_request(self, request_id, access_token=None, **kw):
        try:
            record = self._document_check_access(
                "supplier.payment.request", request_id, access_token
            )
        except (AccessError, MissingError):
            return request.redirect("/my")
        values = self._spr_common_values()
        values.update({
            "record": record,
            "findings": record.sudo()._portal_findings(),
            "page_name": "payment_request",
            "purchase_name": record.sudo().purchase_names,
            "origin_name": record.sudo().origin_move_id.ref or record.sudo().origin_move_id.name,
            "document_labels": dict(DOCUMENT_TYPES),
        })
        return request.render("supplier_invoice_portal.portal_payment_request_page", values)

    # ------------------------------------------------------------------
    # Radicar
    # ------------------------------------------------------------------

    @http.route(["/my/payment-requests/new"], type="http", auth="user",
                website=True, methods=["GET", "POST"])
    def portal_new_payment_request(self, **post):
        partner = self._spr_partner()
        if not partner:
            return request.render("supplier_invoice_portal.portal_spr_not_enabled",
                                  {"page_name": "payment_request"})

        document_types = self._spr_document_types(partner)
        document_type = post.get("document_type")
        if document_type not in dict(document_types):
            document_type = document_types[0][0]
        form = request.httprequest.form
        values = self._spr_common_values()
        values.update({
            "page_name": "payment_request_new",
            "partner": partner,
            "document_type": document_type,
            "document_types": document_types,
            "orders": self._spr_open_orders(partner),
            # Obligado a facturar = no radica cuenta de cobro (DECISIONS.md #45).
            "xml_required": not partner.sudo().spr_support_document,
            "origin_moves": self._spr_origin_moves(partner),
            "cutoff": request.env["supplier.payment.request"].sudo()._radication_cutoff(),
            "errors": {},
            "form": {
                # El boton de la orden en el portal llega con ?purchase_id=.
                "purchase_ids": form.getlist("purchase_ids") or (
                    [post["purchase_id"]] if post.get("purchase_id") else []
                ),
                "origin_move_id": post.get("origin_move_id") or "",
                "cufe": (post.get("cufe") or "").strip(),
                "invoice_ref": (post.get("invoice_ref") or "").strip(),
                "invoice_date": (post.get("invoice_date") or "").strip(),
                "amount_total": (post.get("amount_total") or "").strip(),
                "supplier_note": (post.get("supplier_note") or "").strip(),
            },
        })
        # Cierre de fin de mes: ni el formulario se muestra (DECISIONS.md #38).
        if request.httprequest.method != "POST" or values["cutoff"]["closed"]:
            return request.render("supplier_invoice_portal.portal_payment_request_form", values)

        errors, payload = self._spr_validate_submission(partner, document_type, values, post)
        if errors:
            values["errors"] = errors
            return request.render("supplier_invoice_portal.portal_payment_request_form", values)

        try:
            with request.env.cr.savepoint():
                record = request.env["supplier.payment.request"].sudo().create(payload)
                record._run_pipeline()
        except (UserError, dian_xml_parser.DianXmlError) as error:
            values["errors"] = {"general": str(error)}
            return request.render("supplier_invoice_portal.portal_payment_request_form", values)
        except Exception:  # noqa: BLE001 - al proveedor no se le muestra el traceback
            _logger.exception("[SPR] Error radicando solicitud del proveedor %s", partner.id)
            values["errors"] = {"general": _(
                "Ocurrio un error inesperado al procesar la factura. Intente de nuevo o "
                "escribanos a compras."
            )}
            return request.render("supplier_invoice_portal.portal_payment_request_form", values)

        _logger.info("[SPR] %s radicada desde el portal por %s", record.name, request.env.user.login)
        return request.redirect("/my/payment-requests/%s?submitted=1" % record.id)

    def _spr_document_types(self, partner):
        """Tipos que el proveedor puede radicar. El primero es el de por defecto.

        La cuenta de cobro solo la ven los proveedores no obligados a facturar,
        y para ellos es la opcion por defecto.
        """
        labels = dict(DOCUMENT_TYPES)
        types = ["invoice", "credit_note", "debit_note"]
        if partner.sudo().spr_support_document:
            types.insert(0, "support_doc")
        return [(key, labels[key]) for key in types]

    def _spr_origin_domain(self, partner):
        """Facturas de este proveedor que una nota puede corregir.

        Todas las facturas de proveedor no canceladas, no solo las radicadas por
        el portal: la nota puede ser de una factura anterior.
        """
        return [
            ("move_type", "=", "in_invoice"),
            ("state", "!=", "cancel"),
            ("commercial_partner_id", "=", partner.id),
        ]

    def _spr_origin_moves(self, partner):
        """Las 100 mas recientes, para la lista del formulario. La validacion
        busca en todas (``_spr_validate_origin``): un proveedor con anos de
        compras tiene miles de facturas."""
        return request.env["account.move"].sudo().search(
            self._spr_origin_domain(partner),
            order="invoice_date desc, id desc",
            limit=100,
        )

    def _spr_read_file(self, field_name, post):
        """Devuelve (bytes, nombre) del archivo subido o (None, None) si no vino."""
        upload = post.get(field_name)
        if upload is None or not getattr(upload, "filename", ""):
            return None, None
        data = upload.read()
        return data, upload.filename

    @staticmethod
    def _spr_parse_amount(raw):
        """Lee un valor digitado a la colombiana ('1.234.567,89') o a la inglesa.

        Devuelve float o None si no es un numero.
        """
        text = re.sub(r"[^\d.,]", "", raw or "")
        if not text:
            return None
        if "." in text and "," in text:
            decimal = "," if text.rfind(",") > text.rfind(".") else "."
            thousands = "." if decimal == "," else ","
            text = text.replace(thousands, "").replace(decimal, ".")
        elif "," in text or "." in text:
            sep = "," if "," in text else "."
            head, _sep, tail = text.rpartition(sep)
            # Un solo separador con 1 o 2 decimales es la coma decimal; si no,
            # son miles ('1.500' es mil quinientos).
            if text.count(sep) == 1 and len(tail) in (1, 2):
                text = head + "." + tail
            else:
                text = text.replace(sep, "")
        try:
            return float(text)
        except ValueError:
            return None

    def _spr_validate_submission(self, partner, document_type, values, post):
        errors = {}
        is_note = document_type in NOTE_TYPES
        is_support = document_type == "support_doc"

        # Ordenes de compra (factura y cuenta de cobro): todas deben ser de las
        # abiertas del proveedor. Nunca se confia en ids del formulario sin
        # pasarlos por ese filtro.
        orders = request.env["purchase.order"].sudo()
        origin = request.env["account.move"].sudo()
        if not is_note:
            wanted = set()
            for raw_id in values["form"]["purchase_ids"]:
                try:
                    wanted.add(int(raw_id))
                except (TypeError, ValueError):
                    continue
            orders = values["orders"].filtered(lambda o: o.id in wanted)
            if not orders or len(orders) != len(wanted):
                errors["purchase_ids"] = _("Seleccione una o varias ordenes de compra vigentes.")
            elif len(orders.company_id) > 1:
                errors["purchase_ids"] = _("Las ordenes elegidas son de companias distintas; "
                                           "radique una factura por compania.")

        # PDF obligatorio.
        pdf_bytes, pdf_name = self._spr_read_file("pdf_file", post)
        if not pdf_bytes:
            errors["pdf_file"] = _("Adjunte el PDF del documento.")
        elif pdf_check.pdf_problem(pdf_bytes):  # liviano y sin contrasena (DECISIONS.md #47)
            errors["pdf_file"] = _("PDF: %s.") % pdf_check.pdf_problem(pdf_bytes)

        payload_extra = {}
        xml_bytes, xml_name, final_cufe = None, None, ""
        if is_support:
            payload_extra = self._spr_validate_support_doc(values["form"], errors)
        else:
            xml_bytes, xml_name, parsed, final_cufe = self._spr_validate_dian_document(
                document_type, post, errors, xml_required=values["xml_required"]
            )
            if is_note:
                origin = self._spr_validate_origin(values, parsed, errors)
                if origin:
                    orders = request.env["supplier.payment.request"].sudo()._origin_orders(origin)

        if errors:
            return errors, {}

        company = orders.company_id[:1] or origin.company_id
        Request = request.env["supplier.payment.request"].sudo().with_company(company)
        journal = Request._default_journal_id(document_type)
        payload = {
            "document_type": document_type,
            "partner_id": partner.id,
            "submitted_by_partner_id": request.env.user.partner_id.id,
            "purchase_ids": [(6, 0, orders.ids)],
            "origin_move_id": origin.id or False,
            "company_id": company.id,
            "currency_id": company.currency_id.id,
            "journal_id": journal.id if journal else False,
            "cufe": final_cufe or False,
            "pdf_file": base64.b64encode(pdf_bytes),
            "pdf_filename": pdf_name,
            "xml_file": base64.b64encode(xml_bytes) if xml_bytes else False,
            "xml_filename": xml_name or False,
            "supplier_note": (post.get("supplier_note") or "").strip() or False,
            "portal_submitted": True,
        }
        payload.update(payload_extra)
        return errors, payload

    def _spr_validate_dian_document(self, document_type, post, errors, xml_required=False):
        """XML y CUFE de una factura o nota electronica.

        El XML es obligatorio para los proveedores obligados a facturar
        (DECISIONS.md #45); para los demas es opcional y basta el CUFE. Si
        viene, tiene que ser un documento DIAN legible y del tipo que se radica.

        :return: ``(xml_bytes, xml_name, parsed, cufe)``
        """
        xml_bytes, xml_name = self._spr_read_file("xml_file", post)
        parsed = None
        if xml_required and not xml_bytes:
            errors["xml_file"] = _(
                "Adjunte el XML de la DIAN: es obligatorio para los proveedores que "
                "facturan electronicamente."
            )
        if xml_bytes:
            if len(xml_bytes) > MAX_FILE_BYTES:
                errors["xml_file"] = _("El XML supera el tamano maximo de 10 MB.")
            else:
                try:
                    parsed = dian_xml_parser.parse_dian_xml(xml_bytes)
                except dian_xml_parser.DianXmlError as error:
                    errors["xml_file"] = _("No se pudo leer el XML: %s") % error
            if parsed and parsed.get("document_type") != document_type:
                labels = dict(DOCUMENT_TYPES)
                errors["xml_file"] = _(
                    "El XML es %s y usted esta radicando %s. Elija el tipo de documento "
                    "correcto arriba."
                ) % (labels.get(parsed.get("document_type"), "otro documento").lower(),
                     labels[document_type].lower())
                parsed = None

        # CUFE: obligatorio si no hay XML; si hay XML, debe coincidir. Con XML
        # obligatorio el formulario no lo pide y sale solo del XML.
        cufe = "" if xml_required else dian_xml_parser.normalize_cufe(post.get("cufe") or "")
        xml_cufe = dian_xml_parser.normalize_cufe(parsed.get("cufe")) if parsed else ""
        if xml_cufe and not dian_xml_parser.is_valid_cufe(xml_cufe):
            errors["xml_file"] = _("El CUFE del XML no tiene el formato esperado (96 hexadecimales).")
            xml_cufe = ""
        if cufe and not dian_xml_parser.is_valid_cufe(cufe):
            errors["cufe"] = _("El CUFE debe tener 96 caracteres hexadecimales.")
        elif cufe and xml_cufe and cufe != xml_cufe:
            errors["cufe"] = _("El CUFE digitado no coincide con el del XML adjunto.")
        elif not cufe and not xml_cufe and "xml_file" not in errors:
            errors["cufe"] = _("Digite el CUFE del documento o adjunte el XML de la DIAN.")
        return xml_bytes, xml_name, parsed, cufe or xml_cufe

    def _spr_validate_origin(self, values, parsed, errors):
        """Factura que afecta la nota: la elegida en el formulario o la que el
        XML referencia, siempre entre las facturas de este proveedor."""
        Move = request.env["account.move"].sudo()
        domain = self._spr_origin_domain(values["partner"])
        try:
            chosen_id = int(values["form"]["origin_move_id"] or 0)
        except (TypeError, ValueError):
            chosen_id = 0
        origin = Move.search(domain + [("id", "=", chosen_id)]) if chosen_id else Move
        if chosen_id and not origin:
            errors["origin_move_id"] = _("Seleccione una de sus facturas.")
            return origin
        reference = (parsed or {}).get("billing_reference") or {}
        if not origin:
            if reference.get("cufe"):
                origin = Move.search(domain + [("cufe", "=", reference["cufe"])], limit=1)
            if not origin and reference.get("number"):
                origin = Move.search(domain + [("ref", "=", reference["number"])], limit=1)
        if not origin:
            errors["origin_move_id"] = _(
                "Seleccione la factura que afecta la nota."
            ) + (_(" La nota menciona la factura %s, que no encontramos registrada.")
                 % reference["number"] if reference.get("number") else "")
        return origin

    def _spr_validate_support_doc(self, form, errors):
        """Cuenta de cobro: el proveedor declara numero, fecha y total."""
        extra = {}
        if not form["invoice_ref"]:
            errors["invoice_ref"] = _("Digite el numero de la cuenta de cobro.")
        else:
            extra["invoice_ref"] = form["invoice_ref"][:64]
        try:
            issue_date = fields.Date.to_date(form["invoice_date"]) if form["invoice_date"] else None
        except ValueError:
            issue_date = None
        if not issue_date:
            errors["invoice_date"] = _("Indique la fecha de la cuenta de cobro.")
        elif issue_date > fields.Date.context_today(request.env.user):
            errors["invoice_date"] = _("La fecha no puede estar en el futuro.")
        else:
            extra["invoice_date"] = issue_date
        amount = self._spr_parse_amount(form["amount_total"])
        if not amount or amount <= 0:
            errors["amount_total"] = _("Digite el valor total de la cuenta de cobro.")
        else:
            extra["amount_total"] = amount
        return extra
