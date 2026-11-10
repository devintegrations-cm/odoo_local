# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

from ..services import ai_adapter


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # --- Contabilidad -------------------------------------------------
    spr_default_journal_id = fields.Many2one(
        "account.journal",
        string="Diario de compras por defecto",
        domain="[('type', '=', 'purchase')]",
        config_parameter="spr.default_journal_id",
    )
    spr_amount_tolerance_pct = fields.Float(
        string="Tolerancia de monto (%)",
        default=0.5,
        config_parameter="spr.amount_tolerance_pct",
        help="Diferencia porcentual admitida entre el total de la factura y el "
             "pendiente por facturar de la orden de compra.",
    )
    spr_amount_tolerance_abs = fields.Float(
        string="Tolerancia de monto (absoluta)",
        default=1000.0,
        config_parameter="spr.amount_tolerance_abs",
        help="Se acepta la diferencia si esta dentro del porcentaje O dentro de "
             "este monto, lo que resulte mas permisivo.",
    )
    spr_company_nit = fields.Char(
        string="NIT de la compania",
        config_parameter="spr.company_nit",
        help="Si se deja vacio se usa el NIT (campo NIF/RUT) de la compania activa.",
    )
    # Sin config_parameter: Odoo borra el parametro cuando un Boolean queda en
    # False y "ausente" debe seguir siendo "activado". Ver get_values/set_values.
    spr_party_check = fields.Boolean(
        string="Verificar emisor y receptor",
        default=True,
        help="Compara el NIT del emisor con el del proveedor y el NIT del "
             "adquiriente con el de la compania, y muestra emisor y adquiriente "
             "(NIT y razon social) en la solicitud. Desactivado, esas dos reglas "
             "no corren y los campos se ocultan: una factura a nombre de otro "
             "proveedor o de otra empresa ya no se rechaza por eso.",
    )
    spr_support_journal_id = fields.Many2one(
        "account.journal",
        string="Diario de documento soporte",
        domain="[('type', '=', 'purchase')]",
        config_parameter="spr.support_journal_id",
        help="Diario de compras con la resolucion DIAN de documento soporte. Con el se "
             "registran las cuentas de cobro de proveedores no obligados a facturar.",
    )
    spr_freight_product_id = fields.Many2one(
        "product.product",
        string="Producto de flete",
        config_parameter="spr.freight_product_id",
        help="Producto (normalmente un servicio) con el que se registran en la factura "
             "los fletes y cargos de envio que no estan en la orden de compra.",
    )
    # IVA como producto (DECISIONS.md #43): las lineas van con impuesto 0 % y el
    # IVA de la factura entra como una linea del producto de mayor valor IVA.
    spr_tax_as_product = fields.Boolean(
        string="IVA como producto (mayor valor IVA)",
        help="Las lineas de la orden y de la factura van con el impuesto 0 % de abajo, "
             "y el IVA total de la factura se registra como una linea del producto de "
             "mayor valor IVA, que se agrega a la orden si no la tiene.",
    )
    spr_tax_product_id = fields.Many2one(
        "product.product",
        string="Producto de mayor valor IVA",
        config_parameter="spr.tax_product_id",
        help="Producto con el que se registra el IVA de la factura, por ejemplo "
             "[MAYVALIVACOM191] Mayor Valor Iva Compras 19-15-8-5.",
    )
    spr_exempt_tax_id = fields.Many2one(
        "account.tax",
        string="Impuesto de las lineas",
        domain="[('type_tax_use', '=', 'purchase')]",
        config_parameter="spr.exempt_tax_id",
        help="Impuesto que llevan todas las lineas de la orden y de la factura cuando el "
             "IVA va como producto, normalmente IVA Compra Exento 0 %.",
    )
    # Cierre de fin de mes. El Boolean va a mano como los de abajo: activo por
    # defecto y Odoo borra el parametro cuando queda en False.
    spr_cutoff_enabled = fields.Boolean(
        string="Cierre de radicacion de fin de mes",
        default=True,
        help="El ultimo dia habil de cada mes, desde la hora de corte, el portal no "
             "recibe facturas hasta el primer dia del mes siguiente. Dias habiles: lunes "
             "a viernes sin festivos de Colombia.",
    )
    spr_cutoff_hour = fields.Float(
        string="Hora de corte",
        default=12.0,
        config_parameter="spr.cutoff_hour",
        help="Hora local (zona horaria de la compania, por defecto Bogota).",
    )
    spr_validator_group_id = fields.Many2one(
        "res.groups",
        string="Grupo validador",
        config_parameter="spr.validator_group_id",
        help="Grupo que recibe las actividades cuando una solicitud queda "
             "aprobada o con observaciones.",
    )

    # --- Consulta DIAN ------------------------------------------------
    # Sin config_parameter a proposito: Odoo borra el parametro cuando un
    # Boolean queda en False. Se guarda a mano como "True"/"False" en
    # get_values/set_values. Apagado por defecto: el catalogo exige captcha
    # desde 2026 (DECISIONS.md #31).
    spr_dian_cufe_check = fields.Boolean(
        string="Consultar el CUFE en el catalogo DIAN",
        default=False,
        help="El catalogo publico de la DIAN exige captcha desde 2026, asi que la "
             "consulta automatica solo puede dejar la observacion 'no se pudo "
             "verificar' con el enlace para revisarlo a mano. Actívela solo si la "
             "DIAN vuelve a permitir la consulta directa.",
    )
    spr_dian_catalog_url = fields.Char(
        string="URL del catalogo DIAN",
        default="https://catalogo-vpfe.dian.gov.co",
        config_parameter="spr.dian_catalog_url",
    )

    # --- OCR ----------------------------------------------------------
    spr_ocr_enabled = fields.Boolean(
        string="Habilitar OCR del PDF",
        config_parameter="spr.ocr_enabled",
        help="Solo se usa cuando el proveedor no adjunta el XML DIAN.",
    )
    spr_ocr_url = fields.Char(string="URL del servicio OCR", config_parameter="spr.ocr_url")
    spr_ocr_api_key = fields.Char(
        string="API key del OCR", config_parameter="spr.ocr_api_key"
    )
    spr_ocr_timeout = fields.Integer(
        string="Timeout del OCR (s)", default=60, config_parameter="spr.ocr_timeout"
    )

    # --- IA -----------------------------------------------------------
    spr_ai_enabled = fields.Boolean(
        string="Habilitar emparejamiento por IA", config_parameter="spr.ai_enabled"
    )
    spr_ai_provider = fields.Selection(
        [("anthropic", "Anthropic"), ("http", "Servicio HTTP propio")],
        string="Proveedor de IA",
        default="anthropic",
        config_parameter="spr.ai_provider",
    )
    spr_ai_url = fields.Char(string="URL del servicio de IA", config_parameter="spr.ai_url")
    spr_ai_api_key = fields.Char(string="API key de IA", config_parameter="spr.ai_api_key")
    spr_ai_model = fields.Char(
        string="Modelo",
        default="claude-opus-5-5",
        help="Modelo de Anthropic. claude-opus-5-5 es el recomendado; "
             "claude-sonnet-5-5 cuesta menos por token si el volumen lo justifica.",
        config_parameter="spr.ai_model",
    )
    spr_ai_timeout = fields.Integer(
        string="Timeout de IA (s)", default=45, config_parameter="spr.ai_timeout"
    )

    # --- IA: conexion de prueba por CLI local --------------------------
    spr_ai_cli_enabled = fields.Boolean(
        string="Conexion de prueba por CLI local",
        config_parameter="spr.ai_cli_enabled",
        help="En lugar de Anthropic, llama al puente local anthropic_cli_bridge, "
             "que ejecuta una CLI de IA (claude, agy o codex) con la sesion ya "
             "iniciada en el equipo del administrador. Pensado para desarrollo, "
             "no para produccion.",
    )
    spr_ai_cli_tool = fields.Selection(
        [
            ("claude-empresa", "claude-empresa"),
            ("claude-team", "claude-team"),
            ("claude-personal", "claude-personal"),
            ("agy", "agy"),
            ("codex", "codex"),
        ],
        string="Herramienta",
        default="claude-empresa",
        config_parameter="spr.ai_cli_tool",
    )
    spr_ai_cli_url = fields.Char(
        string="URL del puente",
        default=ai_adapter.DEFAULT_CLI_URL,
        config_parameter="spr.ai_cli_url",
        help="Donde escucha anthropic_cli_bridge. Desde Docker, host.docker.internal "
             "es el equipo anfitrion.",
    )
    spr_ai_cli_api_key = fields.Char(
        string="API key del puente",
        config_parameter="spr.ai_cli_api_key",
        help="Solo si el puente se arranco con BRIDGE_API_KEY.",
    )
    spr_ai_cli_timeout = fields.Integer(
        string="Timeout de la CLI (s)", default=180, config_parameter="spr.ai_cli_timeout"
    )

    # Booleans guardados a mano como "True"/"False": (campo, parametro, defecto).
    _SPR_MANUAL_BOOLEANS = (
        ("spr_dian_cufe_check", "spr.dian_cufe_check", "False"),
        ("spr_party_check", "spr.party_check", "True"),
        ("spr_cutoff_enabled", "spr.cutoff_enabled", "True"),
        ("spr_tax_as_product", "spr.tax_as_product", "False"),
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        params = self.env["ir.config_parameter"].sudo()
        for field_name, key, default in self._SPR_MANUAL_BOOLEANS:
            res[field_name] = str(params.get_param(key, default)).lower() in ("true", "1")
        return res

    def set_values(self):
        super().set_values()
        params = self.env["ir.config_parameter"].sudo()
        for field_name, key, _default in self._SPR_MANUAL_BOOLEANS:
            params.set_param(key, "True" if self[field_name] else "False")

    def action_spr_ai_cli_test(self):
        """Llama al puente con una linea de ejemplo y muestra el resultado."""
        self.ensure_one()
        adapter = ai_adapter.CliAiAdapter(
            tool=self.spr_ai_cli_tool, url=self.spr_ai_cli_url,
            api_key=self.spr_ai_cli_api_key, timeout=self.spr_ai_cli_timeout,
        )
        invoice_lines = [{
            "idx": 0, "description": "Cafe verde excelso saco 70 kg", "code": "",
            "quantity": 10, "price_unit": 2500000,
        }]
        po_lines = [{
            "id": 1, "description": "CAFE VERDE EXCELSO 70KG", "code": "CV-70",
            "quantity": 10, "price_unit": 2500000,
        }]
        result = adapter.match_lines(invoice_lines, po_lines, {"currency": "COP"})
        ok = bool(result.get("matches"))
        if ok:
            title = _("Conexion con %s correcta") % self.spr_ai_cli_tool
        else:
            title = _("Conexion con %s fallida") % self.spr_ai_cli_tool
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": result.get("summary_es") or "",
                "type": "success" if ok else "danger",
                "sticky": not ok,
            },
        }
