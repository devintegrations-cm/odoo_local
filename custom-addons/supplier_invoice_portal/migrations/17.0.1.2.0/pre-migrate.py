# -*- coding: utf-8 -*-
"""17.0.1.2.0: quita la vista vieja de documentos antes de cargar las nuevas.

``view_partner_form_spr_documents`` (17.0.1.1.0) heredaba del grupo
``spr_portal``, que ya no existe: al recargar la vista padre, Odoo valida esa
hija vieja contra el arch nuevo y la actualizacion falla antes de llegar a
borrarla por su cuenta. Se borra aqui, antes de cargar los datos.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        DELETE FROM ir_ui_view
         WHERE id IN (SELECT res_id FROM ir_model_data
                       WHERE module = 'supplier_invoice_portal'
                         AND name = 'view_partner_form_spr_documents'
                         AND model = 'ir.ui.view')
        """
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'supplier_invoice_portal'
           AND name = 'view_partner_form_spr_documents'
           AND model = 'ir.ui.view'
        """
    )
