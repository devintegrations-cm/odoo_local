# -*- coding: utf-8 -*-
"""17.0.1.4.0: el correo del instructivo adjunta el PDF (DECISIONS.md #52).

La plantilla esta en un bloque noupdate: en bases que ya la tienen, la
actualizacion del modulo no le agrega el reporte. Se agrega aqui sin tocar
el texto, que contabilidad pudo haber editado.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    template = env.ref("supplier_invoice_portal.mail_template_spr_instructions", raise_if_not_found=False)
    report = env.ref("supplier_invoice_portal.report_spr_instructions", raise_if_not_found=False)
    if template and report and report not in template.report_template_ids:
        template.report_template_ids = [(4, report.id)]
