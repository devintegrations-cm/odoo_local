# -*- coding: utf-8 -*-
"""Adaptador de OCR para el PDF de la factura.

Solo se usa cuando el proveedor NO adjunto XML. Si hay XML, el XML manda.

El OCR es un servicio externo (n8n, FastAPI, lo que sea) detras de un contrato
HTTP documentado en el README. Cualquier fallo del servicio deja la solicitud
en modo de captura manual: nunca tumba la transaccion de Odoo.
"""

import json
import datetime
import logging

import requests

from . import dian_xml_parser

_logger = logging.getLogger(__name__)

OCR_UNAVAILABLE = "OCR no disponible, capturar manualmente."
DEFAULT_TIMEOUT = 60
CONNECT_TIMEOUT = 10  # abrir la conexion; el timeout configurado es para la respuesta


def pdf_page_count(pdf_bytes):
    """Cuenta paginas del PDF si ``pypdf`` esta disponible; si no, devuelve None.

    pypdf es opcional a proposito: la imagen odoo:17.0 no lo trae y no queremos
    que su ausencia impida instalar el modulo.
    """
    try:
        import io

        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:  # noqa: BLE001 - un PDF corrupto no debe tumbar el flujo
        _logger.warning("[SPR] No se pudo leer el PDF para contar paginas.")
        return None


# ---------------------------------------------------------------------------
# Normalizacion de la respuesta
# ---------------------------------------------------------------------------

def _to_float(value, default=0.0):
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _party(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "nit": dian_xml_parser.normalize_nit(raw.get("nit") or ""),
        "dv": str(raw.get("dv") or ""),
        "name": str(raw.get("name") or ""),
        "address": str(raw.get("address") or ""),
    }


def _line(raw, index):
    raw = raw if isinstance(raw, dict) else {}
    quantity = _to_float(raw.get("quantity"))
    price_unit = _to_float(raw.get("price_unit"), None)
    subtotal = _to_float(raw.get("subtotal"), None)
    if subtotal is None:
        subtotal = quantity * (price_unit or 0.0)
    if price_unit is None:
        price_unit = subtotal / quantity if quantity else 0.0
    return {
        "sequence": str(raw.get("sequence") or index + 1),
        "description": str(raw.get("description") or ""),
        "code": str(raw.get("code") or ""),
        "quantity": quantity,
        "unit": str(raw.get("unit") or ""),
        "price_unit": price_unit,
        "tax_rate": _to_float(raw.get("tax_rate")),
        "tax_amount": _to_float(raw.get("tax_amount")),
        "discount": _to_float(raw.get("discount")),
        "charge": _to_float(raw.get("charge")),
        "subtotal": subtotal,
    }


def normalize_ocr_response(payload, provider):
    """Convierte lo que devuelva el servicio al dict normalizado del parser.

    Es tolerante: numeros como texto, campos ausentes, listas mal formadas.
    Lo que no se entienda se descarta y se anota en ``raw_warnings``.
    """
    payload = payload if isinstance(payload, dict) else {}
    warnings = [str(w) for w in (payload.get("raw_warnings") or []) if w]
    raw_lines = payload.get("lines")
    if raw_lines is not None and not isinstance(raw_lines, list):
        warnings.append("El OCR devolvio 'lines' en un formato no reconocido.")
        raw_lines = []
    lines = [_line(line, index) for index, line in enumerate(raw_lines or [])]
    lines = [line for line in lines if line["description"] or line["code"] or line["subtotal"]]

    cufe = dian_xml_parser.normalize_cufe(payload.get("cufe") or "")
    if cufe and not dian_xml_parser.is_valid_cufe(cufe):
        warnings.append("El CUFE leido por OCR no tiene formato valido; se descarta.")
        cufe = ""

    issue_date = str(payload.get("issue_date") or "")[:10]
    if issue_date:
        try:
            datetime.date.fromisoformat(issue_date)
        except ValueError:
            warnings.append("La fecha leida por OCR (%s) no es AAAA-MM-DD; se descarta." % issue_date)
            issue_date = ""
    return {
        "document_type": "invoice",
        "cufe": cufe,
        "invoice_ref": str(payload.get("invoice_ref") or ""),
        "issue_date": issue_date,
        "supplier": _party(payload.get("supplier")),
        "customer": _party(payload.get("customer")),
        "lines": lines,
        "amount_untaxed": _to_float(payload.get("amount_untaxed")),
        "amount_tax": _to_float(payload.get("amount_tax")),
        "amount_total": _to_float(payload.get("amount_total")),
        "allowance_total": _to_float(payload.get("allowance_total")),
        "charge_total": _to_float(payload.get("charge_total")),
        "prepaid_amount": _to_float(payload.get("prepaid_amount")),
        "currency": str(payload.get("currency") or "COP").upper(),
        "raw_warnings": warnings,
        "confidence": max(0.0, min(1.0, _to_float(payload.get("confidence")))),
        "provider": str(payload.get("provider") or provider),
        "raw": payload.get("raw") if isinstance(payload.get("raw"), dict) else {},
    }


def degraded_result(provider, reason):
    """Resultado vacio en modo degradado: el validador captura a mano."""
    return {
        "provider": provider,
        "confidence": 0.0,
        "raw": {},
        "lines": [],
        "raw_warnings": ["%s (%s)" % (OCR_UNAVAILABLE, reason)],
    }


# ---------------------------------------------------------------------------
# Adaptadores
# ---------------------------------------------------------------------------

class OcrAdapter:
    """Contrato del servicio de OCR."""

    name = "base"

    def extract(self, pdf_bytes, filename, hint=None):
        """Devuelve el MISMO dict normalizado que ``parse_dian_xml``, mas:

        ``confidence`` (float 0-1), ``provider`` (str) y ``raw`` (dict).

        :param hint: dict opcional con ``expected_po`` y ``expected_supplier_nit``
                     para ayudar al servicio; nunca datos personales.
        """
        raise NotImplementedError


class NoopOcrAdapter(OcrAdapter):
    """OCR deshabilitado: no extrae nada y lo dice sin romper el flujo."""

    name = "noop"

    def extract(self, pdf_bytes, filename, hint=None):
        _logger.info("[SPR] OCR deshabilitado, no se extraen datos de %s", filename)
        return {
            "provider": "noop",
            "confidence": 0.0,
            "raw": {},
            "lines": [],
            "raw_warnings": [
                "OCR deshabilitado: los datos de la factura deben capturarse a mano."
            ],
        }


class HttpOcrAdapter(OcrAdapter):
    """Servicio HTTP externo (contrato en el README, seccion OCR)."""

    name = "http"

    def __init__(self, url, api_key=None, timeout=DEFAULT_TIMEOUT):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout or DEFAULT_TIMEOUT

    def _headers(self):
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        return headers

    def extract(self, pdf_bytes, filename, hint=None):
        if not self.url:
            return degraded_result(self.name, "URL del OCR no configurada")
        files = {"file": (filename or "factura.pdf", pdf_bytes, "application/pdf")}
        data = {"hint": json.dumps(hint or {}, ensure_ascii=False)}
        try:
            response = requests.post(
                self.url, headers=self._headers(), files=files, data=data,
                timeout=(CONNECT_TIMEOUT, self.timeout)
            )
        except requests.RequestException as error:
            _logger.warning("[SPR] OCR: error de conexion (%s)", error.__class__.__name__)
            return degraded_result(self.name, error.__class__.__name__)
        if response.status_code != 200:
            _logger.warning("[SPR] OCR: HTTP %s", response.status_code)
            return degraded_result(self.name, "HTTP %s" % response.status_code)
        try:
            payload = response.json()
        except ValueError:
            _logger.warning("[SPR] OCR: la respuesta no es JSON")
            return degraded_result(self.name, "respuesta no es JSON")
        result = normalize_ocr_response(payload, self.name)
        _logger.info(
            "[SPR] OCR: %s lineas, total %s, confianza %.2f",
            len(result["lines"]), result["amount_total"], result["confidence"],
        )
        return result


def _param(env, key, default=None):
    value = env["ir.config_parameter"].sudo().get_param(key)
    return value if value not in (None, False, "") else default


def _param_bool(env, key):
    return str(_param(env, key, "")).lower() in ("true", "1")


def _param_int(env, key, default):
    try:
        return int(_param(env, key, default))
    except (TypeError, ValueError):
        return default


def get_ocr_adapter(env):
    """Devuelve la implementacion configurada en Ajustes."""
    if not _param_bool(env, "spr.ocr_enabled"):
        return NoopOcrAdapter()
    url = _param(env, "spr.ocr_url")
    if not url:
        _logger.warning("[SPR] OCR habilitado en Ajustes pero sin URL. Usando NoopOcrAdapter.")
        return NoopOcrAdapter()
    return HttpOcrAdapter(
        url=url,
        api_key=_param(env, "spr.ocr_api_key"),
        timeout=_param_int(env, "spr.ocr_timeout", DEFAULT_TIMEOUT),
    )
