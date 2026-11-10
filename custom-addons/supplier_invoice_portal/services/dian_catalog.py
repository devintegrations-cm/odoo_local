# -*- coding: utf-8 -*-
"""Consulta del CUFE en el catalogo publico de la DIAN.

El catalogo (https://catalogo-vpfe.dian.gov.co) no tiene API: responde HTML.
La verificacion es heuristica y NUNCA tumba el flujo: ante cualquier duda
devuelve ``error`` y la regla ``DIAN_CUFE_CHECK`` lo trata como no bloqueante.

Comportamiento observado el 2026-09-09 con un CUFE inexistente:

- Sin cabeceras de navegador el sitio responde 403 "Solicitud bloqueada por
  controles de seguridad".
- Con cabeceras de navegador responde 200 con el formulario de busqueda
  ("Por favor diligencia los siguientes datos: CUFE o UUID ...").

Para un CUFE valido el catalogo muestra la ficha del documento con el CUFE en
el cuerpo. Ese caso no se pudo verificar en desarrollo (no habia un CUFE real a
mano), por eso el resultado positivo exige las dos senales a la vez.
"""

import logging
import re

import requests

_logger = logging.getLogger(__name__)

DEFAULT_CATALOG_URL = "https://catalogo-vpfe.dian.gov.co"
SEARCH_PATH = "/document/searchqr"
DEFAULT_TIMEOUT = 15

# Texto que solo aparece cuando el catalogo devuelve el formulario vacio.
SEARCH_FORM_MARKERS = (
    "por favor diligencia los siguientes datos",
    "cufe o uuid",
)
NOT_FOUND_MARKERS = (
    "documento no encontrado",
    "no se encontr",
    "no existe",
)
# Desde 2026 el buscador exige un token de captcha: la consulta automatica ya
# no resuelve el documento y devuelve el formulario. Eso NO significa que el
# CUFE no exista.
CAPTCHA_MARKERS = ("captcha",)
MANUAL_SEARCH_PATH = "/User/SearchDocument"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-CO,es;q=0.9",
}

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


def manual_search_url(base_url):
    """Pagina del buscador para que contabilidad verifique a mano."""
    base = (base_url or DEFAULT_CATALOG_URL).rstrip("/")
    if base.endswith(SEARCH_PATH):
        base = base[: -len(SEARCH_PATH)]
    return base + MANUAL_SEARCH_PATH


def catalog_search_url(base_url, cufe):
    base = (base_url or DEFAULT_CATALOG_URL).rstrip("/")
    if base.endswith(SEARCH_PATH):
        return "%s?documentkey=%s" % (base, cufe)
    return "%s%s?documentkey=%s" % (base, SEARCH_PATH, cufe)


def _visible_text(html):
    text = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def interpret_response(status_code, html, cufe):
    """Traduce la respuesta HTML del catalogo a ok / not_found / error.

    Funcion pura para poder probarla sin red.
    """
    if status_code != 200:
        return STATUS_ERROR, "El catalogo DIAN respondio HTTP %s." % status_code
    text = _visible_text(html or "")
    cufe = (cufe or "").lower()
    if not text:
        return STATUS_ERROR, "El catalogo DIAN devolvio una pagina vacia."
    if any(marker in text for marker in NOT_FOUND_MARKERS):
        return STATUS_NOT_FOUND, "El catalogo DIAN informa que el documento no existe."
    shows_form = any(marker in text for marker in SEARCH_FORM_MARKERS)
    has_cufe = cufe in text
    if has_cufe and not shows_form:
        return STATUS_OK, "El CUFE aparece en el catalogo DIAN."
    if shows_form or any(marker in text for marker in CAPTCHA_MARKERS):
        return STATUS_ERROR, ("El catalogo DIAN devolvio el buscador (exige captcha); "
                              "no se pudo verificar automaticamente.")
    return STATUS_ERROR, "Respuesta del catalogo DIAN no reconocida; se omite la verificacion."


def check_cufe(cufe, base_url=None, timeout=DEFAULT_TIMEOUT):
    """Consulta el CUFE. Devuelve ``(status, mensaje)``. Nunca lanza."""
    if not cufe:
        return STATUS_SKIPPED, "Sin CUFE que consultar."
    url = catalog_search_url(base_url, cufe)
    try:
        response = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException as error:
        _logger.warning("[SPR] Catalogo DIAN no disponible: %s", error.__class__.__name__)
        return STATUS_ERROR, "No se pudo conectar con el catalogo DIAN (%s)." % (
            error.__class__.__name__
        )
    status, message = interpret_response(response.status_code, response.text, cufe)
    _logger.info("[SPR] Catalogo DIAN para %s...: %s", cufe[:12], status)
    return status, message
