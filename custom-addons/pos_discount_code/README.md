# POS Discount Code (`pos_discount_code`)

Odoo 17 · Versión 17.0.1.2.0 · Libertario Coffee Roasters

Agrega al Punto de Venta un botón **"Código Descuento"** para aplicar bonos (códigos promocionales, cupones y tarjetas de regalo) capturando el **tipo y número de documento** de quien lo canjea, y deja el canje **registrado en la orden** para su trazabilidad.

El módulo no crea descuentos propios: aplica los programas de *Punto de venta › Productos › Descuentos y lealtad* mediante la lealtad nativa de Odoo (`pos_loyalty`).

---

## Funcionalidades

- Botón "Código Descuento" en la pantalla de la orden, que reemplaza al botón nativo de códigos promocionales.
- Formulario con tipo de documento, número de documento y código.
- Validación desde el primer clic en **Validar**:
  - promociones: vigencia y lista de precios, en la caja;
  - cupones y tarjetas de regalo: consulta al servidor que **no gasta** el cupón (se gasta al cobrar);
  - número de documento solo con dígitos en los tipos numéricos (NIT, CC, TI, TE, CE, NIUP).
- Vista previa antes de aplicar: programa, valor (%, fijo o saldo de la tarjeta), vigencia y documento.
- Un mismo código no se aplica dos veces en la orden. Si el cajero quitó la línea del descuento, el código se puede volver a aplicar sin que el cliente pierda el bono.
- Reemplazo de código: si la orden ya tenía otro código, se quita y se aplica el nuevo.
- Registro del canje en `pos.order`, validado y calculado en el servidor.
- Revisión de compatibilidad con `pos_loyalty`: si una actualización de Odoo cambia su funcionamiento interno, el botón muestra un aviso en lugar de aplicar descuentos a medias.

## Dependencias

- `point_of_sale`
- `pos_loyalty`

## Configuración

1. **Programas de descuento:** *Punto de venta › Productos › Descuentos y lealtad*. Crear el programa (código promocional, cupones o tarjeta de regalo) con *Punto de venta* habilitado.
   - Un descuento fijo nunca supera el total de la orden. Si el bono no debe usarse en compras menores a su valor, configurar una **compra mínima** en la regla del código; un código promocional no guarda saldo (para eso, usar tarjeta de regalo).
2. **Botón por caja:** *Punto de venta › Configuración › Ajustes*, elegir la caja en el selector superior, bloque **Precios** › **Código de descuento (bono)** › *Mostrar botón de código de descuento*. Viene activo por defecto.
   - Desmarcarlo vuelve a mostrar el botón nativo de códigos de Odoo en esa caja.

## Uso en la caja

1. Agregar los productos a la orden.
2. **Código Descuento** › elegir el tipo de documento, escribir el número y el código › **Validar**.
3. Revisar la vista previa › **Aplicar**.
4. Cobrar normalmente. Al sincronizar la orden, el servidor registra el canje.

## Datos que se guardan en la orden

Pestaña **Descuento** en el formulario de la orden (visible solo si hay código).

| Campo | Origen |
|---|---|
| Programa de descuento (`discount_program_id`) | Validado en el servidor |
| Nombre del descuento (`discount_name`) | Nombre del programa |
| Código de descuento (`discount_code`) | Digitado en la caja |
| Valor del descuento (`discount_amount`) | Calculado en el servidor: suma de las líneas de recompensa del programa (con impuestos) |
| Fecha de vigencia (`discount_vigencia`) | Fecha final del programa |
| Estado al canjear (`discount_state`) | Vigente o vencido **en la fecha del canje**, según el día local del cajero |
| Tipo y número de documento | Digitados en la caja; en tipos numéricos se quitan espacios, puntos y guiones |
| Fecha de canje (`discount_redeem_date`) | Momento de aplicar el código, en UTC |

Reglas del servidor:

- El canje **solo se registra** si el programa existe, el código pertenece a ese programa (regla con código o tarjeta) y la orden tiene al menos una línea de recompensa del programa. Si no, los campos quedan vacíos.
- Un dato inválido **nunca** impide que la orden se sincronice.
- En órdenes que no están en borrador, los datos del canje no se pueden modificar.

## Estructura

```
pos_discount_code/
├── models/
│   ├── pos_config.py        Opción del botón por caja (+ campo en Ajustes)
│   ├── pos_order.py         Campos del canje, validación y cálculo en el servidor
│   └── pos_session.py       Envía al POS los tipos de documento
├── views/
│   ├── pos_config_views.xml Ajuste en Punto de venta › Precios
│   └── pos_order_views.xml  Pestaña "Descuento" en la orden
├── static/src/
│   ├── js/loyalty_adapter.js      Único punto de contacto con el interior de pos_loyalty
│   ├── js/discount_code_popup.js  Popup: validación, vista previa y aplicación
│   ├── js/discount_code_button.js Botón, revisión de compatibilidad y extensión de la orden
│   ├── xml/discount_code_popup.xml
│   └── xml/hide_promo_code.xml    Oculta el botón nativo de códigos
├── migrations/              17.0.1.1.0 y 17.0.1.2.0: recalculan valor y estado en órdenes existentes
└── tests/test_discount_code.py
```

### Fuentes únicas

- **Tipos de documento:** `DISCOUNT_DOC_TYPES` en `models/pos_order.py`. El POS los recibe del servidor al abrir la sesión; no se repiten en JavaScript.
- **Tipos numéricos:** `NUMERIC_DOC_TYPES` en el mismo archivo.

Para agregar, quitar o marcar como numérico un tipo de documento: editar esas listas, actualizar el módulo y recargar las cajas. Antes de quitar un tipo, revisar que ninguna orden lo tenga guardado.

### Relación con `pos_loyalty`

`static/src/js/loyalty_adapter.js` es el único archivo que usa el estado interno de `pos_loyalty` (`codeActivatedProgramRules`, `codeActivatedCoupons`, `couponPointChanges`, `disabledRewards`) y sus métodos internos. Al actualizar Odoo, es el archivo a revisar. Su función `checkLoyaltyCompatibility` lista lo que espera encontrar y se ejecuta cada vez que se abre el botón.

## Pruebas automáticas

19 pruebas en `tests/test_discount_code.py` (lógica del servidor). Corren solo con `--test-enable`, nunca en uso normal.

```bash
odoo -c <config> -d <base_temporal> -i pos_discount_code \
     --test-enable --test-tags /pos_discount_code --stop-after-init --no-http
```

Usar siempre una base temporal, nunca una base real.

La lógica del popup no tiene prueba automática; se valida con la lista manual siguiente.

## Lista de pruebas manuales

Repetir al cambiar el módulo o actualizar Odoo. Recargar la caja después de actualizar (en local, con `?debug=assets`).

| # | Caso | Resultado esperado |
|---|---|---|
| 1 | Aplicar un código promocional válido | Línea de descuento en la orden y notificación con el valor |
| 2 | Aplicar el mismo código otra vez | "Este código ya fue aplicado en la orden." |
| 3 | Aplicar otro código | Aviso de reemplazo en la vista previa; queda solo el nuevo descuento |
| 4 | Quitar la línea del descuento y volver a aplicar el código | El descuento vuelve |
| 5 | Código inexistente | Error en el formulario con el primer clic |
| 6 | Código vencido o aún no vigente | Error en el formulario |
| 7 | Cupón o tarjeta de regalo válidos | Vista previa con datos reales (saldo en tarjeta) |
| 8 | Cédula con puntos (`10.324.567`) | Error de solo dígitos |
| 9 | Pasaporte con letras | Pasa a la vista previa |
| 10 | Cambiar cantidades después de aplicar un descuento porcentual | El valor registrado al cobrar corresponde al descuento final |
| 11 | Recargar la caja con una orden abierta que tiene código | La orden conserva el código y el documento |
| 12 | Cobrar y abrir la orden en el backend | Pestaña Descuento con programa, valor, estado "Vigente", documento y fecha de canje en hora local |
| 13 | Cobrar una orden a la que se le quitó el descuento | La orden no tiene canje registrado |
| 14 | Orden con acumulación de puntos Leal (`odooleal`) + código | El bono y los puntos funcionan sin conflicto |
| 15 | Orden con código cobrada con factura electrónica (Jorels) | Factura con el descuento; canje registrado |
| 16 | Caja con la opción desactivada | Aparece el botón nativo de códigos de Odoo |

## Limitaciones conocidas

- Devoluciones: devolver una venta con bono no reactiva el bono ni marca el canje como reversado.
- No hay vistas de consulta de canjes (listas, filtros); los datos se ven en cada orden.
- Las cajas que no se recarguen después de actualizar el módulo siguen con el JavaScript anterior hasta recargar.
- Migración a Odoo 19: el POS cambia su modelo de datos y su lealtad; `static/` y `_order_fields` deben reescribirse. `loyalty_adapter.js` concentra la parte más afectada.

## Historial

- **17.0.1.2.0**
  - Estado del descuento guardado según la fecha del canje (antes comparaba con la fecha actual).
  - Tipos de documento enviados al POS desde Python (fuente única); quitados Registro Civil, PEP y PPT.
  - Validación de solo dígitos en tipos numéricos (caja) y limpieza de separadores (servidor).
  - Acceso a `pos_loyalty` concentrado en `loyalty_adapter.js` con revisión de compatibilidad.
  - Corregido: al recargar la caja, las órdenes abiertas perdían el código aplicado.
- **17.0.1.1.0**
  - Valor del descuento calculado en el servidor.
  - Solo se registran bonos aplicados (programa, código y línea de recompensa).
  - Fecha de canje en UTC (antes quedaba corrida 5 horas).
  - Un programa inexistente o un dato inválido ya no impiden sincronizar la orden.
  - Validación de cupones y tarjetas de regalo desde el primer clic.
  - Se permite volver a aplicar un código cuyo descuento quitó el cajero.
- **17.0.1.0.0** — Versión inicial.
