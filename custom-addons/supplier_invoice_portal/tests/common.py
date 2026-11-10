# -*- coding: utf-8 -*-
"""Preparacion comun de los tests con ORM."""

import base64
import json
import os

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
DUMMY_PDF = base64.b64encode(b"%PDF-1.4\n%dummy\n%%EOF\n")


def read_fixture(filename):
    with open(os.path.join(FIXTURES, filename), "rb") as handle:
        return handle.read()


class SprCase(AccountTestInvoicingCommon):
    """Compania en COP, proveedor con NIT, impuesto 19% y productos con codigo."""

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.company = cls.company_data["company"]
        cls.env.user.company_ids |= cls.company
        cls.env.user.company_id = cls.company
        cls.env.user.groups_id |= cls.env.ref("supplier_invoice_portal.group_spr_manager")
        cls.validator = cls.env["res.users"].create({
            "name": "Validadora",
            "login": "spr_validator",
            "email": "validadora@example.com",
            "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("supplier_invoice_portal.group_spr_validator").id,
            ])],
        })

        # Los fixtures vienen en COP; la compania de pruebas debe trabajar en COP.
        cop = cls.env.ref("base.COP")
        cop.active = True
        cls.company.currency_id = cop
        cls.env["ir.config_parameter"].sudo().set_param("spr.company_nit", "901234567-8")
        # Ningun test hace red: la consulta al catalogo DIAN se apaga aqui y
        # se prueba aparte con respuestas simuladas.
        cls.env["ir.config_parameter"].sudo().set_param("spr.dian_cufe_check", "False")
        # El cierre de fin de mes depende del reloj: apagado para que la suite no
        # falle el ultimo dia habil por la tarde. Se prueba aparte.
        cls.env["ir.config_parameter"].sudo().set_param("spr.cutoff_enabled", "False")

        # NIT con digito de verificacion: el XML trae 900123456 sin DV.
        cls.supplier = cls.env["res.partner"].create({
            "name": "CAFES DEL SUR SAS",
            "is_company": True,
            "vat": "900123456-7",
            "portal_invoice_enabled": True,
            "company_id": False,
        })

        cls.tax_19 = cls.env["account.tax"].create({
            "name": "IVA 19% compras",
            "amount": 19.0,
            "amount_type": "percent",
            "type_tax_use": "purchase",
            "company_id": cls.company.id,
        })

        # CAFE-001 se empareja por referencia interna.
        cls.product_cafe = cls.env["product.product"].create({
            "name": "Cafe verde excelso",
            "default_code": "CAFE-001",
            "type": "consu",
            "purchase_method": "purchase",
            "supplier_taxes_id": [(6, 0, cls.tax_19.ids)],
        })
        # El empaque NO tiene la referencia del proveedor como default_code: se
        # empareja por el codigo del catalogo del proveedor (supplierinfo).
        cls.product_bag = cls.env["product.product"].create({
            "name": "Empaque 500 g",
            "default_code": "INT-EMP-500",
            "type": "consu",
            "purchase_method": "purchase",
            "supplier_taxes_id": [(6, 0, cls.tax_19.ids)],
            "seller_ids": [(0, 0, {
                "partner_id": cls.supplier.id,
                "product_code": "EMP-500",
                "price": 2000.0,
            })],
        })
        cls.product_cafe2 = cls.env["product.product"].create({
            "name": "Cafe verde supremo",
            "default_code": "CAFE-002",
            "type": "consu",
            "purchase_method": "purchase",
            "supplier_taxes_id": [(6, 0, cls.tax_19.ids)],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_po(self, lines, confirm=True):
        order = self.env["purchase.order"].create({
            "partner_id": self.supplier.id,
            "company_id": self.company.id,
            "currency_id": self.company.currency_id.id,
            "order_line": [(0, 0, {
                "product_id": product.id,
                "name": product.name,
                "product_qty": qty,
                "product_uom": product.uom_po_id.id,
                "price_unit": price,
                "taxes_id": [(6, 0, self.tax_19.ids)],
                "date_planned": "2026-03-01",
            }) for product, qty, price in lines],
        })
        if confirm:
            order.button_confirm()
        return order

    def _create_request(self, order, xml_name="invoice_direct.xml", xml_bytes=None, **extra):
        """``order`` puede ser una orden, varias (recordset) o None."""
        values = {
            "partner_id": self.supplier.id,
            "purchase_ids": [(6, 0, order.ids if order else [])],
            "company_id": self.company.id,
            "currency_id": self.company.currency_id.id,
            "pdf_file": DUMMY_PDF,
            "pdf_filename": "factura.pdf",
            "xml_file": base64.b64encode(xml_bytes or read_fixture(xml_name)),
            "xml_filename": xml_name,
        }
        values.update(extra)
        return self.env["supplier.payment.request"].create(values)

    def _codes(self, request, level=None):
        findings = json.loads(request.validation_json)["findings"]
        return {f["code"] for f in findings if level is None or f["level"] == level}

    def _standard_po(self):
        return self._create_po([
            (self.product_cafe, 100, 12000.0),
            (self.product_bag, 50, 2000.0),
        ])
