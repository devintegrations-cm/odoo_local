# POS Inventory Queue

Serializa las operaciones de inventario en tiempo real del POS para prevenir concurrencia y deadlocks en PostgreSQL.

---

## Problema

Cuando múltiples terminales POS facturan simultáneamente con inventario configurado **"En tiempo real"**, cada factura genera un `stock.picking` que ejecuta `_action_done()`. Este método modifica `stock.quant`, adquiriendo bloqueos a nivel de fila en PostgreSQL.

Cuando dos o más transacciones intentan modificar el mismo `stock.quant` al mismo tiempo:

```
POS A ──→ _action_done() ──→ UPDATE stock.quant WHERE id = 101 ──→ LOCK
POS B ──→ _action_done() ──→ UPDATE stock.quant WHERE id = 101 ──→ WAIT / DEADLOCK
```

El resultado es un `SerializationFailure` (PostgreSQL error `40001`). Odoo sí reintenta (5 veces vía `service.model.retrying` con `savepoint`), pero al agotar los reintentos **devuelve la orden a borrador** y el cajero ve un error de sincronización; al reenviar, la colisión se repite. El inventario queda inconsistente y las cajas se pisan entre sí dentro del mismo request HTTP (límite ~66 s en Odoo.sh).

## Solución

Un módulo que intercepta la creación de pickings POS en tiempo real y los canaliza a una **cola persistente** procesada por el **worker de cron**, fuera del request del cajero. Un `pg_advisory_xact_lock` por **(producto, compañía)** serializa las escrituras que colisionan, de modo que dos cajas que venden el mismo producto **dejan de chocar dentro del request**: la venta se confirma rápido y el descuento de inventario se resuelve cuando cada ítem toma su turno. El pedido no cae a borrador y el cajero no reenvía.

> Nota de alcance: la cola quita la **colisión**; no elimina el **combustible** (stock negativo, ítems de menú almacenables, PDF/EDI síncrono, cron DIAN). Esas se tratan en el plan global aparte.

```
POS A ──→ picking ──→ COLA #1 ──→ PROCESANDO ──→ stock OK
POS B ──→ picking ──→ COLA #2 ──→ ESPERA     ──→ PROCESANDO ──→ stock OK
POS C ──→ picking ──→ COLA #3 ──→ ESPERA     ──→ ESPERA      ──→ PROCESANDO
```

## Cómo funciona

### Arquitectura

```
                         ┌────────────────────┐
                         │       POS          │
                         └─────────┬──────────┘
                                   │
                                   ▼
                              pos.order
                                   │
                                   ▼
                        _create_order_picking()
                                   │
                                   ▼
                           stock.picking
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │  POS INVENTORY QUEUE     │
                    │                          │
                    │  #1  PROCESSING          │
                    │  #2  PENDING             │
                    │  #3  PENDING             │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                         _action_done()
                                 │
                                 ▼
                            stock.quant
                                 │
                                 ▼
                               DONE
                                 │
                                 ▼
                         siguiente de cola
```

### Cómo se dispara el procesamiento

El encolado **no procesa dentro del request del cajero**. Al crear la venta, `stock_picking` encola los ítems y registra `ir.cron._trigger()`: el trigger se escribe en la MISMA transacción de la venta, así que al commitearse todo queda visible atómicamente (sin carrera). El **worker de cron** (límite ~330 s, conexión aparte) drena la cola, sin competir con los requests HTTP ni con el límite de 66 s de la petición. Un cron cada 1 minuto es la red de seguridad.

### Mecanismo de concurrencia

1. **Claim atómico** — `SELECT ... FOR UPDATE SKIP LOCKED` selecciona el siguiente item reclamable. Si otro worker ya lo tomó, se salta automáticamente. Se reclama `pending`, `processing` obsoleto (>5 min tras un crash) o `failed` cuyo `next_retry_date` ya venció.

2. **Superusuario + compañía del picking** — Cada ítem se procesa con `SUPERUSER_ID` y `with_company(picking.company_id)` en un cursor aislado del pool normal de Odoo (`self.env.registry.cursor()`). Así el drenaje (que puede tocar pickings de otras tiendas) nunca falla por los permisos o las compañías del cajero que sincronizó.

3. **Lock por recurso de valoración** — Antes de `_action_done()` se adquieren `pg_advisory_xact_lock` por **(producto, compañía)** (ordenados asc para evitar deadlock). `stock_valuation_layer` no tiene ubicación, así que dos tiendas que venden el mismo producto chocan en las mismas capas AVCO aunque sus `stock.quant` sean distintos: el lock debe ser por producto+compañía, no por ubicación.

4. **Dos políticas de reintento distintas:**
   - **Contención** (`SerializationFailure` / `lock_not_available`): reintentos rápidos dentro del mismo ciclo con backoff exponencial (50 ms → ~800 ms). Si se agotan, el ítem **cede a `pending`** (no es un error de lógica, solo hay pugna; otro drenador/cron lo retoma). No consume los ciclos de fallo.
   - **Error de lógica** (UserError, constraint, lote, etc.): **no sirve reintentar ya** (el error se repite). Se marca `failed` con backoff diferido `next_retry_date = now + 2^n minutos` (2, 4, 8, 16, 32…). El cron lo re-clama cuando vence, dando tiempo a corregir la causa raíz. Solo tras agotar `MAX_RETRIES` ciclos (≈1 h acumulado) pasa a `failed_permanent` y genera **alerta** (`mail.activity` a los gestores de inventario + log `POS Queue: PERMANENT`).

5. **Savepoints + `lock_timeout`** — Cada intento corre en savepoint y repite `SET LOCAL lock_timeout` al inicio (es `TRANSACTION-scoped`: un rollback lo borra), para que un lock de fila ajeno en `idle in transaction` no cuelgue el intento indefinidamente.

6. **Presupuesto de tiempo** — `_process_queue(time_budget=240)` sale limpio antes del límite del worker de cron y re-dispara el trigger, sin dejar ítems `processing` huérfanos.

### Ciclo de vida de un item

```
pending ──→ processing ──→ done
                 │
                 ├──(contención: pugna DB)──→ pending        (cede, sin consumir ciclos)
                 │
                 └──(error de lógica)──→ failed ──(2^n min)──→ pending ─┐
                                            │                           │
                                            └──── agota MAX_RETRIES ────┘
                                                          └──→ failed_permanent (alerta)
```

| Estado | Descripción |
|--------|-------------|
| `pending` | Esperando ser procesado |
| `processing` | Siendo procesado (reclama stale a los 5 min si el worker murió) |
| `done` | Procesado exitosamente |
| `failed` | Error de lógica con reintento diferido (`next_retry_date = now + 2^n min`) |
| `failed_permanent` | Agotados los ciclos; requiere intervención manual; dispara alerta |

## Instalación

```bash
odoo-bin -u pos_inventory_queue -d <base_de_datos> --stop-after-init
```

El módulo depende únicamente de `point_of_sale`.

## Configuración

### Interruptor GLOBAL (activar / desactivar la cola)

**Menú: Point of Sale > Configuración > Inventario (Inventory)**

Es un switch **GLOBAL** (se guarda en `ir.config_parameter` clave `pos_inventory_queue.enabled`, default activado). No depende del terminal: afecta a todas las cajas.

- **Activado:** los pickings POS en tiempo real se encolan y serializa su validación.
- **Desactivado:** los pickings se validan de forma sincronizada (comportamiento nativo, como si el módulo no existiera). La cola **no se abandona**: los ítems ya pendientes terminan de procesarse (el procesador siempre drena, vea abajo).

### Requisito: Inventario en tiempo real

El módulo solo actúa cuando el POS genera el picking en tiempo real:

El módulo solo actúa cuando el POS está configurado para procesar inventario en tiempo real:

**Contabilidad > Configuración > Empresas >** `Actualizar cantidades de inventario` = **"En tiempo real"**

Si está configurado en **"Al cierre de la sesión"**, el módulo no interviene. Odoo comporta exactamente igual que antes.

### Excepción: Facturación electrónica (Anglo-Saxon)

Cuando la contabilidad anglo-saxon está activa y la orden se factura (`to_invoice = True`), Odoo genera el picking inmediatamente **incluso si la configuración es "Al cierre"**. En este caso, el módulo sí interviene y serializa el procesamiento.

### Contabilidad anglosajona: conciliación de la cuenta transitoria

Con anglosajona + valoración en tiempo real, la factura se asienta **antes** de que la cola valide el picking. Para que la cuenta transitoria de salida se concilie cuando el picking se procesa después, el módulo sobreescribe `stock.move._get_related_invoices()` devolviendo también la factura `posted` del pedido POS (mismo patrón que `sale_stock`). Sin esto la transitoria quedaría sin conciliar.

### Guarda de cierre de sesión

`pos.session._validate_session()` (override en `models/pos_session.py`): antes de cerrar, drena **en línea** los ítems pendientes de esa sesión; si aún queda alguno sin validar (`failed_permanent` u otros), **bloquea el cierre** con un `UserError` que lista las referencias. Evita cerrar con asiento de cierre / conciliación incompletos. El campo `queue_pending_count` de la sesión refleja los pendientes.

## Uso

### Monitorear la cola

**Menú: Point of Sale > Órdenes > Cola de Inventario**

Abre con el filtro **"Pendientes + Fallidos"** por defecto (`needs_attention`: `pending/processing/failed/failed_permanent`), que es lo que requiere atención. Vista de lista con colores por estado:
- Azul: pendiente
- Amarillo: procesando
- Verde: completado
- Rojo: fallido / fallido permanente

Filtros: Pendientes+Fallidos, Pending, Processing, Done, Failed. Agrupaciones: estado, picking, orden POS.

### Acciones del formulario (managers)

- **Procesar ahora** — registra `ir.cron._trigger()` y muestra una notificación. **No** procesa en el request (no bloquea el navegador). El worker de cron drena en segundos.
- **Retry** (visible en `failed`/`failed_permanent`) — resetea el ítem a `pending` (limpia `retry_count` y `next_retry_date`) y dispara el cron.

### Alertas

Al pasar a `failed_permanent` se crea una `mail.activity` para los gestores de inventario (`stock.group_stock_manager`) de la compañía del picking y se registra el log con prefijo estable `POS Queue: PERMANENT`. El drenaje emite además un `POS Queue: summary reason=... done=N contention=N failed=N permanent=N elapsed=Ns` por pasada, para monitoreo con `analyze_logs.py`.

### Script de prueba de concurrencia

Para probar que la serialización funciona bajo carga concurrente:

```bash
python3 tools/test_pos_inventory_concurrency.py \
    --config /etc/odoo/odoo.conf \
    --db <base_de_datos> \
    --session "POS/00147" \
    --template-id 62 \
    --workers 5
```

> Los scripts de carga viven en `tools/` (son standalone con IDs/parámetros de entorno, no `unittest`). No forman parte de la suite automática de Odoo.

Este script crea N pickings concurrentes y verifica que:
- Solo un item está en `processing` a la vez
- Todos los items terminan en `done`
- El stock final coincide con el esperado

## Manejo de errores

### Contención: SerializationFailure (`40001`) / `lock_not_available` (`55P03`)

Ocurre cuando dos transacciones pelean por la misma fila (`stock.quant`, `stock_valuation_layer`). **No es un error de lógica**, así que NO consume los ciclos de fallo:

1. Rollback del intento actual
2. Backoff exponencial corto (50 ms → ~800 ms) + savepoint nuevo
3. Reintento dentro del mismo ciclo (hasta `MAX_RETRIES` intentos)
4. Si se agotan los intentos del ciclo → el ítem **cede a `pending`** para que el cron/otro drenador lo retome

### Errores de lógica (UserError, constraint, lote, permisos…)

Reintentar en el mismo ciclo no sirve (el error se repite). El ítem pasa a `failed` con `next_retry_date = now + 2^n min`; el cron lo re-clama al vencer. Tras agotar `MAX_RETRIES` ciclos (≈1 h) pasa a `failed_permanent` + alerta.

### Error de conexión (`OperationalError` / `InterfaceError`)

Si la conexión de BD cae durante el procesamiento, el cursor aislado ya no sirve. No se marca permanente: el ítem queda en `processing` y el cron lo reclama como obsoleto tras `STALE_PROCESSING_MINUTES`. Nunca se propaga al worker de cron.

### Errores visibilizados (no silenciados)

En el flujo estándar Odoo envuelve `_action_done()` en un `try/except` que captura `UserError`/`ValidationError` y **devuelve la orden a borrador**; el usuario ve el recibo sin saber que el inventario falló. Este módulo **elimina ese silencio**: el error queda en `error_message`, con reintentos diferidos y, si persiste, `failed_permanent` + `mail.activity` para revisión.

## Detalles técnicos

### Datos que NO se modifican

El módulo no altera:
- La creación de la orden POS
- La factura electrónica
- La creación de la factura
- Los productos, cantidades, ubicaciones
- Lotes, series, valoración de inventario
- La configuración de Odoo

### Datos que SÍ se agregan / sobreescriben

| Modelo | Campo/Método | Descripción |
|--------|-------------|-------------|
| `pos.inventory.queue` | Nuevo modelo | Cola de serialización |
| `pos.inventory.queue` | `next_retry_date` | Reintento diferido de errores de lógica |
| `stock.picking` | `pos_order_id` | Referencia a la orden POS |
| `pos.inventory.queue.config` | Nuevo (TransientModel) | Formulario del toggle en Configuración > Inventario |
| `stock.move` | `_get_related_invoices()` | Conciliación transitoria con anglosajona (P0-6) |
| `pos.session` | `_validate_session()`, `queue_pending_count` | Guarda de cierre + indicador (P0-7/P1-5) |

### Idempotencia

| Escenario | Comportamiento |
|-----------|----------------|
| `create()` con mismo picking | Retorna item existente, no crea duplicado |
| Dos workers reclaman el mismo item | Solo uno lo obtiene (`FOR UPDATE SKIP LOCKED`) |
| `_action_done()` sobre picking ya hecho | Odoo procesa moves vacíos, no modifica quants |
| `action_retry()` múltiples veces | Resetea a pending sin efectos secundarios |

### Limpieza automática

Un cron ejecuta cada 7 días y elimina items en estado `done` con más de 30 días de antigüedad.

## Estructura del módulo

```
pos_inventory_queue/
├── __manifest__.py
├── README.md
├── data/
│   ├── ir_sequence.xml              # Secuencia PIQ/000001
│   ├── ir_cron.xml                  # Cron drenaje (1 min) + limpieza (7 días)
│   └── ir_config_parameter.xml      # Default del toggle (noupdate)
├── models/
│   ├── __init__.py
│   ├── inventory_queue.py           # Cola: claim, procesamiento, locks, reintentos
│   ├── inventory_queue_config.py    # TransientModel: toggle en Configuración > Inventario
│   ├── stock_picking.py             # Encolado + _trigger() del cron
│   ├── stock_move.py                # _get_related_invoices (conciliación anglosajona)
│   ├── pos_order.py                 # Contexto pos_inventory_queue + _should_create_picking_real_time
│   └── pos_session.py               # Guarda de cierre + queue_pending_count
├── security/
│   └── ir.model.access.csv          # Permisos (cola + config)
├── views/
│   ├── pos_inventory_queue_views.xml       # Tree, form, search, action, menú (Órdenes)
│   └── inventory_queue_config_views.xml    # Form + acción + menú (Configuración > Inventario)
├── tools/
│   ├── test_pos_inventory_concurrency.py   # Script de carga (standalone, no unittest)
│   └── test_pos_invoice_concurrency.py     # Script de carga (standalone, no unittest)
└── tests/
    ├── __init__.py
    └── test_queue_model.py           # Tests formales (TransactionCase)
```

## Pruebas

### Tests unitarios (suite de Odoo)

```bash
odoo-bin -d <base_de_datos> -i pos_inventory_queue --test-tags /pos_inventory_queue --stop-after-init
```

Cobertura (`tests/test_queue_model.py`):
- Generación de secuencia y prevención de duplicados
- Estado por defecto y `action_retry` (resetea + trigger; luego drenaje → `done`)
- Claim de items, cola vacía, y `retry_count` en `pending`/`failed`
- **Gating de `next_retry_date`**: un `failed` con fecha futura NO se reclama; con fecha vencida SÍ
- Orden de procesamiento y drenaje completo

> `_get_related_invoices` (conciliación transitoria anglosajona) y la guarda de `_validate_session` se validan en **staging con carga real** (plan de despliegue §5), porque requieren el stack POS + contabilidad anglosajona + factura posted.

### Prueba de concurrencia (carga)

```bash
python3 tools/test_pos_inventory_concurrency.py \
    --config /etc/odoo/odoo.conf \
    --db <base_de_datos> \
    --workers 5
```

## Licencia

LGPL-3

## Autor

Miguel Bolivar — Libertario Coffee

---

## Herramientas de Testing

El módulo incluye dos scripts de testing en `tools/` para validar concurrencia real fuera del entorno de tests de Odoo.

### 1. `test_pos_inventory_concurrency.py` — Concurrencia de pickings

Valida que múltiples workers procesando la cola simultáneamente no duplican stock ni dejan items stuck.

```bash
docker compose -f stack.yml run --rm web python3 \
    tools/test_pos_inventory_concurrency.py \
    --config /etc/odoo/odoo.conf \
    --db pruebas \
    --session "POS/00148" \
    --template-id 62 \
    --workers 5
```

**Parámetros:**
- `--config`: Ruta al `odoo.conf`
- `--db`: Nombre de la base de datos
- `--session`: Nombre de la sesión POS (default: `POS/00148`)
- `--template-id`: ID del `product.template` a usar (default: `62`)
- `--workers`: Número de workers concurrentes (default: `5`, mínimo: `2`)

**Qué valida:**
- Stock final == stock inicial - (workers × quantity)
- Todos los items terminan en estado `done`
- Pickings se procesan en orden de sequence
- No hay errores en la cola

### 2. `test_pos_invoice_concurrency.py` — Concurrencia de órdenes + facturas

Valida el flujo completo: crear orden POS → pago → picking → factura → cola, con N workers concurrentes.

```bash
docker compose -f stack.yml run --rm web python3 \
    tools/test_pos_invoice_concurrency.py \
    --config /etc/odoo/odoo.conf \
    --db pruebas \
    --session "POS/00148" \
    --product-id 81 \
    --partner-id 84 \
    --workers 50 \
    --quantity 1
```

**Parámetros:**
- `--config`: Ruta al `odoo.conf`
- `--db`: Nombre de la base de datos
- `--session`: Nombre de la sesión POS
- `--product-id`: ID del `product.product` (default: `81`)
- `--partner-id`: ID del `res.partner` (default: `84`)
- `--product-ids`: CSV de IDs de productos (repartidos entre workers)
- `--partner-ids`: CSV de IDs de partners
- `--sessions`: CSV de nombres de sesión (multi-POS)
- `--workers`: Número de workers (default: `100`)
- `--quantity`: Cantidad por orden (default: `1.0`)
- `--attempts`: Reintentos por worker (default: `4`)

**Qué valida:**
- Órdenes en estado `paid` o `invoiced`
- Facturas en estado `posted`
- Pickings en estado `done`
- Items de cola en estado `done`
- Stock final correcto
- Taxes pertenecen a la compañía del POS (multi-company)

### Notas

- Ambos scripts crean datos de prueba y los limpian al finalizar.
- Para `test_pos_invoice_concurrency.py` en Odoo.sh, usar `main_shell()` en lugar de `main_cli()`.
- Los scripts manejan `SerializationFailure` y `PoolError` con reintentos automáticos.
- El `--max-conn` (default: `64`) limita conexiones simultáneas para no agotar el pool de PostgreSQL.
