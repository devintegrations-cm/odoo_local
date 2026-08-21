# Odoo Local

Entorno Odoo 17 con Docker Compose. Los datos persisten en los volúmenes
`odoo_local_odoo17-db-data` y `odoo_local_odoo17-web-data`, por lo que
**reconstruir o reiniciar los contenedores no borra datos**.

## Comandos de gestión

> **Base de datos:** en este entorno la base de trabajo se llama **`pruebas`**
> (no `postgres`). Se crea desde el selector web en `http://localhost:8100`.

### Levantar / iniciar (sin perder datos)
```bash
docker compose -f stack.yml up -d
```

### Reiniciar el servicio web (aplica cambios de código)
```bash
docker compose -f stack.yml restart web
```

### Instalar un módulo por primera vez (crea las tablas y los campos)
```bash
docker compose -f stack.yml stop web
docker compose -f stack.yml run --rm web odoo -i pos_closing_validation -d pruebas --stop-after-init
docker compose -f stack.yml up -d web
```

### Actualizar un módulo ya instalado (corre migraciones)
```bash
docker compose -f stack.yml stop web
docker compose -f stack.yml run --rm web odoo -u pos_closing_validation -d pruebas --stop-after-init
docker compose -f stack.yml up -d web
```

- Para **actualizar todos** los módulos custom: usa `-u all`.
- Para **instalar/actualizar varios** módulos: `-i mod1,mod2` / `-u mod1,mod2`.

### Shell interactivo de Odoo
```bash
docker compose -f stack.yml run --rm web odoo shell -d pruebas
```

### Shell de la base de datos (Postgres)
```bash
docker compose -f stack.yml exec db psql -U odoo -d pruebas
```

## Logs

### Ver los logs en vivo (sin que la terminal se cierre)
El siguiente comando se queda **pegado** a los logs y se actualiza en tiempo
real. Para detenerlo pulsa `Ctrl+C`:

```bash
docker compose -f stack.yml logs -f web
```

Opciones útiles:
- `--tail=100` → mostrar las últimas 100 líneas y luego seguir en vivo:
  ```bash
  docker compose -f stack.yml logs -f --tail=100 web
  ```
- `-t` → añadir marca de tiempo a cada línea:
  ```bash
  docker compose -f stack.yml logs -f -t web
  ```
- Sin `-f`, devuelve los logs y la terminal termina de inmediato (no se queda
  abierta). Útil si solo quieres un vistazo rápido:
  ```bash
  docker compose -f stack.yml logs --tail=50 web
  ```

## Notas
- Base de datos de trabajo: **`pruebas`**.
- Puerto web: `8100` (mapeo al `8069` interno del contenedor).
- Ajustes de la BD: usuario `odoo`, password `odoo`, host `db`.
- `stack.yml` usa `image: odoo:17.0` (sin `build:`), así que no hay que
  reconstruir la imagen; basta `up -d`.
- Al instalar/actualizar un módulo, el JS de la app de Punto de Venta se
  refresca con `Ctrl+F5` en el navegador.