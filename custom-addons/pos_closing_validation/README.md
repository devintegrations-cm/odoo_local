# POS Closing Validation

Extensión del flujo de control de caja del Punto de Venta de Odoo 17.

Este módulo protege la integridad del efectivo mediante reglas de validación en apertura, operación y cierre de sesiones. No reemplaza la contabilidad estándar de Odoo: la complementa bloqueando operaciones inseguras antes de que ocurran.

---

## Funcionalidades

- **Límite configurable de movimientos de efectivo** por sesión (Cash In / Cash Out).
- **Advertencia al último movimiento permitido** antes de confirmar.
- **Bloqueo de aperturas nuevas** cuando existen sesiones de rescate pendientes.
- **Rescates no pueden generar movimientos de efectivo** (regla de negocio reforzada en frontend y backend).
- **Auditoría de continuidad de caja** — verifica que la apertura de una sesión nueva coincida con el cierre de la anterior.
- **Snapshot unificado** — una sola fuente de verdad para cálculos de efectivo.
- **Validación de diferencia autorizada** al cerrar sesión.
- **Cierre transaccional con bloqueo** (`FOR UPDATE`) que previene condiciones de carrera entre terminales.
- **Popup de cierre enriquecido** con desglose explícito: apertura, ventas, cash in, cash out, efectivo esperado.

---

## Requisitos

- Odoo 17.0
- Módulo `point_of_sale` instalado y configurado
- Python 3.8+

---

## Instalación

1. Copie el directorio `pos_closing_validation` en su carpeta de addons personalizados.
2. Actualice la lista de addons en Odoo.
3. Busque **"Pos Closing Validation"** en el menú de Apps.
4. Instale.

El módulo depende únicamente de `point_of_sale`. No requiere dependencias externas.

---

## Configuración

### En el Punto de Venta (`Point of Sale → Configuración → Puntos de Venta`)

| Campo | Descripción | Valor por defecto |
|-------|-------------|-------------------|
| **Máximo de movimientos de efectivo** | Número máximo de Cash In/Out permitidos por sesión (todas las sesiones, normales y de rescate). | 2 |
| **Mensaje de diferencia de efectivo** | Texto personalizado mostrado cuando la diferencia al cerrar supera el máximo. Vacío usa el mensaje por defecto. | vacío |
| **Validar sesiones de rescate** | Bloquea apertura de sesiones nuevas si hay rescates pendientes. | Falso |

### En `Ajustes → Punto de Venta`

Los mismos tres campos están expuestos en la configuración global para facilitar aplicación uniforme.

### Control de diferencia máxima

El módulo usa `set_maximum_difference` y `amount_authorized_diff` del POS estándar de Odoo. Si `set_maximum_difference` está activo, el cierre se bloquea cuando `|counted - expected| > amount_authorized_diff`.

---

## Reglas de negocio implementadas

### Regla 1 — Apertura bloqueada por rescate pendiente

Cuando un POS tiene una sesión de rescate (`rescue=True`) en cualquier estado distinto de `closed`, **no se permite abrir una nueva sesión normal**.

El mensaje de error lista las sesiones pendientes. El administrador debe cerrar cada rescate antes de continuar.

### Regla 2 — Rescates no pueden generar Cash In/Out

Una sesión de rescate es solo para **recuperar pedidos**, no para operar caja. Cualquier intento de Cash In o Cash Out desde el frontend muestra:

> El Punto de Venta no está sincronizado con la sesión actual. Actualice la página y vuelva a intentarlo.

El backend también lanza un `UserError` equivalente si alguien intenta saltarse la validación del frontend (RPC directo, script, etc.). Doble capa de defensa.

### Regla 3 — Auditoría de saldo inicial

Cuando una sesión normal abre después de otra cerrada, Odoo calcula el saldo esperado automáticamente (`cash_register_balance_start` = cierre anterior).

Este módulo **registra ese valor esperado** en un campo propio (`expected_opening_balance`) y lo compara contra lo que el operador ingresa en la apertura de caja. Si la diferencia supera `amount_authorized_diff`, la apertura se bloquea.

**Nota**: Esta regla NO aplica cuando el saldo esperado es exactamente `0.0` (caso legítimo de "cerrar con cero y abrir con float nuevo al día siguiente").

### Regla 4 — Snapshot unificado

El endpoint `get_closing_control_data()` es la única fuente de verdad para los cálculos del popup de cierre. Backend Y frontend consumen el mismo snapshot.

Fórmula de efectivo esperado:
```
expected = opening + cash_sales + cash_in − cash_out
```

**Los rescates NO se agregan a la fórmula.** La continuidad de caja se resuelve en apertura (Regla 1), no en cierre. Esto evita doble contabilización.

### Regla 5 — Cierre transaccional

Cuando el usuario confirma el cierre:
1. El backend adquire un `FOR UPDATE` sobre la fila de `pos_session`.
2. Invalida el caché del ORM para leer datos frescos.
3. Ejecuta todas las validaciones con datos actuales.
4. Solo si todo pasa, almacena `cash_register_balance_end_real` y continúa.

Si dos terminales intentan cerrar simultáneamente, el segundo espera al primero y luego ve el estado actualizado.

### Regla 6 — Integridad de datos en cierre

Al cerrar, se validan:
- Órdenes pagadas deben tener pagos registrados.
- No deben existir pagos huérfanos (sin orden asociada).
- Cantidad de movimientos Cash In/Out no debe exceder el límite configurado.

Cualquiera de estas inconsistencias bloquea el cierre.

---

## Errores comunes en el POS y qué hacer

### "No puede cerrar esta sesión de Punto de Venta. La diferencia de efectivo supera la diferencia máxima autorizada."

**Causa**: El dinero contado difiere del esperado más de `amount_authorized_diff`.

**Solución**:
1. Recuentar el efectivo.
2. Verificar que no falte ningún movimiento Cash In/Out no registrado.
3. Si la diferencia es correcta pero real, contacte a un responsable.

---

### "El Punto de Venta no está sincronizado con la sesión actual."

**Causa**: El navegador está mostrando una sesión antigua ya cerrada, o el usuario intentó hacer Cash In/Out en una sesión de rescate.

**Solución**:
1. Recargar el navegador (F5).
2. Si se carga una sesión de rescate, NO se puede operar caja — contacte al administrador para cerrarla.
3. Si no se puede recuperar, cierre sesión desde el backend y abra una nueva.

---

### "Existe(n) X sesión(es) de rescate pendiente(s) para este Punto de Venta."

**Causa**: Un administrador intentó abrir una nueva sesión normal mientras hay sesiones de rescate sin cerrar.

**Solución**: Ir al backend, localizar las sesiones de rescate (tienen "RESCATE" en el nombre), revisarlas, y cerrarlas antes de abrir otra sesión normal.

---

### "Se alcanzó el límite de movimientos de efectivo."

**Causa**: Se llegó al máximo de movimientos Cash In/Out configurados.

**Solución**: Ya no se permiten más movimientos en esta sesión. Para casos excepcionales, el administrador debe subir el límite (no recomendado por control) o cerrar esta sesión y abrir una nueva.

---

### "Existe una inconsistencia en los movimientos de efectivo."

**Causa**: La sesión tiene más movimientos Cash In/Out que el límite configurado. Ocurre si se bajó el límite después de registrar movimientos.

**Solución**: Un administrador debe restaurar el límite alto, cerrar la sesión normalmente, y luego ajustar el límite. Nunca baje el límite con sesiones activas.

---

### "Inconsistencia de continuidad de caja."

**Causa**: El saldo ingresado al abrir la sesión difiere mucho del esperado según la sesión anterior.

**Solución**:
1. Verificar que el conteo de apertura sea correcto.
2. Si efectivamente hay un faltante/sobrante, investigar con el operador anterior.
3. Un administrador puede corregir el `cash_register_balance_start` manualmente si aplica.

---

### "No se pudo cargar la información. Cierre esta ventana y vuelva a intentarlo."

**Causa**: Fallo de red al consultar el estado de la sesión.

**Solución**: Verificar conexión. Cerrar el popup y reintentar. Si persiste, contactar a TI.

---

## Flujo operativo esperado

### Apertura de caja (operador)

1. En el backend, clic en **Abrir sesión** del Punto de Venta.
2. Si hay un rescate pendiente → el sistema bloquea y muestra las sesiones. Ir a cerrarlas antes de continuar.
3. Si no hay rescate → el sistema carga el POS.
4. Ingresar el saldo de apertura. El sistema verifica que coincida con el cierre anterior (Regla 3).
5. Confirmar. La sesión queda en estado `opened`.

### Durante la operación (operador)

1. Vender normalmente.
2. Si es necesario mover efectivo: botón **Cash in/out** en la barra superior.
   - Máximo de movimientos permitido según configuración.
   - Advertencia al último movimiento.
3. Nunca bloquear la sesión para Cash In/Out desde un rescate.

### Cierre de caja (operador)

1. Clic en **Cerrar sesión** en el menú.
2. El sistema muestra el desglose:
   - Apertura
   - Ventas en efectivo
   - Cash In
   - Cash Out
   - **Efectivo esperado**
3. Ingresar el efectivo contado.
4. Confirmar.
   - Si la diferencia está dentro del máximo autorizado → se cierra.
   - Si la diferencia excede → bloquea con mensaje explicativo.

### Gestión de rescates (administrador)

1. Ver el rescate en `Punto de Venta → Sesiones`, filtrar por "Recovery Session".
2. Revisar los pedidos del rescate.
3. Cerrar la sesión normalmente. El sistema calcula el `cash_register_balance_end_real` como `apertura + ventas + movimientos`.
4. Después del cierre del rescate, la siguiente sesión normal podrá abrirse.

---

## Notas técnicas

### Campos agregados a `pos.session`

- `rescue_parent_session_id` — Many2one a `pos.session`, referencia la sesión original que originó un rescate.
- `rescue_session_ids` — One2many inverso, lista los rescates de una sesión normal.
- `expected_opening_balance` — Monetary, captura el saldo que Odoo calculó al abrir la sesión (para auditoría).

### Campos agregados a `pos.config`

- `maximum_cash_in_out_moves` — Integer, límite configurable.
- `cash_difference_exceeded_message` — Text, mensaje custom.
- `enable_rescue_session_validation` — Boolean, activa Regla 1.

### Campos agregados a `account.bank.statement.line`

- `pos_cash_move` — Boolean, flag que marca si una línea de estado de cuenta bancario fue creada como movimiento Cash In/Out manual (vs. pago de orden).

### Overrides de métodos Odoo core

- `pos.session.try_cash_in_out` — FOR UPDATE + límite + bloqueo de rescates + tag `pos_cash_move`.
- `pos.session.post_closing_cash_details` — FOR UPDATE + validación de diferencia con snapshot unificado.
- `pos.session._cannot_close_session` — Checks de integridad adicionales.
- `pos.session.get_closing_control_data` — Enriched snapshot con campos de validación.
- `pos.session.action_pos_session_open` — Captura `expected_opening_balance`.
- `pos.session.set_cashbox_pos` — Valida contra `expected_opening_balance`.
- `pos.session.action_pos_session_closing_control` — Recalcula `cash_register_balance_end_real` incluyendo movimientos de rescate (defensivo para datos históricos).
- `pos.session.create` — Enlaza `rescue_parent_session_id` automáticamente.
- `pos.config.open_ui` — Bloquea apertura si hay rescates pendientes.

### Seguridad y aislamiento

Todas las validaciones están en el **backend** (`models/pos_session.py`). El frontend bloquea operaciones pero es únicamente para UX — cualquier bypass (RPC directo) encuentra las mismas validaciones del lado del servidor.

### Tests

57 tests unitarios en `tests/test_closing_validation.py` cubren: snapshot de efectivo, límites, rescates, validaciones de apertura/cierre, condiciones de carrera, integridad de datos, batching de filtros, y comportamiento de Cash In/Out en sesiones normales vs. rescates.

---

## Licencia

LGPL-3

## Autor

Miguel Bolivar — Libertario Coffee
