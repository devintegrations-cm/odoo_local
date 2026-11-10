# -*- coding: utf-8 -*-
"""17.0.1.1.0: una solicitud puede tener varias ordenes de compra.

``purchase_id`` (Many2one) pasa a ``purchase_ids`` (Many2many). Odoo no borra
la columna vieja, asi que aqui se copia a la tabla de relacion.
"""


def migrate(cr, version):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'supplier_payment_request' AND column_name = 'purchase_id'
        """
    )
    if not cr.fetchone():
        return
    cr.execute(
        """
        INSERT INTO supplier_payment_request_purchase_rel (request_id, purchase_id)
        SELECT id, purchase_id FROM supplier_payment_request
         WHERE purchase_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
