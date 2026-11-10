# -*- coding: utf-8 -*-
"""Parser de facturas electronicas DIAN (UBL 2.1).

Funciones puras, sin ORM: se pueden probar sin base de datos.
Acepta un ``AttachedDocument`` (el XML que la DIAN devuelve al proveedor, con la
factura embebida en un CDATA) o un ``Invoice`` UBL directo.
"""

import logging
import re
from datetime import datetime

from lxml import etree

_logger = logging.getLogger(__name__)

# Namespaces UBL 2.1 + extensiones DIAN.
NS = {
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "att": "urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2",
    "cn": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
    "dn": "urn:oasis:names:specification:ubl:schema:xsd:DebitNote-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "sts": "dian:gov:co:facturaelectronica:Structures-2-1",
}

CUFE_RE = re.compile(r"^[0-9a-f]{96}$")

# Raiz UBL -> (tipo, etiqueta de linea, etiqueta de cantidad, etiqueta de totales).
# La nota debito lleva los totales en RequestedMonetaryTotal, no en
# LegalMonetaryTotal (Anexo tecnico DIAN 1.9).
DOCUMENTS = {
    "Invoice": ("invoice", "InvoiceLine", "InvoicedQuantity", "LegalMonetaryTotal"),
    "CreditNote": ("credit_note", "CreditNoteLine", "CreditedQuantity", "LegalMonetaryTotal"),
    "DebitNote": ("debit_note", "DebitNoteLine", "DebitedQuantity", "RequestedMonetaryTotal"),
}


class DianXmlError(Exception):
    """El XML no se pudo leer o no es un documento DIAN soportado."""


# ---------------------------------------------------------------------------
# Helpers de datos DIAN (usados tambien por validation_rules)
# ---------------------------------------------------------------------------

def normalize_nit(value):
    """Deja solo los digitos de un NIT: '900.123.456-7' -> '9001234567'."""
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def same_nit(a, b):
    """Compara dos NITs ignorando formato y digito de verificacion.

    Los XML DIAN traen el NIT sin DV (el DV va en ``schemeID``), pero
    ``res.partner.vat`` suele guardarlo pegado ('900123456-7'). Como los NIT
    no tienen largo fijo (8 o 9 digitos de persona juridica, cedulas de 6 a
    10), no se recorta a ciegas: son iguales si coinciden digito a digito o si
    uno es el otro mas exactamente un digito al final (el DV).
    """
    na, nb = normalize_nit(a), normalize_nit(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    longer, shorter = (na, nb) if len(na) > len(nb) else (nb, na)
    return len(longer) == len(shorter) + 1 and longer[:-1] == shorter


def normalize_cufe(value):
    """Normaliza el CUFE a minusculas sin espacios."""
    if not value:
        return ""
    return re.sub(r"\s+", "", str(value)).lower()


def is_valid_cufe(value):
    return bool(CUFE_RE.match(normalize_cufe(value)))


# ---------------------------------------------------------------------------
# Lectura del XML
# ---------------------------------------------------------------------------

def _make_parser():
    """Parser endurecido: el XML lo sube un tercero, no confiamos en el.

    ``resolve_entities=False`` + ``no_network=True`` cierran XXE y las bombas de
    entidades, que es el riesgo real de aceptar XML por el portal.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        recover=False,
        huge_tree=False,
    )


def _to_bytes(xml_input):
    if isinstance(xml_input, bytes):
        return xml_input
    if isinstance(xml_input, str):
        return xml_input.encode("utf-8")
    raise DianXmlError("El XML debe entregarse como bytes o str.")


def _parse_root(xml_bytes):
    try:
        return etree.fromstring(xml_bytes.strip(), parser=_make_parser())
    except etree.XMLSyntaxError as exc:
        raise DianXmlError("El archivo no es un XML valido: %s" % exc) from exc


def _localname(element):
    return etree.QName(element).localname


def _text(node, xpath, default=""):
    found = node.find(xpath, NS)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _attr(node, xpath, attribute, default=""):
    found = node.find(xpath, NS)
    if found is None:
        return default
    return (found.get(attribute) or default).strip()


def _number(node, xpath, default=None):
    """Lee un importe UBL. Devuelve float o ``default`` si no existe."""
    raw = _text(node, xpath)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _date(raw):
    if not raw:
        return ""
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# AttachedDocument -> Invoice embebido
# ---------------------------------------------------------------------------

def extract_embedded_invoice(root):
    """Saca el ``Invoice`` que viaja como CDATA dentro de un AttachedDocument.

    lxml ya devuelve el texto desescapado, asi que sirve igual si el emisor lo
    mando como CDATA real o con entidades (&lt;Invoice&gt;).
    """
    description = root.find(
        ".//cac:Attachment/cac:ExternalReference/cbc:Description", NS
    )
    if description is None or not (description.text or "").strip():
        raise DianXmlError(
            "El AttachedDocument no trae la factura embebida en "
            "cac:Attachment/cac:ExternalReference/cbc:Description."
        )
    inner = description.text.strip()
    # Algunos emisores dejan el prologo XML dentro del CDATA.
    if inner.startswith("<?xml"):
        inner = inner[inner.index("?>") + 2:].lstrip()
    return _parse_root(_to_bytes(inner))


def pretty_xml(xml_input):
    """XML con sangria para la vista previa del backend (DECISIONS.md #54).

    Si es un AttachedDocument, primero va la factura embebida, que es lo que se
    quiere leer; despues el documento completo.
    """
    root = _parse_root(_to_bytes(xml_input))
    whole = etree.tostring(root, pretty_print=True, encoding="unicode")
    if _localname(root) != "AttachedDocument":
        return whole
    try:
        invoice = extract_embedded_invoice(root)
    except DianXmlError:
        return whole
    return "%s\n%s\n\n%s\n%s" % (
        "<!-- Factura embebida en el AttachedDocument -->",
        etree.tostring(invoice, pretty_print=True, encoding="unicode"),
        "<!-- AttachedDocument completo -->",
        whole,
    )


# ---------------------------------------------------------------------------
# Partes (emisor / adquiriente)
# ---------------------------------------------------------------------------

def _parse_party(invoice, party_xpath):
    party = invoice.find(party_xpath, NS)
    if party is None:
        return {"nit": "", "dv": "", "name": "", "address": ""}

    nit = _text(party, "cac:PartyTaxScheme/cbc:CompanyID")
    dv = _attr(party, "cac:PartyTaxScheme/cbc:CompanyID", "schemeID")
    if not nit:
        nit = _text(party, "cac:PartyIdentification/cbc:ID")

    name = (
        _text(party, "cac:PartyTaxScheme/cbc:RegistrationName")
        or _text(party, "cac:PartyName/cbc:Name")
        or _text(party, "cac:PartyLegalEntity/cbc:RegistrationName")
    )

    address_node = party.find("cac:PhysicalLocation/cac:Address", NS)
    if address_node is None:
        address_node = party.find("cac:PartyTaxScheme/cac:RegistrationAddress", NS)
    address = ""
    if address_node is not None:
        parts = [
            _text(address_node, "cac:AddressLine/cbc:Line"),
            _text(address_node, "cbc:CityName"),
            _text(address_node, "cbc:CountrySubentity"),
        ]
        address = ", ".join(p for p in parts if p)

    return {
        "nit": normalize_nit(nit),
        "dv": dv,
        "name": name,
        "address": address,
    }


# ---------------------------------------------------------------------------
# Lineas
# ---------------------------------------------------------------------------

def _parse_line(line_node, quantity_tag, warnings):
    quantity = _number(line_node, "cbc:%s" % quantity_tag, 0.0) or 0.0
    unit = _attr(line_node, "cbc:%s" % quantity_tag, "unitCode")
    subtotal = _number(line_node, "cbc:LineExtensionAmount", 0.0) or 0.0

    item = line_node.find("cac:Item", NS)
    description = ""
    code = ""
    if item is not None:
        description = _text(item, "cbc:Description") or _text(item, "cbc:Name")
        # El codigo del vendedor es el que el proveedor usa en su catalogo: es el
        # que sirve para el matching contra product_supplierinfo.
        code = (
            _text(item, "cac:SellersItemIdentification/cbc:ID")
            or _text(item, "cac:StandardItemIdentification/cbc:ID")
            or _text(item, "cac:BuyersItemIdentification/cbc:ID")
        )

    price_unit = _number(line_node, "cac:Price/cbc:PriceAmount")
    if price_unit is None:
        price_unit = subtotal / quantity if quantity else 0.0

    tax_rate = _number(
        line_node, "cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory/cbc:Percent", 0.0
    ) or 0.0
    tax_amount = _number(line_node, "cac:TaxTotal/cbc:TaxAmount", 0.0) or 0.0

    # Descuentos y recargos a nivel de linea.
    discount = 0.0
    charge = 0.0
    for allowance in line_node.findall("cac:AllowanceCharge", NS):
        amount = _number(allowance, "cbc:Amount", 0.0) or 0.0
        is_charge = _text(allowance, "cbc:ChargeIndicator").lower() == "true"
        if is_charge:
            charge += amount
        else:
            discount += amount

    if quantity == 0.0:
        warnings.append(
            "La linea '%s' viene con cantidad cero." % (description or code or "?")
        )

    return {
        "sequence": _text(line_node, "cbc:ID"),
        "description": description,
        "code": code,
        "quantity": quantity,
        "unit": unit,
        "price_unit": price_unit,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "discount": discount,
        "charge": charge,
        "subtotal": subtotal,
    }


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def parse_dian_xml(xml_input):
    """Devuelve el dict normalizado de una factura electronica DIAN.

    Es el mismo contrato que debe cumplir ``OcrAdapter.extract``.
    """
    root = _parse_root(_to_bytes(xml_input))
    root_tag = _localname(root)
    warnings = []

    if root_tag == "AttachedDocument":
        invoice = extract_embedded_invoice(root)
        invoice_tag = _localname(invoice)
        # El AttachedDocument tambien declara el CUFE; si difiere del embebido,
        # algo se manipulo por el camino.
        outer_cufe = normalize_cufe(
            _text(root, ".//cac:ParentDocumentLineReference/cac:DocumentReference/cbc:UUID")
            or _text(root, ".//cac:DocumentReference/cbc:UUID")
        )
    else:
        invoice = root
        invoice_tag = root_tag
        outer_cufe = ""

    if invoice_tag not in DOCUMENTS:
        raise DianXmlError(
            "El XML no es una factura ni una nota electronica DIAN (raiz: %s)." % invoice_tag
        )
    document_type, line_tag, quantity_tag, totals_tag = DOCUMENTS[invoice_tag]

    cufe = normalize_cufe(_text(invoice, "cbc:UUID"))
    if not cufe:
        warnings.append("La factura no declara CUFE (cbc:UUID).")
    elif not is_valid_cufe(cufe):
        warnings.append("El CUFE del XML no tiene el formato esperado (96 hexadecimales).")
    if outer_cufe and cufe and outer_cufe != cufe:
        warnings.append(
            "El CUFE del AttachedDocument no coincide con el de la factura embebida."
        )

    lines = []
    for line_node in invoice.findall("cac:%s" % line_tag, NS):
        lines.append(_parse_line(line_node, quantity_tag, warnings))
    if not lines:
        warnings.append("El documento no trae lineas de detalle.")
    # Cargos a nivel de documento (flete, seguro...): no estan en ninguna linea
    # pero si en el total. Se agregan como lineas para que contabilidad los vea
    # y la factura borrador los incluya (DECISIONS.md #35).
    for charge in invoice.findall("cac:AllowanceCharge", NS):
        if _text(charge, "cbc:ChargeIndicator").lower() != "true":
            continue
        amount = _number(charge, "cbc:Amount", 0.0) or 0.0
        if not amount:
            continue
        reason = _text(charge, "cbc:AllowanceChargeReason") or "sin descripcion"
        lines.append({
            "sequence": "", "description": "Cargo adicional: %s" % reason, "code": "",
            "quantity": 1.0, "unit": "", "price_unit": amount, "tax_rate": 0.0,
            "tax_amount": 0.0, "discount": 0.0, "charge": 0.0, "subtotal": amount,
        })

    # Solo los TaxTotal hijos directos del documento: los de linea ya se leyeron.
    amount_tax = 0.0
    for tax_total in invoice.findall("cac:TaxTotal", NS):
        amount_tax += _number(tax_total, "cbc:TaxAmount", 0.0) or 0.0

    totals = invoice.find("cac:%s" % totals_tag, NS)
    if totals is None:
        raise DianXmlError("El documento no trae cac:%s." % totals_tag)

    amount_untaxed = _number(totals, "cbc:LineExtensionAmount", 0.0) or 0.0
    amount_total = _number(totals, "cbc:PayableAmount")
    if amount_total is None:
        amount_total = _number(totals, "cbc:TaxInclusiveAmount", 0.0) or 0.0
    allowance_total = _number(totals, "cbc:AllowanceTotalAmount", 0.0) or 0.0
    charge_total = _number(totals, "cbc:ChargeTotalAmount", 0.0) or 0.0
    prepaid = _number(totals, "cbc:PrepaidAmount", 0.0) or 0.0

    currency = (
        _text(invoice, "cbc:DocumentCurrencyCode")
        or _attr(totals, "cbc:PayableAmount", "currencyID")
        or "COP"
    ).upper()

    result = {
        "document_type": document_type,
        "billing_reference": {
            "number": _text(invoice, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID"),
            "cufe": normalize_cufe(
                _text(invoice, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:UUID")
            ),
        },
        "cufe": cufe,
        "invoice_ref": _text(invoice, "cbc:ID"),
        "issue_date": _date(_text(invoice, "cbc:IssueDate")),
        "supplier": _parse_party(invoice, "cac:AccountingSupplierParty/cac:Party"),
        "customer": _parse_party(invoice, "cac:AccountingCustomerParty/cac:Party"),
        "lines": lines,
        "amount_untaxed": amount_untaxed,
        "amount_tax": amount_tax,
        "amount_total": amount_total,
        "allowance_total": allowance_total,
        "charge_total": charge_total,
        "prepaid_amount": prepaid,
        "currency": currency,
        "raw_warnings": warnings,
    }
    _logger.info(
        "[SPR] XML DIAN leido: %s %s, CUFE %s..., %s lineas, total %s %s",
        document_type, result["invoice_ref"], cufe[:12], len(lines), amount_total, currency,
    )
    return result


def check_cufe_matches(parsed, captured_cufe):
    """Verifica que el CUFE que digito el proveedor sea el del XML.

    Devuelve ``(ok, mensaje)``; el mensaje ya viene en espanol para mostrarlo.
    """
    xml_cufe = normalize_cufe(parsed.get("cufe"))
    given = normalize_cufe(captured_cufe)
    if not given:
        return False, "No se capturo el CUFE."
    if not xml_cufe:
        return False, "El XML no declara CUFE, no se puede comparar."
    if xml_cufe != given:
        return False, (
            "El CUFE capturado no coincide con el del XML "
            "(XML: %s..., capturado: %s...)." % (xml_cufe[:16], given[:16])
        )
    return True, "El CUFE coincide con el del XML."
