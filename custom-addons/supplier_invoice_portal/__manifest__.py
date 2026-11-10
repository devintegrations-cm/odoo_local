# -*- coding: utf-8 -*-
{
    "name": "Portal de facturas de proveedor",
    "summary": "Los proveedores solicitan el pago de sus facturas electronicas DIAN "
               "asociandolas a una orden de compra",
    "version": "17.0.1.8.0",
    "category": "Inventory/Purchase",
    "license": "LGPL-3",
    "author": "Libertario Coffee Roasters",
    "website": "https://libertariocoffee.com",
    "depends": [
        "purchase",
        "account",
        "portal",
        "mail",
    ],
    # pypdf NO se declara aqui a proposito: la imagen odoo:17.0 no lo trae y su
    # ausencia bloquearia la instalacion. Se importa de forma diferida y opcional
    # (ver services/ocr_adapter.py) y se declara en el requirements.txt del repo.
    "external_dependencies": {
        "python": ["lxml"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "report/spr_instructions_report.xml",
        "data/mail_template_data.xml",
        "views/supplier_payment_request_views.xml",
        "views/res_config_settings_views.xml",
        "views/menus.xml",
        "wizards/spr_reject_wizard_views.xml",
        "views/portal_templates.xml",
        "views/portal_account_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "supplier_invoice_portal/static/src/views/spr_preview_binary_field.js",
            "supplier_invoice_portal/static/src/views/spr_preview_binary_field.xml",
        ],
        "web.assets_frontend": [
            "supplier_invoice_portal/static/src/css/spr_dropzone.css",
            "supplier_invoice_portal/static/src/js/spr_dropzone.js",
        ],
    },
    "demo": [
        "data/demo.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
