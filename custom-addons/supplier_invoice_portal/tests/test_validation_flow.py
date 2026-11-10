# -*- coding: utf-8 -*-
"""Tests de la Fase 2: reglas duras, matching por codigo y creacion de factura.

Usan una orden de compra real y los fixtures XML del parser. Ninguno hace red:
la IA y el OCR quedan en modo noop.
"""


import json

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import Form

from odoo.addons.supplier_invoice_portal.services import validation_rules

from .common import SprCase, read_fixture


@tagged("post_install", "-at_install")
class TestValidationFlow(SprCase):

    # ------------------------------------------------------------------
    # Flujo feliz
    # ------------------------------------------------------------------

    def test_approved_and_bill_created(self):
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()

        self.assertEqual(request.state, "approved", request.validation_summary)
        self.assertFalse(self._codes(request, "error"))
        self.assertFalse(self._codes(request, "warning"))
        self.assertEqual(request.source, "xml")
        self.assertEqual(request.supplier_nit, "900123456")
        self.assertEqual(request.amount_total, 1547000.0)

        # Emparejamiento: uno por default_code, otro por supplierinfo.
        lines = request.line_ids.sorted("sequence")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].po_line_id.product_id, self.product_cafe)
        self.assertEqual(lines[1].po_line_id.product_id, self.product_bag)
        self.assertEqual(set(lines.mapped("match_method")), {"code"})

        # Actividad para el grupo validador.
        self.assertTrue(request.activity_ids)
        self.assertIn(request.activity_ids.user_id, request._get_validator_group().users)

        action = request.action_create_bill()
        move = request.move_id
        self.assertTrue(move)
        self.assertEqual(action["res_id"], move.id)
        self.assertEqual(request.state, "invoiced")
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.partner_id, self.supplier)
        self.assertEqual(move.ref, "SETP990000001")
        self.assertEqual(move.cufe, request.cufe)
        self.assertEqual(move.invoice_origin, order.name)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertEqual(
            set(move.invoice_line_ids.mapped("purchase_line_id")), set(order.order_line)
        )
        self.assertAlmostEqual(move.amount_untaxed, 1300000.0, places=2)
        self.assertAlmostEqual(move.amount_tax, 247000.0, places=2)
        self.assertAlmostEqual(move.amount_total, 1547000.0, places=2)

        # La OC ya sabe que esta facturada (aunque el borrador no este validado).
        self.assertEqual(order.order_line[0].qty_invoiced, 100)
        self.assertEqual(order.invoice_status, "invoiced")

        # PDF y XML viajan a la factura.
        attachments = self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"), ("res_id", "=", move.id),
        ])
        self.assertEqual(set(attachments.mapped("name")), {"factura.pdf", "invoice_direct.xml"})

        # No se puede crear dos veces.
        with self.assertRaises(UserError):
            request.action_create_bill()

    def test_bill_with_line_discount(self):
        order = self._create_po([(self.product_cafe2, 200, 10000.0)])
        request = self._create_request(order, xml_name="invoice_with_discounts.xml")
        request.action_validate()
        self.assertEqual(request.state, "approved", request.validation_summary)

        line = request.line_ids
        self.assertEqual(line._implied_discount_pct(), 10.0)
        request.action_create_bill()
        move = request.move_id
        self.assertEqual(move.invoice_line_ids.discount, 10.0)
        self.assertAlmostEqual(move.amount_untaxed, 1800000.0, places=2)
        self.assertAlmostEqual(move.amount_total, 2142000.0, places=2)

    def test_unlink_bill_resets_request(self):
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        request.action_create_bill()
        request.move_id.unlink()
        self.assertFalse(request.move_id)
        self.assertEqual(request.state, "approved")

    # ------------------------------------------------------------------
    # Reglas duras
    # ------------------------------------------------------------------

    def test_supplier_nit_mismatch(self):
        self.supplier.vat = "800999888-1"
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertIn("SUPPLIER_NIT", self._codes(request, "error"))

    def test_party_check_can_be_disabled(self):
        # Con la verificacion apagada, un NIT de emisor distinto ya no rechaza
        # y los campos de emisor/adquiriente se ocultan.
        self.supplier.vat = "800000001-1"
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertIn("SUPPLIER_NIT", self._codes(request, "error"))
        self.assertTrue(request.party_check_enabled)

        settings = self.env["res.config.settings"].create({})
        self.assertTrue(settings.spr_party_check)
        settings.spr_party_check = False
        settings.set_values()
        self.assertEqual(self.env["ir.config_parameter"].sudo().get_param("spr.party_check"), "False")
        request.action_validate()
        codes = [f["code"] for f in json.loads(request.validation_json)["findings"]]
        self.assertNotIn("SUPPLIER_NIT", codes)
        self.assertNotIn("CUSTOMER_NIT", codes)
        self.assertEqual(request.state, "approved", request.validation_summary)
        self.assertFalse(request.party_check_enabled)

        settings.spr_party_check = True
        settings.set_values()
        request.action_validate()
        self.assertEqual(request.state, "rejected")

    def test_customer_nit_mismatch(self):
        self.env["ir.config_parameter"].sudo().set_param("spr.company_nit", "800000000")
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertIn("CUSTOMER_NIT", self._codes(request, "error"))

    def test_qty_over_po(self):
        # La OC solo pide 60 kg; la factura trae 100.
        order = self._create_po([
            (self.product_cafe, 60, 12000.0),
            (self.product_bag, 50, 2000.0),
        ])
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        errors = self._codes(request, "error")
        self.assertIn("QTY_OVER_PO", errors)
        self.assertIn("AMOUNT_TOTAL", errors)
        with self.assertRaises(UserError):
            request.action_create_bill()

    def test_price_mismatch_is_warning(self):
        order = self._create_po([
            (self.product_cafe, 100, 15000.0),
            (self.product_bag, 50, 2000.0),
        ])
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "warning")
        self.assertIn("PRICE_UNIT_MISMATCH", self._codes(request, "warning"))
        # Con observaciones si se puede facturar: el validador decide.
        request.action_create_bill()
        self.assertEqual(request.state, "invoiced")

    def test_price_tolerance_is_percent_only(self):
        # 10.000 unidades a 500: un sobreprecio de 900 por unidad es 2,7 M en
        # total, pero cabe en la tolerancia absoluta de 1.000. Debe observarse.
        rules = validation_rules
        tol = rules.Tolerance(pct=0.5, abs_amount=1000.0, rounding=0.01)
        self.assertTrue(tol.within(1400.0, 500.0))
        self.assertFalse(tol.within_pct(1400.0, 500.0))
        self.assertTrue(tol.within_pct(502.0, 500.0))
        self.assertTrue(tol.within_pct(500.004, 500.0))

    def test_po_pending_counts_only_linked_lines(self):
        # Una factura manual que mezcla una linea de esta OC con una linea
        # ajena (flete) solo descuenta la linea ligada.
        order = self._standard_po()
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.company.id)], limit=1)
        bill = self.env["account.move"].create({
            "move_type": "in_invoice",
            "partner_id": self.supplier.id,
            "journal_id": journal.id,
            "invoice_date": "2026-03-10",
            "invoice_line_ids": [
                (0, 0, {"product_id": self.product_cafe.id, "quantity": 10, "price_unit": 12000.0,
                        "purchase_line_id": order.order_line[0].id,
                        "tax_ids": [(6, 0, self.tax_19.ids)]}),
                (0, 0, {"name": "Flete", "quantity": 1, "price_unit": 5000000.0, "tax_ids": []}),
            ],
        })
        bill.action_post()
        pending = validation_rules._po_pending_amount(order)
        # 1.547.000 de la OC menos 10 x 12.000 x 1,19 = 142.800.
        self.assertAlmostEqual(pending, 1547000.0 - 142800.0, places=2)

    def test_bill_cufe_is_normalized(self):
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        request.action_create_bill()
        bill = request.move_id
        upper = bill.cufe.upper()
        bill.cufe = upper
        self.assertEqual(bill.cufe, upper.lower())
        journal = bill.journal_id
        with self.assertRaises(ValidationError):
            self.env["account.move"].create({
                "move_type": "in_invoice", "partner_id": self.supplier.id,
                "journal_id": journal.id, "cufe": upper,
            })

    def test_malformed_xml_cufe_is_a_finding_not_a_crash(self):
        order = self._standard_po()
        xml = read_fixture("invoice_direct.xml").replace(
            b'schemeName="CUFE-SHA384">5b7ed', b'schemeName="CUFE-SHA384">zz7ed')
        request = self._create_request(order, xml_bytes=xml)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertFalse(request.cufe)
        self.assertIn("CUFE_FORMAT", self._codes(request, "error"))

    def test_manual_reject_blocks_revalidation_and_invoiced(self):
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        request.action_reject(reason="No corresponde al contrato.")
        self.assertEqual(request.state, "rejected")
        with self.assertRaises(UserError):
            request.action_validate()
        request.action_reset_to_draft()
        self.assertFalse(request.rejection_reason)
        request.action_validate()
        self.assertEqual(request.state, "approved")
        request.action_create_bill()
        with self.assertRaises(UserError):
            request.action_reject(reason="Tarde.")
        self.assertEqual(request.state, "invoiced")

    def test_cufe_duplicate_between_requests(self):
        order = self._standard_po()
        first = self._create_request(order)
        first.action_validate()
        self.assertEqual(first.state, "approved")

        second = self._create_request(order)
        second.action_validate()
        self.assertEqual(second.state, "rejected")
        self.assertIn("CUFE_DUPLICATE", self._codes(second, "error"))

    def test_cufe_duplicate_against_bill(self):
        order = self._standard_po()
        first = self._create_request(order)
        first.action_validate()
        first.action_create_bill()

        order2 = self._standard_po()
        # Rechazar la primera solicitud no libera el CUFE: la factura ya existe.
        second = self._create_request(order2)
        second.action_validate()
        self.assertIn("CUFE_DUPLICATE", self._codes(second, "error"))

    def test_cufe_mismatch_with_xml(self):
        order = self._standard_po()
        request = self._create_request(order, cufe="a" * 96)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertIn("CUFE_MISMATCH", self._codes(request, "error"))

    def test_currency_unsupported(self):
        order = self._standard_po()
        xml_usd = read_fixture("invoice_direct.xml").replace(b"COP", b"USD")
        request = self._create_request(order, xml_bytes=xml_usd)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertIn("CURRENCY_UNSUPPORTED", self._codes(request, "error"))

    def test_po_not_confirmed(self):
        order = self._standard_po()
        order.button_cancel()
        request = self._create_request(order)
        request.action_validate()
        self.assertIn("PO_STATE", self._codes(request, "error"))

    def test_po_with_bill_rejects_a_second_invoice(self):
        order = self._standard_po()
        first = self._create_request(order)
        first.action_validate()
        first.action_create_bill()
        # La factura propia no le cuenta a la misma solicitud.
        self.assertNotIn("PO_STATE", self._codes(first, "error"))

        second = self._create_request(order)
        second.action_validate()
        self.assertEqual(second.state, "rejected")
        self.assertIn("PO_STATE", self._codes(second, "error"))

        # Anulada la factura, la orden vuelve a aceptar facturas.
        first.move_id.button_cancel()
        self.assertFalse(order._spr_vendor_bills())

    def test_second_invoice_on_an_order_with_an_open_request(self):
        order = self._standard_po()
        first = self._create_request(order)
        first.action_validate()
        self.assertEqual(first.state, "approved")
        # Otra factura (otro CUFE) sobre la misma orden mientras la primera sigue en curso.
        second = self._create_request(order, xml_bytes=read_fixture("invoice_direct.xml").replace(
            b"5b7ed", b"6b7ed"))
        second.action_validate()
        self.assertEqual(second.state, "rejected")
        self.assertIn("PO_STATE", self._codes(second, "error"))
        # La primera no se rechaza por la segunda al revalidarla.
        first.action_validate()
        self.assertNotIn("PO_STATE", self._codes(first, "error"))

    def test_no_po_is_error(self):
        request = self._create_request(None)
        request.action_validate()
        self.assertEqual(request.state, "rejected")
        self.assertIn("PO_STATE", self._codes(request, "error"))

    def test_pdf_required(self):
        order = self._standard_po()
        request = self._create_request(order, pdf_file=False)
        with self.assertRaises(UserError):
            request.action_validate()

    # ------------------------------------------------------------------
    # Emparejamiento manual
    # ------------------------------------------------------------------

    def test_unmatched_line_then_manual_match_survives_revalidation(self):
        # Producto sin ningun codigo que coincida con EMP-500.
        other = self.env["product.product"].create({
            "name": "Bolsa generica",
            "type": "consu",
            "purchase_method": "purchase",
            "supplier_taxes_id": [(6, 0, self.tax_19.ids)],
        })
        order = self._create_po([
            (self.product_cafe, 100, 12000.0),
            (other, 50, 2000.0),
        ])
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "warning")
        self.assertIn("LINES_MATCHED", self._codes(request, "warning"))

        lines = request.line_ids.sorted("sequence")
        self.assertFalse(lines[1].po_line_id)
        with self.assertRaises(UserError):
            request.action_create_bill()

        # El validador empareja a mano desde el formulario.
        with Form(request) as form:
            with form.line_ids.edit(1) as line_form:
                line_form.po_line_id = order.order_line[1]
        lines = request.line_ids.sorted("sequence")
        self.assertEqual(lines[1].match_method, "manual")

        # El validador arrastra la linea emparejada al principio (el handle
        # reescribe las secuencias) y revalida: el emparejamiento manual sigue
        # en la linea correcta, no en la que ocupa su posicion.
        cafe_desc, bag_desc = lines[0].description, lines[1].description
        lines[1].sequence = 0
        request.action_validate()
        by_desc = {line.description: line for line in request.line_ids}
        self.assertEqual(by_desc[bag_desc].po_line_id, order.order_line[1])
        self.assertEqual(by_desc[bag_desc].match_method, "manual")
        self.assertEqual(by_desc[cafe_desc].match_method, "code")
        self.assertEqual(request.state, "approved", request.validation_summary)

        request.action_create_bill()
        self.assertEqual(len(request.move_id.invoice_line_ids), 2)

    # ------------------------------------------------------------------
    # Rechazo
    # ------------------------------------------------------------------

    def test_reject_wizard_requires_reason(self):
        order = self._standard_po()
        request = self._create_request(order)
        request.action_validate()
        with self.assertRaises(UserError):
            request.action_reject()
        wizard = self.env["supplier.payment.request.reject"].create({
            "request_id": request.id,
            "reason": "Factura duplicada en fisico.",
        })
        wizard.action_confirm()
        self.assertEqual(request.state, "rejected")
        self.assertEqual(request.rejection_reason, "Factura duplicada en fisico.")
        with self.assertRaises(UserError):
            request.action_create_bill()
