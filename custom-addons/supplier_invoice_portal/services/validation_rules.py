# -*- coding: utf-8 -*-
"""Reglas duras de validacion contable.

Lo contable se valida aqui, con reglas deterministas. La IA no participa.

Cada regla es una funcion ``_rule_xxx(ctx)`` que devuelve una lista de
``Finding``. ``run_rules`` las ejecuta todas y nunca deja de correr una regla
porque otra haya fallado: contabilidad necesita ver todos los hallazgos de una
vez, no de uno en uno.
"""

import logging
from datetime import date

from odoo.tools import float_compare, float_is_zero

from . import dian_catalog, dian_xml_parser

_logger = logging.getLogger(__name__)

LEVEL_OK = "ok"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

# Valores por defecto de los ajustes cuando nunca se han guardado.
DEFAULT_TOLERANCE_PCT = 0.5
DEFAULT_TOLERANCE_ABS = 1000.0

# Estados de orden de compra que aceptan facturas.
PO_OPEN_STATES = ("purchase", "done")

# Estados del request que cuentan para detectar un CUFE repetido. Un request
# rechazado o cancelado no bloquea: el proveedor puede corregir y reenviar.
REQUEST_ACTIVE_STATES = ("draft", "validating", "approved", "warning", "invoiced")


class Finding:
    """Resultado de una regla."""

    __slots__ = ("level", "code", "message_es", "details")

    def __init__(self, level, code, message_es, details=None):
        self.level = level
        self.code = code
        self.message_es = message_es
        self.details = details or {}

    def to_dict(self):
        return {
            "level": self.level,
            "code": self.code,
            "message_es": self.message_es,
            "details": self.details,
        }

    def __repr__(self):
        return "<Finding %s %s>" % (self.level, self.code)


def verdict_from_findings(findings):
    """Traduce los hallazgos al estado del request."""
    levels = {f.level for f in findings}
    if LEVEL_ERROR in levels:
        return "rejected"
    if LEVEL_WARNING in levels:
        return "warning"
    return "approved"


# ---------------------------------------------------------------------------
# Ajustes y helpers
# ---------------------------------------------------------------------------

def _param_float(env, key, default):
    raw = env["ir.config_parameter"].sudo().get_param(key)
    if raw in (None, False, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        _logger.warning("[SPR] El ajuste %s tiene un valor invalido (%r).", key, raw)
        return default


class Tolerance:
    """Combina las dos tolerancias de monto (DECISIONS.md #5: pasa si esta dentro
    del porcentaje O dentro del monto absoluto)."""

    def __init__(self, pct, abs_amount, rounding):
        self.pct = pct
        self.abs_amount = abs_amount
        self.rounding = rounding

    @classmethod
    def from_env(cls, env, currency):
        return cls(
            _param_float(env, "spr.amount_tolerance_pct", DEFAULT_TOLERANCE_PCT),
            _param_float(env, "spr.amount_tolerance_abs", DEFAULT_TOLERANCE_ABS),
            currency.rounding if currency else 0.01,
        )

    def allowed(self, base):
        return max(abs(base) * self.pct / 100.0, self.abs_amount)

    def within(self, actual, expected):
        diff = abs((actual or 0.0) - (expected or 0.0))
        if float_is_zero(diff, precision_rounding=self.rounding):
            return True
        return float_compare(diff, self.allowed(expected), precision_rounding=self.rounding) <= 0

    def within_pct(self, actual, expected):
        """Solo el porcentaje: para precios unitarios, donde el monto absoluto
        de los totales dejaria pasar sobreprecios pequenos multiplicados por
        cantidades grandes."""
        diff = abs((actual or 0.0) - (expected or 0.0))
        if float_is_zero(diff, precision_rounding=self.rounding):
            return True
        allowed = max(abs(expected or 0.0) * self.pct / 100.0, self.rounding)
        return float_compare(diff, allowed, precision_rounding=self.rounding) <= 0


def _money(value):
    return "{:,.2f}".format(value or 0.0)


def party_check_enabled(env):
    """Ajuste *Verificar emisor y receptor*. Activado por defecto; el parametro
    se guarda como "True"/"False" (ver res_config_settings)."""
    value = env["ir.config_parameter"].sudo().get_param("spr.party_check", "True")
    return str(value).lower() in ("true", "1")


def _company_nit(env, company):
    configured = env["ir.config_parameter"].sudo().get_param("spr.company_nit")
    return dian_xml_parser.normalize_nit(configured or company.vat or "")


def _po_pending_amount(order, skip_products=None):
    """Pendiente por facturar de la OC (DECISIONS.md #6).

    Se suma lo facturado linea por linea (las lineas de factura ligadas a cada
    linea de la orden), no el total de cada factura: una factura que agrupa
    varias ordenes o trae lineas extra no debe descontarse entera de esta.
    ``skip_products`` deja por fuera las lineas de esos productos (el mayor
    valor IVA y el flete, que se revisan aparte).
    """
    pending = 0.0
    for po_line in order.order_line:
        if skip_products and po_line.product_id in skip_products:
            continue
        pending += po_line.price_total
        for inv_line in po_line.invoice_lines:
            move = inv_line.move_id
            if move.state == "cancel":
                continue
            if move.move_type == "in_invoice":
                pending -= inv_line.price_total
            elif move.move_type == "in_refund":
                pending += inv_line.price_total
    return pending


def _orders_pending_amount(orders, skip_products=None):
    return sum(_po_pending_amount(order, skip_products) for order in orders)


def _extra_charges_total(request):
    """Fletes y cargos adicionales de la factura, con su impuesto."""
    return sum(
        line.subtotal * (1 + (line.tax_rate or 0.0) / 100.0)
        for line in request.line_ids if line.is_extra_charge
    )


def _po_line_tax_rate(po_line):
    """Suma de los impuestos porcentuales de la linea de OC."""
    return sum(tax.amount for tax in po_line.taxes_id if tax.amount_type == "percent")


# ---------------------------------------------------------------------------
# Reglas
# ---------------------------------------------------------------------------

def _rule_manual_capture(ctx):
    """Sin XML y sin OCR no hay datos: la solicitud queda para captura manual.

    En ese caso las reglas que dependen de montos y lineas no corren (darian
    errores vacios) y se deja una sola observacion clara.
    """
    if not ctx["no_data"]:
        return []
    return [Finding(LEVEL_WARNING, "MANUAL_CAPTURE_REQUIRED",
                    "No se extrajeron datos de la factura (sin XML y sin OCR). "
                    "Capture numero, fecha, totales y lineas en el backend y revalide.")]


def _rule_document_type(ctx):
    """El XML adjunto debe ser del tipo con el que se radico."""
    request, parsed = ctx["request"], ctx["parsed"]
    if parsed.get("source") != "xml" or not parsed.get("document_type"):
        return []
    if parsed["document_type"] == request.document_type:
        return []
    labels = dict(request._fields["document_type"].selection)
    return [Finding(LEVEL_ERROR, "DOCUMENT_TYPE",
                    "El XML adjunto es %s, pero se radico como %s. Radiquelo con el tipo "
                    "de documento correcto."
                    % (labels.get(parsed["document_type"], parsed["document_type"]).lower(),
                       labels.get(request.document_type, request.document_type).lower()))]


def _rule_supplier_nit(ctx):
    if not party_check_enabled(ctx["env"]):
        return []
    request, parsed = ctx["request"], ctx["parsed"]
    xml_nit = dian_xml_parser.normalize_nit((parsed.get("supplier") or {}).get("nit"))
    partner_nit = dian_xml_parser.normalize_nit(request.partner_id.commercial_partner_id.vat)
    if not xml_nit:
        return [Finding(LEVEL_WARNING, "SUPPLIER_NIT",
                        "La factura no trae el NIT del emisor; no se pudo verificar.")]
    if not partner_nit:
        return [Finding(LEVEL_WARNING, "SUPPLIER_NIT",
                        "El proveedor %s no tiene NIT registrado en Odoo; no se pudo "
                        "verificar contra el NIT %s de la factura."
                        % (request.partner_id.display_name, xml_nit),
                        {"invoice_nit": xml_nit})]
    if not dian_xml_parser.same_nit(xml_nit, partner_nit):
        return [Finding(LEVEL_ERROR, "SUPPLIER_NIT",
                        "El NIT del emisor (%s) no coincide con el del proveedor %s (%s)."
                        % (xml_nit, request.partner_id.display_name, partner_nit),
                        {"invoice_nit": xml_nit, "partner_nit": partner_nit})]
    return [Finding(LEVEL_OK, "SUPPLIER_NIT", "El NIT del emisor coincide con el proveedor.")]


def _rule_customer_nit(ctx):
    if not party_check_enabled(ctx["env"]):
        return []
    request, parsed, env = ctx["request"], ctx["parsed"], ctx["env"]
    xml_nit = dian_xml_parser.normalize_nit((parsed.get("customer") or {}).get("nit"))
    company_nit = _company_nit(env, request.company_id)
    if not xml_nit:
        return [Finding(LEVEL_WARNING, "CUSTOMER_NIT",
                        "La factura no trae el NIT del adquiriente; no se pudo verificar.")]
    if not company_nit:
        return [Finding(LEVEL_WARNING, "CUSTOMER_NIT",
                        "No hay NIT de la compania configurado (Ajustes o NIT de la "
                        "compania); no se pudo verificar el adquiriente %s." % xml_nit)]
    if not dian_xml_parser.same_nit(xml_nit, company_nit):
        return [Finding(LEVEL_ERROR, "CUSTOMER_NIT",
                        "La factura esta dirigida al NIT %s, no al de la compania (%s)."
                        % (xml_nit, company_nit),
                        {"invoice_nit": xml_nit, "company_nit": company_nit})]
    return [Finding(LEVEL_OK, "CUSTOMER_NIT", "La factura esta dirigida a la compania.")]


def _rule_cufe(ctx):
    request, parsed = ctx["request"], ctx["parsed"]
    findings = []
    captured = dian_xml_parser.normalize_cufe(request.cufe)
    from_document = dian_xml_parser.normalize_cufe(parsed.get("cufe"))

    if not captured:
        findings.append(Finding(LEVEL_ERROR, "CUFE_FORMAT",
                                "La solicitud no tiene CUFE. Es obligatorio para "
                                "identificar la factura ante la DIAN."))
        return findings
    if not dian_xml_parser.is_valid_cufe(captured):
        findings.append(Finding(LEVEL_ERROR, "CUFE_FORMAT",
                                "El CUFE no tiene el formato esperado (96 hexadecimales)."))
        return findings
    if from_document and from_document != captured:
        findings.append(Finding(LEVEL_ERROR, "CUFE_MISMATCH",
                                "El CUFE capturado (%s...) no es el del documento (%s...)."
                                % (captured[:16], from_document[:16]),
                                {"captured": captured, "document": from_document}))
    else:
        findings.append(Finding(LEVEL_OK, "CUFE_FORMAT", "El CUFE tiene el formato correcto."))
    return findings


def _rule_cufe_duplicate(ctx):
    request, env = ctx["request"], ctx["env"]
    cufe = dian_xml_parser.normalize_cufe(request.cufe)
    findings = []
    if cufe:
        other_request = env["supplier.payment.request"].sudo().search(
            [
                ("id", "!=", request.id),
                ("cufe", "=", cufe),
                ("company_id", "=", request.company_id.id),
                ("state", "in", REQUEST_ACTIVE_STATES),
            ],
            limit=1,
        )
        if other_request:
            findings.append(Finding(LEVEL_ERROR, "CUFE_DUPLICATE",
                                    "El CUFE ya fue radicado en la solicitud %s (%s)."
                                    % (other_request.name, other_request.state),
                                    {"request_id": other_request.id}))
        move = env["account.move"].sudo().search(
            [
                ("cufe", "=", cufe),
                ("company_id", "=", request.company_id.id),
                ("move_type", "in", ("in_invoice", "in_refund")),
                ("state", "!=", "cancel"),
            ],
            limit=1,
        )
        if move and move != request.move_id:
            findings.append(Finding(LEVEL_ERROR, "CUFE_DUPLICATE",
                                    "El CUFE ya esta registrado en la factura de proveedor %s."
                                    % (move.name if move.name != "/" else move.id),
                                    {"move_id": move.id}))
    if not findings:
        findings.append(Finding(LEVEL_OK, "CUFE_DUPLICATE", "El CUFE no esta repetido."))
    return findings


def _rule_invoice_ref_duplicate(ctx):
    """El numero repetido para el mismo proveedor es sospechoso aunque el CUFE
    sea otro: en modo OCR el CUFE puede venir mal leido. En la cuenta de cobro
    (sin CUFE) el numero es lo unico que identifica el documento, asi que ahi
    repetirlo es error."""
    request, env = ctx["request"], ctx["env"]
    if not request.invoice_ref:
        return []
    partner = request.partner_id.commercial_partner_id
    is_support = request.document_type == "support_doc"
    findings = []
    if is_support:
        other_request = env["supplier.payment.request"].sudo().search(
            [
                ("id", "!=", request.id),
                ("document_type", "=", "support_doc"),
                ("invoice_ref", "=", request.invoice_ref),
                ("partner_id", "=", partner.id),
                ("company_id", "=", request.company_id.id),
                ("state", "in", REQUEST_ACTIVE_STATES),
            ],
            limit=1,
        )
        if other_request:
            findings.append(Finding(LEVEL_ERROR, "INVOICE_REF_DUPLICATE",
                                    "La cuenta de cobro %s ya fue radicada en la solicitud %s."
                                    % (request.invoice_ref, other_request.name),
                                    {"request_id": other_request.id}))
    move_type = "in_refund" if request.document_type == "credit_note" else "in_invoice"
    same_ref = env["account.move"].sudo().search(
        [
            ("ref", "=", request.invoice_ref),
            ("commercial_partner_id", "=", partner.id),
            ("company_id", "=", request.company_id.id),
            ("move_type", "=", move_type),
            ("state", "!=", "cancel"),
        ],
        limit=1,
    )
    if same_ref and same_ref != request.move_id:
        findings.append(Finding(LEVEL_ERROR if is_support else LEVEL_WARNING,
                                "INVOICE_REF_DUPLICATE",
                                "Ya existe el documento %s de este proveedor con el numero %s."
                                % (same_ref.name, request.invoice_ref),
                                {"move_id": same_ref.id}))
    return findings


def _rule_po_state(ctx):
    request = ctx["request"]
    orders = request.purchase_ids
    if not orders:
        return [Finding(LEVEL_ERROR, "PO_STATE",
                        "La solicitud no tiene orden de compra. Las facturas sin orden "
                        "no se aceptan por el portal.")]
    findings = []
    for order in orders:
        # La factura que esta misma solicitud creo no cuenta como previa.
        bills = order._spr_vendor_bills() - request.move_id
        others = order._spr_open_requests() - request
        if order.state not in PO_OPEN_STATES:
            findings.append(Finding(LEVEL_ERROR, "PO_STATE",
                                    "La orden de compra %s no esta confirmada (estado: %s)."
                                    % (order.name, order.state), {"po_state": order.state}))
        elif order.invoice_status == "invoiced":
            findings.append(Finding(LEVEL_ERROR, "PO_STATE",
                                    "La orden de compra %s ya esta totalmente facturada."
                                    % order.name))
        elif bills:
            findings.append(Finding(LEVEL_ERROR, "PO_STATE",
                                    "La orden de compra %s ya tiene la factura %s. Lo que falte "
                                    "o sobre se radica como nota credito o debito sobre esa "
                                    "factura." % (order.name, ", ".join(
                                        bill.ref or bill.name for bill in bills))))
        elif others and request.document_type in ("invoice", "support_doc"):
            findings.append(Finding(LEVEL_ERROR, "PO_STATE",
                                    "La orden de compra %s ya tiene la radicacion %s en curso."
                                    % (order.name, ", ".join(others.mapped("name")))))
        elif order.partner_id.commercial_partner_id != request.partner_id.commercial_partner_id:
            findings.append(Finding(LEVEL_ERROR, "PO_STATE",
                                    "La orden de compra %s pertenece a otro proveedor."
                                    % order.name))
    if not findings:
        findings.append(Finding(LEVEL_OK, "PO_STATE",
                                "La orden de compra %s acepta facturas." % request.purchase_names))
    return findings


def _rule_note_origin(ctx):
    """Una nota credito o debito corrige una factura de este mismo proveedor."""
    request, parsed = ctx["request"], ctx["parsed"]
    origin = request.origin_move_id
    reference = parsed.get("billing_reference") or {}
    if not origin:
        hint = (" La nota referencia la factura %s, que no esta registrada." % reference["number"]
                if reference.get("number") else "")
        return [Finding(LEVEL_ERROR, "NOTE_ORIGIN",
                        "No se identifico la factura que afecta la nota.%s Seleccionela al "
                        "radicar." % hint)]
    if origin.commercial_partner_id != request.partner_id.commercial_partner_id:
        return [Finding(LEVEL_ERROR, "NOTE_ORIGIN",
                        "La factura %s es de otro proveedor." % (origin.ref or origin.name))]
    if origin.state == "cancel":
        return [Finding(LEVEL_ERROR, "NOTE_ORIGIN",
                        "La factura %s esta cancelada." % (origin.ref or origin.name))]
    if reference.get("cufe") and origin.cufe and reference["cufe"] != origin.cufe:
        return [Finding(LEVEL_ERROR, "NOTE_ORIGIN",
                        "La nota referencia otra factura (CUFE %s...), no la %s."
                        % (reference["cufe"][:16], origin.ref or origin.name))]
    return [Finding(LEVEL_OK, "NOTE_ORIGIN",
                    "La nota afecta la factura %s." % (origin.ref or origin.name))]


def _rule_credit_note_amount(ctx):
    """La nota credito no puede descontar mas de lo que queda de la factura."""
    request, env, tol = ctx["request"], ctx["env"], ctx["tolerance"]
    origin = request.origin_move_id
    if not origin:
        return []
    refunds = env["account.move"].sudo().search([
        ("reversed_entry_id", "=", origin.id),
        ("move_type", "=", "in_refund"),
        ("state", "!=", "cancel"),
        ("id", "!=", request.move_id.id),
    ])
    other_requests = env["supplier.payment.request"].sudo().search([
        ("id", "!=", request.id),
        ("document_type", "=", "credit_note"),
        ("origin_move_id", "=", origin.id),
        ("move_id", "=", False),
        ("state", "in", REQUEST_ACTIVE_STATES),
    ])
    available = (origin.amount_total - sum(refunds.mapped("amount_total"))
                 - sum(other_requests.mapped("amount_total")))
    total = request.amount_total or 0.0
    if float_compare(total, available + tol.allowed(available), precision_rounding=tol.rounding) > 0:
        return [Finding(LEVEL_ERROR, "NOTE_AMOUNT",
                        "La nota credito (%s) supera el saldo de la factura %s (%s)."
                        % (_money(total), origin.ref or origin.name, _money(available)),
                        {"total": total, "available": available})]
    return [Finding(LEVEL_OK, "NOTE_AMOUNT",
                    "La nota credito esta dentro del saldo de la factura (%s)." % _money(available))]


def _rule_debit_note_acceptance(ctx):
    """La nota debito sube el precio pactado: siempre la acepta una persona."""
    request = ctx["request"]
    if request.note_accepted_by_id:
        return [Finding(LEVEL_OK, "DEBIT_NOTE_ACCEPTANCE",
                        "Nota debito aceptada por %s." % request.note_accepted_by_id.name)]
    return [Finding(LEVEL_WARNING, "DEBIT_NOTE_ACCEPTANCE",
                    "La nota debito aumenta el valor de la factura en %s y requiere la "
                    "aceptacion del Responsable antes de registrarse."
                    % _money(request.amount_total))]


def _rule_support_doc(ctx):
    """La cuenta de cobro solo aplica a proveedores no obligados a facturar."""
    request = ctx["request"]
    if request.partner_id.commercial_partner_id.spr_support_document:
        return []
    return [Finding(LEVEL_WARNING, "SUPPORT_DOC",
                    "El proveedor no esta marcado como no obligado a facturar; revise si "
                    "debia enviar factura electronica en vez de cuenta de cobro.")]


def _rule_currency(ctx):
    request, parsed = ctx["request"], ctx["parsed"]
    currency = (parsed.get("currency") or "").upper()
    company_currency = request.company_id.currency_id.name
    if currency and currency != company_currency:
        return [Finding(LEVEL_ERROR, "CURRENCY_UNSUPPORTED",
                        "La factura viene en %s y la compania trabaja en %s. Multi-moneda "
                        "no esta soportado." % (currency, company_currency),
                        {"currency": currency})]
    return []


def _rule_amount_consistency(ctx):
    request, parsed, tol = ctx["request"], ctx["parsed"], ctx["tolerance"]
    findings = []
    total = request.amount_total or 0.0
    if float_is_zero(total, precision_rounding=tol.rounding):
        return [Finding(LEVEL_ERROR, "AMOUNT_TOTAL", "El documento tiene total cero.")]

    # Consistencia interna. En UBL DIAN el AllowanceTotalAmount a veces resume
    # descuentos de linea (ya descontados del subtotal) y a veces descuentos de
    # documento (que si restan del total). Se aceptan ambas lecturas.
    untaxed = request.amount_untaxed or 0.0
    tax = request.amount_tax or 0.0
    allowance = parsed.get("allowance_total") or 0.0
    charge = parsed.get("charge_total") or 0.0
    prepaid = parsed.get("prepaid_amount") or 0.0
    candidates = (
        untaxed + tax,
        untaxed + tax - allowance + charge,
        untaxed + tax - allowance + charge - prepaid,
    )
    if not any(tol.within(total, expected) for expected in candidates):
        findings.append(Finding(LEVEL_WARNING, "AMOUNT_INCONSISTENT",
                                "El total (%s) no cuadra con subtotal + impuestos (%s)."
                                % (_money(total), _money(untaxed + tax)),
                                {"total": total, "untaxed": untaxed, "tax": tax,
                                 "allowance": allowance, "charge": charge}))

    return findings


def _rule_po_pending(ctx):
    """El total, sin fletes, no supera lo pendiente por facturar de las ordenes.

    Los fletes no estan en la orden: se descuentan aqui y se reportan aparte
    en EXTRA_CHARGES.
    """
    request, tol = ctx["request"], ctx["tolerance"]
    orders = request.purchase_ids.filtered(lambda order: order.state in PO_OPEN_STATES)
    total = request.amount_total or 0.0
    if not orders or float_is_zero(total, precision_rounding=tol.rounding):
        return []
    tax_product, _exempt = request._tax_as_product()
    if tax_product:
        # IVA como producto (DECISIONS.md #43): se compara sin IVA y sin las
        # lineas de mayor valor IVA y flete de la orden; el IVA va en TAX_AS_PRODUCT.
        charges = sum(line.subtotal for line in request.line_ids if line.is_extra_charge)
        goods = total - (request.amount_tax or 0.0) - charges
        pending = _orders_pending_amount(orders, tax_product | (request._freight_product() or tax_product))
    else:
        charges = _extra_charges_total(request)
        goods = total - charges
        pending = _orders_pending_amount(orders)
    names = ", ".join(orders.mapped("name"))
    if float_compare(goods, pending + tol.allowed(pending), precision_rounding=tol.rounding) > 0:
        return [Finding(LEVEL_ERROR, "AMOUNT_TOTAL",
                        "El total de la factura (%s%s) supera lo pendiente por facturar de "
                        "%s (%s)."
                        % (_money(goods), ", sin fletes" if charges else "", names, _money(pending)),
                        {"total": total, "charges": charges, "pending": pending})]
    return [Finding(LEVEL_OK, "AMOUNT_TOTAL",
                    "El total (%s) esta dentro de lo pendiente de %s (%s)."
                    % (_money(goods), names, _money(pending)))]


def _rule_extra_charges(ctx):
    """Fletes y envios: con producto de flete en Ajustes no frenan la aprobacion
    (DECISIONS.md #53); sin el, la factura no se puede crear y queda observacion."""
    request = ctx["request"]
    charges = request.line_ids.filtered("is_extra_charge")
    if not charges:
        return []
    amount = _extra_charges_total(request)
    descriptions = "; ".join(charges.mapped("description"))
    freight = request._freight_product()
    if freight:
        return [Finding(LEVEL_OK, "EXTRA_CHARGES",
                        "Flete o envio por %s (%s): se registra con el producto %s."
                        % (_money(amount), descriptions, freight.display_name),
                        {"amount": amount})]
    return [Finding(LEVEL_WARNING, "EXTRA_CHARGES",
                    "La factura cobra %s de flete o envio (%s) y no hay producto de flete "
                    "en Ajustes. Configurelo y revalide."
                    % (_money(amount), descriptions),
                    {"amount": amount})]


def _rule_amount_tax(ctx):
    request, tol = ctx["request"], ctx["tolerance"]
    lines = request.line_ids
    if not lines:
        return []
    findings = []
    expected_tax = sum(line.subtotal * line.tax_rate / 100.0 for line in lines)
    if not tol.within(request.amount_tax or 0.0, expected_tax):
        findings.append(Finding(LEVEL_WARNING, "AMOUNT_TAX",
                                "Los impuestos declarados (%s) no cuadran con los de las "
                                "lineas (%s)." % (_money(request.amount_tax), _money(expected_tax)),
                                {"declared": request.amount_tax, "from_lines": expected_tax}))
    mismatched = []
    tax_product, _exempt = request._tax_as_product()
    # Con el IVA como producto la orden va al 0 % a proposito: no se compara.
    for line in lines.filtered("po_line_id") if not tax_product else []:
        po_rate = _po_line_tax_rate(line.po_line_id)
        if float_compare(line.tax_rate, po_rate, precision_digits=2) != 0:
            mismatched.append("%s (factura %.2f%%, orden %.2f%%)"
                              % (line.description, line.tax_rate, po_rate))
    if mismatched:
        findings.append(Finding(LEVEL_WARNING, "TAX_RATE_MISMATCH",
                                "El porcentaje de impuesto difiere del de la orden en: %s."
                                % "; ".join(mismatched)))
    if not findings:
        findings.append(Finding(LEVEL_OK, "AMOUNT_TAX", "Los impuestos cuadran con las lineas."))
    return findings


def _rule_tax_as_product(ctx):
    """El IVA de la factura contra la linea de mayor valor IVA de la orden (#43)."""
    request, tol = ctx["request"], ctx["tolerance"]
    tax_product, _exempt = request._tax_as_product()
    tax = request.amount_tax or 0.0
    if not tax_product or float_is_zero(tax, precision_rounding=tol.rounding):
        return []
    po_lines = request.purchase_ids.order_line.filtered(
        lambda line: line.product_id == tax_product and not line.qty_invoiced
    )
    if not po_lines:
        return [Finding(LEVEL_WARNING, "TAX_AS_PRODUCT",
                        "La factura trae IVA por %s y la orden no tiene linea de %s. Al crear "
                        "la factura se agrega a la orden; contabilidad debe confirmarlo."
                        % (_money(tax), tax_product.display_name), {"tax": tax})]
    on_order = sum(po_lines.mapped("price_subtotal"))
    if not tol.within(tax, on_order):
        return [Finding(LEVEL_WARNING, "TAX_AS_PRODUCT",
                        "El IVA de la factura (%s) no coincide con el mayor valor IVA de la "
                        "orden (%s)." % (_money(tax), _money(on_order)),
                        {"tax": tax, "on_order": on_order})]
    return [Finding(LEVEL_OK, "TAX_AS_PRODUCT",
                    "El IVA de la factura (%s) coincide con el mayor valor IVA de la orden."
                    % _money(tax))]


def _rule_lines_matched(ctx):
    request = ctx["request"]
    lines = request.line_ids
    if not lines:
        return [Finding(LEVEL_ERROR, "LINES_MATCHED",
                        "La factura no tiene lineas. Sin lineas no hay nada que facturar.")]
    unmatched = lines.filtered(lambda line: not line.po_line_id and not line.is_extra_charge)
    if unmatched:
        names = ", ".join(line.product_code or line.description for line in unmatched)
        return [Finding(LEVEL_WARNING, "LINES_MATCHED",
                        "%d de %d lineas sin emparejar con la orden de compra: %s. "
                        "El validador debe asignarlas manualmente."
                        % (len(unmatched), len(lines), names),
                        {"unmatched": len(unmatched), "total": len(lines)})]
    return [Finding(LEVEL_OK, "LINES_MATCHED",
                    "Las %d lineas estan emparejadas con la orden o son cargos adicionales."
                    % len(lines))]


def _rule_qty_and_price(ctx):
    request, tol = ctx["request"], ctx["tolerance"]
    matched = request.line_ids.filtered("po_line_id")
    if not matched:
        return []
    findings = []
    # Cantidad y precio en la unidad de la orden: si la factura cobra en otra
    # unidad, el factor de unidad las convierte (DECISIONS.md #51).
    converted = matched.filtered(lambda line: float_compare(line.uom_factor, 1.0, precision_digits=6) != 0)
    if converted:
        findings.append(Finding(LEVEL_OK, "UOM_CONVERTED",
                                "Cantidades llevadas a la unidad de la orden: %s."
                                % "; ".join("%s (x %g: %g a %g)"
                                            % (line.description, line.uom_factor,
                                               line.quantity, line.po_quantity)
                                            for line in converted)))
    # Varias lineas de factura pueden apuntar a la misma linea de OC: se suman.
    qty_by_po_line = {}
    for line in matched:
        qty_by_po_line[line.po_line_id] = qty_by_po_line.get(line.po_line_id, 0.0) + line.po_quantity

    over = []
    for po_line, qty in qty_by_po_line.items():
        remaining = po_line.product_qty - po_line.qty_invoiced
        rounding = po_line.product_uom.rounding or 0.01
        if float_compare(qty, remaining, precision_rounding=rounding) > 0:
            over.append("%s (factura %.2f, pendiente %.2f)"
                        % (po_line.name, qty, remaining))
    if over:
        findings.append(Finding(LEVEL_ERROR, "QTY_OVER_PO",
                                "La cantidad facturada supera lo pendiente de la orden en: %s."
                                % "; ".join(over)))

    price_diff = []
    for line in matched:
        po_price = line.po_line_id.price_unit
        if not tol.within_pct(line.po_price_unit, po_price):
            price_diff.append("%s (factura %s, orden %s)"
                              % (line.description, _money(line.po_price_unit), _money(po_price)))
    if price_diff:
        findings.append(Finding(LEVEL_WARNING, "PRICE_UNIT_MISMATCH",
                                "El precio unitario difiere del de la orden en: %s."
                                % "; ".join(price_diff)))
    if not over and not price_diff:
        findings.append(Finding(LEVEL_OK, "QTY_OVER_PO",
                                "Cantidades y precios coinciden con la orden."))
    return findings


def _rule_invoice_date(ctx):
    request = ctx["request"]
    if not request.invoice_date:
        return [Finding(LEVEL_WARNING, "INVOICE_DATE", "La factura no tiene fecha de emision.")]
    if request.invoice_date > date.today():
        return [Finding(LEVEL_WARNING, "INVOICE_DATE",
                        "La fecha de la factura (%s) esta en el futuro." % request.invoice_date)]
    return []


def _rule_dian_cufe_check(ctx):
    """Refleja el resultado de la consulta al catalogo DIAN.

    La consulta la hace ``request._check_dian_cufe()`` antes de las reglas.
    Aqui solo se interpreta. Como la lectura del catalogo es heuristica
    (devuelve HTML, ver ``services/dian_catalog.py``), *no encontrado* y *no
    se pudo verificar* son observaciones con el enlace para que contabilidad
    verifique a mano, nunca un error que rechace la factura. *No consultado*
    no agrega nada (DECISIONS.md #7, #26 y #31).
    """
    request = ctx["request"]
    status = request.dian_cufe_check
    manual_url = dian_catalog.manual_search_url(
        ctx["env"]["ir.config_parameter"].sudo().get_param("spr.dian_catalog_url")
    )
    if status == "not_found":
        return [Finding(LEVEL_WARNING, "DIAN_CUFE_CHECK",
                        "El CUFE no aparece en el catalogo de la DIAN. Verifiquelo "
                        "manualmente antes de aprobar: %s" % manual_url)]
    if status == "ok":
        return [Finding(LEVEL_OK, "DIAN_CUFE_CHECK", "El CUFE existe en la DIAN.")]
    if status == "error":
        return [Finding(LEVEL_WARNING, "DIAN_CUFE_CHECK",
                        "No se pudo verificar el CUFE en la DIAN (el catalogo exige "
                        "captcha o no respondio). Verifiquelo a mano en %s" % manual_url)]
    return [Finding(LEVEL_OK, "DIAN_CUFE_CHECK", "CUFE no consultado en la DIAN.")]


# Por tipo de documento: (reglas que corren siempre, reglas que necesitan datos
# de la factura: XML, OCR o captura). Sin datos solo corren las primeras.
_NOTE_ALWAYS = (
    _rule_manual_capture,
    _rule_document_type,
    _rule_cufe,
    _rule_cufe_duplicate,
    _rule_invoice_ref_duplicate,
    _rule_note_origin,
    _rule_dian_cufe_check,
)
_NOTE_DATA = (
    _rule_supplier_nit,
    _rule_customer_nit,
    _rule_currency,
    _rule_amount_consistency,
    _rule_extra_charges,
    _rule_amount_tax,
    _rule_lines_matched,
    _rule_invoice_date,
)
RULESETS = {
    "invoice": (
        (
            _rule_manual_capture,
            _rule_document_type,
            _rule_cufe,
            _rule_cufe_duplicate,
            _rule_invoice_ref_duplicate,
            _rule_po_state,
            _rule_dian_cufe_check,
        ),
        (
            _rule_supplier_nit,
            _rule_customer_nit,
            _rule_currency,
            _rule_amount_consistency,
            _rule_po_pending,
            _rule_extra_charges,
            _rule_amount_tax,
            _rule_tax_as_product,
            _rule_lines_matched,
            _rule_qty_and_price,
            _rule_invoice_date,
        ),
    ),
    # Las notas no se comparan con lo pendiente de la orden (la orden ya se
    # facturo): se comparan con la factura que corrigen.
    "credit_note": (_NOTE_ALWAYS, _NOTE_DATA + (_rule_credit_note_amount,)),
    "debit_note": (_NOTE_ALWAYS, _NOTE_DATA + (_rule_debit_note_acceptance,)),
    # Sin CUFE ni XML: no hay emisor ni adquiriente que verificar.
    "support_doc": (
        (
            _rule_support_doc,
            _rule_invoice_ref_duplicate,
            _rule_po_state,
        ),
        (
            _rule_amount_consistency,
            _rule_po_pending,
            _rule_amount_tax,
            _rule_lines_matched,
            _rule_qty_and_price,
            _rule_invoice_date,
        ),
    ),
}


def run_rules(request, parsed):
    """Ejecuta todas las reglas duras sobre un request ya extraido.

    :param request: recordset ``supplier.payment.request`` (uno solo)
    :param parsed: dict normalizado del parser/OCR
    :return: lista de ``Finding``
    """
    request.ensure_one()
    tolerance = Tolerance.from_env(request.env, request.currency_id)
    no_data = not request.line_ids and float_is_zero(
        request.amount_total or 0.0, precision_rounding=tolerance.rounding
    )
    ctx = {
        "request": request,
        "parsed": parsed or {},
        "env": request.env,
        "tolerance": tolerance,
        "no_data": no_data,
    }
    findings = []
    always, data = RULESETS.get(request.document_type) or RULESETS["invoice"]
    rules = always if no_data else always + data
    for rule in rules:
        try:
            findings.extend(rule(ctx))
        except Exception:  # noqa: BLE001 - una regla rota no debe ocultar las demas
            _logger.exception("[SPR] %s: la regla %s fallo", request.name, rule.__name__)
            findings.append(Finding(LEVEL_ERROR, "RULE_CRASHED",
                                    "La regla %s fallo al ejecutarse; revise el log."
                                    % rule.__name__.replace("_rule_", "").upper()))
    # Las advertencias del parser (cantidad cero, CUFE no coincide, etc.)
    # tambien se muestran: son datos del documento, no de las reglas.
    for warning in (parsed or {}).get("raw_warnings") or []:
        findings.append(Finding(LEVEL_WARNING, "DOCUMENT_WARNING", str(warning)))
    return findings
