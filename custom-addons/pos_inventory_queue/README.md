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

El resultado es un `SerializationFailure` (PostgreSQL error `40001`) que Odoo captura silenciosamente, dejando el picking sin procesar y el inventario sin actualizar.

## Solución

Un módulo que intercepta la creación de pickings POS y los canaliza a una cola persistente. Cada picking se procesa **uno a la vez**, en orden de entrada, garantizando consistencia sin modificar la lógica de negocio de Odoo.

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

### Mecanismo de concurrencia

1. **Claim atómico** — `SELECT ... FOR UPDATE SKIP LOCKED` selecciona el siguiente item pendiente. Si otro worker ya lo reclamó, se salta automáticamente.

2. **Cursor aislado** — Cada item se procesa en un cursor de base de datos independiente. Un `SerializationFailure` en un item no afecta a los demás.

3. **Retry con backoff exponencial** — Si ocurre un conflicto de serialización, se reintenta con backoff de 50ms, 100ms, 200ms, 400ms, 800ms.

4. **Savepoints** — Cada reintento se ejecuta dentro de un savepoint, permitiendo rollback sin perder la transacción completa.

### Ciclo de vida de un item

```
pending ──→ processing ──→ done
                │
                ├──→ failed ──→ (retry manual) ──→ pending
                │
                └──→ failed_permanent (después de 5 intentos)
```

| Estado | Descripción |
|--------|-------------|
| `pending` | Esperando ser procesado |
| `processing` | Siendo procesado por un worker |
| `done` | Procesado exitosamente |
| `failed` | Error transitorio, reintentable |
| `failed_permanent` | Error después de 5 intentos, requiere intervención manual |

## Instalación

```bash
odoo-bin -u pos_inventory_queue -d <base_de_datos> --stop-after-init
```

El módulo depende únicamente de `point_of_sale`.

## Configuración

### Requisito: Inventario en tiempo real

El módulo solo actúa cuando el POS está configurado para procesar inventario en tiempo real:

**Contabilidad > Configuración > Empresas >** `Actualizar cantidades de inventario` = **"En tiempo real"**

Si está configurado en **"Al cierre de la sesión"**, el módulo no interviene. Odoo comporta exactamente igual que antes.

### Excepción: Facturación electrónica (Anglo-Saxon)

Cuando la contabilidad anglo-saxon está activa y la orden se factura (`to_invoice = True`), Odoo genera el picking inmediatamente **incluso si la configuración es "Al cierre"**. En este caso, el módulo sí interviene y serializa el procesamiento.

## Uso

### Monitorear la cola

**Menú: Point of Sale > Inventory Queue**

Vista de lista con colores por estado:
- Azul: pendiente
- Amarillo: procesando
- Verde: completado
- Rojo: fallido

Filtros disponibles:
- Pendientes
- Procesando
- Completados
- Fallidos

Agrupaciones: por estado, picking, orden POS.

### Reintentar items fallidos

1. Ir a **Inventory Queue**
2. Buscar items en estado **Failed Permanent**
3. Abrir el item
4. Hacer clic en **Retry**

El item vuelve a `pending` y se procesa en el siguiente ciclo.

### Script de prueba de concurrencia

Para probar que la serialización funciona bajo carga concurrente:

```bash
python3 test_pos_inventory_concurrency.py \
    --config /etc/odoo/odoo.conf \
    --db <base_de_datos> \
    --session "POS/00147" \
    --template-id 62 \
    --workers 5
```

Este script crea N pickings concurrentes y verifica que:
- Solo un item está en `processing` a la vez
- Todos los items terminan en `done`
- El stock final coincide con el esperado

## Manejo de errores

### SerializationFailure (PostgreSQL `40001`)

Ocurre cuando dos transacciones intentan modificar la misma fila de `stock.quant` simultáneamente.

**Comportamiento del módulo:**
1. Rollback de la transacción actual
2. Espera exponencial (50ms → 800ms)
3. Reintento con savepoint nuevo
4. Después de 5 intentos: `failed_permanent`

### Errores generales

Cualquier otro error (permisos, datos faltantes, validaciones de Odoo) sigue la misma política de retry.

### Errores silenciados por Odoo

En el flujo estándar, Odoo envuelve `_action_done()` en un `try/except` que captura `UserError` y `ValidationError`, silenciando el error. El picking queda en estado borrador y el inventario no se actualiza, pero el usuario ve el recibo sin saber que falló.

Este módulo **elimina ese silencio**: los errores se registran en `error_message` y el item queda en `failed_permanent` para revisión.

## Detalles técnicos

### Datos que NO se modifican

El módulo no altera:
- La creación de la orden POS
- La factura electrónica
- La creación de la factura
- Los productos, cantidades, ubicaciones
- Lotes, series, valoración de inventario
- La configuración de Odoo
- El cierre de sesión POS

### Datos que SÍ se agregan

| Modelo | Campo | Descripción |
|--------|-------|-------------|
| `stock.picking` | `pos_order_id` | Referencia a la orden POS |
| `pos.inventory.queue` | Nuevo modelo | Cola de serialización |

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
├── __init__.py
├── __manifest__.py
├── README.md
├── data/
│   ├── ir_sequence.xml          # Secuencia PIQ/000001
│   └── ir_cron.xml              # Cron de limpieza
├── models/
│   ├── __init__.py
│   ├── inventory_queue.py       # Modelo de cola + lógica de concurrencia
│   ├── pos_order.py             # Override de _create_order_picking
│   └── stock_picking.py         # Override de _create_picking_from_pos_order_lines
├── security/
│   └── ir.model.access.csv      # Permisos
├── views/
│   └── pos_inventory_queue_views.xml  # Tree, form, search, action, menú
└── tests/
    ├── __init__.py
    └── test_queue_model.py      # Tests formales (TransactionCase)
```

## Pruebas

### Tests unitarios

```bash
odoo-bin -d <base_de_datos> --test-tags /pos_inventory_queue --stop-after-init
```

Cobertura:
- Generación de secuencia
- Prevención de duplicados
- Estados por defecto
- Retry desde `failed_permanent`
- Claim de items
- Procesamiento completo
- Orden de procesamiento

### Prueba de concurrencia

```bash
python3 test_pos_inventory_concurrency.py \
    --config /etc/odoo/odoo.conf \
    --db <base_de_datos> \
    --workers 5
```

## Licencia

LGPL-3

## Autor

Miguel Bolivar — Libertario Coffee
