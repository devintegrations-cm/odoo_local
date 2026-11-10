# -*- coding: utf-8 -*-
"""Tests de "Edit information" (/my/account) del proveedor (DECISIONS.md #44).

Documentos (camara de comercio, RUT, certificacion bancaria) y campos de
facturacion electronica de Jorels. La base de pruebas no tiene Jorels: se
comprueba que sus campos no aparecen ni rompen nada, y el camino generico
(select, select multiple, texto) se prueba cambiando la lista de campos por
campos estandar de res.partner.
"""

import base64
import re
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..controllers import portal_account
from .common import SprCase

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
PDF = b"%PDF-1.4\n%dummy\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
# Un PDF cifrado declara /Encrypt en el trailer (DECISIONS.md #47).
ENCRYPTED_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<</Root 1 0 R/Encrypt 2 0 R>>\n%%EOF\n"


@tagged("post_install", "-at_install")
class TestPortalAccount(SprCase, HttpCase):

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.country = cls.env.ref("base.co")
        # NIT con DV valido: con base_vat (Jorels lo trae) y pais Colombia se
        # verifica el digito, y el de common.py no lo es.
        cls.supplier.vat = "900123456-8"
        cls.contact = cls.env["res.partner"].create({
            "name": "Ana Contadora",
            "parent_id": cls.supplier.id,
            "email": "ana@cafesdelsur.example",
            "phone": "3001234567",
            "street": "Calle 1 # 2-3",
            "city": "Pitalito",
            "country_id": cls.country.id,
            "company_id": False,
        })
        cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Ana Contadora",
            "login": "ana_account",
            "password": "ana_account_pw_2026",
            "partner_id": cls.contact.id,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "groups_id": [(6, 0, cls.env.ref("base.group_portal").ids)],
        })
        cls.portal_user = cls.contact.user_ids

        # Proveedor no habilitado en el portal de facturas, con su usuario.
        cls.other_supplier = cls.env["res.partner"].create({
            "name": "OTRO PROVEEDOR SAS",
            "is_company": True,
            "vat": "800111222-7",
            "phone": "6011234567",
            "email": "otro@example.com",
            "street": "Carrera 9",
            "city": "Bogota",
            "country_id": cls.country.id,
            "company_id": False,
        })
        cls.env["res.users"].with_context(no_reset_password=True).create({
            "name": "Otro Portal",
            "login": "otro_account",
            "password": "otro_account_pw_2026",
            "partner_id": cls.other_supplier.id,
            "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "groups_id": [(6, 0, cls.env.ref("base.group_portal").ids)],
        })

    # ------------------------------------------------------------------

    def _form(self, partner, **extra):
        data = {
            "name": partner.name,
            "email": partner.email,
            "phone": partner.phone,
            "street": partner.street,
            "city": partner.city,
            "country_id": str(partner.country_id.id),
        }
        data.update(extra)
        return data

    def _post(self, data, files=None):
        page = self.url_open("/my/account")
        self.assertEqual(page.status_code, 200)
        match = CSRF_RE.search(page.text)
        self.assertTrue(match, "El formulario no trae csrf_token")
        data = dict(data, csrf_token=match.group(1))
        return self.url_open("/my/account", data=data, files=files or [])

    def _doc_notes(self, partner):
        return partner.message_ids.filtered(lambda m: "desde el portal" in (m.body or ""))

    # ------------------------------------------------------------------

    def _skip_unless_jorels(self, installed):
        if ("type_regime_id" in self.env["res.partner"]._fields) != installed:
            self.skipTest("l10n_co_edi_jorels %s instalado" % ("no esta" if installed else "esta"))

    def test_form_shows_documents_without_jorels(self):
        self._skip_unless_jorels(False)
        self.authenticate("ana_account", "ana_account_pw_2026")
        page = self.url_open("/my/account")
        self.assertEqual(page.status_code, 200)
        self.assertIn('enctype="multipart/form-data"', page.text)
        for name, _label in portal_account.SPR_DOCUMENTS:
            self.assertIn('name="%s"' % name, page.text)
        self.assertIn("Sin documento cargado", page.text)
        self.assertNotIn("Facturacion electronica", page.text)
        for name, _label in portal_account.JORELS_FIELDS:
            self.assertNotIn('name="%s"' % name, page.text)

    def test_submit_without_documents(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        # Asi manda el navegador un input de archivo sin elegir: nombre vacio.
        files = [(name, ("", b"", "application/octet-stream"))
                 for name, _label in portal_account.SPR_DOCUMENTS]
        response = self._post(self._form(self.contact, phone="3109998877"), files)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.url.endswith("/my/home"), response.url)
        self.assertEqual(self.contact.phone, "3109998877")
        self.assertFalse(self.supplier.spr_doc_rut_id)
        self.assertFalse(self._doc_notes(self.supplier))

    def test_submit_with_documents_goes_to_commercial_partner(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        files = [
            ("spr_doc_chamber", ("camara.pdf", PDF, "application/pdf")),
            ("spr_doc_rut", ("rut.pdf", PDF, "application/pdf")),
            ("spr_doc_bank_cert", ("certificado.pdf", PDF, "application/pdf")),
        ]
        response = self._post(self._form(self.contact), files)
        self.assertTrue(response.url.endswith("/my/home"), response.url)

        self.assertEqual(self.supplier.spr_doc_chamber_id.raw, PDF)
        self.assertEqual(self.supplier.spr_doc_chamber_id.name, "camara.pdf")
        self.assertEqual(self.supplier.spr_doc_rut_id.raw, PDF)
        self.assertEqual(self.supplier.spr_doc_rut_id.name, "rut.pdf")
        self.assertEqual(self.supplier.spr_doc_bank_cert_id.name, "certificado.pdf")
        # Quedan como adjuntos de la empresa, en el chatter (DECISIONS.md #48).
        attachments = self.env["ir.attachment"].search(
            [("res_model", "=", "res.partner"), ("res_id", "=", self.supplier.id)])
        self.assertIn(self.supplier.spr_doc_rut_id, attachments)
        # Nada en el contacto hijo: los documentos son de la empresa.
        self.assertFalse(self.contact.spr_doc_rut_id)

        notes = self._doc_notes(self.supplier)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.author_id, self.contact)
        self.assertIn("RUT (rut.pdf)", notes.body)
        self.assertIn("Camara de comercio (camara.pdf)", notes.body)
        self.assertEqual(len(notes.attachment_ids), 3, "Los documentos van en la nota del chatter")

        # El portal muestra el nombre del archivo cargado.
        page = self.url_open("/my/account")
        self.assertIn("Cargado: <span>rut.pdf</span>", page.text)
        # Vista previa: el PDF se sirve en linea, solo el del propio proveedor.
        self.assertIn('href="/my/account/document/spr_doc_rut"', page.text)
        preview = self.url_open("/my/account/document/spr_doc_rut")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["Content-Type"], "application/pdf")
        self.assertTrue(preview.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(preview.content, PDF)
        self.assertEqual(self.url_open("/my/account/document/name").status_code, 404)

    def test_no_upload_keeps_existing_document(self):
        self.supplier._spr_attach_document("spr_doc_rut", "rut_viejo.pdf", PDF)
        self.authenticate("ana_account", "ana_account_pw_2026")
        files = [("spr_doc_rut", ("", b"", "application/octet-stream")),
                 ("spr_doc_bank_cert", ("cert.pdf", PDF, "application/pdf"))]
        response = self._post(self._form(self.contact), files)
        self.assertTrue(response.url.endswith("/my/home"), response.url)
        self.assertEqual(self.supplier.spr_doc_rut_id.name, "rut_viejo.pdf")
        self.assertEqual(self.supplier.spr_doc_rut_id.raw, PDF)
        self.assertEqual(self.supplier.spr_doc_bank_cert_id.name, "cert.pdf")
        self.assertNotIn("RUT", self._doc_notes(self.supplier).body)

    def test_rejects_file_too_big(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        big = PDF + b"0" * 2048
        with patch.object(portal_account, "MAX_DOC_BYTES", 1024):
            response = self._post(
                self._form(self.contact, phone="3000000000"),
                [("spr_doc_rut", ("rut.pdf", big, "application/pdf"))],
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("RUT: el PDF supera", response.text)
        # Con errores no se guarda nada, ni el documento ni el resto.
        self.assertFalse(self.supplier.spr_doc_rut_id)
        self.assertEqual(self.contact.phone, "3001234567")

    def test_rejects_file_type(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        response = self._post(
            self._form(self.contact),
            [("spr_doc_chamber", ("camara.pdf", b"MZ\x90\x00 no es pdf", "application/pdf"))],
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Camara de comercio: solo se aceptan archivos PDF", response.text)
        self.assertFalse(self.supplier.spr_doc_chamber_id)

    def test_rejects_images_and_password_protected_pdf(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        response = self._post(self._form(self.contact), [
            ("spr_doc_rut", ("rut.png", PNG, "image/png")),
            ("spr_doc_bank_cert", ("cert.pdf", ENCRYPTED_PDF, "application/pdf")),
        ])
        self.assertIn("RUT: solo se aceptan archivos PDF", response.text)
        self.assertIn("tiene contrasena o esta protegido", response.text)
        self.assertFalse(self.supplier.spr_doc_rut_id or self.supplier.spr_doc_bank_cert_id)

    def test_backend_document_must_be_a_light_pdf_of_the_contact(self):
        with self.assertRaises(ValidationError):
            self.supplier._spr_attach_document("spr_doc_rut", "r.pdf", ENCRYPTED_PDF)
        with self.assertRaises(ValidationError):
            self.supplier._spr_attach_document("spr_doc_chamber", "c.pdf", PNG)
        # Un adjunto de otro contacto no sirve: el documento es de este (#48).
        foreign = self.env["ir.attachment"].create({
            "name": "ajeno.pdf", "raw": PDF, "res_model": "res.partner",
            "res_id": self.other_supplier.id,
        })
        with self.assertRaises(ValidationError):
            self.supplier.spr_doc_chamber_id = foreign
        # Un PDF adjunto en el chatter del contacto se puede elegir.
        own = self.env["ir.attachment"].create({
            "name": "camara.pdf", "raw": PDF, "res_model": "res.partner",
            "res_id": self.supplier.id,
        })
        self.supplier.spr_doc_chamber_id = own
        self.assertEqual(self.supplier.spr_doc_chamber_preview, own.datas)

    def test_preview_needs_an_enabled_supplier_and_a_document(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        self.assertEqual(self.url_open("/my/account/document/spr_doc_rut").status_code, 404)
        self.supplier._spr_attach_document("spr_doc_rut", "r.pdf", PDF)
        self.assertEqual(self.url_open("/my/account/document/spr_doc_rut").status_code, 200)
        self.supplier.portal_invoice_enabled = False
        self.assertEqual(self.url_open("/my/account/document/spr_doc_rut").status_code, 404)

    def test_jorels_field_is_unknown_without_jorels(self):
        self._skip_unless_jorels(False)
        self.authenticate("ana_account", "ana_account_pw_2026")
        response = self._post(self._form(self.contact, type_regime_id="1",
                                         phone="3000000000"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unknown field", response.text)
        self.assertEqual(self.contact.phone, "3001234567")

    def test_billing_fields_generic_path(self):
        """Sin Jorels, el mismo codigo se prueba con campos estandar de
        res.partner: many2one (title), many2many (category_id) y texto (ref)."""
        title = self.env["res.partner.title"].create({"name": "Doctora SPR"})
        tag_a = self.env["res.partner.category"].create({"name": "SPR A"})
        tag_b = self.env["res.partner.category"].create({"name": "SPR B"})
        fields_list = [("title", "Titulo"), ("category_id", "Etiquetas"), ("ref", "Referencia")]
        self.authenticate("ana_account", "ana_account_pw_2026")
        with patch.object(portal_account, "JORELS_FIELDS", fields_list):
            page = self.url_open("/my/account")
            self.assertIn("Facturacion electronica", page.text)
            self.assertIn("Doctora SPR", page.text)
            self.assertIn('multiple="multiple"', page.text)
            self.assertIn('name="ref"', page.text)

            # Un id que no existe se rechaza y no se escribe nada.
            response = self._post(self._form(self.contact, title="999999", ref="X"))
            self.assertIn("Titulo: elija una opcion de la lista.", response.text)
            self.assertFalse(self.supplier.title)

            response = self._post(self._form(
                self.contact, title=str(title.id),
                category_id=["", str(tag_a.id), str(tag_b.id)], ref=" REF-1 ",
            ))
            self.assertTrue(response.url.endswith("/my/home"), response.url)
        # Se escribe en el comercial, no en el contacto del usuario.
        self.assertEqual(self.supplier.title, title)
        self.assertEqual(self.supplier.category_id, tag_a | tag_b)
        self.assertEqual(self.supplier.ref, "REF-1")
        self.assertFalse(self.contact.title)
        self.assertIn("Titulo", self._doc_notes(self.supplier).body)

    def test_jorels_fields_when_installed(self):
        """Solo corre en una base con l10n_co_edi_jorels."""
        self._skip_unless_jorels(True)
        regime = self.env["l10n_co_edi_jorels.type_regimes"].search([], limit=1)
        liability = self.env["l10n_co_edi_jorels.type_liabilities"].search([], limit=1)
        municipality = self.env["l10n_co_edi_jorels.municipalities"].search([], limit=1)
        self.assertTrue(regime and liability and municipality, "Faltan las listas de Jorels")
        self.authenticate("ana_account", "ana_account_pw_2026")
        page = self.url_open("/my/account")
        self.assertIn("Facturacion electronica", page.text)
        for name, _label in portal_account.JORELS_FIELDS:
            self.assertIn('name="%s"' % name, page.text)
        self.assertIn(municipality.display_name, page.text)

        response = self._post(self._form(self.contact, email_edi="no-es-correo"))
        self.assertIn("Email de facturacion: el correo no es valido.", response.text)

        liability_ids = [str(liability.id)]
        if self.env["res.partner"]._fields["type_liability_id"].type == "many2many":
            liability_ids = [""] + liability_ids
        response = self._post(self._form(
            self.contact, type_regime_id=str(regime.id), type_liability_id=liability_ids,
            municipality_id=str(municipality.id), email_edi="fe@cafesdelsur.example",
        ))
        self.assertTrue(response.url.endswith("/my/home"), response.url)
        self.assertEqual(self.supplier.type_regime_id, regime)
        self.assertEqual(self.supplier.type_liability_id, liability)
        self.assertEqual(self.supplier.municipality_id, municipality)
        self.assertEqual(self.supplier.email_edi, "fe@cafesdelsur.example")

    def test_cannot_write_other_partner(self):
        self.authenticate("ana_account", "ana_account_pw_2026")
        # Un id de contacto en el formulario no se acepta.
        response = self._post(
            self._form(self.contact, partner_id=str(self.other_supplier.id)),
            [("spr_doc_rut", ("rut.pdf", PDF, "application/pdf"))],
        )
        self.assertIn("Unknown field", response.text)
        self.assertFalse(self.other_supplier.spr_doc_rut_id)
        self.assertFalse(self.supplier.spr_doc_rut_id)

        # Por ORM el usuario portal no escribe ningun contacto ni lee los documentos.
        portal_env = self.env(user=self.portal_user)
        with self.assertRaises(AccessError):
            self.other_supplier.with_env(portal_env).write({"spr_doc_rut_id": False})
        with self.assertRaises(AccessError):
            self.supplier.with_env(portal_env).read(["spr_doc_rut_id"])

    def test_not_enabled_supplier_has_standard_form(self):
        self.authenticate("otro_account", "otro_account_pw_2026")
        page = self.url_open("/my/account")
        self.assertNotIn('name="spr_doc_rut"', page.text)
        response = self._post(self._form(self.other_supplier),
                              [("spr_doc_rut", ("rut.pdf", PDF, "application/pdf"))])
        self.assertIn("Unknown field", response.text)
        self.assertFalse(self.other_supplier.spr_doc_rut_id)

    def test_child_still_cannot_change_vat(self):
        """El bloqueo estandar de NIT para contactos hijos sigue igual."""
        self.authenticate("ana_account", "ana_account_pw_2026")
        response = self._post(self._form(self.contact, vat="999999999"),
                              [("spr_doc_rut", ("rut.pdf", PDF, "application/pdf"))])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.url.endswith("/my/home"))
        self.assertEqual(self.supplier.vat, "900123456-8")
        self.assertFalse(self.supplier.spr_doc_rut_id)
