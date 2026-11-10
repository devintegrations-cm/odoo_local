# -*- coding: utf-8 -*-
"""Emparejamiento de lineas de factura con lineas de orden de compra.

Orden: primero por codigo (determinista), lo que sobre va a la IA. Las lineas
que el validador ya emparejo a mano no se tocan.
"""

import logging
import re
import unicodedata

from .ai_adapter import get_ai_adapter

_logger = logging.getLogger(__name__)

# Por debajo de este umbral no se asigna la linea de OC: se deja la sugerencia
# en match_note para que el validador decida.
MIN_CONFIDENCE = 0.7

MATCH_CODE = "code"
MATCH_AI = "ai"
MATCH_MANUAL = "manual"
MATCH_NONE = "none"
MATCH_ORDER = "order"
MATCH_CHARGE = "charge"

# Lineas que la factura cobra y la orden no tiene: el envio casi nunca esta en
# la orden ni en la remision (DECISIONS.md #35 y #53). Se buscan como palabra,
# sin tildes, y solo en lineas que no se emparejaron por codigo.
CHARGE_KEYWORDS = (
    "flete", "fletes", "envio", "envios", "transporte", "transportes",
    "domicilio", "domicilios", "despacho", "despachos", "acarreo", "acarreos",
    "mensajeria", "mensajerias", "encomienda", "encomiendas", "courier",
    "shipping", "delivery", "freight", "cargo adicional",
)
_CHARGE_RE = re.compile(r"\b(%s)\b" % "|".join(re.escape(k) for k in CHARGE_KEYWORDS))


def _plain(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def is_charge_description(description):
    """True si la descripcion parece un flete o cargo de envio."""
    return bool(_CHARGE_RE.search(_plain(description)))


def normalize_code(value):
    """Los codigos llegan con espacios, guiones y mayusculas mezcladas."""
    if not value:
        return ""
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def _po_lines(request):
    # El flete y el mayor valor IVA no se emparejan con productos de la factura:
    # se ligan al crearla (DECISIONS.md #43).
    tax_product, _exempt = request._tax_as_product()
    skip = (request._freight_product() or request.env["product.product"]) | (
        tax_product or request.env["product.product"])
    return request.purchase_ids.order_line.filtered(
        lambda line: not line.display_type and line.product_id and line.product_id not in skip
    )


def build_code_index(request, po_lines):
    """Diccionario codigo normalizado -> lineas de OC que lo tienen.

    Se indexan la referencia interna del producto, su codigo de barras y el
    codigo que el proveedor usa en su propio catalogo (product.supplierinfo).
    """
    partner = request.partner_id.commercial_partner_id
    index = {}

    def add(code, line):
        key = normalize_code(code)
        if key:
            index.setdefault(key, []).append(line)

    for line in po_lines:
        product = line.product_id
        add(product.default_code, line)
        add(product.barcode, line)
        for seller in product.product_tmpl_id.seller_ids:
            if seller.partner_id.commercial_partner_id != partner:
                continue
            if seller.product_id and seller.product_id != product:
                continue
            add(seller.product_code, line)
    return index


def match_by_code(request, lines, po_lines):
    """Empareja por codigo exacto. Devuelve {linea_request: linea_po}."""
    index = build_code_index(request, po_lines)
    result = {}
    for line in lines:
        key = normalize_code(line.product_code)
        if not key:
            continue
        candidates = index.get(key) or []
        # Si el mismo codigo aparece en varias lineas de la OC (p. ej. dos
        # entregas), se prefiere la que todavia tiene cantidad por facturar y,
        # de esas, la que mas se parece en precio.
        candidates = [c for c in candidates if c not in result.values()] or candidates
        if not candidates:
            continue
        open_candidates = [c for c in candidates if c.product_qty - c.qty_invoiced > 0]
        pool = open_candidates or candidates
        result[line] = min(pool, key=lambda c: abs(c.price_unit - line.price_unit))
    return result


def _ai_payload_line(line):
    return {
        "idx": None,  # se completa afuera
        "description": line.description or "",
        "code": line.product_code or "",
        "quantity": line.quantity,
        "price_unit": line.price_unit,
    }


def match_by_ai(request, lines, po_lines):
    """Pide sugerencias al adaptador de IA para lo que quedo sin codigo.

    Solo viajan descripciones, codigos, cantidades y precios. Nunca NITs ni
    datos personales. Devuelve una lista de dicts con la forma de
    ``match_lines``.
    """
    if not lines or not po_lines:
        return []
    adapter = get_ai_adapter(request.env)
    invoice_payload = []
    for idx, line in enumerate(lines):
        payload = _ai_payload_line(line)
        payload["idx"] = idx
        invoice_payload.append(payload)
    po_payload = [
        {
            "id": po_line.id,
            "description": po_line.name or "",
            "code": po_line.product_id.default_code or "",
            "quantity": po_line.product_qty - po_line.qty_invoiced,
            "price_unit": po_line.price_unit,
        }
        for po_line in po_lines
    ]
    context = {"currency": request.currency_id.name}
    try:
        response = adapter.match_lines(invoice_payload, po_payload, context) or {}
    except Exception:  # noqa: BLE001 - la IA nunca tumba la validacion
        _logger.exception("[SPR] %s: el adaptador de IA fallo", request.name)
        return []

    po_ids = {po_line.id for po_line in po_lines}
    results = []
    for match in response.get("matches") or []:
        try:
            idx = int(match.get("invoice_idx"))
            po_line_id = int(match.get("po_line_id"))
            confidence = float(match.get("confidence") or 0.0)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(lines) or po_line_id not in po_ids:
            continue
        note = match.get("note") or ""
        if confidence >= MIN_CONFIDENCE:
            results.append({
                "line": lines[idx],
                "po_line_id": po_line_id,
                "method": MATCH_AI,
                "confidence": confidence,
                "note": note,
            })
        else:
            po_line = request.env["purchase.order.line"].browse(po_line_id)
            results.append({
                "line": lines[idx],
                "po_line_id": False,
                "method": MATCH_NONE,
                "confidence": confidence,
                "note": "Sugerencia IA (%.0f%%): %s. %s"
                        % (confidence * 100, po_line.name, note),
            })
    return results


def match_lines(request, parsed=None):
    """Empareja las lineas del request contra las de la orden de compra.

    :return: lista de dicts ``{invoice_idx, po_line_id, method, confidence, note}``
             donde ``invoice_idx`` es la posicion de la linea en
             ``request.line_ids`` ordenadas por secuencia.
    """
    request.ensure_one()
    lines = request.line_ids.sorted(lambda line: (line.sequence, line.id))
    if not lines:
        return []
    po_lines = _po_lines(request)

    index_of = {line: idx for idx, line in enumerate(lines)}
    results = []

    # 1. Lo que el validador ya emparejo a mano se respeta.
    manual = lines.filtered(
        lambda line: line.match_method == MATCH_MANUAL and (line.po_line_id or line.is_extra_charge)
    )
    pending = lines - manual

    # 2. Codigo exacto.
    by_code = match_by_code(request, pending, po_lines)
    for line, po_line in by_code.items():
        results.append({
            "invoice_idx": index_of[line],
            "po_line_id": po_line.id,
            "method": MATCH_CODE,
            "confidence": 1.0,
            "note": "Codigo %s" % (line.product_code or ""),
        })
    pending = pending.filtered(lambda line: line not in by_code)

    # 3. Fletes y cargos de envio: no estan en la orden, no se le pasan a la IA
    #    (los emparejaria con cualquier producto). Se reconocen por el nombre o
    #    porque traen el codigo del producto de flete de Ajustes.
    freight = request._freight_product()
    freight_codes = {normalize_code(code) for code in (freight.default_code, freight.barcode)
                     if code} if freight else set()
    charges = pending.filtered(
        lambda line: is_charge_description(line.description)
        or normalize_code(line.product_code) in freight_codes
    )
    for line in charges:
        results.append({
            "invoice_idx": index_of[line],
            "po_line_id": False,
            "method": MATCH_CHARGE,
            "confidence": 1.0,
            "note": ("Flete: se registra con %s" % freight.display_name if freight
                     else "Cargo adicional: falta el producto de flete en Ajustes"),
        })
    pending = pending - charges

    # 4. IA para lo que sobra, contra las lineas de OC que nadie tomo todavia.
    if pending and po_lines:
        taken = set(by_code.values()) | set(manual.mapped("po_line_id"))
        free_po_lines = po_lines.filtered(lambda line: line not in taken) or po_lines
        for match in match_by_ai(request, pending, free_po_lines):
            results.append({
                "invoice_idx": index_of[match["line"]],
                "po_line_id": match["po_line_id"],
                "method": match["method"],
                "confidence": match["confidence"],
                "note": match["note"],
            })

    _logger.info(
        "[SPR] %s: %d lineas, %d manuales, %d por codigo, %d cargos, %d por IA",
        request.name, len(lines), len(manual), len(by_code), len(charges),
        sum(1 for r in results if r["method"] == MATCH_AI),
    )
    return results
