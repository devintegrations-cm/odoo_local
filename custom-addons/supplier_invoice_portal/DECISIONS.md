# Decisiones de diseno

Registro de las decisiones tomadas donde el spec era ambiguo o chocaba con el
entorno. Cada una dice **que** se decidio y **por que**.

## Entorno

- **Odoo 17.0** confirmado leyendo `odoo/release.py` dentro de la imagen
  `odoo:17.0`: `17.0-20260119`, `(17, 0, 0, 'final', 0, '')`.
- El entorno local es Docker Compose (`odoodkr17/stack.yml`), no Odoo.sh. El
  destino de produccion sigue siendo Odoo.sh y el codigo no asume nada del
  entorno local.

## 1. `pypdf` no se declara en `external_dependencies`

La imagen `odoo:17.0` no trae `pypdf`. Declararlo en el manifest impediria
instalar el modulo en el entorno local y, por lo tanto, verificar cada fase.

- `lxml` (4.8.0, ya presente) si se declara.
- `pypdf` se importa de forma diferida y opcional en
  `services/ocr_adapter.pdf_page_count()`: si no esta, devuelve `None` y el
  flujo sigue.
- Para Odoo.sh se agrega `pypdf` al `requirements.txt` de la raiz del repo.

En el MVP el OCR es un servicio externo, asi que `pypdf` solo sirve para
comprobar que el PDF se puede abrir. No justifica bloquear la instalacion.

## 2. Campo `cufe` en `account.move` y su relacion con Jorels

`l10n_co_edi_jorels` ya guarda un CUFE en `account.move.ei_uuid`, pero ese es el
CUFE de las facturas que **nosotros emitimos**. El campo `cufe` que agrega este
modulo es el del documento que el **proveedor** recibio de la DIAN. Son cosas
distintas y el nombre `cufe` estaba libre.

- La constraint de unicidad se acota a `move_type in ('in_invoice', 'in_refund')`
  y `state != 'cancel'`, por compania. Si fuera global chocaria el dia que otro
  modulo escriba un CUFE de venta en el mismo campo.
- **No** se declara dependencia de Jorels en el manifest: el modulo debe poder
  instalarse en otras instancias del grupo que no lo tengan.

## 3. Comparacion de NITs

`res.partner.vat` en Colombia llega inconsistente: con DV, sin DV, con guiones o
con puntos. Se normaliza a solo digitos y se compara **sin digito de
verificacion** (`services/dian_xml_parser.same_nit`). Como los NIT no tienen
largo fijo (8 o 9 digitos de persona juridica, cedulas de 6 a 10), no se recorta
a 9 a ciegas: son iguales si coinciden digito a digito o si uno es el otro mas
exactamente un digito al final. (Corregido en la revision de la Fase 5: el
recorte fijo rechazaba a personas naturales con DV pegado.)

## 4. Moneda distinta a la de la compania

Multi-moneda esta fuera del alcance del MVP, pero un XML puede venir en USD.
En vez de crear una factura con montos silenciosamente equivocados, la Fase 2
agrega la regla `CURRENCY_UNSUPPORTED` con nivel **error**.

## 5. Como se combinan las dos tolerancias de monto

El spec define `spr_amount_tolerance_pct` y `spr_amount_tolerance_abs` sin decir
como se combinan. Se usa el criterio mas permisivo: la diferencia pasa si esta
dentro del porcentaje **o** dentro del monto absoluto. Es lo que uno espera con
facturas pequenas, donde 0.5 % son veinte pesos.

Para el **precio unitario** (`PRICE_UNIT_MISMATCH`) se usa solo el porcentaje:
con el absoluto, un sobreprecio de 900 pesos por unidad pasaba sin observacion
en una linea de 10.000 unidades. (Revision de la Fase 5.)

## 6. Pendiente por facturar de la orden de compra

Odoo 17 no expone un campo directo. Se calcula como `order.amount_total` menos
lo facturado **linea por linea**: la suma de `price_total` de las lineas de
factura no canceladas ligadas a cada `purchase.order.line`. No se resta el
total de cada factura porque `invoice_ids` incluye facturas que agrupan varias
ordenes o traen lineas extra (flete), y descontarlas enteras rechazaba facturas
legitimas. (Revision de la Fase 5.)

## 7. URL del catalogo DIAN

El spec escribe el parametro como `documentKey`; Jorels usa `documentkey` en
minuscula y es el que responde. Se usa el de Jorels. El catalogo devuelve HTML,
no JSON, asi que la verificacion es heuristica y `dian_cufe_check` puede quedar
en `error` o `skipped` **sin bloquear nunca** el flujo.

## 8. Formato y normalizacion del CUFE

`^[0-9a-f]{96}$` (SHA-384 en hexadecimal). Se normaliza a minusculas y sin
espacios antes de guardar y de comparar duplicados, tanto en `create` como en
`write`. El campo lleva indice.

## 9. Textos de interfaz en espanol, sin archivo `.po`

Todas las instancias del grupo son colombianas y una sola persona administra IT.
Un `i18n/es.po` duplicaria el mantenimiento sin beneficio real. Los nombres
tecnicos (modelos, campos, metodos) siguen en ingles, como pide el spec.

Los textos van sin tildes en el codigo fuente para evitar problemas de
codificacion en entornos mal configurados; las tildes reales se pueden agregar
despues via traduccion si hiciera falta.

## 10. Secuencia sin compania

`ir.sequence` con `company_id = False`: multi-compania no esta en el MVP y una
secuencia global es una cosa menos que configurar al instalar en otra instancia.

## 11. El estado `validating` se mantiene aunque la validacion sea sincrona

En el MVP el pipeline corre dentro de la misma transaccion, asi que
`validating` casi no se ve. Se deja declarado porque es el punto de enganche
para mover el pipeline a una cola (cron o `queue_job`) despues, sin migracion de
datos.

## 12. El PDF obligatorio se valida en el pipeline, no con `required=True`

`pdf_file` no es `required` a nivel de modelo: eso obligaria a tener el PDF
antes de poder guardar un borrador desde el backend. La obligatoriedad se
verifica en `_run_pipeline()` (`UserError` si falta) y se exigira tambien en el
formulario del portal (Fase 3).

## 13. Helpers de NIT y CUFE dentro de `dian_xml_parser`

`normalize_nit`, `strip_dv`, `same_nit`, `normalize_cufe` e `is_valid_cufe`
viven en `services/dian_xml_parser.py` en vez de en un `utils.py` aparte, para
no agregar archivos fuera del arbol acordado. Son funciones puras y las importan
tanto los modelos como `validation_rules`.

## 14. Alcance real de la Fase 1

Lo que quedo como esqueleto documentado, no implementado:

- `services/validation_rules.run_rules()` devuelve un unico hallazgo
  `RULES_PENDING` de nivel *warning*. Es deliberado: asi ninguna solicitud queda
  marcada como *aprobada* mientras las reglas duras no existan. Se reemplaza en
  la Fase 2.
- `services/line_matcher.match_lines()` devuelve lista vacia. Fase 2.
- `get_ocr_adapter()` y `get_ai_adapter()` siempre devuelven la implementacion
  *noop*, y dejan un WARNING en el log si el ajuste esta habilitado. Fase 4.
- `action_create_bill()` **no** se declara todavia: un metodo que existe y
  revienta es peor que un metodo que no existe. Fase 2.

## 15. Las vistas de lista usan `<tree>`, no `<list>`

En Odoo **17.0** el campo `ir.ui.view.type` todavia no acepta el valor `list`:
usar `<list>` como etiqueta raiz falla al instalar con

```
ValueError: Wrong value for ir.ui.view.type: 'list'
```

El renombramiento de `<tree>` a `<list>` es de Odoo 18.0. Aqui se usa `<tree>`
y `view_mode = "tree,form"`. Verificado contra la imagen `odoo:17.0`
(`17.0-20260119`), no asumido.

## 16. Revalidar conserva los emparejamientos manuales, por posicion

Las lineas del request se recrean desde el documento en cada validacion (el
XML manda). Antes de borrarlas se guarda `{posicion: linea de OC}` de las que
tienen `match_method = manual` y se vuelven a aplicar a la misma posicion. Es
por posicion y no por codigo porque las lineas sin codigo son justo las que
requieren emparejamiento manual.

## 17. Consistencia del total: se aceptan varias lecturas del UBL

En los XML DIAN reales `AllowanceTotalAmount` a veces resume descuentos de
linea (ya restados de `LineExtensionAmount`) y a veces descuentos de documento
(que si restan del total). La regla `AMOUNT_INCONSISTENT` acepta el total si
cuadra con **cualquiera** de: `subtotal + impuestos`, `subtotal + impuestos -
descuentos + recargos`, o eso menos el anticipo. Es una observacion, no un
error, porque el total que manda es el de la OC (`AMOUNT_TOTAL`).

## 18. El descuento de la factura se traslada como porcentaje implicito

Al crear la factura se usa la cantidad y el precio unitario del proveedor, no
los de la OC, y el descuento de la OC **no** se copia. Si `subtotal < cantidad
x precio`, la diferencia se convierte en `discount` porcentual de la linea de
factura. Asi el borrador reproduce lo que dice el documento del proveedor y las
diferencias con la OC quedan visibles como observaciones, no escondidas.

## 19. Borrar la factura borrador devuelve la solicitud a *Aprobada*

`account.move.unlink` busca solicitudes ligadas y las deja en `approved` sin
factura. No se hace lo mismo al **cancelar** la factura: una factura cancelada
sigue existiendo y su CUFE queda liberado por la constraint, asi que el
validador decide si crea otra o rechaza la solicitud.

## 20. Rechazar pide motivo con un asistente

`action_reject` exige motivo. El boton del formulario abre un
`TransientModel` de un solo campo en vez de rechazar directo, porque ese texto
lo va a leer el proveedor en el portal (Fase 3). Solo el grupo *Responsable*
puede rechazar, como dice el README.

## 21. El controlador del portal corre con `sudo` y filtra por proveedor

El usuario portal no tiene acceso a `purchase.order.line`, `product.product`,
`account.move` ni a escribir en la solicitud, y no se le va a dar: el pipeline
toca todo eso. Por eso el controlador crea la solicitud y corre el pipeline con
`sudo`, y compensa con dos reglas fijas: el proveedor es siempre
`commercial_partner_id` del usuario conectado, y el id de la orden que viene
del formulario solo se acepta si esta en la lista de ordenes abiertas de ese
proveedor. Las lecturas (lista y detalle) si van con el usuario real y la
record rule; el detalle acepta `access_token` para el enlace del correo.

## 22. La validacion del formulario ocurre antes de crear el registro

PDF presente y con firma `%PDF`, XML parseable, CUFE con formato y coincidente
con el XML, orden valida: todo se comprueba en memoria. Si algo falla se
re-renderiza el formulario con los errores y no queda nada en la base. Si
falla el pipeline despues de crear (caso raro), un `savepoint` deshace la
creacion. Asi el proveedor no ve "solicitudes fantasma" rechazadas por errores
de captura, y `CUFE_DUPLICATE` no se dispara por sus propios reintentos.

## 23. Al proveedor se le escribe una sola vez por validacion

`_apply_findings` envia el correo solo si `validation_json` estaba vacio, es
decir en la primera validacion. Las revalidaciones que hace contabilidad
(despues de emparejar lineas a mano, por ejemplo) no le cambian nada al
proveedor y solo generarian ruido. Rechazo manual y factura creada si avisan
siempre, porque son decisiones.

## 24. Los archivos se sirven por `/web/content` sin controlador propio

El PDF y el XML del detalle se descargan con
`/web/content/supplier.payment.request/<id>/pdf_file`. Odoo aplica ahi la
misma ACL y record rule del modelo, asi que el proveedor solo baja lo suyo.
No hace falta un endpoint aparte. Nota: `/web/content` ignora `access_token`
en modelos que no son `ir.attachment`, asi que el enlace del correo sirve para
ver el detalle pero la descarga exige sesion; es aceptable para el MVP.

## 25. Anthropic se llama con `requests`, no con el SDK `anthropic`

La recomendacion general para codigo Python es el SDK oficial. Aqui no aplica:
la imagen `odoo:17.0` no lo trae, el modulo no agrega dependencias Python
(ver #1) y la llamada es una sola (`POST /v1/messages` con salida
estructurada). `requests` ya viene con Odoo. Si en el futuro se necesita
streaming, reintentos finos o herramientas, se agrega el SDK al
`requirements.txt` y se reemplaza `AnthropicAiAdapter` sin tocar el contrato.

Detalles que si se tomaron de la referencia de la API y no de memoria:
`output_config.format` con `type: json_schema` y `additionalProperties: false`
en todos los objetos; no se envia `thinking` (el modelo por defecto lo trae
adaptativo); no se envia `temperature`; `stop_reason` `refusal` y `max_tokens`
se tratan como fallo.

## 26. *No encontrado* en la DIAN es observacion, no error

El catalogo DIAN responde HTML sin API y la lectura es heuristica. El caso
positivo (CUFE existente) no se pudo verificar en desarrollo porque no habia un
CUFE real disponible. Rechazar facturas validas por una heuristica sin
calibrar seria peor que dejar pasar una observacion que contabilidad revisa en
segundos con el boton *Consultar DIAN*. Cuando se calibre con documentos reales
se puede subir a error cambiando una linea en `_rule_dian_cufe_check`.

## 27. Sin datos extraidos, las reglas de montos y lineas no corren

Sin XML y con el OCR apagado o caido, la solicitud no tiene montos ni lineas.
Correr `AMOUNT_TOTAL` o `LINES_MATCHED` en ese estado daria errores vacios
("total cero", "sin lineas") y la solicitud quedaria rechazada por una falla
nuestra, no del proveedor. En su lugar corre solo el bloque `ALWAYS_RULES`
(CUFE, duplicados, estado de la OC, DIAN) mas la observacion
`MANUAL_CAPTURE_REQUIRED`, y la solicitud queda *Con observaciones* con
actividad para el validador. La condicion es "no hay lineas y el total es
cero" sobre el request, no sobre el origen: en cuanto contabilidad captura
datos y revalida, corren todas.

## 28. El modelo por defecto de Anthropic es `claude-opus-5`

En la Fase 1 quedo `claude-sonnet-5`. Se cambia a `claude-opus-5`, que es el
modelo recomendado actualmente para uso general. El emparejamiento de lineas
es una tarea corta (unas decenas de lineas por factura), asi que la diferencia
de costo por factura es de centavos. Si el volumen crece, `claude-sonnet-5`
se configura en Ajustes sin tocar codigo.

**Actualizado el 2026-10-01:** el defecto pasa a `claude-opus-5-5`, el Opus
vigente y 20 % mas barato por token. La peticion no cambia: no fuerza
`tool_choice` ni desactiva `thinking`, que es lo que Opus 5.5 rechaza. Probado
contra la API real con la llave de staging (7,8 s, emparejo bien).

## 29. La conexion de prueba por CLI pasa por un puente HTTP, no por subprocesos en Odoo

Se agrega un tercer camino para la IA: las CLI con sesion iniciada del
administrador (`claude` con perfil, `agy`, `codex`), para probar el
emparejamiento sin API key.

La primera version ejecutaba las CLI con `subprocess` dentro de Odoo. Se
descarto el mismo dia porque Odoo corre en Docker (y en produccion en Odoo.sh),
donde no hay CLI ni sesion interactiva, y montar la home del anfitrion en el
contenedor acopla el modulo a la maquina. En su lugar:

- Un proyecto aparte, `claude_projects/anthropic_cli_bridge`, **emula la
  Messages API de Anthropic** y ejecuta la CLI que diga el campo `model`.
  Corre en el equipo del administrador con la biblioteca estandar de Python.
- En el modulo, `CliAiAdapter` es una subclase de `AnthropicAiAdapter` con la
  URL del puente y la herramienta como `model`. Cero codigo nuevo de protocolo
  y nada que ejecutar en el servidor de Odoo.
- Las CLI corren sin herramientas y en carpeta temporal, nunca por shell (eso
  vive en el puente, que es quien toca el sistema).
- Es un ajuste separado del proveedor y manda sobre el. Queda marcado como
  *solo para desarrollo*.
- Cualquier fallo (puente apagado, CLI sin sesion, timeout) devuelve la
  respuesta vacia del contrato, igual que los otros adaptadores.

## 30. Correcciones de la revision de codigo de la Fase 5

Hallazgos de `/code-review` sobre el modulo completo (2026-09-10) que cambiaron
comportamiento; los tests cubren cada uno.

- **El portal no tiene `create` por ORM.** El controlador crea con `sudo` y
  valida todo antes; el permiso de creacion para `base.group_portal` permitia
  fabricar solicitudes por JSON-RPC con estado y factura a dedo. Se quita del
  CSV y de la regla de registro.
- **La consulta DIAN se puede apagar de verdad.** Odoo borra el
  `ir.config_parameter` cuando un Boolean de Ajustes queda en False, y el
  codigo tomaba "ausente" como "activado". El campo ya no usa
  `config_parameter`: se guarda a mano como `"True"`/`"False"`.
- **CUFE normalizado tambien en `account.move`**, para que una factura manual
  con el CUFE en mayusculas no escape a la deteccion de duplicados.
- **Un CUFE malformado en el XML no aborta la validacion**: no se escribe en
  el request y la regla `CUFE_FORMAT` lo reporta. El portal lo rechaza con
  mensaje claro en el formulario.
- **Los emparejamientos manuales se reconocen por contenido** (codigo,
  descripcion, precio), no por posicion: reordenar las lineas con el handle
  los movia a otra linea.
- **Rechazo manual**: no se puede rechazar una solicitud facturada o
  cancelada, y una rechazada por el Responsable no se revalida hasta
  "Volver a borrador" (que limpia el motivo).
- Tambien: tolerancia porcentual para precios unitarios (#5), pendiente de la
  OC linea por linea (#6), comparacion de NIT sin largo fijo (#3), y la fecha
  del OCR se descarta si no es AAAA-MM-DD.

**Quedan anotados sin cambiar** (decision pendiente del usuario):

- El pipeline corre dentro de la peticion HTTP del portal y la suma de los
  timeouts (OCR 60 s + DIAN 15 s + IA 45 s) roza el `limit_time_real` de 120 s
  de Odoo (DECISIONS #11). Mitigacion posible: bajar los timeouts o pasar el
  pipeline a un cron.
- Una tolerancia de monto en 0 vuelve al valor por defecto por el mismo
  mecanismo de los Boolean (Odoo borra el parametro cuando el Float es 0).
- Con OCR que devuelve totales pero ninguna linea, la solicitud se rechaza por
  `LINES_MATCHED` en vez de pedir captura manual.

## 31. La consulta al catalogo DIAN queda apagada por defecto: exige captcha

Probado el 2026-09-11 con un CUFE real (factura FEDC2437 de un proveedor,
validada por la DIAN el 2026-09-07): `GET /document/searchqr?documentkey=...`
redirige al buscador `/User/SearchDocument`, y el POST de ese formulario con
CUFE y NIT responde *"Falta Token de validacion de captcha"*. No hay captcha
visible ni API publica; el token lo genera JavaScript en el navegador.

Decisiones:

- **No se intenta saltar el captcha.** La verificacion automatica queda
  fuera del alcance mientras la DIAN lo exija.
- **El buscador ya no se interpreta como "no encontrado"** (antes si, y con
  esta factura real daba un falso *no aparece en la DIAN*). Ahora es `error`:
  *no se pudo verificar*.
- `error` y `not_found` son **observaciones con el enlace al buscador**, para
  que contabilidad verifique a mano. Antes `error` era silencioso (nivel ok);
  con la consulta activa, callar que el CUFE no se verifico es peor que una
  observacion.
- El ajuste queda **apagado por defecto** (antes activado): activo solo puede
  agregar 15 s por validacion y una observacion fija. Se deja configurable por
  si la DIAN reabre la consulta directa; la heuristica de lectura sigue ahi.
- El boton *Consultar DIAN* ahora refresca el hallazgo en el resumen sin
  cambiar el estado de la solicitud.

Esto cierra la verificacion "caso positivo del catalogo DIAN" que estaba
pendiente desde la Fase 4: no es alcanzable con el catalogo actual.

## 32. La verificacion de emisor y receptor es un ajuste

A pedido del usuario (2026-09-11), *Verificar emisor y receptor* (NIT y razon
social) se puede desactivar en Ajustes. Con el lote de 25 facturas reales de
staging, todas sin XML, cada solicitud traia las observaciones `SUPPLIER_NIT`
y `CUSTOMER_NIT` "no se pudo verificar", que en captura manual no aportan.

- Activado por defecto: con XML, el NIT del emisor es la barrera contra
  radicar la factura de otro proveedor, y el del adquiriente contra facturas
  dirigidas a otra empresa. El texto de ayuda lo advierte.
- Desactivado, las dos reglas devuelven nada y los grupos *Emisor* y
  *Adquiriente* se ocultan en la pestana *Datos extraidos* (campo calculado
  `party_check_enabled`). Los datos se siguen extrayendo y guardando.
- Se guarda como `"True"/"False"` con el mismo mecanismo que la consulta
  DIAN (hallazgo 7 de la revision), centralizado en `_SPR_MANUAL_BOOLEANS`.

## Ajustes del comite (2026-09-23)

Lista de 12 puntos del comite de compras y contabilidad. Lo que cambio el
codigo esta en #33 a #38 (version 17.0.1.1.0, con migracion); lo que no, en
#39.

## 33. Notas credito y debito

El parser ya no rechaza `CreditNote` ni `DebitNote`: lee sus lineas
(`CreditNoteLine`/`CreditedQuantity`, `DebitNoteLine`/`DebitedQuantity`), los
totales (la nota debito los trae en `RequestedMonetaryTotal`, no en
`LegalMonetaryTotal`) y la factura que corrigen (`BillingReference`: numero y
CUFE). La solicitud tiene `document_type` y `origin_move_id`.

- **La factura de origen** se toma de lo que elija el proveedor o, si no
  elige, del CUFE y luego del numero de `BillingReference`, siempre entre las
  facturas de proveedor de ese mismo tercero. Se ofrecen todas las facturas
  no canceladas, no solo las radicadas por el portal: la nota puede ser de una
  factura anterior al portal.
- **Emparejamiento**: las lineas de la nota se emparejan contra las lineas de
  las ordenes de la factura de origen, con el mismo matcher (codigo, flete,
  IA, manual). Solo sirve para saber producto, cuenta e impuesto: la linea de
  la nota **no** lleva `purchase_line_id`, porque una nota corrige valor y no
  cantidades recibidas; con el vinculo, una nota por descuento bajaria la
  cantidad facturada de la orden. Las devoluciones fisicas siguen el flujo de
  inventario de Odoo.
- **Reglas**: no corren las de la orden (`PO_STATE`, `AMOUNT_TOTAL`,
  `QTY_OVER_PO`, `PRICE_UNIT_MISMATCH`), porque la orden ya se facturo. Corren
  `NOTE_ORIGIN` (error si no hay factura, es de otro proveedor, esta cancelada
  o el CUFE referenciado es otro) y, en credito, `NOTE_AMOUNT` (error si supera
  el saldo: total de la factura menos notas credito registradas y radicadas).
- **Nota credito** -> `in_refund` con `reversed_entry_id`.
- **Nota debito** -> `in_invoice`, con `debit_origin_id` solo si
  `account_debit_note` esta instalado (Jorels lo usa); no se agrega como
  dependencia. **Siempre requiere aceptacion**: la regla
  `DEBIT_NOTE_ACCEPTANCE` es observacion hasta que el Responsable pulsa
  *Aceptar nota debito* (queda quien y cuando), y `action_create_bill` la
  exige. Es el "aceptar las notas debito" que pidio el comite: un incremento
  de precio nunca pasa solo. *Volver a borrador* borra la aceptacion.
- `DOCUMENT_TYPE` (error): el XML adjunto es de otro tipo del radicado. El
  portal lo ataja antes, en el formulario.
- Las notas de ajuste al **documento soporte** las emite la compania, no el
  proveedor: no aplican aqui.

## 34. Varias ordenes de compra en una solicitud

`purchase_id` (Many2one) pasa a `purchase_ids` (Many2many, tabla
`supplier_payment_request_purchase_rel`). Algunos proveedores agrupan varias
remisiones en una sola factura para facturar menos.

- En el portal las ordenes se marcan con casillas; todas deben estar en la
  lista de abiertas del proveedor y ser de la misma compania.
- `PO_STATE` revisa cada orden; `AMOUNT_TOTAL` compara contra la **suma** de
  lo pendiente; el matcher busca en las lineas de todas; `invoice_origin` lleva
  los nombres separados por coma. Plazo de pago y posicion fiscal se toman de
  la primera orden.
- **Migracion**: `post-migrate` copia la columna vieja `purchase_id` a la tabla
  de relacion (Odoo no la borra). `pre-migrate` borra la plantilla *recibida*
  para que se recree: usaba `object.purchase_id`, y en un archivo `noupdate`
  Odoo 17 salta los registros existentes aunque se cambie la marca. Probado
  instalando el zip de 17.0.1.0.0 con demo y actualizando.

## 35. Fletes y envios: cargo adicional

El envio sale en la factura pero no en la remision ni en la orden. Antes eso
dejaba una linea sin emparejar y el total por encima de lo pendiente
(rechazo por `AMOUNT_TOTAL`).

- Campo `is_extra_charge` en la linea. El matcher marca como cargo, **antes**
  de la IA, las lineas no emparejadas por codigo cuya descripcion tenga
  flete, envio, transporte, domicilio, despacho, acarreo, mensajeria o cargo
  adicional (palabra completa, sin tildes). No se le pasan a la IA porque las
  emparejaria con cualquier producto.
- Los cargos de **documento** (`cac:AllowanceCharge` hijo directo con
  `ChargeIndicator=true`) no estan en ninguna linea pero si en el total: el
  parser los agrega como linea "Cargo adicional: <motivo>", que el matcher
  reconoce.
- El validador puede marcar o desmarcar la casilla a mano; queda como
  `manual` y sobrevive a la revalidacion igual que un emparejamiento manual.
- `AMOUNT_TOTAL` compara el total **sin** cargos (con su IVA) contra lo
  pendiente. `EXTRA_CHARGES` es **observacion** con el valor: contabilidad
  confirma el flete antes de registrar. No hay tope automatico.
- En la factura, cada cargo va con el producto de flete de Ajustes
  (`spr.freight_product_id`), sin orden, con los impuestos del producto solo
  si la factura le cobro impuesto. Sin producto configurado, *Crear factura*
  lo pide.

## 36. Cuenta de cobro de no obligados a facturar (documento soporte)

Indicador `spr_support_document` en el proveedor y tipo `support_doc`. Solo
esos proveedores ven la opcion en el portal (y la tienen por defecto).

- Sin XML ni CUFE: el documento electronico lo emite la compania. El
  proveedor declara numero, fecha y total. Las lineas se crean **una vez**
  desde lo pendiente de las ordenes (`qty_to_invoice`, o lo ordenado menos lo
  facturado si la politica es por recepcion y no ha llegado nada), ya
  emparejadas (`match_method = order`); despues son de contabilidad, que las
  ajusta si la cuenta de cobro no cubre todo. El total declarado no se pisa:
  `AMOUNT_INCONSISTENT` lo compara con lo que suman las lineas.
- No corren las reglas de CUFE, NIT de emisor/adquiriente ni DIAN. El numero
  repetido para el mismo proveedor es **error** (`INVOICE_REF_DUPLICATE`),
  porque sin CUFE es lo unico que identifica el documento. `SUPPORT_DOC` es
  observacion si el proveedor no esta marcado como no obligado.
- La factura se crea **siempre** en el diario de documento soporte
  (`spr.support_journal_id`): con Jorels, es el diario con resolucion tipo 12
  el que convierte la `in_invoice` en documento soporte al confirmarla. No se
  depende de Jorels; sin el diario configurado, *Crear factura* lo pide.

## 37. Requisitos para habilitar un proveedor; contactos padre e hijo

Constraint en `res.partner` sobre `portal_invoice_enabled`:

1. Solo el contacto comercial (empresa, o persona natural sin padre). En un
   hijo el indicador no tendria efecto: el portal lee el del comercial. La
   vista esconde la casilla en los hijos (`parent_id and not is_company`);
   sigue visible para personas naturales sin padre (regresion del
   2026-09-16).
2. Con NIT.
3. ~~Sin otro tercero con el mismo NIT~~. **Quitado el 2026-09-30** a pedido
   del usuario: al desplegar a staging, 87 de los 705 proveedores con ordenes
   en 2026 tenian otro tercero con el mismo NIT (casi siempre un duplicado sin
   DV y sin ordenes), y la regla no dejaba habilitar a ninguno de ellos. No
   hace falta: quien radica es el usuario portal que se le crea al proveedor,
   y ese usuario solo ve las ordenes de su propio tercero. Fusionar los
   repetidos sigue siendo buena limpieza, pero ya no es requisito.

Contactos padre e hijo:

- La solicitud queda siempre a nombre del contacto comercial, aunque se elija
  un hijo (en `create`/`write`). El dominio del campo acepta personas
  naturales sin padre (antes exigia `is_company`, el mismo error del
  2026-09-16).
- `submitted_by_partner_id` guarda el contacto que radico. Los correos van a
  la empresa **y** a esa persona: el correo de la empresa suele ser generico
  y quien espera respuesta es quien subio la factura. Antes, sin correo en la
  empresa no salia nada.

## 38. Cierre de radicacion de fin de mes

El ultimo dia habil del mes, desde la hora de corte (12:00 por defecto), el
portal no muestra el formulario ni acepta el POST hasta el dia 1 del mes
siguiente, e indica desde cuando reabre. Todos los dias el formulario avisa la
hora de corte.

- Dia habil: lunes a viernes que no sea festivo en Colombia. Los festivos se
  **calculan** en `services/cutoff.py` (fijos, Ley Emiliani y los que
  dependen de la Pascua) en vez de leerlos de un calendario de recursos que
  alguien tendria que cargar cada ano; todas las instancias del grupo son
  colombianas (#9). Verificado contra el calendario oficial 2026 (18
  festivos) y contra junio de 2025, que termina en lunes festivo.
- Hora local de la compania (`partner_id.tz`, por defecto `America/Bogota`).
- Ajustes: activo por defecto (Boolean guardado a mano, como #30) y hora de
  corte. Solo cierra el portal: el backend sigue creando solicitudes.
- Los tests desactivan el cierre en `SprCase` para no fallar el ultimo dia
  habil por la tarde; se prueba aparte con fechas fijas.

## 39. Puntos del comite que no cambian codigo

- **2. "Factura el pago"**: quedo anotado sin detalle ("no me acuerdo"). Sin
  definicion no hay que implementar.
- **9. Compras de otras areas**: el modulo no restringe por area; cualquier
  orden de compra confirmada del proveedor sirve. Quien valida y a quien le
  llega la actividad es organizacional (grupo validador en Ajustes). Queda
  para el comite.
- **10. Proveedores que facturan solo despues de cobrar** (pago anticipado,
  no entregan inventario antes): pendiente de validar el caso de uso. Hoy el
  modulo no lo bloquea: las reglas comparan contra lo **ordenado**, no lo
  recibido, asi que una factura por mercancia no entregada pasa si la orden
  esta confirmada. Lo que no existe es una solicitud de pago **sin**
  factura (anticipo contra cotizacion o cuenta de cobro previa); si el comite
  lo confirma, seria un tipo de documento nuevo.
- **11. Instructivo por correo**: no es regla sino plantilla
  (`mail_template_spr_instructions`, en `res.partner`) y un boton *Enviar
  instructivo del portal* en la ficha del proveedor habilitado. Abre el
  asistente de correo en vez de enviar directo, para revisar destinatarios y
  adjuntar la guia en PDF. El texto menciona la hora de corte configurada y,
  si aplica, la cuenta de cobro.

## 40. Una orden con factura ya no acepta otra factura: solo notas

Pedido del usuario el 2026-10-01. Antes se bloqueaba solo la orden
**totalmente** facturada (`invoice_status = invoiced`) y una orden con factura
parcial seguia aceptando facturas por lo pendiente. Ahora basta con que la
orden tenga una factura de proveedor no anulada (borrador incluido,
`purchase.order._spr_vendor_bills`):

- El portal no la ofrece para factura ni cuenta de cobro, y un id enviado a
  mano no pasa del filtro de ordenes vigentes.
- La regla `PO_STATE` rechaza la solicitud creada desde el backend. La factura
  que creo la misma solicitud no cuenta, para que revalidarla no la rechace.
- Las notas credito y debito no cambian: se radican sobre la factura que
  corrigen y no pasan por el estado de la orden.
- Anular la factura libera la orden.

En staging, al 2026-10-01, son 8 ordenes de 2026 con factura parcial.

## 41. La factura que corrige una nota se busca entre todas las del proveedor

Encontrado al validar staging el 2026-10-01: la proveedora de prueba tiene
1.618 facturas y el formulario de notas solo trae las 100 mas recientes
(`limit=100`, para que la lista sea usable). Esa misma lista se usaba para
validar, asi que una nota sobre una factura mas vieja salia con "no
encontramos registrada" aunque existiera. Ahora la lista sigue en 100, pero
`_spr_validate_origin` busca por id, CUFE o numero con el dominio completo del
proveedor (`_spr_origin_domain`).

## 42. Diario por tipo de proveedor; las casillas del contacto en su grupo

- **Diario.** Factura y notas van al *Diario de compras por defecto*; la
  cuenta de cobro (proveedor no obligado) al *Diario de documento soporte*.
  Es el mismo patron de staging en 2026: 276 proveedores solo en FACTU, 175
  solo en DSO. Si el diario por defecto queda vacio, el modulo toma el primer
  diario de compras por secuencia, que en staging es *Facturas de Activos*:
  hay que configurarlo siempre (en staging, FACTU id 3).
- **Vista del contacto.** Las casillas iban "despues del NIT". Con `base_vat`
  el NIT vive dentro de `div[@name='vat_vies_container']`, y ahi los campos
  salian sin etiqueta. Ahora van en un grupo propio *Portal de facturas*
  debajo del bloque principal del contacto.

## 43. IVA como producto (mayor valor IVA) y cargos que entran a la orden

Pedido del usuario el 2026-10-01. En Libertario el IVA no va como impuesto de
la linea: las lineas de la orden y de la factura van con *IVA Compra Exento*
0 % y el IVA se registra con el producto *[MAYVALIVACOM191] Mayor Valor Iva
Compras 19-15-8-5*. En staging, desde junio, 2.882 lineas de orden van sin
impuesto y 470 lineas de 2026 usan ese producto.

Ajuste *IVA como producto* (`spr.tax_as_product`, apagado por defecto) con
dos campos: producto de mayor valor IVA (`spr.tax_product_id`) e impuesto de
las lineas (`spr.exempt_tax_id`). Activo:

- **Factura:** todas las lineas llevan el impuesto 0 %, y el IVA total del
  documento (`amount_tax` del XML) va como una linea del producto de mayor
  valor IVA. El total sigue cuadrando con el de la factura.
- **Orden:** las lineas involucradas pasan al 0 %, y si la orden no tiene una
  linea de mayor valor IVA sin facturar, se le agrega (mensaje en su chatter).
  Si la tiene, se reusa.
- **Reglas:** `TAX_RATE_MISMATCH` no corre (la orden al 0 % es la practica);
  `AMOUNT_TOTAL` compara sin IVA y sin las lineas de IVA y flete de la orden;
  `TAX_AS_PRODUCT` compara el IVA con la linea de la orden: OK si coincide,
  observacion si no coincide o si la orden no la tiene.
- **Notas:** llevan la linea de IVA, pero no tocan la orden (DECISIONS #33).
- Activo sin producto o sin impuesto, *Crear factura* lo pide.

**Fletes (con o sin el ajuste):** el cargo ya no queda suelto en la factura
(cambia #35). Se liga a una linea del producto de flete de la orden; si no la
hay, se agrega. El flete y el mayor valor IVA de la orden no se ofrecen al
emparejador: se ligan al crear la factura.

## 44. Documentos y datos de facturacion del proveedor en "Editar informacion"

Pedido del usuario el 2026-10-01. El proveedor usa el formulario estandar del
portal (`/my/account`) para cargar camara de comercio, RUT y certificacion de
cuenta bancaria, y sus datos de facturacion electronica de Jorels. Codigo en
archivos propios: `controllers/portal_account.py`,
`views/portal_account_templates.xml`, `models/res_partner_documents.py`,
`views/res_partner_documents_views.xml`.

- **Solo para proveedores habilitados** (`portal_invoice_enabled` en el
  comercial). A los demas usuarios portal el formulario no les cambia y un
  envio con estos campos se rechaza como *Unknown field*. Si compras quiere
  pedir documentos antes de habilitar, habria que ampliar la condicion en
  `_spr_account_partner`.
- **Se escribe en el contacto comercial**, con sudo, aunque el usuario sea un
  contacto hijo: los documentos y los datos DIAN son de la empresa (Jorels
  muestra su pestana solo en el comercial). El contacto nunca viene del
  formulario. El resto (nombre, NIT, direccion) sigue el camino de Odoo, con
  su bloqueo `can_edit_vat` intacto: un hijo sigue sin poder cambiar el NIT.
- **Jorels es condicional.** Campos `type_regime_id`, `type_liability_id`,
  `municipality_id` (Many2one) y `email_edi` (Char), leidos en
  `l10n_co_edi_jorels/models/res_partner.py` de staging (version
  17.0.24.05.010000). No se agrega a `depends`: si el campo no esta en
  `res.partner._fields`, no se muestra ni se acepta. El codigo soporta
  many2one, many2many y texto por si Jorels cambia el tipo de alguno. Las
  opciones se leen con sudo (el portal no tiene ACL en las listas de Jorels) y
  solo se aceptan ids que existan en el modelo del campo.
- **Mecanismo**: se extiende `_get_optional_fields` (asi
  `details_form_validate` no los tiene por desconocidos),
  `details_form_validate` (ids validos, correo, tipo y tamano de archivo) y
  `on_account_update`, que saca estos campos de `values` antes de que Odoo
  escriba el contacto del usuario. Los valores de la pagina se agregan al
  `qcontext` de la respuesta de `account()`.
- **Documentos**: `Binary` con `attachment=True` y su `_filename`, con
  `groups="base.group_user"`: el usuario portal no los lee por ORM, el portal
  solo le muestra el nombre del archivo. Se aceptan PDF, PNG y JPEG
  reconocidos por la firma del contenido, hasta 10 MB (`MAX_DOC_BYTES`). Un
  input sin archivo no borra el que habia. Con cualquier error no se guarda
  nada; el navegador no conserva los archivos elegidos al volver el formulario.
- **Chatter**: cada envio con cambios deja una nota en el comercial con el
  autor (el contacto que envio) y la lista de documentos (con nombre de
  archivo) y campos de facturacion cambiados.
- En el backend los documentos van en el grupo *Portal de facturas* de la ficha
  (`view_partner_form_spr`), heredando esa vista sin editarla.

Pruebas: sin Jorels, el camino generico (many2one, many2many, texto) se
prueba cambiando la lista por `title`, `category_id` y `ref`.
`test_jorels_fields_when_installed` solo corre si Jorels esta: se paso en una
base desechable con la copia local `addons/l10n_co_edi_jorels` (otra
revision que staging, con los mismos cuatro campos). Ahi `base_vat` exige DV valido con pais
Colombia, por eso los tests usan `900123456-8`. Falta verlo en pantalla con
datos reales de staging (municipios: un select de unas 1.100 opciones).

## 45. XML obligatorio para obligados a facturar; arrastrar y soltar archivos

Pedido del usuario el 2026-10-01.

- **XML.** Un proveedor obligado a facturar (sin la casilla *No obligado a
  facturar*) tiene que adjuntar el XML de la DIAN en facturas y notas: sin el
  no pasa del formulario, y el campo CUFE ya no se muestra porque sale del XML.
  Para los no obligados sigue opcional y basta el CUFE. Solo aplica al portal:
  lo que contabilidad captura en el backend no cambia.
- **Arrastrar y soltar.** Los campos de archivo del portal (PDF y XML al
  radicar; Camara de Comercio, RUT y certificacion bancaria en *Edit
  information*) van dentro de una zona `.o_spr_dropzone`. El input es
  transparente y cubre la zona, asi que el clic abre el dialogo y soltar el
  archivo funciona sin JS; `static/src/js/spr_dropzone.js` (eventos delegados)
  solo resalta la zona y muestra el nombre del archivo.

## 46. Una orden con una radicacion en curso no acepta otra factura

Encontrado en la prueba del usuario el 2026-10-01: la RPS4584 sobre P42693
quedo *Aprobada* y el portal seguia ofreciendo la orden, porque todavia no
tenia factura en Odoo (#40 solo miraba facturas). Se radico dos veces mas;
salieron rechazadas por CUFE repetido, pero no debia poder intentarse.

`purchase.order._spr_open_requests`: radicaciones de factura o cuenta de
cobro sobre la orden que no esten rechazadas ni canceladas. Con alguna, el
portal no ofrece la orden (ni el boton de la orden) y `PO_STATE` rechaza otra
solicitud sobre ella. Rechazada la radicacion, la orden vuelve a ofrecerse
para que el proveedor corrija. Las notas no cuentan.

## 47. Solo PDF livianos y sin contrasena; vista previa de los documentos

Pedido del usuario el 2026-10-01.

- **PDF.** `services/pdf_check.py`: todo PDF que se adjunta (el de la
  radicacion y los tres documentos del proveedor) debe ser PDF, de maximo
  **2 MB** y sin contrasena ni cifrado. 2 MB porque en staging los documentos
  reales pesan 180-520 KB y el p95 de los PDF de facturas es 95 KB. El cifrado
  se detecta con pypdf si esta (Odoo.sh lo trae) y si no, por el `/Encrypt`
  del trailer. Los documentos ya no aceptan imagenes (cambia #44): asi se
  pueden previsualizar igual en todos lados. Tambien hay constraint en
  `res.partner` para lo que se cargue desde el backend. El XML sigue en 10 MB.
- **Vista previa en el backend:** pestana *Documentos del proveedor* en la
  ficha del contacto con `widget="pdf_viewer"` para cada documento.
- **Vista previa en el portal:** en *Edit information*, "Ver" despliega el PDF
  en un iframe (lazy, no se descarga hasta abrirlo) y "Abrir" lo abre en otra
  pestana. Ruta `/my/account/document/<campo>`: solo los tres campos y solo
  del contacto comercial del usuario conectado; no lleva id, asi que no se
  puede pedir el de otro proveedor.

## 48. Pestana "Portal de proveedor"; los documentos son adjuntos del contacto

Pedido del usuario el 2026-10-01. Version 17.0.1.2.0.

- **Pestana.** *Puede radicar facturas en el portal* y *No obligado a facturar*
  salen del grupo junto al NIT (#42) y van, con los documentos, en una sola
  pestana **Portal de proveedor** de la ficha del contacto (solo comercial,
  solo Responsable).
- **Documentos.** Camara de comercio, RUT y certificacion bancaria dejan de
  ser campos binarios propios (#44): cada uno es un Many2one a `ir.attachment`
  (`spr_doc_*_id`) con dominio a los PDF adjuntos al mismo contacto. En el
  backend se adjunta el PDF en el chatter y se elige en el campo; desde el
  portal, el archivo se crea como adjunto del contacto comercial, se enlaza al
  campo y va en la nota del chatter. La vista previa es un related a
  `datas` del adjunto (`spr_doc_*_preview`, `pdf_viewer`). La constraint exige
  que sea adjunto del propio contacto y PDF liviano sin contrasena (#47).
  Reemplazar un documento deja el anterior en el chatter como historial.
- **Migracion** (`migrations/17.0.1.2.0/post-migrate.py`): reusa el adjunto
  del campo binario viejo, le quita `res_field` (asi aparece en el chatter), le
  pone el nombre de archivo guardado y lo enlaza. Probada simulando el estado
  de staging.

## 49. Los campos del portal en contacto, orden y factura son solo de sus grupos

Error en produccion el 2026-10-01 (version 17.0.1.2.1): el boton de cliente
Leal del POS (`odooleal`, `btn_leal_cnt.js`) lee el contacto con
`searchRead("res.partner", domain, [])`, es decir, **todos los campos**. Eso
calculaba `spr_request_count` / `spr_request_ids`, que leen
`supplier.payment.request`, y el cajero no tiene acceso: *No puede acceder a
los registros 'Solicitud de pago de proveedor'*. Cualquier "leer todo" de un
usuario sin los grupos del portal (exportar, otro modulo, una integracion)
habria fallado igual, tambien en ordenes de compra y facturas.

- `spr_request_ids` y `spr_request_count` en `res.partner`, `purchase.order` y
  `account.move` llevan `groups="...group_spr_validator"` (Responsable lo
  implica): para los demas usuarios el campo no existe en vez de fallar.
- Los documentos del proveedor y sus vistas previas (#48) pasan de
  `base.group_user` a `group_spr_manager`: ademas del permiso, un "leer todo"
  del POS ya no descarga los PDF de cada contacto.
- `portal_invoice_enabled` y `spr_support_document` quedan sin grupo: son
  booleanos simples y no leen otros modelos.
- El portal no cambia: lee y escribe esos campos con sudo.

Test de regresion `test_user_without_portal_groups_can_read_all_fields`
(usuario interno + compras + facturacion, `search_read` con `[]`): falla con
el mismo AccessError sin el cambio.

## 50. El portal solo ofrece ordenes de compra del ano en curso

Pedido del usuario el 2026-10-08: al proveedor le aparecian en el formulario
de radicacion ordenes del ano pasado. `purchase.order._spr_year_start()`
calcula el 1 de enero a las 00:00 en la zona de la compania (por defecto
America/Bogota) y se usa en `_spr_open_orders` del controlador y en
`_spr_accepts_invoice` (boton de /my/purchase/<id>). El envio del formulario
pasa por el mismo filtro, asi que forzar el id de una orden vieja tampoco
crea la solicitud.

- Se filtra por `date_order` (fecha de la orden), no por fecha de
  confirmacion ni de recepcion.
- Consecuencia aceptada: una orden de diciembre recibida en enero ya no se
  radica por el portal; contabilidad la registra en el backend, donde no hay
  filtro de ano.
- Las notas credito y debito no cambian: se radican contra la factura, no
  contra la orden.

Test `test_orders_from_last_year_are_not_offered`.

## 51. Revision de unidades con IA y accion "Validar con IA"

Pedido del usuario el 2026-10-08: las unidades del XML no coinciden con las de
la orden aunque el total si (la factura cobra 2 bultos a 980.000 y la orden
pide 140 kg a 14.000). Sin conversion eso daba PRICE_UNIT_MISMATCH
(observaciones) o QTY_OVER_PO (rechazo).

- La linea de la solicitud guarda la unidad del XML (`unit_code`) y un
  **factor de unidad** (`uom_factor`, unidades de la orden por unidad de la
  factura). `po_quantity = quantity x factor` y `po_price_unit = price_unit /
  factor`; el subtotal no cambia. Las reglas de cantidad y precio comparan en
  la unidad de la orden y la factura de Odoo se crea con esos valores, asi la
  cantidad facturada de la orden queda bien.
- `_ai_review_units`: en cada validacion, despues de las reglas, si hay lineas
  emparejadas cuyo precio no cuadra con la orden y la IA esta configurada, se
  le piden los factores (prompt y esquema propios, mismos datos sin NIT ni
  nombres). Un factor **solo se aplica** si la confianza es >= 0,7 y el precio
  convertido queda dentro de la tolerancia porcentual del de la orden: la IA
  sugiere, la regla decide. Si cambio algo, las reglas corren otra vez antes
  de fijar el estado, asi que el proveedor y contabilidad reciben el veredicto
  ya revisado. El detalle queda en el chatter y como hallazgo OK
  `UOM_CONVERTED`.
- Es automatico: corre cuando la validacion iba a dejar la solicitud con
  observaciones (o rechazada) por cantidad o precio. No hay casilla aparte:
  depende de "Habilitar emparejamiento por IA".
- **Validar con IA**: boton del formulario y accion de servidor
  (`action_server_spr_validate_ai`, menu Accion de la lista y del formulario,
  grupo Validador) para validar varias a la vez. Exige IA configurada (si no,
  lo dice) y salta facturadas, canceladas y rechazadas por contabilidad.
- El validador puede poner el factor a mano; queda como emparejamiento manual
  y sobrevive a la revalidacion.
- El proveedor HTTP propio no implementa la revision de unidades: devuelve
  "no disponible" y la validacion sigue como antes.

Version 17.0.1.3.0. Tests `test_units_*` y `test_validate_ai_action`.

## 52. El instructivo en PDF se genera dentro del modulo y va adjunto solo

Pedido del usuario el 2026-10-08. Antes el PDF salia de un script externo
(`odoodkr17/instructivo_proveedores/construir_pdf.py`, reportlab) y compras
tenia que adjuntarlo a mano en el correo del boton *Enviar instructivo del
portal*.

- Reporte QWeb `report_spr_instructions` sobre `res.partner` (wkhtmltopdf,
  carta, `report/spr_instructions_report.xml`), con el mismo contenido del
  script. Es dinamico: nombre y NIT de la compania, hora del cierre de fin de
  mes (`_spr_cutoff_label`) y, para no obligados a facturar, cuenta de cobro
  en vez del XML.
- Las 9 capturas viven en `static/src/img/instructivo/` (extraidas del PDF
  anterior, ya con datos de ejemplo) y se incrustan como data URI
  (`_spr_instructions_image`): wkhtmltopdf no siempre alcanza el servidor.
- La plantilla `mail_template_spr_instructions` lleva el reporte en
  `report_template_ids`; el asistente de correo de Odoo 17, en modo de un solo
  registro, lo genera y lo muestra ya adjunto. Como la plantilla es
  `noupdate`, la migracion 17.0.1.4.0 se lo agrega en bases existentes.
- El script externo queda obsoleto; regenerar capturas sigue siendo con
  `capturas.py`, copiandolas luego a `static/src/img/instructivo/`.

Version 17.0.1.4.0. Test `test_instructions_pdf_is_attached_in_the_composer`.

## 53. El flete o envio con producto configurado queda aprobado

Pedido del usuario el 2026-10-20: la factura que trae envio (flete, domicilio,
mensajeria...) debe mapear al producto de flete y quedar **Aprobada**. Cambia
lo de #35, donde `EXTRA_CHARGES` era observacion.

- `EXTRA_CHARGES` es **OK** si hay producto de flete en Ajustes; el hallazgo
  dice el valor y el producto. Sin producto sigue como observacion, porque
  *Crear factura* lo exige.
- Mas nombres para detectarlo (plurales, encomienda, courier, shipping,
  delivery, freight) y tambien se reconoce por el codigo: una linea con la
  referencia interna o el codigo de barras del producto de flete es flete.
- La nota de la linea dice con que producto se registra.
- Sin tope de valor: el flete no esta en la orden y no hay contra que
  compararlo. Contabilidad lo ve en el resumen al registrar.
- Textos del portal, del instructivo y de la plantilla del instructivo
  ajustados. La plantilla es `noupdate`: en bases existentes conserva el texto
  viejo ("contabilidad lo revisa"), no se migra porque contabilidad pudo
  editarla.

Version 17.0.1.5.0. Tests `test_freight_is_an_extra_charge_not_an_error`,
`test_freight_matched_by_product_code` y `test_charge_keywords`.

## 54. Ir a las ordenes de compra y ver el PDF y el XML sin descargarlos

Pedido del usuario el 2026-10-20.

- Boton **Ordenes de compra** en el formulario (`action_view_purchases`):
  con una orden abre su formulario; con varias, la lista filtrada. Las
  etiquetas del campo siguen igual, porque en Odoo 17 no abren el registro.
- Pestana **Vista del PDF**: el mismo `pdf_file` con `pdf_viewer`, solo
  lectura. La pestana *Adjuntos* sigue para descargar o reemplazar.
- Pestana **Vista del XML**: `xml_preview`, calculado y sin guardar, con el
  XML con sangria en el editor `ace` de solo lectura. Si es un
  AttachedDocument, primero va la factura embebida (`pretty_xml`, mismo parser
  endurecido del resto). Un XML danado muestra el error en vez de romper el
  formulario.

Version 17.0.1.6.0. Test `test_open_orders_and_preview_files`.

## 55. La vista previa es un boton en Adjuntos y el PDF va al chatter

Pedido del usuario el 2026-10-20, ajusta #54: sin pestanas de vista previa.

- Widget `spr_preview_binary` (hereda el binario de Odoo): un **ojo** al lado
  de editar, descargar y eliminar. El PDF abre en un dialogo con el visor
  PDF.js de Odoo; el XML, en un dialogo con `spr_xml_preview` (el `pretty_xml`
  de #54). Solo aparece con la solicitud guardada y sin cambios pendientes,
  igual que el de descargar.
- Se quitan las pestanas *Vista del PDF* y *Vista del XML* y el campo
  calculado `xml_preview`.
- El modelo hereda `mail.thread.main.attachment`. Al crear la solicitud o
  cambiar el PDF se crea una copia del PDF como adjunto del chatter (el
  `pdf_file` vive en un adjunto con `res_field`, que el chatter no muestra),
  se postea como nota interna y queda como adjunto principal. Con
  `<div class="o_attachment_preview"/>` el PDF se ve al lado del formulario
  en pantallas anchas (XXL), como en las facturas de proveedor.
- Migracion 17.0.1.7.0: copia el PDF de las solicitudes que ya existen, sin
  mensaje; salta las que no tienen el archivo en el filestore.

Version 17.0.1.7.0. Test `test_open_orders_and_preview_files`.

## 56. Pestana "Orden de compra" en la solicitud

Pedido del usuario el 2026-10-20: ver la orden desde la solicitud para validar
rapido, sin salir de ella (el boton de #54 la abre en otra pantalla).

- `purchase_line_ids`, calculado y sin guardar: las lineas de todas las
  ordenes de la solicitud (sin secciones ni notas). Solo lectura, con orden,
  producto, cantidad pedida, recibida, facturada y por facturar, unidad,
  precio, impuestos y subtotal. Las que ya no tienen nada por facturar salen
  en gris.
- Arriba, subtotal y total de la factura frente a los de las ordenes
  (`purchase_amount_untaxed`, `purchase_amount_total`).
- Con el IVA como producto (#43), el subtotal de las ordenes no suma la
  linea de mayor valor IVA (si el total); si no, la comparacion con el
  subtotal de la factura salia descuadrada por el IVA.
- Sin sudo: quien valida ya necesita leer compras para emparejar lineas.

Version 17.0.1.8.0. Test `test_open_orders_and_preview_files`.
