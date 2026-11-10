# -*- coding: utf-8 -*-
"""17.0.1.1.0: la plantilla 'recibida' cambia de campo (purchase_id -> purchase_names).

Las plantillas estan en un archivo noupdate, y en actualizacion Odoo 17 salta
esos registros si ya existen (no basta con cambiar la marca noupdate). Sin
esto, la version vieja se quedaria en la base y fallaria al renderizar porque
purchase_id ya no existe. Se borra para que la carga de datos la cree de nuevo.
Pierde cambios hechos a mano en esa plantilla, que son menos graves que un
correo que no sale.
"""


def migrate(cr, version):
    cr.execute(
        """
        SELECT id, res_id FROM ir_model_data
         WHERE module = 'supplier_invoice_portal'
           AND name = 'mail_template_spr_received'
           AND model = 'mail.template'
        """
    )
    row = cr.fetchone()
    if not row:
        return
    data_id, template_id = row
    cr.execute("DELETE FROM mail_template WHERE id = %s", (template_id,))
    cr.execute("DELETE FROM ir_model_data WHERE id = %s", (data_id,))
