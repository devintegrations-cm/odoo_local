# -*- coding: utf-8 -*-
"""Tests de la Fase 3: portal del proveedor.

Un usuario portal del proveedor radica una factura por HTTP con PDF y XML, la
solicitud se valida en el envio y el proveedor ve el resultado. Tambien se
comprueba que un proveedor no habilitado no puede radicar y que nadie ve las
solicitudes de otro.
"""

import re
from datetime import date, datetime
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import SprCase, read_fixture

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


@tagged("post_install", "-at_install")
class TestSupplierPortal(SprCase, HttpCase):

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.company.email = "compras@libertario.example"
        cls.supplier.email = "facturacion@cafesdelsur.example"

        cls.contact = cls.env["res.partner"].create({
            "name": "Ana Contadora",
            "parent_id": cls.supplier.id,
            "email": "ana@cafesdelsur.example",
            "company_id": False,
        })
        cls.portal_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Ana Contadora",
            "login": "ana_portal",
            "password": "ana_portal_pw_2026",
            "partner_id": cls.contact.id,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "groups_id": [(6, 0, cls.env.ref("base.group_portal").ids)],
        })

        # Otro proveedor con su propio usuario portal: no debe ver nada ajeno.
        cls.other_supplier = cls.env["res.partner"].create({
            "name": "OTRO PROVEEDOR SAS",
            "is_company": True,
            "vat": "800111222",
            "email": "otro@example.com",
            "company_id": False,
        })
        cls.other_user = cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Otro Portal",
            "login": "otro_portal",
            "password": "otro_portal_pw_2026",
            "partner_id": cls.env["res.partner"].create({
                "name": "Otro Contacto", "parent_id": cls.other_supplier.id, "company_id": False,
            }).id,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "groups_id": [(6, 0, cls.env.ref("base.group_portal").ids)],
        })

    def _csrf(self, html):
        match = CSRF_RE.search(html)
        self.assertTrue(match, "El formulario no trae csrf_token")
        return match.group(1)

    def _submit(self, order, xml_name="invoice_direct.xml", cufe="", with_xml=True, with_pdf=True,
                document_type="invoice", **fields):
        page = self.url_open("/my/payment-requests/new?document_type=%s" % document_type)
        self.assertEqual(page.status_code, 200)
        token = self._csrf(page.text)
        files = []
        if with_pdf:
            files.append(("pdf_file", ("factura.pdf", b"%PDF-1.4\n%dummy\n%%EOF\n", "application/pdf")))
        if with_xml:
            files.append(("xml_file", (xml_name, read_fixture(xml_name), "application/xml")))
        data = {
            "csrf_token": token,
            "document_type": document_type,
            "purchase_ids": [str(order_id) for order_id in order.ids] if order else [],
            "cufe": cufe,
            "supplier_note": "Radicada desde el test",
        }
        data.update(fields)
        return self.url_open("/my/payment-requests/new", data=data, files=files)

    # ------------------------------------------------------------------

    def test_home_shows_entry_and_list_is_empty(self):
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        home = self.url_open("/my")
        self.assertEqual(home.status_code, 200)
        self.assertIn("/my/payment-requests", home.text)
        # Regresion: sin radicaciones el contador es 0 y Odoo dejaba la tarjeta
        # con d-none. Tiene que verse aunque nunca haya radicado.
        card = re.search(r'<div class="(o_portal_index_card[^"]*)">\s*<a href="[^"]*/my/payment-requests"',
                         home.text)
        self.assertTrue(card, "No esta la tarjeta del portal de facturas")
        self.assertNotIn("d-none", card.group(1))
        listing = self.url_open("/my/payment-requests")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("Todavia no ha radicado ninguna factura", listing.text)

    def test_submit_with_xml_is_validated_and_visible(self):
        order = self._standard_po()
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        response = self._submit(order)
        self.assertEqual(response.status_code, 200)

        record = self.env["supplier.payment.request"].search(
            [("partner_id", "=", self.supplier.id)], order="id desc", limit=1
        )
        self.assertTrue(record, "La solicitud no se creo")
        self.assertTrue(record.portal_submitted)
        self.assertEqual(record.purchase_ids, order)
        self.assertEqual(record.submitted_by_partner_id, self.contact)
        self.assertEqual(record.company_id, self.company)
        self.assertEqual(record.state, "approved", record.validation_summary)
        self.assertEqual(record.invoice_ref, "SETP990000001")
        self.assertEqual(record.supplier_note, "Radicada desde el test")
        self.assertEqual(record.pdf_filename, "factura.pdf")
        self.assertEqual(record.xml_filename, "invoice_direct.xml")

        # Redirigio al detalle y el detalle muestra el estado.
        self.assertIn("/my/payment-requests/%s" % record.id, response.url)
        self.assertIn("Aprobada", response.text)
        self.assertIn("SETP990000001", response.text)

        # Correo al proveedor: queda como mensaje en el chatter dirigido a el.
        mails = record.message_ids.filtered(lambda m: self.supplier in m.partner_ids)
        self.assertTrue(mails, "No se notifico al proveedor")
        self.assertIn("recibimos su factura", (mails[0].subject or "").lower())

        # Y aparece en la lista.
        listing = self.url_open("/my/payment-requests")
        self.assertIn(record.name, listing.text)

    def test_submit_rejected_shows_errors(self):
        # OC muy pequena: la factura supera lo pendiente y la cantidad.
        order = self._create_po([
            (self.product_cafe, 10, 12000.0),
            (self.product_bag, 50, 2000.0),
        ])
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        response = self._submit(order)
        record = self.env["supplier.payment.request"].search(
            [("partner_id", "=", self.supplier.id)], order="id desc", limit=1
        )
        self.assertEqual(record.state, "rejected")
        self.assertIn("Rechazada", response.text)
        self.assertIn("supera lo pendiente", response.text)
        mails = record.message_ids.filtered(lambda m: self.supplier in m.partner_ids)
        self.assertTrue(mails)
        self.assertIn("requiere correccion", (mails[0].subject or "").lower())

    def test_form_errors_do_not_create_record(self):
        order = self._standard_po()
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        before = self.env["supplier.payment.request"].search_count([])

        # Sin PDF.
        response = self._submit(order, with_pdf=False)
        self.assertIn("Adjunte el PDF", response.text)
        # PDF con contrasena (DECISIONS.md #47).
        page = self.url_open("/my/payment-requests/new?document_type=invoice")
        response = self.url_open("/my/payment-requests/new", data={
            "csrf_token": self._csrf(page.text), "document_type": "invoice",
            "purchase_ids": [str(order.id)],
        }, files=[
            ("pdf_file", ("f.pdf", b"%PDF-1.4\ntrailer<</Encrypt 2 0 R>>\n%%EOF\n", "application/pdf")),
            ("xml_file", ("invoice_direct.xml", read_fixture("invoice_direct.xml"), "application/xml")),
        ])
        self.assertIn("tiene contrasena o esta protegido", response.text)
        # Obligado a facturar sin XML, aunque traiga CUFE (DECISIONS.md #45).
        response = self._submit(order, with_xml=False, cufe="a" * 96)
        self.assertIn("Adjunte el XML de la DIAN", response.text)
        # OC de otro proveedor.
        other_order = self.env["purchase.order"].create({
            "partner_id": self.other_supplier.id,
            "company_id": self.company.id,
            "order_line": [(0, 0, {
                "product_id": self.product_cafe.id, "name": "x", "product_qty": 1,
                "product_uom": self.product_cafe.uom_po_id.id, "price_unit": 1.0,
                "date_planned": "2026-03-01",
            })],
        })
        other_order.button_confirm()
        response = self._submit(other_order)
        self.assertIn("Seleccione una o varias ordenes de compra vigentes", response.text)

        self.assertEqual(self.env["supplier.payment.request"].search_count([]), before)

    def test_not_enabled_supplier_cannot_submit(self):
        self.other_supplier.portal_invoice_enabled = False
        self.authenticate("otro_portal", "otro_portal_pw_2026")
        response = self.url_open("/my/payment-requests/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no esta habilitada", response.text)
        self.assertNotIn("Radicar facturas", self.url_open("/my").text)

    def test_portal_user_cannot_create_by_rpc(self):
        # El portal crea por el controlador con sudo; por ORM directo (JSON-RPC)
        # un proveedor no puede fabricar solicitudes ni fijar el estado.
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.env["supplier.payment.request"].with_user(self.portal_user).create({
                "partner_id": self.supplier.id,
                "state": "approved",
                "amount_total": 50000000,
            })

    def test_other_supplier_cannot_see_request(self):
        order = self._standard_po()
        record = self._create_request(order)
        record.action_validate()

        self.authenticate("otro_portal", "otro_portal_pw_2026")
        self.other_supplier.portal_invoice_enabled = True
        listing = self.url_open("/my/payment-requests")
        self.assertNotIn(record.name, listing.text)
        detail = self.url_open("/my/payment-requests/%s" % record.id, allow_redirects=False)
        self.assertIn(detail.status_code, (302, 303))
        self.assertTrue(detail.headers.get("Location", "").endswith("/my"))

        # Con el token de acceso si se puede ver (es lo que va en el correo).
        detail = self.url_open(record.get_portal_url())
        self.assertEqual(detail.status_code, 200)
        self.assertIn(record.name, detail.text)

    def test_rejection_by_manager_notifies_supplier(self):
        order = self._standard_po()
        record = self._create_request(order)
        record.action_validate()
        record.action_reject(reason="Falta el soporte de entrega.")
        mails = record.message_ids.filtered(lambda m: self.supplier in m.partner_ids)
        self.assertTrue(any("rechazada" in (m.subject or "").lower() for m in mails))

        self.authenticate("ana_portal", "ana_portal_pw_2026")
        detail = self.url_open("/my/payment-requests/%s" % record.id)
        self.assertIn("Falta el soporte de entrega.", detail.text)

    def test_la_vista_del_contacto_no_exige_que_sea_empresa(self):
        """Regresion: la casilla se escondia con invisible="not is_company".

        Libertario tiene proveedores empresa y persona natural. Con esa
        condicion, a los segundos no habia forma de habilitarlos desde la
        interfaz: la casilla ni se dibujaba. Ahora solo se esconde en los
        contactos hijos (que tienen padre y no son empresa).
        """
        view = self.env.ref("supplier_invoice_portal.view_partner_form_spr")
        self.assertNotIn(
            'invisible="not is_company"', view.arch,
            "La casilla de radicacion no puede depender de que el contacto sea "
            "empresa: hay proveedores persona natural.",
        )
        self.assertIn('invisible="parent_id and not is_company"', view.arch)

    def test_la_casilla_del_contacto_va_en_su_propio_grupo(self):
        """Regresion: en staging el NIT esta dentro de un div en linea
        (base_vat) y las casillas, puestas despues del NIT, salian sin etiqueta."""
        from lxml import etree
        arch = self.env["res.partner"].get_views([(False, "form")])["views"]["form"]["arch"]
        field = etree.fromstring(arch).xpath("//field[@name='portal_invoice_enabled']")[0]
        self.assertEqual(field.getparent().tag, "group")
        page = field.xpath("ancestor::page[1]")[0]
        self.assertEqual((page.get("name"), page.get("string")), ("spr_portal", "Portal de proveedor"))

    def test_persona_natural_habilitada_puede_radicar(self):
        """Una persona natural sin contacto padre es su propio comercial.

        El controlador siempre lo soporto (lee el flag de commercial_partner_id);
        lo que faltaba era poder marcarlo. Aqui se fija el flujo completo.
        """
        natural = self.env["res.partner"].create({
            "name": "GOMEZ RUIZ, CARLOS",
            "is_company": False,
            "vat": "1018468414-1",
            "email": "carlos@example.com",
            "portal_invoice_enabled": True,
            "company_id": False,
        })
        self.assertEqual(
            natural.commercial_partner_id, natural,
            "Sin contacto padre, la persona natural es su propio comercial.",
        )
        self.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Carlos Portal",
            "login": "carlos_portal",
            "password": "carlos_portal_pw_2026",
            "partner_id": natural.id,
            "company_id": self.company.id,
            "company_ids": [(6, 0, self.company.ids)],
            "groups_id": [(6, 0, self.env.ref("base.group_portal").ids)],
        })
        order = self.env["purchase.order"].create({
            "partner_id": natural.id,
            "company_id": self.company.id,
            "currency_id": self.company.currency_id.id,
            "order_line": [(0, 0, {
                "product_id": self.product_cafe.id,
                "name": self.product_cafe.name,
                "product_qty": 100,
                "product_uom": self.product_cafe.uom_po_id.id,
                "price_unit": 12000.0,
                "taxes_id": [(6, 0, self.tax_19.ids)],
                "date_planned": "2026-03-01",
            })],
        })
        order.button_confirm()

        self.authenticate("carlos_portal", "carlos_portal_pw_2026")
        home = self.url_open("/my")
        self.assertIn("Radicar facturas", home.text,
                      "La tarjeta del portal debe aparecerle a la persona natural.")
        response = self._submit(order)
        self.assertEqual(response.status_code, 200)
        record = self.env["supplier.payment.request"].search(
            [("partner_id", "=", natural.id)], limit=1)
        self.assertTrue(record, "La persona natural habilitada debe poder radicar.")

    # ------------------------------------------------------------------
    # Ajustes del comite
    # ------------------------------------------------------------------

    def test_submit_several_orders_and_notify_submitter(self):
        order_cafe = self._create_po([(self.product_cafe, 100, 12000.0)])
        order_bag = self._create_po([(self.product_bag, 50, 2000.0)])
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        # Regresion: la variable del ciclo se llamaba "order" y las migas de
        # pan de Compras mostraban la ultima orden de la lista.
        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertNotIn("/my/purchase/%s" % order_bag.id, form.text)
        self.assertNotIn("/my/purchase/%s" % order_cafe.id, form.text)
        self._submit(order_cafe | order_bag)
        record = self.env["supplier.payment.request"].search(
            [("partner_id", "=", self.supplier.id)], order="id desc", limit=1
        )
        self.assertEqual(record.purchase_ids, order_cafe | order_bag)
        self.assertEqual(record.state, "approved", record.validation_summary)
        mails = record.message_ids.filtered(lambda m: self.contact in m.partner_ids)
        self.assertTrue(mails, "Quien radico tambien recibe el correo.")

    def test_submit_credit_note_from_portal(self):
        order = self._standard_po()
        invoice_request = self._create_request(order)
        invoice_request.action_validate()
        invoice_request.action_create_bill()

        self.authenticate("ana_portal", "ana_portal_pw_2026")
        form = self.url_open("/my/payment-requests/new?document_type=credit_note")
        self.assertIn("Factura que afecta", form.text)
        self.assertIn("SETP990000001", form.text)

        # Una factura de otro proveedor no se puede elegir.
        foreign = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.other_supplier.id,
            "invoice_date": "2026-03-01", "ref": "AJENA-1",
            "invoice_line_ids": [(0, 0, {"name": "x", "quantity": 1, "price_unit": 10})],
        })
        response = self._submit(None, xml_name="credit_note.xml", document_type="credit_note",
                                origin_move_id=str(foreign.id))
        self.assertIn("Seleccione una de sus facturas", response.text)

        # Sin elegirla: se toma del XML.
        response = self._submit(None, xml_name="credit_note.xml", document_type="credit_note")
        note = self.env["supplier.payment.request"].search(
            [("document_type", "=", "credit_note")], limit=1
        )
        self.assertTrue(note, response.text[:500])
        self.assertEqual(note.origin_move_id, invoice_request.move_id)
        self.assertEqual(note.state, "approved", note.validation_summary)
        self.assertIn("Nota credito", response.text)

        # Una nota radicada como factura no pasa del formulario. Orden nueva:
        # la primera ya quedo facturada y el formulario no la ofrece.
        response = self._submit(self._standard_po(), document_type="invoice",
                                xml_name="credit_note.xml")
        self.assertIn("usted esta radicando factura electronica", response.text)

    def test_order_with_partial_bill_only_accepts_notes(self):
        # Factura parcial: solo la primera linea. La orden sigue "por facturar",
        # pero ya tiene factura y no se ofrece para otra factura.
        order = self._standard_po()
        bill = self.env["account.move"].create({
            "move_type": "in_invoice", "partner_id": self.supplier.id,
            "invoice_date": "2026-03-10", "ref": "PARCIAL-1",
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product_cafe.id, "quantity": 10, "price_unit": 12000.0,
                "purchase_line_id": order.order_line[0].id,
            })],
        })
        bill.action_post()
        self.assertEqual(order.invoice_status, "to invoice")
        open_order = self._standard_po()  # sin ordenes vigentes el formulario no se dibuja

        self.authenticate("ana_portal", "ana_portal_pw_2026")
        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertIn(open_order.name, form.text)
        self.assertNotIn(order.name, form.text)
        response = self._submit(order)
        self.assertIn("Seleccione una o varias ordenes de compra vigentes", response.text)
        self.assertFalse(self.env["supplier.payment.request"].search(
            [("purchase_ids", "in", order.id)]))

        # La nota credito si puede elegir esa factura.
        form = self.url_open("/my/payment-requests/new?document_type=credit_note")
        self.assertIn("PARCIAL-1", form.text)

    def test_note_finds_an_old_invoice_beyond_the_list(self):
        # La lista del formulario muestra las 100 facturas mas recientes; una
        # nota sobre una factura mas vieja se tiene que encontrar igual.
        order = self._standard_po()
        invoice_request = self._create_request(order)
        invoice_request.action_validate()
        invoice_request.action_create_bill()
        old_bill = invoice_request.move_id
        old_bill.invoice_date = "2024-01-15"
        self.env["account.move"].create([{
            "move_type": "in_invoice", "partner_id": self.supplier.id,
            "invoice_date": "2026-03-01", "ref": "RECIENTE-%s" % n,
            "invoice_line_ids": [(0, 0, {"name": "x", "quantity": 1, "price_unit": 10})],
        } for n in range(100)])

        self.authenticate("ana_portal", "ana_portal_pw_2026")
        form = self.url_open("/my/payment-requests/new?document_type=credit_note")
        self.assertNotIn('value="%s"' % old_bill.id, form.text)
        self._submit(None, xml_name="credit_note.xml", document_type="credit_note")
        note = self.env["supplier.payment.request"].search(
            [("document_type", "=", "credit_note")], limit=1)
        self.assertEqual(note.origin_move_id, old_bill)

    def test_purchase_order_page_links_to_the_form(self):
        order = self._standard_po()
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        page = self.url_open(order.get_portal_url())
        self.assertEqual(page.status_code, 200)
        self.assertIn("/my/payment-requests/new?purchase_id=%s" % order.id, page.text)
        form = self.url_open("/my/payment-requests/new?purchase_id=%s" % order.id)
        self.assertRegex(form.text, r'value="%s"\s+checked' % order.id)

        # Con factura, la orden ya no ofrece el boton.
        invoice_request = self._create_request(order)
        invoice_request.action_validate()
        invoice_request.action_create_bill()
        page = self.url_open(order.get_portal_url())
        self.assertNotIn("/my/payment-requests/new?purchase_id=%s" % order.id, page.text)

    def test_xml_is_optional_only_for_non_invoicing_suppliers(self):
        order = self._standard_po()
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertRegex(form.text, r'name="xml_file"[^>]*required')
        self.assertNotIn('name="cufe"', form.text)
        self.assertEqual(form.text.count("o_spr_dropzone "), 2, "PDF y XML con arrastrar y soltar")

        # No obligado: el XML es opcional y basta el CUFE.
        self.supplier.spr_support_document = True
        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertIn('name="cufe"', form.text)
        response = self._submit(order, cufe="b" * 96)
        self.assertIn("no coincide con el del XML", response.text)
        response = self._submit(order, with_xml=False, cufe="a" * 96)
        self.assertNotIn("Adjunte el XML de la DIAN", response.text)
        self.assertTrue(self.env["supplier.payment.request"].search(
            [("purchase_ids", "in", order.id)]))

    def test_order_with_an_open_request_is_not_offered_again(self):
        order = self._standard_po()
        other = self._standard_po()  # para que el formulario se siga dibujando
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        self._submit(order)
        first = self.env["supplier.payment.request"].search([("purchase_ids", "in", order.id)])
        self.assertEqual(len(first), 1)

        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertIn(other.name, form.text)
        self.assertNotIn(order.name, form.text, "Radicada y en curso: no se ofrece otra vez.")
        self._submit(order)
        self.assertEqual(
            self.env["supplier.payment.request"].search_count([("purchase_ids", "in", order.id)]), 1)

        # Rechazada, el proveedor puede corregir y radicar de nuevo.
        first.action_reject(reason="PDF ilegible.")
        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertIn(order.name, form.text)

    def test_orders_from_last_year_are_not_offered(self):
        current = self._standard_po()
        old = self._standard_po()
        last_year = datetime(datetime.now().year - 1, 12, 15, 15, 0)
        old.date_order = last_year
        self.assertFalse(old._spr_accepts_invoice())
        self.assertTrue(current._spr_accepts_invoice())

        self.authenticate("ana_portal", "ana_portal_pw_2026")
        form = self.url_open("/my/payment-requests/new?document_type=invoice")
        self.assertIn(current.name, form.text)
        self.assertNotIn(old.name, form.text, "Del ano pasado: no se ofrece.")
        # Ni forzando el id en el envio.
        self._submit(old)
        self.assertFalse(self.env["supplier.payment.request"].search_count(
            [("purchase_ids", "in", old.id)]))

    def test_support_doc_only_for_non_invoicing_suppliers(self):
        order = self._standard_po()
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        form = self.url_open("/my/payment-requests/new")
        self.assertNotIn("Cuenta de cobro", form.text)

        self.supplier.spr_support_document = True
        form = self.url_open("/my/payment-requests/new")
        self.assertIn("Cuenta de cobro", form.text)
        self.assertIn('name="amount_total"', form.text, "Es el tipo por defecto para ellos.")
        self._submit(order, with_xml=False, document_type="support_doc",
                     invoice_ref="CC-7", invoice_date="2026-03-15", amount_total="1.547.000")
        record = self.env["supplier.payment.request"].search(
            [("document_type", "=", "support_doc")], limit=1
        )
        self.assertTrue(record)
        self.assertEqual(record.amount_total, 1547000.0)
        self.assertEqual(record.invoice_ref, "CC-7")
        self.assertFalse(record.cufe)
        self.assertEqual(record.state, "approved", record.validation_summary)

    def test_month_end_cutoff_closes_the_form(self):
        self.env["ir.config_parameter"].sudo().set_param("spr.cutoff_enabled", "True")
        order = self._standard_po()
        self.authenticate("ana_portal", "ana_portal_pw_2026")
        # El token se toma con el portal abierto: cerrado, el formulario no sale.
        token = self._csrf(self.url_open("/my/payment-requests/new").text)
        closed = (True, datetime(2026, 9, 30, 12, 0), date(2026, 10, 1))
        with patch("odoo.addons.supplier_invoice_portal.services.cutoff.radication_closed",
                   return_value=closed):
            form = self.url_open("/my/payment-requests/new")
            self.assertIn("cerrada por cierre de mes", form.text)
            self.assertIn("01/10/2026", form.text)
            self.assertNotIn('name="pdf_file"', form.text)
            # Ni forzando el POST.
            before = self.env["supplier.payment.request"].search_count([])
            self.url_open(
                "/my/payment-requests/new",
                data={"csrf_token": token, "purchase_ids": [str(order.id)]},
                files=[("pdf_file", ("f.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")),
                       ("xml_file", ("f.xml", read_fixture("invoice_direct.xml"), "application/xml"))],
            )
            self.assertEqual(self.env["supplier.payment.request"].search_count([]), before)
        # Abierto: el formulario avisa la hora de corte.
        form = self.url_open("/my/payment-requests/new")
        self.assertIn("ultimo dia habil de cada mes", form.text)

