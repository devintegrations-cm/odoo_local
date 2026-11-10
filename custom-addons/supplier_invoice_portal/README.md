# Portal de facturas de proveedor (`supplier_invoice_portal`)

La guia para contabilidad y compras, sin parte tecnica, esta en
`GUIA_OPERACION.md`. Las decisiones de diseno estan en `DECISIONS.md`.

Permite que los proveedores de Libertario Coffee Roasters soliciten el pago de
una factura electronica DIAN asociandola a una o varias ordenes de compra, y
radiquen notas credito, notas debito y cuentas de cobro (documento soporte),
con validacion
automatica (reglas duras + IA para el emparejamiento de lineas) y creacion de la
factura de proveedor en borrador.

- **Odoo:** 17.0 (Community o Enterprise)
- **Dependencias Odoo:** `purchase`, `account`, `portal`, `mail`
- **Dependencias Python:** `lxml` (incluida en Odoo), `pypdf` (opcional)

El nombre del modulo y todos sus nombres tecnicos son genericos a proposito:
esta pensado para instalarse tambien en otras instancias de Odoo del grupo.

## Arquitectura

```
Proveedor (usuario portal)
   |  /my/payment-requests  (controlador portal + QWeb)
   v
supplier.payment.request  --> pipeline de validacion
      |- 1. Parser XML UBL 2.1 DIAN   (determinista, dentro del modulo)
      |- 2. OCR del PDF               (SOLO si no hay XML) --> servicio HTTP externo
      |- 3. Reglas duras              (NITs, totales, CUFE duplicado, OC abierta)
      |- 4. Matching de lineas        (codigo/SKU -> IA)
      \- 5. Veredicto + JSON de resultados
   v
account.move (in_invoice, borrador, lineas ligadas a purchase.order.line)
```

Principios que no se negocian:

1. **Lo contable se valida con reglas, no con IA.** La IA solo empareja lineas y
   redacta observaciones.
2. **OCR e IA son servicios externos detras de un adaptador.** El modulo funciona
   sin ninguno de los dos (modo degradado).
3. **La respuesta de IA/OCR nunca crea ni modifica registros.** Se guarda como
   JSON y pasa por las reglas duras.
4. Todo el estado vive en Odoo. Los servicios externos son stateless.

## Estado de las fases

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Manifest, modelos, seguridad, ajustes, parser XML + tests | **Hecha** |
| 2 | Reglas duras, matching por codigo, vistas backend, creacion de factura | **Hecha** |
| 3 | Portal del proveedor con validacion en el envio y notificaciones | **Hecha** |
| 4 | Adaptadores OCR (HTTP) e IA (Anthropic/HTTP) + consulta CUFE DIAN | **Hecha** |
| 5 | Pulido, datos demo, conexion IA por CLI local, documentacion final | **Hecha** |
| 17.0.1.1.0 | Ajustes del comite: notas, varias ordenes, fletes, documento soporte, requisitos de habilitacion, cierre de fin de mes, instructivo (DECISIONS #33-#39) | **Hecha**, sin desplegar |

## Instalacion

### En Odoo.sh

1. Poner el modulo en la carpeta de addons del repositorio.
2. Agregar al `requirements.txt` de la **raiz del repositorio**:
   ```
   pypdf
   ```
   `lxml` ya viene con Odoo. No se requiere ningun paquete de sistema: no hay
   tesseract ni poppler, el OCR es un servicio externo.
3. Hacer push a la rama y esperar la construccion.
4. Instalar el modulo desde *Aplicaciones*.

### En el entorno local (Docker)

```bash
cd odoodkr17
docker start odoodkr17-db-1
docker run --rm --network odoodkr17_webnet \
  -v "$PWD/config:/etc/odoo" \
  -v "$PWD/addons:/mnt/extra-addons/custom-addons" \
  odoo:17.0 odoo -c /etc/odoo/odoo.conf -d spr_test \
  -i supplier_invoice_portal --stop-after-init --without-demo=all
```

## Configuracion

Bloque *Portal de facturas de proveedor* dentro de *Ajustes -> Compras*. Lo ve
quien pueda abrir los ajustes de Compras (administrador de Compras); no tiene
restriccion de grupo propia.

| Ajuste | Parametro | Por defecto |
|---|---|---|
| Diario de compras por defecto | `spr.default_journal_id` | primer diario de compras |
| Tolerancia de monto (%) | `spr.amount_tolerance_pct` | 0.5 |
| Tolerancia de monto (absoluta) | `spr.amount_tolerance_abs` | 1000 |
| NIT de la compania | `spr.company_nit` | el de la compania activa |
| Verificar emisor y receptor | `spr.party_check` | activado |
| Grupo validador | `spr.validator_group_id` | grupo *Validador* del modulo |
| Diario de documento soporte | `spr.support_journal_id` | vacio (obligatorio para cuentas de cobro) |
| Producto de flete | `spr.freight_product_id` | vacio (obligatorio para facturas con flete) |
| Cierre de radicacion de fin de mes / hora de corte | `spr.cutoff_enabled` / `spr.cutoff_hour` | activado / 12:00 |
| Consultar CUFE en la DIAN | `spr.dian_cufe_check` | desactivado (el catalogo exige captcha) |
| URL del catalogo DIAN | `spr.dian_catalog_url` | `https://catalogo-vpfe.dian.gov.co` |
| OCR habilitado / URL / API key / timeout | `spr.ocr_*` | desactivado / 60 s |
| IA habilitada / proveedor / URL / API key / modelo / timeout | `spr.ai_*` | desactivado / `anthropic` / `claude-opus-5-5` / 45 s |
| Conexion de prueba por CLI local / herramienta / URL del puente / API key / timeout | `spr.ai_cli_*` | desactivado / `claude-empresa` / `http://host.docker.internal:8787/v1/messages` / vacia / 180 s |

Las API keys se guardan en `ir.config_parameter`, se muestran enmascaradas y
nunca se escriben en el log.

### Habilitar un proveedor

1. Si el proveedor esta repetido (mismo NIT en otro tercero), conviene
   fusionarlo; no es obligatorio (DECISIONS #37).
2. Abrir el contacto comercial del proveedor (empresa o persona natural sin
   padre) y marcar **Puede radicar facturas en el portal** (visible para el
   grupo *Responsable*, junto al NIT; exige NIT). Si no esta obligado a
   facturar, marcar **No obligado a facturar (documento soporte)**.
3. Darle acceso al portal a cada contacto que radica, desde *Accion ->
   Conceder acceso al portal*.
4. **Enviar instructivo del portal** (boton en la ficha).

Solo los proveedores con ese indicador podran radicar facturas. El indicador
se lee del contacto **comercial** (`commercial_partner_id`), asi que basta
marcarlo una vez aunque el proveedor tenga varios usuarios de portal.

## Portal del proveedor

| Ruta | Que hace |
|---|---|
| `/my` | Tarjeta *Facturas radicadas* con el contador, en la seccion de proveedor |
| `/my/payment-requests` | Lista con filtros (en revision, rechazadas, registradas) y orden |
| `/my/payment-requests/new` | Formulario de radicacion |
| `/my/payment-requests/<id>` | Detalle: estado, montos, lineas, hallazgos, PDF y XML. Acepta `access_token` para el enlace del correo |

**Radicar**: el proveedor elige el tipo de documento (enlaces
`?document_type=`, sin JavaScript): factura, nota credito, nota debito o, si
no esta obligado a facturar, cuenta de cobro. En factura y cuenta de cobro
marca una o varias de sus ordenes de compra abiertas (confirmadas y no
facturadas del todo); en las notas, la factura que corrige (o se toma del
XML). Adjunta el PDF (obligatorio, maximo 10 MB, se comprueba la firma `%PDF`)
y el XML de la DIAN (opcional pero recomendado; debe ser del tipo elegido). El
CUFE se digita solo si no hay XML; si hay ambos, deben coincidir. La cuenta de
cobro pide numero, fecha y total en vez de XML y CUFE. El ultimo dia habil del
mes, desde la hora de corte, el formulario no se muestra (DECISIONS #38). Todo se valida **antes** de crear el registro: un formulario con
errores no deja nada en la base. Al enviar, el pipeline corre de inmediato y el
proveedor ve el veredicto en el detalle.

Lo que el proveedor ve del resultado son los hallazgos de nivel *error* y
*observacion* con su mensaje en espanol. Los hallazgos *ok* y el JSON tecnico
no salen del backend.

El controlador (`controllers/portal.py`) corre con `sudo` todo lo que toca
ordenes de compra y el pipeline, y filtra siempre por el proveedor conectado.
El id de la orden que llega del formulario se valida contra la lista de
ordenes abiertas de ese proveedor; nunca se usa directo.

### Notificaciones al proveedor

Plantillas en `data/mail_template_data.xml` (editables desde *Ajustes ->
Tecnico -> Plantillas de correo*, no se sobreescriben al actualizar):

| Plantilla | Cuando |
|---|---|
| *Recibida* | Primera validacion con veredicto aprobada o con observaciones |
| *No paso las validaciones* | Primera validacion con veredicto rechazada; lista los errores |
| *Rechazada por contabilidad* | El Responsable rechaza con motivo |
| *Factura registrada* | Se crea la factura borrador |

Se envian con `message_post_with_source`, asi que quedan en el chatter de la
solicitud. Las revalidaciones del equipo no generan correo: al proveedor se le
escribe solo cuando cambia algo para el. Van al contacto comercial y al
contacto que radico; si ninguno tiene correo no se envia nada y se deja un
INFO en el log.

La plantilla *Portal de facturas: instructivo para el proveedor* (modelo
`res.partner`) se abre con el boton **Enviar instructivo del portal** de la
ficha del proveedor.

Al equipo interno se le sigue avisando con una actividad para el grupo
validador cuando la solicitud queda aprobada o con observaciones.

## Reglas duras de validacion

Corren en cada validacion (`services/validation_rules.py`), con un juego de
reglas por tipo de documento (`RULESETS`): las notas no se comparan con lo
pendiente de la orden y la cuenta de cobro no tiene CUFE ni NIT que verificar.
El veredicto es
*Rechazada* si hay algun **error**, *Con observaciones* si solo hay
**observaciones**, *Aprobada* si todo esta correcto. El detalle queda en la
pestana *Validacion* del request y en `validation_json`.

| Codigo | Nivel | Que revisa |
|---|---|---|
| `SUPPLIER_NIT` | error | El NIT del emisor es el del proveedor (sin DV, ver DECISIONS #3). Solo si *Verificar emisor y receptor* esta activo |
| `CUSTOMER_NIT` | error | La factura esta dirigida al NIT de la compania (`spr.company_nit` o el NIT de la compania). Solo si *Verificar emisor y receptor* esta activo |
| `CUFE_FORMAT` | error | Hay CUFE y tiene 96 hexadecimales |
| `CUFE_MISMATCH` | error | El CUFE capturado es el mismo del XML |
| `CUFE_DUPLICATE` | error | El CUFE no esta en otra solicitud activa ni en una factura de proveedor |
| `INVOICE_REF_DUPLICATE` | observacion | Ya hay una factura de ese proveedor con el mismo numero |
| `INVOICE_REF_DUPLICATE` | error | Cuenta de cobro: el numero ya se radico o registro para ese proveedor |
| `DOCUMENT_TYPE` | error | El XML es del tipo con que se radico (factura, nota credito, nota debito) |
| `PO_STATE` | error | Hay al menos una OC y cada una esta confirmada, no esta totalmente facturada y es del proveedor |
| `CURRENCY_UNSUPPORTED` | error | La factura viene en la moneda de la compania |
| `AMOUNT_TOTAL` | error | El total sin fletes no supera lo pendiente por facturar de las OC, calculado linea por linea (+ tolerancia) |
| `EXTRA_CHARGES` | OK / observacion | La factura cobra fletes o envios que no estan en la OC: OK con producto de flete en Ajustes, observacion sin el (DECISIONS #35, #53) |
| `NOTE_ORIGIN` | error | Notas: se identifico la factura que corrigen y es de este proveedor |
| `NOTE_AMOUNT` | error | Nota credito: no supera el saldo de la factura |
| `DEBIT_NOTE_ACCEPTANCE` | observacion | Nota debito: pendiente de aceptacion del Responsable |
| `SUPPORT_DOC` | observacion | Cuenta de cobro de un proveedor no marcado como no obligado a facturar |
| `AMOUNT_INCONSISTENT` | observacion | Subtotal + impuestos (- descuentos + recargos) cuadra con el total |
| `AMOUNT_TAX` | observacion | Los impuestos declarados cuadran con los de las lineas |
| `TAX_RATE_MISMATCH` | observacion | El % de impuesto de cada linea es el de la linea de OC |
| `LINES_MATCHED` | observacion / error | Todas las lineas tienen linea de OC (error si no hay lineas) |
| `QTY_OVER_PO` | error | La cantidad facturada no supera lo pendiente de cada linea de OC |
| `PRICE_UNIT_MISMATCH` | observacion | El precio unitario es el de la OC (+ tolerancia, solo porcentual) |
| `INVOICE_DATE` | observacion | Hay fecha de factura y no esta en el futuro |
| `DIAN_CUFE_CHECK` | observacion | Solo si la consulta esta activa: *no encontrado* o *no se pudo verificar*, con enlace al buscador de la DIAN. Nunca rechaza |
| `MANUAL_CAPTURE_REQUIRED` | observacion | Sin XML y sin OCR no hay datos: las reglas de montos y lineas no corren hasta que contabilidad capture |
| `DOCUMENT_WARNING` | observacion | Advertencias del parser (cantidad cero, etc.) |
| `RULE_CRASHED` | error | Una regla revento; el resto siguio corriendo |

Las tolerancias de monto se combinan con el criterio mas permisivo: pasa si la
diferencia esta dentro del porcentaje **o** dentro del monto absoluto.

### Emparejamiento de lineas

1. Lo que el validador emparejo a mano (`match_method = manual`) no se toca,
   ni siquiera al revalidar.
2. **Por codigo**: el codigo del vendedor en el XML se compara, normalizado,
   contra la referencia interna del producto, su codigo de barras y el codigo
   del proveedor en *Compra -> Proveedores* del producto (`product.supplierinfo`).
3. **Por IA** para lo que sobre, solo si esta habilitada en Ajustes. Por debajo
   de 0.7 de confianza no se asigna: queda como nota para el validador. Si la IA
   falla (red, 4xx/5xx, respuesta rara) las lineas quedan sin emparejar y la
   solicitud sigue con la observacion `LINES_MATCHED`.

### Crear la factura

Desde *Aprobada* o *Con observaciones*, el boton **Crear factura** genera un
`account.move` en borrador con las lineas ligadas a `purchase.order.line`
(cantidad, precio y descuento implicito segun la factura del proveedor), el
CUFE, el numero de factura como referencia y el PDF/XML como adjuntos. La OC
actualiza su cantidad facturada de inmediato. Si el total que calcula Odoo
difiere del de la factura, queda una nota en el chatter; la factura no se
valida sola. Borrar la factura borrador devuelve la solicitud a *Aprobada*.

## Grupos de seguridad

| Grupo | Puede |
|---|---|
| **Validador** (`group_spr_validator`) | Ver y validar solicitudes, crear la factura borrador |
| **Responsable** (`group_spr_manager`) | Todo lo anterior, mas configurar, rechazar y devolver a borrador |

El administrador (`base.user_admin`) entra a los dos grupos al instalar, y el
grupo *Compras / Administrador* implica *Responsable*: quien administre Compras
queda como Responsable sin asignarlo a mano.
| Portal | Ver y crear **solo sus propias** solicitudes |

`extracted_json` y `validation_json` solo son visibles para el grupo
*Responsable*: pueden contener datos de terceros.

## Contrato del servicio de OCR (para implementarlo en n8n o FastAPI)

Se invoca **solo** cuando el proveedor no adjunta XML. Si hay XML, el XML manda.

**Peticion**

```
POST {spr_ocr_url}
Authorization: Bearer {spr_ocr_api_key}
Content-Type: multipart/form-data

file: <el PDF>
hint: {"expected_po": "P00042", "expected_supplier_nit": "900123456"}
```

**Respuesta 200** — el mismo dict normalizado que produce el parser XML, mas
`confidence`, `provider` y `raw`:

```json
{
  "cufe": "5b7ed1...",
  "invoice_ref": "SETP990000001",
  "issue_date": "2026-03-15",
  "supplier": {"nit": "900123456", "dv": "1", "name": "CAFES DEL SUR SAS", "address": "..."},
  "customer": {"nit": "901234567", "dv": "8", "name": "LIBERTARIO ...", "address": "..."},
  "lines": [
    {"description": "Cafe verde excelso", "code": "CAFE-001", "quantity": 100.0,
     "unit": "KGM", "price_unit": 12000.0, "tax_rate": 19.0, "tax_amount": 228000.0,
     "discount": 0.0, "charge": 0.0, "subtotal": 1200000.0}
  ],
  "amount_untaxed": 1300000.0,
  "amount_tax": 247000.0,
  "amount_total": 1547000.0,
  "currency": "COP",
  "raw_warnings": [],
  "confidence": 0.87,
  "provider": "n8n-ocr",
  "raw": {}
}
```

Cualquier 4xx o 5xx, o un timeout, deja la solicitud en estado *Con
observaciones* con la nota "OCR no disponible, capturar manualmente" y el
hallazgo `MANUAL_CAPTURE_REQUIRED`. Nunca tumba la transaccion de Odoo.
Contabilidad captura numero, fecha, totales y lineas en la pestana *Datos
extraidos* y *Lineas*, y pulsa **Revalidar**: lo capturado se conserva aunque
el OCR siga caido, y las reglas corren completas sobre esos datos.

La respuesta se normaliza con tolerancia (`normalize_ocr_response`): numeros
como texto, campos ausentes o un CUFE mal leido no rompen nada; lo raro queda
en `raw_warnings`.

## Contrato del adaptador de IA

```
POST {spr_ai_url}          (proveedor "http")
Authorization: Bearer {spr_ai_api_key}
Content-Type: application/json
```

Solo se envian descripciones, codigos, cantidades y precios. **Nunca** NITs ni
datos personales.

**Peticion**

```json
{
  "invoice_lines": [
    {"idx": 0, "description": "Cafe verde excelso", "code": "CAFE-001",
     "quantity": 100.0, "price_unit": 12000.0}
  ],
  "po_lines": [
    {"id": 12, "description": "Cafe verde excelso saco", "code": "CAFE-001",
     "quantity": 100.0, "price_unit": 12000.0}
  ],
  "context": {"currency": "COP"}
}
```

**Respuesta 200**

```json
{
  "matches": [
    {"invoice_idx": 0, "po_line_id": 12, "confidence": 0.95,
     "note": "Mismo codigo y precio unitario"}
  ],
  "unmatched_invoice": [],
  "unmatched_po": [],
  "summary_es": "Todas las lineas se emparejaron por codigo."
}
```

Con el proveedor `anthropic` se llama directamente la Messages API
(`POST https://api.anthropic.com/v1/messages`, cabeceras `x-api-key` y
`anthropic-version: 2023-06-01`) con **salida estructurada**
(`output_config.format` de tipo `json_schema`), asi la respuesta llega como
JSON valido contra el esquema del contrato sin limpiar texto. Se usa `requests`
en vez del SDK `anthropic` porque la imagen de Odoo no lo trae (DECISIONS #25).
El modelo por defecto es `claude-opus-5-5`; se cambia en Ajustes. Si hace falta
pasar por un proxy, el parametro del sistema `spr.ai_url` reemplaza la URL de
la API (con este proveedor la pantalla de Ajustes no lo muestra).

Las sugerencias con confianza menor a **0.7** no asignan la linea de la orden
de compra: quedan como nota para el validador. Un `stop_reason` de `refusal` o
`max_tokens` se trata como fallo y no asigna nada.

**Pendiente de verificar en vivo:** la peticion a Anthropic se probo con
respuestas simuladas, no contra la API real (no habia API key en el entorno de
desarrollo). Antes de habilitarla en produccion, conviene una prueba manual
desde `odoo shell`:

```python
from odoo.addons.supplier_invoice_portal.services import ai_adapter
adapter = ai_adapter.AnthropicAiAdapter(api_key="sk-ant-...")
print(adapter.match_lines(
    [{"idx": 0, "description": "Cafe verde excelso saco 70 kg", "code": "", "quantity": 10, "price_unit": 840000}],
    [{"id": 1, "description": "Cafe verde excelso", "code": "CAFE-001", "quantity": 10, "price_unit": 840000}],
    {"currency": "COP"},
))
```

### Conexion de prueba por CLI local

En *Ajustes -> Compras -> Portal de facturas de proveedor*, con la IA
habilitada, la casilla **Conexion de prueba por CLI local** cambia la API de
Anthropic por el puente `anthropic_cli_bridge`
(`~/Documents/repos_libertario/claude_projects/anthropic_cli_bridge`), que
corre en el equipo del administrador y ejecuta una CLI de IA con la sesion ya
iniciada: `claude-empresa`, `claude-team`, `claude-personal`, `agy` o `codex`.
Sirve para probar el emparejamiento en desarrollo con la suscripcion que uno
ya tiene, sin API key. No es para produccion.

Odoo no ejecuta nada: el adaptador `CliAiAdapter` es el mismo
`AnthropicAiAdapter` con la URL del puente y la herramienta en el campo
`model`. El puente emula `POST /v1/messages` con salida estructurada, asi que
el cuerpo, el esquema y la lectura de la respuesta son identicos a los de
Anthropic (ver el README del puente para el detalle de cada CLI).

Ajustes: herramienta, *URL del puente* (por defecto
`http://host.docker.internal:8787/v1/messages`; en este equipo el firewall
obliga a usar la puerta de enlace de la red Docker,
`http://172.21.0.1:8787/v1/messages`), *API key del puente* (solo si se
arranco con `BRIDGE_API_KEY`) y un timeout propio de 180 s, porque las CLI
arrancan mas lento que la API. El boton **Probar conexion** manda una linea de
ejemplo y muestra el resultado. Mientras la casilla este activa, el proveedor y
la API key de arriba no se usan.

Probado de extremo a extremo el 2026-09-11 desde el contenedor `odoodkr17-web-1`
contra el puente en el anfitrion, con las tres CLI y el boton. Para repetirlo
desde `odoo shell`:

```python
from odoo.addons.supplier_invoice_portal.services import ai_adapter
adapter = ai_adapter.CliAiAdapter("claude-empresa", url="http://172.21.0.1:8787/v1/messages")
print(adapter.match_lines(
    [{"idx": 0, "description": "Cafe verde excelso saco 70 kg", "code": "", "quantity": 10, "price_unit": 2500000}],
    [{"id": 41, "description": "CAFE VERDE EXCELSO 70KG", "code": "CV-70", "quantity": 10, "price_unit": 2500000}],
    {"currency": "COP"},
))
```

## Consulta del CUFE en la DIAN

**Estado al 2026-09-11: el catalogo publico exige captcha y la consulta
automatica ya no puede confirmar un CUFE.** Probado con una factura real
(FEDC2437, validada por la DIAN el 2026-09-07): el GET
`/document/searchqr?documentkey=...` redirige al buscador
`/User/SearchDocument`, y el POST de ese formulario responde *"Falta Token de
validacion de captcha"*. Saltarse un captcha no es una opcion, asi que:

- El ajuste *Consultar el CUFE en el catalogo DIAN* queda **apagado por
  defecto** (DECISIONS #31). Si se activa, cada validacion agrega la
  observacion `DIAN_CUFE_CHECK` *"no se pudo verificar"* con el enlace al
  buscador para que contabilidad lo revise a mano.
- El boton **Consultar DIAN** del formulario repite la consulta y refresca el
  hallazgo en el resumen sin cambiar el estado.

La lectura sigue siendo heuristica (`services/dian_catalog.py`), por si la
DIAN reabre la consulta directa:

| Respuesta | Resultado |
|---|---|
| 200 con el CUFE en el cuerpo y sin el formulario de busqueda | `ok` |
| Texto "documento no encontrado" / "no existe" | `not_found` (observacion con enlace manual) |
| 200 con el formulario de busqueda o mencion de captcha | `error`: no se pudo verificar (observacion con enlace manual) |
| 403, timeout, pagina vacia o cualquier otra cosa | `error` (observacion con enlace manual) |

Timeout: 15 s. El catalogo responde HTML y bloquea (403) las peticiones sin
cabeceras de navegador, asi que se envian cabeceras de Chrome.

## Datos demo

`data/demo.xml` solo se carga en bases creadas con datos de demostracion
(sin `--without-demo`). Trae el mismo escenario de los tests: el proveedor
*CAFES DEL SUR SAS* habilitado, con usuario del portal `proveedor.demo` (clave
`demo`), tres productos con codigo, una orden de compra confirmada y la
solicitud `SPR/.../00001` ya validada con el XML DIAN sintetico. Tambien fija el
NIT de la compania en `901234567-8` (el adquiriente del XML) y apaga la
consulta al catalogo DIAN para que la instalacion no haga red.

La compania debe trabajar en COP para que la solicitud quede *Aprobada*. En la
base demo estandar de Odoo (*YourCompany* en USD) queda *Rechazada* por la regla
`CURRENCY_UNSUPPORTED`, lo cual tambien sirve para ver las reglas en accion.

## Tests

```bash
docker run --rm --network odoodkr17_webnet \
  -v "$PWD/config:/etc/odoo" -v "$PWD/addons:/mnt/extra-addons/custom-addons" \
  odoo:17.0 odoo -c /etc/odoo/odoo.conf -d spr_test \
  -i supplier_invoice_portal --test-enable --test-tags /supplier_invoice_portal \
  --stop-after-init --without-demo=all
```

Los tests del parser (`tests/test_dian_xml_parser.py`) son codigo puro y no
tocan la base de datos. Los de flujo (`tests/test_validation_flow.py`) usan
`AccountTestInvoicingCommon` (via `tests/common.py`), crean una orden de compra
real y recorren validacion, reglas, emparejamiento manual y creacion de la
factura. Los del portal (`tests/test_portal.py`) son `HttpCase`: un usuario
portal radica por HTTP con PDF y XML, y se comprueba el aislamiento entre
proveedores y el acceso con token. Los de adaptadores (`tests/test_adapters.py`)
simulan OCR, Anthropic, servicio HTTP propio, el puente de CLI y catalogo DIAN
con `unittest.mock`, incluyendo fallas de red, 401/503, timeout, rechazos y
respuestas basura. Ningun test hace red. Los fixtures de `tests/fixtures/` son XML sinteticos
validos: un `Invoice` UBL directo, un `AttachedDocument` con la factura embebida
en CDATA y una factura con descuentos de linea. Ningun test hace red.

## Fuera del alcance del MVP

Queda como TODO para versiones posteriores:

- Eventos RADIAN (acuse de recibo, recibo del bien, aceptacion expresa).
- Plan de pagos y programacion de pagos.
- Solicitudes de pago sin factura (anticipos), ver DECISIONS #39.
- Facturas sin orden de compra.
- Soporte CFDI de Mexico.
- OCR propio dentro de Odoo.
- Multi-moneda.
