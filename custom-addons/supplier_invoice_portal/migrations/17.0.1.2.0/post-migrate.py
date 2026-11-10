# -*- coding: utf-8 -*-
"""17.0.1.2.0: los documentos del proveedor pasan a ser adjuntos del contacto.

Antes eran campos binarios (``spr_doc_*``) cuyo adjunto tenia ``res_field`` y
no se veia en el chatter. Ahora cada documento apunta a un adjunto normal del
contacto (``spr_doc_*_id``, DECISIONS.md #48). Se reusa el mismo adjunto: se le
quita ``res_field``, toma el nombre de archivo guardado y queda enlazado.

Corre antes de que Odoo borre las columnas de los campos viejos (eso pasa al
final de la actualizacion), por eso todavia se leen los nombres de archivo.
"""

import logging

_logger = logging.getLogger(__name__)

# (campo viejo, nombre si no se guardo el del archivo)
DOCUMENTS = (
    ("spr_doc_chamber", "Camara de comercio.pdf"),
    ("spr_doc_rut", "RUT.pdf"),
    ("spr_doc_bank_cert", "Certificacion bancaria.pdf"),
)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return
    moved = 0
    for key, default_name in DOCUMENTS:
        filename_column = key + "_filename"
        has_filename = _column_exists(cr, "res_partner", filename_column)
        cr.execute(
            "SELECT id, res_id FROM ir_attachment WHERE res_model = 'res.partner' AND res_field = %s",
            (key,),
        )
        for attachment_id, partner_id in cr.fetchall():
            filename = None
            if has_filename:
                cr.execute(
                    'SELECT "%s" FROM res_partner WHERE id = %%s' % filename_column, (partner_id,)
                )
                row = cr.fetchone()
                filename = row and row[0]
            filename = filename or default_name
            cr.execute(
                "UPDATE ir_attachment SET res_field = NULL, name = %s WHERE id = %s",
                (filename, attachment_id),
            )
            cr.execute(
                'UPDATE res_partner SET "%s_id" = %%s WHERE id = %%s' % key,
                (attachment_id, partner_id),
            )
            moved += 1
    _logger.info("[SPR] 17.0.1.2.0: %s documentos de proveedor pasados a adjuntos del contacto", moved)
