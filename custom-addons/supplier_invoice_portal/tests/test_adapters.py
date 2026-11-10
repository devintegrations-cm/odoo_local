# -*- coding: utf-8 -*-
"""Tests de la Fase 4: OCR HTTP, IA (Anthropic, HTTP y puente CLI) y consulta DIAN.

Todo el trafico HTTP se simula con ``unittest.mock``: ningun test hace red.
"""

import json
from unittest.mock import Mock, patch

import requests

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.supplier_invoice_portal.services import ai_adapter, dian_catalog, ocr_adapter

from .common import SprCase

CUFE_DIRECT = (
    "5b7ed1473378afeccda8a02a8f84e8bc00b6926f9a48720e9bfa7d1a2bb1c924"
    "3a510d94519f045a6afa7d7358c3eda3"
)


def http_response(status_code=200, payload=None, text=None):
    response = Mock()
    response.status_code = status_code
    response.text = text if text is not None else json.dumps(payload or {})
    if payload is not None:
        response.json = Mock(return_value=payload)
    else:
        response.json = Mock(side_effect=ValueError("no json"))
    return response


OCR_PAYLOAD = {
    "cufe": CUFE_DIRECT,
    "invoice_ref": "SETP990000001",
    "issue_date": "2026-03-15",
    "supplier": {"nit": "900123456", "dv": "7", "name": "CAFES DEL SUR SAS"},
    "customer": {"nit": "901234567", "dv": "8", "name": "LIBERTARIO"},
    "lines": [
        {"description": "Cafe verde excelso", "code": "CAFE-001", "quantity": "100",
         "price_unit": "12000", "tax_rate": 19, "subtotal": "1200000"},
        {"description": "Empaque 500 g", "code": "EMP-500", "quantity": 50,
         "price_unit": 2000, "tax_rate": 19, "subtotal": 100000},
    ],
    "amount_untaxed": "1300000.00",
    "amount_tax": 247000,
    "amount_total": 1547000,
    "currency": "COP",
    "confidence": 0.87,
    "provider": "n8n-ocr",
}


@tagged("post_install", "-at_install")
class TestAdapters(SprCase):

    def _params(self, **values):
        params = self.env["ir.config_parameter"].sudo()
        for key, value in values.items():
            params.set_param(key, value)

    # ------------------------------------------------------------------
    # OCR
    # ------------------------------------------------------------------

    def test_ocr_http_extracts_and_validates(self):
        self._params(**{"spr.ocr_enabled": "True", "spr.ocr_url": "https://ocr.example/extract",
                        "spr.ocr_api_key": "ocr-secret"})
        order = self._standard_po()
        request = self._create_request(order, xml_file=False, xml_filename=False, cufe=CUFE_DIRECT)

        with patch.object(ocr_adapter.requests, "post",
                          return_value=http_response(200, OCR_PAYLOAD)) as post:
            request.action_validate()

        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer ocr-secret")
        self.assertIn("file", kwargs["files"])
        hint = json.loads(kwargs["data"]["hint"])
        self.assertEqual(hint["expected_po"], order.name)
        self.assertEqual(hint["expected_supplier_nit"], "900123456")

        self.assertEqual(request.source, "ocr")
        self.assertEqual(request.state, "approved", request.validation_summary)
        self.assertEqual(request.invoice_ref, "SETP990000001")
        self.assertEqual(request.amount_total, 1547000.0)
        self.assertEqual(len(request.line_ids), 2)
        self.assertTrue(all(request.line_ids.mapped("po_line_id")))

    def test_ocr_failure_leaves_manual_capture(self):
        self._params(**{"spr.ocr_enabled": "True", "spr.ocr_url": "https://ocr.example/extract"})
        order = self._standard_po()
        request = self._create_request(order, xml_file=False, xml_filename=False, cufe=CUFE_DIRECT)

        with patch.object(ocr_adapter.requests, "post",
                          side_effect=requests.ConnectionError("down")):
            request.action_validate()

        self.assertEqual(request.source, "manual")
        self.assertEqual(request.state, "warning", request.validation_summary)
        self.assertIn("MANUAL_CAPTURE_REQUIRED", self._codes(request, "warning"))
        self.assertFalse(self._codes(request, "error"))
        self.assertTrue(request.activity_ids)

        # Contabilidad captura a mano y revalida: el OCR sigue caido pero los
        # datos capturados se conservan y las reglas ya corren completas.
        request.write({
            "invoice_ref": "SETP990000001",
            "invoice_date": "2026-03-15",
            "amount_untaxed": 1300000.0,
            "amount_tax": 247000.0,
            "amount_total": 1547000.0,
            "line_ids": [
                (0, 0, {"description": "Cafe", "product_code": "CAFE-001", "quantity": 100,
                        "price_unit": 12000, "tax_rate": 19, "subtotal": 1200000}),
                (0, 0, {"description": "Empaque", "product_code": "EMP-500", "quantity": 50,
                        "price_unit": 2000, "tax_rate": 19, "subtotal": 100000}),
            ],
        })
        with patch.object(ocr_adapter.requests, "post",
                          side_effect=requests.ConnectionError("down")):
            request.action_validate()
        self.assertEqual(len(request.line_ids), 2)
        self.assertNotIn("MANUAL_CAPTURE_REQUIRED", self._codes(request))
        # Sin NIT del emisor en los datos capturados, la regla avisa pero no bloquea.
        self.assertIn("SUPPLIER_NIT", self._codes(request, "warning"))
        self.assertFalse(self._codes(request, "error"))
        self.assertTrue(all(request.line_ids.mapped("po_line_id")))
        request.action_create_bill()
        self.assertEqual(request.state, "invoiced")

    def test_ocr_http_error_status(self):
        self._params(**{"spr.ocr_enabled": "True", "spr.ocr_url": "https://ocr.example/extract"})
        order = self._standard_po()
        request = self._create_request(order, xml_file=False, xml_filename=False, cufe=CUFE_DIRECT)
        with patch.object(ocr_adapter.requests, "post", return_value=http_response(503, None, "x")):
            request.action_validate()
        self.assertEqual(request.source, "manual")
        self.assertIn("DOCUMENT_WARNING", self._codes(request, "warning"))

    def test_normalize_ocr_response_is_tolerant(self):
        result = ocr_adapter.normalize_ocr_response(
            {"lines": "no es lista", "amount_total": "1.234,5", "cufe": "zzz"}, "http"
        )
        self.assertEqual(result["lines"], [])
        self.assertEqual(result["cufe"], "")
        self.assertEqual(len(result["raw_warnings"]), 2)
        self.assertEqual(result["provider"], "http")

    # ------------------------------------------------------------------
    # IA
    # ------------------------------------------------------------------

    def _po_with_uncoded_line(self):
        other = self.env["product.product"].create({
            "name": "Bolsa generica",
            "type": "consu",
            "purchase_method": "purchase",
            "supplier_taxes_id": [(6, 0, self.tax_19.ids)],
        })
        return self._create_po([
            (self.product_cafe, 100, 12000.0),
            (other, 50, 2000.0),
        ])

    @staticmethod
    def _anthropic_reply(confidence):
        """Simula la Messages API leyendo el cuerpo enviado para responder con
        el id real de la linea de OC libre."""
        def side_effect(url, **kwargs):
            body = kwargs["json"]
            body_text = body["messages"][0]["content"].split("\n\n", 1)[1]
            payload = json.loads(body_text)
            po_line_id = payload["po_lines"][0]["id"]
            answer = {
                "matches": [{"invoice_idx": 0, "po_line_id": po_line_id,
                             "confidence": confidence, "note": "Descripcion equivalente"}],
                "unmatched_invoice": [],
                "unmatched_po": [],
                "summary_es": "Una linea emparejada por descripcion.",
            }
            return http_response(200, {
                "id": "msg_test", "type": "message", "role": "assistant",
                "model": body["model"], "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "usage": {"input_tokens": 300, "output_tokens": 80},
            })
        return side_effect

    def test_ai_anthropic_matches_uncoded_line(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_provider": "anthropic",
                        "spr.ai_api_key": "sk-ant-test", "spr.ai_model": "claude-opus-5-5"})
        order = self._po_with_uncoded_line()
        request = self._create_request(order)

        with patch.object(ai_adapter.requests, "post", side_effect=self._anthropic_reply(0.9)) as post:
            request.action_validate()

        self.assertEqual(post.call_count, 1)
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], ai_adapter.ANTHROPIC_URL)
        self.assertEqual(kwargs["headers"]["x-api-key"], "sk-ant-test")
        self.assertEqual(kwargs["headers"]["anthropic-version"], ai_adapter.ANTHROPIC_VERSION)
        body = kwargs["json"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        # Solo viaja la linea que quedo sin codigo, contra la linea de OC libre.
        sent = json.loads(body["messages"][0]["content"].split("\n\n", 1)[1])
        self.assertEqual(len(sent["invoice_lines"]), 1)
        self.assertEqual(sent["invoice_lines"][0]["code"], "EMP-500")
        self.assertEqual(len(sent["po_lines"]), 1)
        # Nunca datos de terceros.
        serialized = json.dumps(body)
        self.assertNotIn("900123456", serialized)
        self.assertNotIn("901234567", serialized)
        self.assertNotIn('"nit"', serialized)
        self.assertNotIn("CAFES DEL SUR", serialized)

        lines = request.line_ids.sorted("sequence")
        self.assertEqual(lines[0].match_method, "code")
        self.assertEqual(lines[1].match_method, "ai")
        self.assertEqual(lines[1].po_line_id, order.order_line[1])
        self.assertAlmostEqual(lines[1].match_confidence, 0.9, places=2)
        self.assertEqual(request.state, "approved", request.validation_summary)

    def test_ai_low_confidence_only_leaves_note(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_provider": "anthropic",
                        "spr.ai_api_key": "sk-ant-test"})
        order = self._po_with_uncoded_line()
        request = self._create_request(order)
        with patch.object(ai_adapter.requests, "post", side_effect=self._anthropic_reply(0.5)):
            request.action_validate()
        line = request.line_ids.sorted("sequence")[1]
        self.assertFalse(line.po_line_id)
        self.assertEqual(line.match_method, "none")
        self.assertIn("Sugerencia IA", line.match_note)
        self.assertEqual(request.state, "warning")
        self.assertIn("LINES_MATCHED", self._codes(request, "warning"))

    def test_ai_http_provider(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_provider": "http",
                        "spr.ai_url": "https://ia.example/match", "spr.ai_api_key": "tok"})
        order = self._po_with_uncoded_line()
        request = self._create_request(order)

        def side_effect(url, **kwargs):
            return http_response(200, {
                "matches": [{"invoice_idx": 0, "po_line_id": kwargs["json"]["po_lines"][0]["id"],
                             "confidence": 0.8, "note": "ok"}],
                "unmatched_invoice": [], "unmatched_po": [], "summary_es": "listo",
            })

        with patch.object(ai_adapter.requests, "post", side_effect=side_effect) as post:
            request.action_validate()
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(request.line_ids.sorted("sequence")[1].match_method, "ai")
        self.assertEqual(request.state, "approved", request.validation_summary)

    def test_ai_failure_is_not_fatal(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_provider": "anthropic",
                        "spr.ai_api_key": "sk-ant-test"})
        order = self._po_with_uncoded_line()
        request = self._create_request(order)
        with patch.object(ai_adapter.requests, "post", side_effect=requests.Timeout("slow")):
            request.action_validate()
        self.assertEqual(request.state, "warning")
        self.assertIn("LINES_MATCHED", self._codes(request, "warning"))

        # Un 401 tampoco tumba nada.
        with patch.object(ai_adapter.requests, "post",
                          return_value=http_response(401, {"error": {"message": "bad key"}})):
            request.action_validate()
        self.assertEqual(request.state, "warning")

    def test_ai_refusal_or_truncation_is_handled(self):
        adapter = ai_adapter.AnthropicAiAdapter(api_key="k")
        invoice_lines = [{"idx": 0, "description": "x", "code": "", "quantity": 1, "price_unit": 1}]
        po_lines = [{"id": 5, "description": "x", "code": "", "quantity": 1, "price_unit": 1}]
        for stop_reason in ("refusal", "max_tokens"):
            reply = http_response(200, {"stop_reason": stop_reason, "content": [], "usage": {}})
            with patch.object(ai_adapter.requests, "post", return_value=reply):
                result = adapter.match_lines(invoice_lines, po_lines, {})
            self.assertEqual(result["matches"], [])
            self.assertEqual(result["unmatched_invoice"], [0])

    def test_sanitize_response_discards_garbage(self):
        invoice_lines = [{"idx": 0}, {"idx": 1}]
        po_lines = [{"id": 10}, {"id": 11}]
        result = ai_adapter.sanitize_response({
            "matches": [
                {"invoice_idx": 0, "po_line_id": 10, "confidence": 1.7, "note": "a"},
                {"invoice_idx": 0, "po_line_id": 11, "confidence": 0.9, "note": "duplicado"},
                {"invoice_idx": 5, "po_line_id": 10, "confidence": 0.9, "note": "fuera de rango"},
                {"invoice_idx": 1, "po_line_id": 99, "confidence": 0.9, "note": "po ajena"},
                "basura",
            ],
        }, invoice_lines, po_lines, "test")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["confidence"], 1.0)
        self.assertEqual(result["unmatched_invoice"], [1])
        self.assertEqual(result["unmatched_po"], [11])

    # ------------------------------------------------------------------
    # IA por CLI local (conexion de prueba via anthropic_cli_bridge)
    # ------------------------------------------------------------------

    def _bridge_reply(self, confidence=0.93):
        """Simula el puente: misma forma que la Messages API, model = herramienta."""
        def side_effect(url, **kwargs):
            body = kwargs["json"]
            payload = json.loads(body["messages"][0]["content"].split("\n\n", 1)[1])
            answer = {
                "matches": [{"invoice_idx": 0, "po_line_id": payload["po_lines"][0]["id"],
                             "confidence": confidence, "note": "Descripcion equivalente"}],
                "unmatched_invoice": [], "unmatched_po": [],
                "summary_es": "Una linea emparejada por la CLI.",
            }
            return http_response(200, {
                "id": "msg_bridge_1", "type": "message", "role": "assistant",
                "model": body["model"], "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "usage": {"input_tokens": 2, "output_tokens": 80},
            })
        return side_effect

    def test_ai_cli_bridge_request(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_cli_enabled": "True",
                        "spr.ai_cli_tool": "claude-team",
                        "spr.ai_cli_url": "http://puente:8787/v1/messages",
                        "spr.ai_cli_api_key": "clave-puente", "spr.ai_cli_timeout": "90"})
        order = self._po_with_uncoded_line()
        request = self._create_request(order)
        with patch.object(ai_adapter.requests, "post", side_effect=self._bridge_reply()) as post:
            request.action_validate()

        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "http://puente:8787/v1/messages")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (ai_adapter.CONNECT_TIMEOUT, 90))
        self.assertEqual(kwargs["headers"]["x-api-key"], "clave-puente")
        body = kwargs["json"]
        self.assertEqual(body["model"], "claude-team")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        serialized = json.dumps(body)
        self.assertNotIn("900123456", serialized)
        self.assertNotIn("CAFES DEL SUR", serialized)

        lines = request.line_ids.sorted("sequence")
        self.assertEqual(lines[1].match_method, "ai")
        self.assertEqual(lines[1].po_line_id, order.order_line[1])
        self.assertEqual(request.state, "approved", request.validation_summary)

    def test_ai_cli_bridge_failures_are_not_fatal(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_cli_enabled": "True",
                        "spr.ai_cli_tool": "codex"})
        order = self._po_with_uncoded_line()
        request = self._create_request(order)
        failures = [
            {"side_effect": requests.ConnectionError("puente apagado")},
            {"return_value": http_response(503, {"type": "error", "error": {
                "type": "api_error", "message": "comando codex no encontrado"}})},
            {"return_value": http_response(200, {"stop_reason": "end_turn",
                                                 "content": [{"type": "text", "text": "no json"}]})},
        ]
        for failure in failures:
            with patch.object(ai_adapter.requests, "post", **failure):
                request.action_validate()
            self.assertEqual(request.state, "warning")
            self.assertIn("LINES_MATCHED", self._codes(request, "warning"))

    def test_ai_cli_test_button(self):
        settings = self.env["res.config.settings"].create({
            "spr_ai_cli_tool": "agy", "spr_ai_cli_url": "http://puente:8787/v1/messages",
            "spr_ai_cli_timeout": 30,
        })
        with patch.object(ai_adapter.requests, "post", side_effect=self._bridge_reply()) as post:
            action = settings.action_spr_ai_cli_test()
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn("agy", action["params"]["title"])
        self.assertEqual(post.call_args.kwargs["json"]["model"], "agy")
        with patch.object(ai_adapter.requests, "post", side_effect=requests.ConnectionError("x")):
            action = settings.action_spr_ai_cli_test()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("ConnectionError", action["params"]["message"])

    def test_get_ai_adapter_factory(self):
        self._params(**{"spr.ai_enabled": "False"})
        self.assertIsInstance(ai_adapter.get_ai_adapter(self.env), ai_adapter.NoopAiAdapter)
        self._params(**{"spr.ai_enabled": "True", "spr.ai_provider": "anthropic", "spr.ai_api_key": ""})
        self.assertIsInstance(ai_adapter.get_ai_adapter(self.env), ai_adapter.NoopAiAdapter)
        self._params(**{"spr.ai_api_key": "k", "spr.ai_model": "claude-sonnet-5-5", "spr.ai_timeout": "20"})
        adapter = ai_adapter.get_ai_adapter(self.env)
        self.assertIsInstance(adapter, ai_adapter.AnthropicAiAdapter)
        self.assertEqual(adapter.model, "claude-sonnet-5-5")
        self.assertEqual(adapter.timeout, 20)
        self._params(**{"spr.ai_provider": "http", "spr.ai_url": ""})
        self.assertIsInstance(ai_adapter.get_ai_adapter(self.env), ai_adapter.NoopAiAdapter)
        # La conexion de prueba por CLI manda sobre el proveedor y no pide API key.
        self._params(**{"spr.ai_cli_enabled": "True", "spr.ai_cli_tool": "agy",
                        "spr.ai_api_key": "", "spr.ai_cli_timeout": "33"})
        adapter = ai_adapter.get_ai_adapter(self.env)
        self.assertIsInstance(adapter, ai_adapter.CliAiAdapter)
        self.assertEqual(adapter.tool, "agy")
        self.assertEqual(adapter.model, "agy")
        self.assertEqual(adapter.url, ai_adapter.DEFAULT_CLI_URL)
        self.assertEqual(adapter.api_key, "local")
        self.assertEqual(adapter.timeout, 33)
        self._params(**{"spr.ai_cli_tool": "inventada"})
        self.assertEqual(ai_adapter.get_ai_adapter(self.env).tool, "claude-empresa")
        self._params(**{"spr.ai_cli_enabled": "False"})
        self.assertIsInstance(ai_adapter.get_ai_adapter(self.env), ai_adapter.NoopAiAdapter)

    # ------------------------------------------------------------------
    # Catalogo DIAN
    # ------------------------------------------------------------------

    def test_dian_interpret_response(self):
        form_html = "<html><body>Buscar documento Por favor diligencia los siguientes datos: CUFE o UUID NIT</body></html>"
        found_html = "<html><body>Documento Electronico CUFE: %s Emisor CAFES</body></html>" % CUFE_DIRECT
        self.assertEqual(dian_catalog.interpret_response(403, "bloqueado", CUFE_DIRECT)[0], "error")
        self.assertEqual(dian_catalog.interpret_response(200, "", CUFE_DIRECT)[0], "error")
        # El buscador (o un captcha) ya no significa "no existe": no se pudo verificar.
        self.assertEqual(dian_catalog.interpret_response(200, form_html, CUFE_DIRECT)[0], "error")
        captcha_html = "<html><body>Falta Token de validaci&#243;n de captcha</body></html>"
        self.assertEqual(dian_catalog.interpret_response(200, captcha_html, CUFE_DIRECT)[0], "error")
        self.assertEqual(dian_catalog.manual_search_url("https://catalogo-vpfe.dian.gov.co/document/searchqr"),
                         "https://catalogo-vpfe.dian.gov.co/User/SearchDocument")
        self.assertEqual(dian_catalog.interpret_response(200, found_html, CUFE_DIRECT)[0], "ok")
        self.assertEqual(
            dian_catalog.interpret_response(200, "<p>Documento no encontrado</p>", CUFE_DIRECT)[0],
            "not_found",
        )
        self.assertEqual(
            dian_catalog.interpret_response(200, "<p>Cualquier otra cosa</p>", CUFE_DIRECT)[0],
            "error",
        )
        self.assertEqual(
            dian_catalog.catalog_search_url("https://catalogo-vpfe.dian.gov.co", "abc"),
            "https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey=abc",
        )

    def test_dian_check_in_pipeline(self):
        self._params(**{"spr.dian_cufe_check": "True"})
        order = self._standard_po()
        form_html = "Por favor diligencia los siguientes datos: CUFE o UUID"
        found_html = "Documento CUFE %s" % CUFE_DIRECT

        request = self._create_request(order)
        with patch.object(dian_catalog.requests, "get",
                          return_value=http_response(200, None, form_html)) as get:
            request.action_validate()
        self.assertIn("documentkey=%s" % CUFE_DIRECT, get.call_args.args[0])
        # El buscador significa "no se pudo verificar": observacion con enlace manual.
        self.assertEqual(request.dian_cufe_check, "error")
        self.assertEqual(request.state, "warning")
        self.assertIn("DIAN_CUFE_CHECK", self._codes(request, "warning"))
        self.assertIn("/User/SearchDocument", request.validation_summary)

        # "No encontrado" explicito tambien es observacion.
        with patch.object(dian_catalog.requests, "get",
                          return_value=http_response(200, None, "Documento no encontrado")):
            request.action_check_dian()
        self.assertEqual(request.dian_cufe_check, "not_found")

        # Reconsulta manual: ahora si aparece, y el resumen se refresca sin cambiar el estado.
        with patch.object(dian_catalog.requests, "get",
                          return_value=http_response(200, None, found_html)):
            request.action_check_dian()
        self.assertEqual(request.dian_cufe_check, "ok")
        self.assertNotIn("DIAN_CUFE_CHECK", self._codes(request, "warning"))
        self.assertIn("El CUFE existe en la DIAN", request.validation_summary)
        self.assertEqual(request.state, "warning")

        request2 = self._create_request(self._standard_po())
        request2.cufe = "b" * 96  # otro CUFE para no chocar con el duplicado
        with patch.object(dian_catalog.requests, "get",
                          side_effect=requests.ConnectionError("down")):
            request2.action_validate()
        self.assertEqual(request2.dian_cufe_check, "error")
        # El XML trae otro CUFE distinto al capturado: eso si es error (CUFE_MISMATCH),
        # pero la consulta DIAN fallida no aporta ningun error.
        self.assertNotIn("DIAN_CUFE_CHECK", self._codes(request2, "error"))

    def test_dian_check_can_be_disabled_from_settings(self):
        # Odoo borra el parametro cuando un Boolean queda en False; el modulo
        # lo guarda a mano para que "desmarcado" no vuelva a ser "activado".
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("spr.dian_cufe_check", False)  # como si nunca se hubiera guardado
        settings = self.env["res.config.settings"].create({})
        self.assertFalse(settings.spr_dian_cufe_check)  # apagado por defecto (DECISIONS #31)
        request = self._create_request(self._standard_po())
        self.assertFalse(request._dian_check_enabled())
        settings.spr_dian_cufe_check = True
        settings.set_values()
        self.assertEqual(params.get_param("spr.dian_cufe_check"), "True")
        self.assertTrue(self.env["res.config.settings"].create({}).spr_dian_cufe_check)
        self.assertTrue(request._dian_check_enabled())
        settings.spr_dian_cufe_check = False
        settings.set_values()
        self.assertEqual(params.get_param("spr.dian_cufe_check"), "False")
        self.assertFalse(request._dian_check_enabled())

    def test_ocr_bad_issue_date_is_discarded(self):
        parsed = ocr_adapter.normalize_ocr_response(dict(OCR_PAYLOAD, issue_date="15/03/2026"), "test")
        self.assertEqual(parsed["issue_date"], "")
        self.assertTrue(any("fecha" in w.lower() for w in parsed["raw_warnings"]))
        parsed = ocr_adapter.normalize_ocr_response(dict(OCR_PAYLOAD, issue_date="2026-03-15T10:00:00"), "test")
        self.assertEqual(parsed["issue_date"], "2026-03-15")

    def test_dian_check_disabled_is_skipped(self):
        order = self._standard_po()
        request = self._create_request(order)
        with patch.object(dian_catalog.requests, "get") as get:
            request.action_validate()
        get.assert_not_called()
        self.assertEqual(request.dian_cufe_check, "skipped")

    # ------------------------------------------------------------------
    # Revision de unidades con IA (DECISIONS.md #51)
    # ------------------------------------------------------------------

    def _ai_on(self):
        self._params(**{"spr.ai_enabled": "True", "spr.ai_provider": "anthropic",
                        "spr.ai_api_key": "sk-ant-test"})

    @staticmethod
    def _units_reply(factor, confidence=0.95):
        """La Messages API: responde factores a la revision de unidades."""
        def side_effect(url, **kwargs):
            body = kwargs["json"]
            if body["system"] != ai_adapter.UNITS_SYSTEM_PROMPT:
                raise AssertionError("Solo se esperaba la revision de unidades")
            sent = json.loads(body["messages"][0]["content"].split("\n\n", 1)[1])
            answer = {
                "lines": [{"idx": line["idx"], "factor": factor, "confidence": confidence,
                           "note": "Bulto contra kilo"} for line in sent["lines"]],
                "summary_es": "Unidades distintas.",
            }
            return http_response(200, {
                "stop_reason": "end_turn", "model": body["model"],
                "content": [{"type": "text", "text": json.dumps(answer)}], "usage": {},
            })
        return side_effect

    def test_units_reviewed_by_ai_when_price_does_not_match(self):
        # La orden pide 200 a 6.000; la factura cobra 100 a 12.000: mismo total.
        self._ai_on()
        order = self._create_po([
            (self.product_cafe, 200, 6000.0),
            (self.product_bag, 50, 2000.0),
        ])
        request = self._create_request(order)
        with patch.object(ai_adapter.requests, "post", side_effect=self._units_reply(2)) as post:
            request.action_validate()
        self.assertEqual(post.call_count, 1)
        sent = json.loads(post.call_args.kwargs["json"]["messages"][0]["content"].split("\n\n", 1)[1])
        self.assertEqual(len(sent["lines"]), 1, "Solo viaja la linea que no cuadra.")
        self.assertEqual(sent["lines"][0]["unit_code"], "KGM")
        self.assertNotIn("900123456", json.dumps(sent))

        self.assertEqual(request.state, "approved", request.validation_summary)
        self.assertIn("UOM_CONVERTED", self._codes(request, "ok"))
        line = request.line_ids.filtered(lambda line: line.product_code == "CAFE-001")
        self.assertEqual(line.uom_factor, 2.0)
        self.assertEqual(line.po_quantity, 200.0)

        request.action_create_bill()
        bill_line = request.move_id.invoice_line_ids.filtered(
            lambda line: line.product_id == self.product_cafe)
        self.assertEqual(bill_line.quantity, 200.0)
        self.assertEqual(bill_line.price_unit, 6000.0)
        self.assertEqual(bill_line.purchase_line_id.qty_invoiced, 200.0)

    def test_units_fix_qty_over_po(self):
        # La orden pide 50 a 24.000; la factura cobra 100 a 12.000: sin
        # conversion seria QTY_OVER_PO (rechazada).
        self._ai_on()
        order = self._create_po([
            (self.product_cafe, 50, 24000.0),
            (self.product_bag, 50, 2000.0),
        ])
        request = self._create_request(order)
        with patch.object(ai_adapter.requests, "post", side_effect=self._units_reply(0.5)):
            request.action_validate()
        self.assertEqual(request.state, "approved", request.validation_summary)

    def test_units_factor_that_does_not_fit_the_price_is_ignored(self):
        self._ai_on()
        order = self._create_po([
            (self.product_cafe, 200, 6000.0),
            (self.product_bag, 50, 2000.0),
        ])
        request = self._create_request(order)
        with patch.object(ai_adapter.requests, "post", side_effect=self._units_reply(3)):
            request.action_validate()
        self.assertEqual(request.state, "warning")
        self.assertIn("PRICE_UNIT_MISMATCH", self._codes(request, "warning"))
        self.assertEqual(request.line_ids.mapped("uom_factor"), [1.0, 1.0])
        # Con confianza baja tampoco se aplica.
        with patch.object(ai_adapter.requests, "post", side_effect=self._units_reply(2, 0.4)):
            request.action_validate()
        self.assertEqual(request.state, "warning")

    def test_units_not_asked_when_everything_matches(self):
        self._ai_on()
        request = self._create_request(self._standard_po())
        with patch.object(ai_adapter.requests, "post") as post:
            request.action_validate()
        post.assert_not_called()
        self.assertEqual(request.state, "approved")

    def test_validate_ai_action(self):
        order = self._create_po([
            (self.product_cafe, 200, 6000.0),
            (self.product_bag, 50, 2000.0),
        ])
        request = self._create_request(order)
        request.action_validate()
        self.assertEqual(request.state, "warning")
        # Sin IA configurada la accion lo dice en vez de fingir que valido.
        with self.assertRaises(UserError):
            request.action_validate_ai()

        self._ai_on()
        with patch.object(ai_adapter.requests, "post", side_effect=self._units_reply(2)):
            request.action_validate_ai()
        self.assertEqual(request.state, "approved", request.validation_summary)
        # La accion de servidor llama al mismo metodo.
        action = self.env.ref("supplier_invoice_portal.action_server_spr_validate_ai")
        self.assertEqual(action.binding_model_id.model, "supplier.payment.request")

        request.action_create_bill()
        with self.assertRaises(UserError):
            request.action_validate_ai()
