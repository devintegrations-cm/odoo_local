# Checklist para pasar a produccion

Revision del 2026-10-01, con el modulo en **17.0.1.2.0** (122 tests en verde; 17.0.1.2.1 con 123)
y desplegado en el staging de Colombia (`libertariocoffee-stg-september`).
Marque cada punto al cerrarlo.

## 0. Pendiente en produccion (urgente al actualizar)

El modulo ya esta en produccion. El 2026-10-01 el boton de cliente Leal del POS
(odooleal) fallo con *No puede acceder a los registros 'Solicitud de pago de
proveedor'* (DECISIONS #49). Como en produccion no se podia actualizar el
codigo, se resolvio con **tres registros temporales** creados en el backend, y
funciono.

- [ ] **Actualizar produccion a 17.0.1.2.1**, que corrige el error de fondo
  (los campos del portal en contacto, orden y factura pasan a ser solo de sus
  grupos).
- [ ] **Despues de actualizar, borrar los tres registros temporales.** Todos
  llevan *(temporal)* en el nombre:
  - *Ajustes -> Tecnico -> Seguridad -> Derechos de acceso*:
    `spr lectura usuarios internos (temporal)`.
  - *Ajustes -> Tecnico -> Seguridad -> Reglas de registro*:
    `spr usuarios internos no ven solicitudes (temporal)` y
    `spr validadores ven todo (temporal)`.

  Borrar primero la regla `spr usuarios internos no ven solicitudes (temporal)`
  y el derecho de acceso, y al final `spr validadores ven todo (temporal)`: en
  el orden contrario, los validadores quedan un momento sin ver sus solicitudes.
  **No borrarlos antes de actualizar**: el boton Leal volveria a fallar.
- [ ] Comprobar despues: el boton Leal en una tienda y la lista de solicitudes
  con un Validador.

## 1. Bloqueos antes de subir el codigo

- [ ] **Archivos que no deben llegar al repositorio.** Dentro de la carpeta del
  modulo hay:
  - `.env`: credenciales de cinco instancias, API key de Anthropic y la
    contrasena del proveedor de prueba.
  - `.claude/` (1,5 MB).
  - En `tests/`, archivos con datos reales: `rp_test.zip` (factura real de RP
    Soluciones), `visual_portal.png` y `ad10184684140852600000919.pdf`.

  No hay `.gitignore`. Sacarlos de la carpeta o crear el `.gitignore` antes de
  cualquier `git add`. Los de `tests/` tampoco deberian viajar en el zip de
  `deploy_remote.sh`.
- [ ] **Decidir como llega a produccion.** El modulo nunca ha entrado a git. En
  Odoo.sh, `~/src/user` es el repositorio de la rama y `supplier_invoice_portal/`
  esta sin versionar: vive ahi solo por el zip. Mientras no entre al repo,
  **cualquier push a la rama de staging lo borra** de ese build.
- [ ] **`requirements.txt` de la raiz del repositorio.** Agregar `pypdf>=3.0`
  (hoy solo tiene `openpyxl==3.1.2`). Odoo.sh ya trae pypdf 3.17.4, pero
  declararlo lo protege si la imagen cambia. La linea esta en el
  `requirements.txt` del modulo, que Odoo.sh no lee.

## 2. Pruebas que faltan en staging

- [ ] **Crear la factura desde la solicitud.** SPR/2026/00001 (RPS4584 sobre
  P42693) esta *Aprobada* y sin factura. Pulsar *Crear factura* y revisar:
  - diario FACTU (Facturas de Proveedores);
  - lineas con IVA Compra Exento 0 %;
  - lineas ligadas a la P42693, y la orden pasa a facturada;
  - PDF y XML adjuntos a la factura.
- [ ] **Una factura con IVA.** La de RP no trae IVA, asi que el *IVA como
  producto* (linea [MAYVALIVACOM191] agregada a la orden si no la tiene) solo
  esta probado con tests, no con un documento real.
- [ ] **Una factura con flete**, para ver la linea de TRAFLEOPE agregada a la
  orden.
- [ ] **Una nota credito real** desde el portal, sobre una factura existente.
- [ ] **Una cuenta de cobro** de un proveedor con *No obligado a facturar*:
  debe ir al diario DSO.
- [ ] **Correos.** Staging esta neutralizado y no envia correos; produccion si
  les escribe a los proveedores (5 plantillas del modulo: radicada, rechazada,
  facturada, etc.). Leer las plantillas antes: es lo primero que ve un
  proveedor real.
- [ ] **Revision visual en navegador** de lo que solo se verifico por HTML:
  arrastrar y soltar archivos, vista previa de documentos en el portal y en la
  pestana *Portal de proveedor*, y los campos de Jorels en *Edit information*.

## 3. Al instalar en produccion

- [ ] **Backup** de produccion justo antes de instalar o actualizar. En staging,
  una actualizacion que fallo a mitad de camino dejo la instancia rota
  alrededor de un minuto y medio (DECISIONS #48).
- [ ] **Configuracion: no viaja con el codigo.** Diarios, productos e impuestos
  se guardan por id, y los de produccion pueden ser otros. En *Compras ->
  Ajustes -> Portal de facturas de proveedor*:

  | Ajuste | Valor en staging |
  |---|---|
  | Diario de compras por defecto | FACTU - Facturas de Proveedores |
  | Diario de documento soporte | DSO - Documento Soporte Odoo (el que tiene resolucion DIAN) |
  | Producto de flete | [TRAFLEOPE] Transportes Fletes Ope |
  | IVA como producto | Activo: [MAYVALIVACOM191] Mayor Valor Iva Compras 19-15-8-5 + IVA Compra Exento |
  | IA | Anthropic, modelo `claude-opus-5-5`, timeout 60 s, API key de produccion |
  | Cierre de fin de mes | Activo, 12:00 |
  | Consulta DIAN del CUFE | Apagada (el catalogo pide captcha) |

  **Sin el diario por defecto, las facturas caen en el primer diario de
  compras, que en staging es *Facturas de Activos*.**
- [ ] **Grupos.** Asignar *Responsable* y *Validador* a quien corresponda (en
  staging hay 10 usuarios con *Responsable*).
- [ ] **Habilitar proveedores de a pocos.** Empezar con 3 a 5 conocidos: marcar
  *Puede radicar facturas en el portal* en la pestana *Portal de proveedor*,
  crear su usuario de portal y enviarle el instructivo con el boton de la
  ficha.
- [ ] **NIT repetidos.** En staging, 87 de 705 proveedores con ordenes en 2026
  tienen otro tercero con el mismo NIT. No bloquea la habilitacion
  (DECISIONS #37), pero conviene fusionarlos con el tiempo.

## 4. Pendientes menores

- [ ] **RUT de RP Soluciones con contrasena.** El que cargo esta cifrado (se
  cargo antes de la regla de DECISIONS #47); el visor pide la clave. Hay que
  reemplazarlo por uno sin clave.
- [ ] **Selector de municipio** en *Edit information*: lista de unas 1.100
  opciones sin buscador. Funciona, pero es incomodo.
- [ ] **Limpiar staging:** SPR/2026/00002 y 00003 son radicaciones repetidas
  de la RPS4584, rechazadas por CUFE duplicado durante la prueba.
- [ ] **Puntos del comite sin codigo** (DECISIONS #39): punto 2 (sin definir),
  punto 9 (depende del comite) y punto 10 (anticipos, pendiente de validar).
