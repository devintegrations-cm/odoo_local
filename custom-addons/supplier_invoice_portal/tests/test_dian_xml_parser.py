# -*- coding: utf-8 -*-
"""Tests del parser de XML DIAN.

El parser es codigo puro (sin ORM), asi que estos tests no tocan la base de
datos: heredan de BaseCase, no de TransactionCase.
"""

import os

from odoo.tests.common import BaseCase

from odoo.addons.supplier_invoice_portal.services import dian_xml_parser
from odoo.addons.supplier_invoice_portal.services.dian_xml_parser import DianXmlError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CUFE_DIRECT = (
    "5b7ed1473378afeccda8a02a8f84e8bc00b6926f9a48720e9bfa7d1a2bb1c924"
    "3a510d94519f045a6afa7d7358c3eda3"
)
CUFE_ATTACHED = (
    "f41d3608caa09666df5163a1e6660bda8b3d66580db498e8458d9cf3d057cfbd"
    "8fd97d38a7bda531b9542bdf06a289cc"
)
CUFE_DISCOUNTS = (
    "0a8be3449446bb0f748d11230eda8e9156affd96330afe5dc2ae3f9ffe0e97e8"
    "e83aba336d338b5ef0786e25000ce4cd"
)


def read_fixture(filename):
    with open(os.path.join(FIXTURES, filename), "rb") as handle:
        return handle.read()


class TestDianXmlParser(BaseCase):

    # ------------------------------------------------------------------
    # Invoice UBL directo
    # ------------------------------------------------------------------

    def test_invoice_direct_header(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_direct.xml"))
        self.assertEqual(parsed["cufe"], CUFE_DIRECT)
        self.assertEqual(parsed["invoice_ref"], "SETP990000001")
        self.assertEqual(parsed["issue_date"], "2026-03-15")
        self.assertEqual(parsed["currency"], "COP")
        self.assertEqual(parsed["document_type"], "invoice")
        self.assertFalse(parsed["raw_warnings"], parsed["raw_warnings"])

    def test_invoice_direct_parties(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_direct.xml"))
        self.assertEqual(parsed["supplier"]["nit"], "900123456")
        self.assertEqual(parsed["supplier"]["dv"], "1")
        self.assertEqual(parsed["supplier"]["name"], "CAFES DEL SUR SAS")
        self.assertIn("Medellin", parsed["supplier"]["address"])
        self.assertEqual(parsed["customer"]["nit"], "901234567")
        self.assertEqual(parsed["customer"]["dv"], "8")
        self.assertEqual(parsed["customer"]["name"], "LIBERTARIO COFFEE ROASTERS SAS")

    def test_invoice_direct_totals(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_direct.xml"))
        self.assertAlmostEqual(parsed["amount_untaxed"], 1300000.0, places=2)
        self.assertAlmostEqual(parsed["amount_tax"], 247000.0, places=2)
        self.assertAlmostEqual(parsed["amount_total"], 1547000.0, places=2)
        # El total del documento debe cuadrar con la suma de las lineas.
        self.assertAlmostEqual(
            sum(line["subtotal"] for line in parsed["lines"]),
            parsed["amount_untaxed"],
            places=2,
        )
        # Y los impuestos de linea con el impuesto del documento.
        self.assertAlmostEqual(
            sum(line["tax_amount"] for line in parsed["lines"]),
            parsed["amount_tax"],
            places=2,
        )

    def test_invoice_direct_lines(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_direct.xml"))
        self.assertEqual(len(parsed["lines"]), 2)
        first, second = parsed["lines"]
        self.assertEqual(first["code"], "CAFE-001")
        self.assertEqual(first["description"], "Cafe verde excelso saco 70kg")
        self.assertAlmostEqual(first["quantity"], 100.0)
        self.assertEqual(first["unit"], "KGM")
        self.assertAlmostEqual(first["price_unit"], 12000.0)
        self.assertAlmostEqual(first["tax_rate"], 19.0)
        self.assertAlmostEqual(first["subtotal"], 1200000.0)
        self.assertAlmostEqual(first["discount"], 0.0)
        self.assertEqual(second["code"], "EMP-500")
        self.assertAlmostEqual(second["subtotal"], 100000.0)

    # ------------------------------------------------------------------
    # AttachedDocument con la factura embebida
    # ------------------------------------------------------------------

    def test_attached_document(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("attached_document.xml"))
        self.assertEqual(parsed["cufe"], CUFE_ATTACHED)
        self.assertEqual(parsed["invoice_ref"], "SETP990000002")
        self.assertEqual(len(parsed["lines"]), 1)
        self.assertEqual(parsed["lines"][0]["code"], "MOLIENDA-01")
        self.assertAlmostEqual(parsed["amount_total"], 595000.0, places=2)
        self.assertAlmostEqual(parsed["amount_tax"], 95000.0, places=2)
        # El CUFE del contenedor coincide con el de la factura: sin advertencias.
        self.assertFalse(parsed["raw_warnings"], parsed["raw_warnings"])

    def test_attached_document_cufe_mismatch(self):
        """Si el contenedor declara otro CUFE que el embebido, hay que avisar."""
        raw = read_fixture("attached_document.xml").decode()
        tampered = raw.replace(
            '<cbc:UUID schemeName="CUFE-SHA384">%s</cbc:UUID>' % CUFE_ATTACHED,
            '<cbc:UUID schemeName="CUFE-SHA384">%s</cbc:UUID>' % ("0" * 96),
        )
        parsed = dian_xml_parser.parse_dian_xml(tampered)
        self.assertTrue(
            any("no coincide" in warning for warning in parsed["raw_warnings"]),
            parsed["raw_warnings"],
        )

    # ------------------------------------------------------------------
    # Factura con descuentos
    # ------------------------------------------------------------------

    def test_invoice_with_discounts(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_with_discounts.xml"))
        self.assertEqual(parsed["cufe"], CUFE_DISCOUNTS)
        line = parsed["lines"][0]
        self.assertAlmostEqual(line["discount"], 200000.0, places=2)
        self.assertAlmostEqual(line["charge"], 0.0, places=2)
        # LineExtensionAmount ya viene neto de descuento.
        self.assertAlmostEqual(line["subtotal"], 1800000.0, places=2)
        self.assertAlmostEqual(parsed["allowance_total"], 200000.0, places=2)
        self.assertAlmostEqual(parsed["amount_total"], 2142000.0, places=2)

    # ------------------------------------------------------------------
    # Comparacion del CUFE capturado
    # ------------------------------------------------------------------

    def test_check_cufe_matches(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_direct.xml"))
        ok, _message = dian_xml_parser.check_cufe_matches(parsed, CUFE_DIRECT.upper())
        self.assertTrue(ok, "El CUFE debe compararse sin importar mayusculas")
        ok, message = dian_xml_parser.check_cufe_matches(parsed, "0" * 96)
        self.assertFalse(ok)
        self.assertIn("no coincide", message)
        ok, message = dian_xml_parser.check_cufe_matches(parsed, "")
        self.assertFalse(ok)

    # ------------------------------------------------------------------
    # Errores y seguridad
    # ------------------------------------------------------------------

    def test_not_xml(self):
        with self.assertRaises(DianXmlError):
            dian_xml_parser.parse_dian_xml(b"esto no es un xml")

    def test_wrong_root(self):
        with self.assertRaises(DianXmlError):
            dian_xml_parser.parse_dian_xml(b"<html><body>hola</body></html>")

    def test_credit_note(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("credit_note.xml"))
        self.assertEqual(parsed["document_type"], "credit_note")
        self.assertEqual(parsed["invoice_ref"], "NC-000045")
        self.assertEqual(parsed["amount_total"], 142800.0)
        self.assertEqual(parsed["billing_reference"]["number"], "SETP990000001")
        self.assertTrue(dian_xml_parser.is_valid_cufe(parsed["billing_reference"]["cufe"]))
        self.assertEqual(len(parsed["lines"]), 1)
        self.assertEqual(parsed["lines"][0]["quantity"], 10.0)

    def test_debit_note_uses_requested_monetary_total(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("debit_note.xml"))
        self.assertEqual(parsed["document_type"], "debit_note")
        self.assertEqual(parsed["amount_untaxed"], 50000.0)
        self.assertEqual(parsed["amount_total"], 59500.0)
        self.assertEqual(parsed["lines"][0]["price_unit"], 500.0)

    def test_note_without_totals_is_rejected(self):
        credit_note = (
            '<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"'
            ' xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
            'CommonBasicComponents-2">'
            "<cbc:ID>NC-1</cbc:ID></CreditNote>"
        )
        with self.assertRaises(DianXmlError):
            dian_xml_parser.parse_dian_xml(credit_note)

    def test_document_level_charge_becomes_a_line(self):
        parsed = dian_xml_parser.parse_dian_xml(read_fixture("invoice_with_freight.xml"))
        descriptions = [line["description"] for line in parsed["lines"]]
        self.assertEqual(len(parsed["lines"]), 3)
        self.assertIn("Cargo adicional: Manejo y cargue", descriptions)
        self.assertEqual(parsed["charge_total"], 20000.0)

    def test_attached_document_without_invoice(self):
        empty = (
            '<AttachedDocument xmlns="urn:oasis:names:specification:ubl:schema:xsd:'
            'AttachedDocument-2"'
            ' xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
            'CommonBasicComponents-2">'
            "<cbc:ID>X</cbc:ID></AttachedDocument>"
        )
        with self.assertRaises(DianXmlError):
            dian_xml_parser.parse_dian_xml(empty)

    def test_xxe_is_not_resolved(self):
        """Un XML con entidades externas no debe leer archivos del servidor."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"'
            ' xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
            'CommonBasicComponents-2">'
            "<cbc:ID>&xxe;</cbc:ID></Invoice>"
        )
        try:
            parsed = dian_xml_parser.parse_dian_xml(payload)
        except DianXmlError:
            return  # rechazarlo tambien es un resultado correcto
        self.assertNotIn("root:", parsed.get("invoice_ref", ""))

    # ------------------------------------------------------------------
    # Helpers de NIT y CUFE
    # ------------------------------------------------------------------

    def test_normalize_nit(self):
        self.assertEqual(dian_xml_parser.normalize_nit("900.123.456-7"), "9001234567")
        self.assertEqual(dian_xml_parser.normalize_nit(""), "")
        self.assertEqual(dian_xml_parser.normalize_nit(None), "")

    def test_same_nit_ignores_dv(self):
        self.assertTrue(dian_xml_parser.same_nit("900123456", "900.123.456-7"))
        self.assertTrue(dian_xml_parser.same_nit("9001234567", "900123456"))
        self.assertFalse(dian_xml_parser.same_nit("900123456", "901234567"))
        self.assertFalse(dian_xml_parser.same_nit("", "900123456"))
        # NIT de 8 digitos con DV pegado (persona natural) y cedulas de 10.
        self.assertTrue(dian_xml_parser.same_nit("80012345-6", "80012345"))
        self.assertTrue(dian_xml_parser.same_nit("1017123456", "1017123456"))
        self.assertFalse(dian_xml_parser.same_nit("1017123456", "1017123457"))
        self.assertFalse(dian_xml_parser.same_nit("90012345678", "900123456"))

    def test_is_valid_cufe(self):
        self.assertTrue(dian_xml_parser.is_valid_cufe(CUFE_DIRECT))
        self.assertTrue(dian_xml_parser.is_valid_cufe(CUFE_DIRECT.upper()))
        self.assertFalse(dian_xml_parser.is_valid_cufe("abc"))
        self.assertFalse(dian_xml_parser.is_valid_cufe("z" * 96))
        self.assertFalse(dian_xml_parser.is_valid_cufe(None))
