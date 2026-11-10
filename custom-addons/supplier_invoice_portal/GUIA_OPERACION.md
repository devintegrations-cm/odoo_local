# Guia de operacion: Portal de facturas de proveedor

Para el equipo de contabilidad y compras. La parte tecnica (instalacion,
contratos de OCR e IA, reglas en detalle) esta en `README.md`.

## 1. Como funciona en dos lineas

El proveedor entra al portal, elige una o varias ordenes de compra, adjunta el
PDF y el XML de la factura electronica DIAN, y envia. Tambien radica notas
credito y debito, y si no esta obligado a facturar, su cuenta de cobro. Odoo lee el XML, revisa la factura
contra la orden de compra y deja una **solicitud de pago** con un veredicto.
Contabilidad la revisa y, con un boton, crea la factura de proveedor en
borrador. La factura nunca se contabiliza sola.

## 2. Configuracion inicial (una sola vez)

Todo esta en *Compras -> Configuracion -> Ajustes*, bloque *Portal de
facturas de proveedor*. Lo ve quien administre Compras.

| Ajuste | Que poner |
|---|---|
| Diario por defecto | El diario de compras donde se crean las facturas borrador. |
| Diario de documento soporte | El diario con la resolucion DIAN de documento soporte. Ahi se registran las cuentas de cobro de los no obligados a facturar. Sin el, esas solicitudes no generan factura. |
| Producto de flete | Un producto de servicio para los fletes y envios que la factura cobra y la orden no tiene. Sin el, una factura con flete no genera factura. |
| Cierre de radicacion de fin de mes | Activo por defecto. El ultimo dia habil del mes, desde la hora de corte (12:00), el portal no recibe documentos hasta el dia 1 del mes siguiente. Cuenta los festivos de Colombia; no hay que cargarlos. Solo cierra el portal: contabilidad puede seguir creando solicitudes en el backend. |
| Tolerancia de montos | Diferencia admitida entre el total de la factura y lo pendiente de la orden. Pasa si esta dentro del porcentaje **o** del monto absoluto. Por defecto 0,5 % o 1.000 COP. |
| NIT de la compania | Se deja vacio si el NIT esta bien puesto en la ficha de la compania. |
| Verificar emisor y receptor | Activa por defecto. Compara el NIT del emisor con el proveedor y el del adquiriente con la compania, y muestra esos datos en la solicitud. Desactivada, esas dos reglas no corren y los campos se ocultan; una factura a nombre de otro ya no se rechaza por eso. |
| Grupo validador | El grupo que recibe la actividad cuando llega una solicitud para revisar. |
| IVA como producto | Activo en Libertario: todas las lineas van con IVA Compra Exento 0 % y el IVA de la factura entra como una linea de [MAYVALIVACOM191] Mayor Valor Iva Compras. Si la orden no tiene esa linea, se agrega sola al crear la factura (igual que el flete). |
| Consulta DIAN | Apagada por defecto: el catalogo de la DIAN pide captcha y Odoo ya no puede confirmar el CUFE solo. Si se activa, cada solicitud trae la observacion "no se pudo verificar" con el enlace para revisarlo a mano. |
| OCR del PDF | Solo si hay un servicio de OCR externo. Se usa cuando el proveedor no adjunta el XML. |
| Emparejamiento por IA | Solo sugiere a que linea de la orden corresponde cada linea de la factura cuando el codigo no alcanza. Nunca crea ni cambia nada. |

### Habilitar un proveedor

1. **Revisar si el proveedor esta repetido** (con y sin digito de
   verificacion, con otro nombre...). No bloquea la habilitacion, pero conviene
   fusionarlo desde *Contactos* (vista de lista, seleccionar los registros,
   *Accion -> Fusionar*) para que ordenes, facturas y pagos queden en un solo
   tercero. Habilite y de acceso al portal en el tercero que tiene las ordenes.
2. Abrir la ficha del proveedor: la **empresa**, o la persona natural si no
   pertenece a ninguna empresa. Debe tener el NIT en el campo NIF. En los
   contactos hijos (la persona de tesoreria de una empresa, por ejemplo) la
   casilla no aparece: radican a nombre de su empresa.
3. Marcar **Puede radicar facturas en el portal**. Si no esta obligado a
   facturar, marcar tambien **No obligado a facturar (documento soporte)**.
4. Darle acceso al portal a cada persona que va a radicar: en la pestana
   *Contactos y direcciones* crear su contacto hijo con correo y luego
   *Accion -> Otorgar acceso al portal*. Todos los contactos de la empresa
   ven las solicitudes de la empresa, y cada correo le llega a la empresa y a
   quien radico.
5. Pulsar **Enviar instructivo del portal** (arriba en la ficha). Abre el
   correo con el paso a paso para el proveedor; ahi se revisan destinatarios
   y se puede adjuntar la guia en PDF.

Sin la casilla marcada, el proveedor entra al portal pero no ve la opcion de
radicar facturas.

### Permisos

| Grupo | Puede |
|---|---|
| Validador | Ver solicitudes, validar, emparejar lineas a mano, crear la factura borrador. |
| Responsable | Lo anterior, mas configurar, rechazar, devolver a borrador y cancelar. |

Se asignan en la ficha del usuario, seccion *Portal de facturas de proveedor*.
Quien sea *Administrador* de Compras ya es Responsable sin asignarlo.

## 3. Lo que hace el proveedor

En el portal (*Mi cuenta -> Facturas radicadas*):

1. **Nueva solicitud**: arriba elige el tipo de documento.
   - *Factura electronica*: marca una o varias ordenes de compra abiertas
     (confirmadas y no facturadas del todo; hay proveedores que agrupan varias
     remisiones en una factura), adjunta el PDF (obligatorio) y el XML DIAN
     (opcional, pero sin el la revision es manual).
   - *Nota credito* o *Nota debito*: elige la factura que corrige (o la deja
     en blanco y se toma del XML) y adjunta PDF y XML.
   - *Cuenta de cobro*: solo le aparece si esta marcado como no obligado a
     facturar. Marca las ordenes, adjunta el PDF y digita numero, fecha y
     valor total.

   En todos puede dejar una nota para contabilidad.
2. Al enviar, ve de inmediato el resultado: *En revision* o *No paso las
   validaciones*, con la lista de errores en espanol.
3. Recibe correo cuando la solicitud queda en revision, cuando no pasa las
   validaciones, cuando contabilidad la rechaza y cuando la factura queda
   registrada.

Si la solicitud no paso, el proveedor corrige (por ejemplo, la orden de compra
equivocada) y radica de nuevo.

El ultimo dia habil de cada mes el portal recibe hasta las 12:00; despues
muestra que la radicacion esta cerrada y desde que dia se reabre.

En *Mi cuenta -> Editar informacion* el proveedor habilitado carga su camara
de comercio, RUT y certificacion bancaria (PDF o imagen, hasta 10 MB) y, si
Jorels esta instalado, sus datos de facturacion electronica. Quedan en la
ficha de la empresa, grupo *Portal de facturas*, con una nota en el chatter.

## 4. Lo que hace contabilidad

Menu *Compras -> Solicitudes de pago de proveedor*. El filtro **Por revisar**
muestra las que estan *Aprobadas* o *Con observaciones* y aun no tienen
factura.

### Estados

| Estado | Significa | Que hacer |
|---|---|---|
| Borrador | Creada a mano en el backend, sin validar. | Validar. |
| Validando | Se esta procesando. | Esperar; si se queda ahi, *Revalidar*. |
| Aprobada | Todas las reglas pasaron. | Crear factura. |
| Con observaciones | Paso, pero hay algo que revisar (precio distinto, linea sin emparejar, CUFE no encontrado en la DIAN...). | Leer las observaciones, corregir lo que toque, crear factura o rechazar. |
| Rechazada | Alguna regla dura fallo (NIT distinto, monto mayor a la orden, CUFE repetido...) o el Responsable la rechazo. El proveedor ya fue avisado. | Nada. Si el rechazo automatico fue por un dato de Odoo (orden mal confirmada, NIT mal escrito), corregirlo y *Validar* de nuevo. Si la rechazo el Responsable, solo se reabre con *Volver a borrador*. |
| Facturada | Ya existe la factura de proveedor. | Seguir el flujo normal de la factura. |
| Cancelada | Descartada por el Responsable. | Nada. |

### Revisar una solicitud

1. **Pestana Validacion**: la lista de hallazgos. Los *errores* rechazan, las
   *observaciones* solo avisan. Cada uno dice en espanol que comparo y que
   encontro.
2. **Pestana Lineas y emparejamiento**: cada linea de la factura y a que
   linea de la orden de compra quedo asociada. La columna *Metodo de emparejamiento* dice si fue
   por codigo, por IA o a mano. Una linea en rojo no tiene orden asociada:
   se elige la linea de la orden en la columna correspondiente y se pulsa
   **Revalidar**. Lo emparejado a mano se conserva en las siguientes
   revalidaciones.
3. **Pestana Datos extraidos**: lo que se leyo del XML (emisor, adquiriente,
   numero, fecha, totales). Si no hay XML ni OCR, estos campos se capturan a
   mano y luego se pulsa *Revalidar*.
4. **Pestana Adjuntos**: el PDF y el XML tal como los subio el proveedor.
   El boton del **ojo**, al lado de editar, descargar y eliminar, los muestra
   sin descargar; si el XML es un AttachedDocument, primero aparece la factura
   que trae adentro. El PDF tambien queda como adjunto en el chatter y, en
   pantallas anchas, se ve al lado del formulario.
5. **Pestana Orden de compra**: las lineas de la orden (o las ordenes) con
   cantidad pedida, recibida, facturada y por facturar, precio y subtotal, y
   arriba los totales de la factura frente a los de las ordenes. Las lineas
   ya facturadas del todo salen en gris.
6. **Boton Ordenes de compra** (arriba a la derecha): abre la orden, o la
   lista si la solicitud tiene varias.
7. **Consultar DIAN**: vuelve a preguntar por el CUFE al catalogo de la DIAN,
   por ejemplo si en la primera validacion la DIAN no respondio.

### Crear la factura

Con la solicitud *Aprobada* o *Con observaciones*, el boton **Crear factura**
genera la factura de proveedor en **borrador**, con:

- las lineas ligadas a la orden de compra (la orden actualiza su cantidad
  facturada de inmediato),
- el CUFE, el numero de factura del proveedor como referencia, la fecha,
- el PDF y el XML como adjuntos.

Si el total que calcula Odoo no coincide con el de la factura del proveedor,
queda una nota en el chatter para revisar antes de contabilizar. La factura se
confirma y se paga por el flujo normal de contabilidad.

Si se borra la factura borrador, la solicitud vuelve a *Aprobada* y se puede
crear otra vez.

### Fletes y envios

El envio casi nunca esta en la orden de compra ni en la remision, pero si en
la factura. Las lineas que dicen flete, envio, transporte, domicilio,
despacho, acarreo, mensajeria, encomienda o courier (o traen el codigo del
producto de flete), y los cargos que el XML trae a nivel de documento, quedan
marcadas como **Cargo adicional**: no se comparan con la orden, no cuentan
contra lo pendiente y se registran con el producto de flete de Ajustes. Con
ese producto configurado **no frenan la aprobacion** (`EXTRA_CHARGES` OK con
el valor); sin el, queda la observacion para configurarlo y revalidar. Si una linea es un flete y no se detecto, se
marca a mano la casilla *Cargo adicional* en la pestana de lineas y se
revalida. En la factura, esas lineas van con el producto de flete de Ajustes.

### Notas credito y debito

- La nota se asocia a la factura que corrige (el proveedor la elige o se toma
  del XML). Sus lineas se emparejan con las de la orden de esa factura solo
  para saber producto, cuenta e impuesto: la nota **no** cambia las cantidades
  facturadas de la orden.
- **Nota credito**: no puede superar el saldo de la factura (lo que no se haya
  descontado ya con otras notas). *Crear factura* genera una nota credito de
  proveedor en borrador, ligada a la factura original.
- **Nota debito** (por ejemplo, un incremento de precio): siempre queda *Con
  observaciones* hasta que el Responsable pulse **Aceptar nota debito**. Sin
  esa aceptacion no se puede crear la factura. El filtro *Notas debito por
  aceptar* las muestra.

### Cuentas de cobro (documento soporte)

Para proveedores no obligados a facturar. Numero, fecha y total los declara
el proveedor; las lineas salen de lo pendiente de sus ordenes. Si la cuenta de
cobro no cubre todo lo pendiente, se ajustan cantidades en la pestana de
lineas y se revalida: las lineas ya no se vuelven a tomar de la orden. La
factura se crea en el **diario de documento soporte**, y al confirmarla la
facturacion electronica emite el documento soporte ante la DIAN.

### Rechazar

El Responsable puede rechazar una solicitud *Aprobada* o *Con observaciones*
con **Rechazar**; pide un motivo, que le llega al proveedor por correo y
queda en el chatter.

### Casos frecuentes

- **El proveedor no encuentra su orden en el portal**: la orden no esta
  confirmada, ya esta facturada del todo, o esta a nombre de otro contacto
  que no es esa empresa ni uno de sus contactos.
- **Rechazada por `AMOUNT_TOTAL`**: la factura supera lo que queda por
  facturar de la orden. O la orden esta incompleta, o el proveedor factura de
  mas. Si la diferencia es pequena y legitima, se ajusta la tolerancia en
  Ajustes y se revalida.
- **Rechazada por `CUFE_DUPLICATE`**: ya hay otra solicitud activa o una
  factura de proveedor con ese CUFE. Casi siempre es un reenvio del
  proveedor; se cancela la repetida.
- **Observacion `DIAN_CUFE_CHECK`**: Odoo no pudo confirmar el CUFE (la DIAN
  pide captcha). Se verifica a mano en el enlace del hallazgo: se pega el CUFE
  y el NIT de Libertario en el buscador de la DIAN. Es opcional; si la
  consulta esta apagada en Ajustes, esta observacion no aparece.
- **Observacion `PRICE_UNIT_MISMATCH`**: el precio facturado no es el de la
  orden. Se decide con compras: o se acepta y se crea la factura (el precio
  que queda es el del proveedor), o se rechaza.
- **Sin XML**: la solicitud queda *Con observaciones* pidiendo captura manual.
  Se digitan numero, fecha, totales y lineas en las pestanas y se revalida.
- **Rechazada por `DOCUMENT_TYPE`**: el XML es de otro tipo (una nota
  radicada como factura, por ejemplo). El proveedor radica de nuevo eligiendo
  el tipo correcto.
- **Rechazada por `NOTE_ORIGIN`**: no se encontro la factura que corrige la
  nota. Si la factura se registro sin CUFE ni numero, se elige a mano en el
  campo *Factura que afecta* y se revalida.
- **El proveedor no ve una orden para radicar la factura**: la orden ya tiene una
  factura registrada (aunque sea parcial o en borrador). Lo que falte o sobre se
  radica como nota credito o debito sobre esa factura. Si la factura estaba mal,
  anularla libera la orden.
- **No deja habilitar al proveedor**: falta el NIT o es un contacto hijo. El
  mensaje dice cual; ver *Habilitar un proveedor*.

## 5. Donde ver el historial

Todo queda en el chatter de la solicitud: validaciones, correos enviados al
proveedor, rechazos y creacion de la factura. En la ficha del proveedor el
boton **Solicitudes** lleva a todas las suyas.
