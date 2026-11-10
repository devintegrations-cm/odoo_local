# -*- coding: utf-8 -*-
"""Validacion de los PDF que se adjuntan (DECISIONS.md #47).

Funciones puras, sin ORM. Un PDF aceptable es liviano y sin contrasena: con
contrasena (o cifrado con restricciones) contabilidad no lo puede abrir ni
previsualizar, y los pesados no tienen razon de ser para un RUT o una factura.
"""

import io
import re

# Los documentos reales de staging pesan entre 90 y 520 KB: 2 MB sobra.
MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_PDF_MB = MAX_PDF_BYTES // (1024 * 1024)

_ENCRYPT_RE = re.compile(rb"/Encrypt\b")


def is_pdf(content):
    return bool(content) and content.lstrip()[:5].startswith(b"%PDF")


def is_encrypted(content):
    """True si el PDF tiene contrasena o esta cifrado.

    Con pypdf (Odoo.sh lo trae) se lee el documento; sin el, o si pypdf no
    puede leerlo, se busca el diccionario /Encrypt que todo PDF cifrado declara
    en su trailer.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            return bool(PdfReader(io.BytesIO(content)).is_encrypted)
        except Exception:  # noqa: BLE001 - PDF que pypdf no entiende: se revisan los bytes
            pass
    return bool(_ENCRYPT_RE.search(content))


def pdf_problem(content):
    """None si ``content`` es un PDF liviano y sin contrasena; si no, el motivo."""
    if not content:
        return "el archivo esta vacio"
    if not is_pdf(content):
        return "solo se aceptan archivos PDF"
    if len(content) > MAX_PDF_BYTES:
        return "el PDF supera %s MB; reduzca su tamano (por ejemplo, escaneando en menor resolucion)" % MAX_PDF_MB
    if is_encrypted(content):
        return "el PDF tiene contrasena o esta protegido; adjunte una version sin clave"
    return None
