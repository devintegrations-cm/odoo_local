# -*- coding: utf-8 -*-
"""Adaptador de IA para emparejar lineas de factura con lineas de orden de compra.

La IA NUNCA crea ni modifica registros: devuelve sugerencias que despues pasan
por las reglas duras y por el criterio del validador. Solo viajan
descripciones, codigos, cantidades y precios; nunca NITs ni datos personales.

Dos proveedores:

- ``anthropic``: llama la Messages API directamente con ``requests`` y salida
  estructurada (``output_config.format``), asi la respuesta es JSON valido
  contra el esquema sin tener que limpiar texto. Se usa HTTP crudo y no el SDK
  ``anthropic`` porque la imagen ``odoo:17.0`` no lo trae y el modulo no
  agrega dependencias Python (DECISIONS.md #1 y #25).
- ``http``: un servicio propio (n8n, FastAPI) que implementa el contrato del
  README y puede usar el modelo que quiera.
- ``cli`` (conexion de prueba): la misma Messages API, pero apuntando al
  puente local ``anthropic_cli_bridge`` que corre en el equipo del
  administrador y ejecuta una CLI con sesion iniciada (``claude`` con un
  perfil, ``agy`` o ``codex``). El campo ``model`` elige la herramienta. Odoo
  no ejecuta nada: solo HTTP. Para desarrollo, no para produccion.

Cualquier fallo devuelve una respuesta vacia con ``summary_es`` explicando el
motivo. La validacion sigue: las lineas quedan para emparejar a mano.
"""

import json
import logging

import requests

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 45
# Tiempo maximo para abrir la conexion. El timeout configurado aplica a la
# respuesta; sin este limite, una URL inalcanzable cuelga todo el timeout.
CONNECT_TIMEOUT = 10
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
MAX_TOKENS = 4096
DEFAULT_CLI_TIMEOUT = 180
DEFAULT_CLI_URL = "http://host.docker.internal:8787/v1/messages"
CLI_TOOLS = ("claude-empresa", "claude-team", "claude-personal", "agy", "codex")

SYSTEM_PROMPT = """Eres un asistente de contabilidad de compras en Colombia.
Recibes las lineas de una factura de proveedor y las lineas de la orden de compra
a la que el proveedor dice que corresponde. Tu unica tarea es decir que linea de
la factura corresponde a que linea de la orden.

Criterios, en orden: mismo codigo o codigo equivalente; misma descripcion o
descripcion equivalente aunque cambie el orden de las palabras, abreviaturas,
tildes o unidades; precio unitario igual o muy cercano; cantidad igual o cercana.
Una linea de la orden puede recibir varias lineas de la factura si es evidente
(por ejemplo, entregas parciales del mismo producto).

Da una confianza entre 0 y 1: 0.95 o mas solo si codigo o descripcion y precio
coinciden; entre 0.7 y 0.95 si la descripcion es equivalente pero cambia algo
menor; por debajo de 0.7 si es una suposicion. No inventes emparejamientos: si
una linea no corresponde a nada, dejala en unmatched_invoice. Las notas y el
resumen van en espanol, breves y sin datos que no esten en la entrada."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "invoice_idx": {"type": "integer"},
                    "po_line_id": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["invoice_idx", "po_line_id", "confidence", "note"],
                "additionalProperties": False,
            },
        },
        "unmatched_invoice": {"type": "array", "items": {"type": "integer"}},
        "unmatched_po": {"type": "array", "items": {"type": "integer"}},
        "summary_es": {"type": "string"},
    },
    "required": ["matches", "unmatched_invoice", "unmatched_po", "summary_es"],
    "additionalProperties": False,
}


# Revision de unidades (DECISIONS.md #51): la factura y la orden hablan de la
# misma mercancia en unidades distintas (bulto contra kilo, caja contra unidad)
# y por eso la cantidad y el precio no cuadran aunque el total si.
UNITS_SYSTEM_PROMPT = """Eres un asistente de contabilidad de compras en Colombia.
Recibes lineas de una factura de proveedor ya emparejadas con una linea de la
orden de compra. En cada par la cantidad o el precio unitario no coinciden, pero
puede ser porque la factura y la orden usan unidades de medida distintas para la
misma mercancia (por ejemplo, la factura cobra 2 bultos de 70 kg y la orden pide
140 kg; o 10 cajas x 12 contra 120 unidades).

Para cada linea di el factor de conversion: cuantas unidades de la orden hay en
una unidad de la factura (cantidad_orden = cantidad_factura x factor y
precio_orden = precio_factura / factor). Usa la descripcion, el codigo de unidad
DIAN de la factura (unit_code, por ejemplo NIU, KGM, BG, BX), la unidad de la
orden y los precios. Si la diferencia no se explica por unidades (es un cambio
de precio o una cantidad de mas), responde factor 1 y explica por que.

Confianza entre 0 y 1: 0.9 o mas solo si la descripcion o la unidad hacen
evidente el factor y los precios lo confirman; por debajo de 0.7 si es una
suposicion. No inventes factores. Notas y resumen en espanol, breves."""

UNITS_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "factor": {"type": "number"},
                    "confidence": {"type": "number"},
                    "note": {"type": "string"},
                },
                "required": ["idx", "factor", "confidence", "note"],
                "additionalProperties": False,
            },
        },
        "summary_es": {"type": "string"},
    },
    "required": ["lines", "summary_es"],
    "additionalProperties": False,
}


class AiUnavailable(Exception):
    """La IA no respondio algo utilizable. El mensaje va al resumen."""


# ---------------------------------------------------------------------------
# Respuesta normalizada
# ---------------------------------------------------------------------------

def empty_response(invoice_lines, po_lines, summary):
    return {
        "matches": [],
        "unmatched_invoice": list(range(len(invoice_lines))),
        "unmatched_po": [line.get("id") for line in po_lines],
        "summary_es": summary,
    }


def sanitize_response(payload, invoice_lines, po_lines, provider):
    """Deja la respuesta con la forma exacta del contrato, sin confiar en nada.

    Indices fuera de rango, ids de OC que no venian en la peticion o
    confianzas fuera de 0-1 se descartan o se acotan.
    """
    if not isinstance(payload, dict):
        return empty_response(invoice_lines, po_lines, "Respuesta de IA no reconocida.")
    valid_po_ids = {line.get("id") for line in po_lines}
    matches = []
    matched_idx = set()
    for match in payload.get("matches") or []:
        if not isinstance(match, dict):
            continue
        try:
            idx = int(match.get("invoice_idx"))
            po_line_id = int(match.get("po_line_id"))
            confidence = float(match.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(invoice_lines) or po_line_id not in valid_po_ids:
            continue
        if idx in matched_idx:
            continue
        matched_idx.add(idx)
        matches.append({
            "invoice_idx": idx,
            "po_line_id": po_line_id,
            "confidence": max(0.0, min(1.0, confidence)),
            "note": str(match.get("note") or "")[:500],
        })
    matched_po = {m["po_line_id"] for m in matches}
    return {
        "matches": matches,
        "unmatched_invoice": [i for i in range(len(invoice_lines)) if i not in matched_idx],
        "unmatched_po": [pid for pid in valid_po_ids if pid not in matched_po],
        "summary_es": str(payload.get("summary_es") or "Emparejamiento sugerido por %s." % provider)[:1000],
    }


def sanitize_units_response(payload, lines):
    """Factores de unidad con la forma exacta del contrato.

    Indices fuera de rango, factores no positivos y confianzas fuera de 0-1 se
    descartan o se acotan. Que el factor cuadre con los precios lo decide
    despues quien lo aplica, no la IA.
    """
    if not isinstance(payload, dict):
        return {"lines": [], "summary_es": "Respuesta de IA no reconocida."}
    result = []
    seen = set()
    for item in payload.get("lines") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx"))
            factor = float(item.get("factor"))
            confidence = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(lines) or idx in seen or factor <= 0:
            continue
        seen.add(idx)
        result.append({
            "idx": idx,
            "factor": factor,
            "confidence": max(0.0, min(1.0, confidence)),
            "note": str(item.get("note") or "")[:500],
        })
    return {"lines": result, "summary_es": str(payload.get("summary_es") or "")[:1000]}


def build_request_payload(invoice_lines, po_lines, context):
    """Lo unico que sale hacia la IA. Sin NITs, sin nombres de terceros."""
    return {
        "invoice_lines": [
            {
                "idx": line.get("idx"),
                "description": line.get("description") or "",
                "code": line.get("code") or "",
                "quantity": line.get("quantity"),
                "price_unit": line.get("price_unit"),
            }
            for line in invoice_lines
        ],
        "po_lines": [
            {
                "id": line.get("id"),
                "description": line.get("description") or "",
                "code": line.get("code") or "",
                "quantity": line.get("quantity"),
                "price_unit": line.get("price_unit"),
            }
            for line in po_lines
        ],
        "context": {"currency": (context or {}).get("currency") or "COP"},
    }


# ---------------------------------------------------------------------------
# Adaptadores
# ---------------------------------------------------------------------------

class AiAdapter:
    """Contrato del servicio de IA."""

    name = "base"

    def match_lines(self, invoice_lines, po_lines, context):
        """Devuelve un dict con la forma::

            {
                "matches": [
                    {"invoice_idx": int, "po_line_id": int,
                     "confidence": float, "note": str},
                ],
                "unmatched_invoice": [int],
                "unmatched_po": [int],
                "summary_es": str,
            }
        """
        raise NotImplementedError

    def reconcile_units(self, lines, context):
        """Factor de unidad de cada par linea de factura / linea de orden.

        ``lines`` es una lista de dicts con ``idx``, los datos de la factura
        (``description``, ``code``, ``unit_code``, ``quantity``,
        ``price_unit``) y los de la orden (``po_description``, ``po_uom``,
        ``po_quantity``, ``po_price_unit``). Devuelve::

            {"lines": [{"idx": int, "factor": float, "confidence": float,
                        "note": str}],
             "summary_es": str}
        """
        return {"lines": [], "summary_es": "La revision de unidades no esta disponible "
                                           "con este proveedor de IA."}


class NoopAiAdapter(AiAdapter):
    """IA deshabilitada: el matching queda solo por codigo."""

    name = "noop"

    def match_lines(self, invoice_lines, po_lines, context):
        _logger.info("[SPR] IA deshabilitada, no se sugieren emparejamientos.")
        return empty_response(invoice_lines, po_lines, "Emparejamiento por IA deshabilitado.")


class HttpAiAdapter(AiAdapter):
    """Servicio HTTP propio que implementa el contrato del README."""

    name = "http"

    def __init__(self, url, api_key=None, timeout=DEFAULT_TIMEOUT):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout or DEFAULT_TIMEOUT

    def match_lines(self, invoice_lines, po_lines, context):
        if not self.url:
            return empty_response(invoice_lines, po_lines, "URL del servicio de IA no configurada.")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        body = build_request_payload(invoice_lines, po_lines, context)
        try:
            response = requests.post(self.url, headers=headers, json=body,
                                     timeout=(CONNECT_TIMEOUT, self.timeout))
        except requests.RequestException as error:
            _logger.warning("[SPR] IA (http): error de conexion (%s)", error.__class__.__name__)
            return empty_response(invoice_lines, po_lines,
                                  "IA no disponible (%s)." % error.__class__.__name__)
        if response.status_code != 200:
            _logger.warning("[SPR] IA (http): HTTP %s", response.status_code)
            return empty_response(invoice_lines, po_lines,
                                  "IA no disponible (HTTP %s)." % response.status_code)
        try:
            payload = response.json()
        except ValueError:
            return empty_response(invoice_lines, po_lines, "La respuesta de IA no es JSON.")
        return sanitize_response(payload, invoice_lines, po_lines, self.name)


class AnthropicAiAdapter(AiAdapter):
    """Messages API de Anthropic con salida estructurada."""

    name = "anthropic"

    def __init__(self, api_key, model=DEFAULT_ANTHROPIC_MODEL, url=None, timeout=DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.url = url or ANTHROPIC_URL
        self.timeout = timeout or DEFAULT_TIMEOUT

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def _body(self, system, user_text, schema):
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {
                "format": {"type": "json_schema", "schema": schema},
            },
        }

    @staticmethod
    def _extract_json(message):
        """Saca el JSON del primer bloque de texto de la respuesta."""
        stop_reason = message.get("stop_reason")
        if stop_reason == "refusal":
            raise ValueError("el modelo rechazo la solicitud")
        if stop_reason == "max_tokens":
            raise ValueError("la respuesta quedo incompleta (max_tokens)")
        for block in message.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                return json.loads(block["text"])
        raise ValueError("la respuesta no trae texto")

    def _ask(self, system, instruction, payload, schema):
        """Una llamada a la Messages API. Devuelve el JSON del esquema o lanza
        ``AiUnavailable`` con el motivo para el resumen."""
        if not self.api_key:
            raise AiUnavailable("API key de Anthropic no configurada.")
        user_text = "%s\n\n%s" % (instruction, json.dumps(payload, ensure_ascii=False, indent=1))
        try:
            response = requests.post(
                self.url, headers=self._headers(),
                json=self._body(system, user_text, schema),
                timeout=(CONNECT_TIMEOUT, self.timeout),
            )
        except requests.RequestException as error:
            _logger.warning("[SPR] IA (%s): error de conexion (%s)", self.name,
                            error.__class__.__name__)
            raise AiUnavailable("IA no disponible (%s)." % error.__class__.__name__) from error
        if response.status_code != 200:
            # El cuerpo del error puede traer detalle util, pero nunca la key.
            detail = ""
            try:
                detail = (response.json().get("error") or {}).get("message") or ""
            except ValueError:
                pass
            _logger.warning("[SPR] IA (%s): HTTP %s %s", self.name, response.status_code,
                            detail[:200])
            raise AiUnavailable("IA no disponible (HTTP %s)." % response.status_code)
        try:
            message = response.json()
            answer = self._extract_json(message)
        except ValueError as error:
            _logger.warning("[SPR] IA (%s): respuesta invalida: %s", self.name, error)
            raise AiUnavailable("Respuesta de IA invalida: %s." % error) from error
        usage = message.get("usage") or {}
        _logger.info(
            "[SPR] IA (%s) modelo %s: %s tokens entrada, %s salida",
            self.name, message.get("model") or self.model, usage.get("input_tokens"),
            usage.get("output_tokens"),
        )
        return answer

    def match_lines(self, invoice_lines, po_lines, context):
        try:
            payload = self._ask(
                SYSTEM_PROMPT, "Empareja estas lineas. Responde solo con el JSON del esquema.",
                build_request_payload(invoice_lines, po_lines, context), RESPONSE_SCHEMA,
            )
        except AiUnavailable as error:
            return empty_response(invoice_lines, po_lines, str(error))
        return sanitize_response(payload, invoice_lines, po_lines, self.name)

    def reconcile_units(self, lines, context):
        payload = {
            "lines": lines,
            "context": {"currency": (context or {}).get("currency") or "COP"},
        }
        try:
            answer = self._ask(
                UNITS_SYSTEM_PROMPT,
                "Da el factor de unidad de cada linea. Responde solo con el JSON del esquema.",
                payload, UNITS_SCHEMA,
            )
        except AiUnavailable as error:
            return {"lines": [], "summary_es": str(error)}
        return sanitize_units_response(answer, lines)


class CliAiAdapter(AnthropicAiAdapter):
    """Conexion de prueba: Messages API contra el puente local de CLI.

    El puente (``claude_projects/anthropic_cli_bridge``) emula
    ``POST /v1/messages`` y ejecuta la CLI que diga ``model``. Todo lo demas
    (cuerpo, esquema, lectura de la respuesta) es identico a Anthropic.
    """

    name = "cli"

    def __init__(self, tool, url=None, api_key=None, timeout=DEFAULT_CLI_TIMEOUT):
        self.tool = tool if tool in CLI_TOOLS else CLI_TOOLS[0]
        super().__init__(
            api_key=api_key or "local", model=self.tool,
            url=url or DEFAULT_CLI_URL, timeout=timeout or DEFAULT_CLI_TIMEOUT,
        )


# ---------------------------------------------------------------------------
# Fabrica
# ---------------------------------------------------------------------------

def _param(env, key, default=None):
    value = env["ir.config_parameter"].sudo().get_param(key)
    return value if value not in (None, False, "") else default


def _param_int(env, key, default):
    try:
        return int(_param(env, key, default))
    except (TypeError, ValueError):
        return default


def get_ai_adapter(env):
    """Devuelve la implementacion configurada en Ajustes."""
    if str(_param(env, "spr.ai_enabled", "")).lower() not in ("true", "1"):
        return NoopAiAdapter()
    if str(_param(env, "spr.ai_cli_enabled", "")).lower() in ("true", "1"):
        return CliAiAdapter(
            tool=_param(env, "spr.ai_cli_tool", CLI_TOOLS[0]),
            url=_param(env, "spr.ai_cli_url", DEFAULT_CLI_URL),
            api_key=_param(env, "spr.ai_cli_api_key"),
            timeout=_param_int(env, "spr.ai_cli_timeout", DEFAULT_CLI_TIMEOUT),
        )
    provider = _param(env, "spr.ai_provider", "anthropic")
    api_key = _param(env, "spr.ai_api_key")
    timeout = _param_int(env, "spr.ai_timeout", DEFAULT_TIMEOUT)
    if provider == "http":
        url = _param(env, "spr.ai_url")
        if not url:
            _logger.warning("[SPR] IA habilitada (http) pero sin URL. Usando NoopAiAdapter.")
            return NoopAiAdapter()
        return HttpAiAdapter(url=url, api_key=api_key, timeout=timeout)
    if not api_key:
        _logger.warning("[SPR] IA habilitada (anthropic) pero sin API key. Usando NoopAiAdapter.")
        return NoopAiAdapter()
    return AnthropicAiAdapter(
        api_key=api_key,
        model=_param(env, "spr.ai_model", DEFAULT_ANTHROPIC_MODEL),
        url=_param(env, "spr.ai_url"),
        timeout=timeout,
    )
