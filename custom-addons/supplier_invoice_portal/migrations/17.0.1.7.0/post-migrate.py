# -*- coding: utf-8 -*-
"""17.0.1.7.0: el PDF de la factura tambien va como adjunto del chatter
(DECISIONS.md #55).

Las solicitudes nuevas lo hacen al crearse. Aqui se agrega a las que ya
existen, sin mensaje en el chatter para no llenarlo con avisos de hoy. Si el
archivo no esta en el filestore se salta esa solicitud.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    Attachment = env["ir.attachment"]
    requests = env["supplier.payment.request"].with_context(active_test=False, bin_size=False).search(
        [("pdf_file", "!=", False), ("message_main_attachment_id", "=", False)])
    done = 0
    for request in requests:
        try:
            content = request.pdf_file
        except Exception:  # noqa: BLE001 - filestore incompleto en bases de prueba
            content = False
        if not content:
            continue
        attachment = Attachment.create({
            "name": request.pdf_filename or "%s.pdf" % request.name.replace("/", "_"),
            "datas": content,
            "mimetype": "application/pdf",
            "res_model": request._name,
            "res_id": request.id,
        })
        request.with_context(tracking_disable=True).message_main_attachment_id = attachment
        done += 1
    _logger.info("[SPR] PDF en el chatter: %d de %d solicitudes", done, len(requests))
