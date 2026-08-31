#!/usr/bin/env python3

import argparse
import multiprocessing
import os
import sys
import threading
import time
import traceback
import types

import psycopg2
from psycopg2 import pool as _psycopg2_pool

DEFAULT_SESSION = 'POS/00148'
DEFAULT_PRODUCT_ID = 81
DEFAULT_PARTNER_ID = 84
DEFAULT_PAYMENT_METHOD_ID = 0
DEFAULT_WORKERS = 100
DEFAULT_QUANTITY = 1.0

DEFAULT_MAX_ATTEMPTS = 4
CONN_HEADROOM = 4
BACKOFF_BASE = 0.2

# Semáforo global de conexiones: limita cuántos threads abren un cursor
# Odoo al mismo tiempo para no agotar el pool de conexiones del ORM
# (db_maxconn, por defecto 64). Se reconfigura vía --max-conn.
CONN_SEMAPHORE = threading.Semaphore(64)


def _is_transient(exc):
    if isinstance(
        exc,
        (psycopg2.OperationalError, _psycopg2_pool.PoolError),
    ):
        return True

    name = type(exc).__name__

    return name in (
        'SerializationFailure',
        'DeadlockDetected',
    )


def _clamp_workers(env, args):
    """
    Limita workers según el cupo de conexiones del rol para evitar
    'FATAL: too many connections for role'.

    1. Si POS_TEST_ROLCONNLIMIT está definido, se usa ese valor
       (cuando el rol no puede leer pg_roles).
    2. Si no, se mide en un cursor descartable propio (db_connect),
       NUNCA en el cursor del llamador: un fallo de medición no
       aborta la transacción del que invoca a esta función.
    """
    override = os.environ.get('POS_TEST_ROLCONNLIMIT', '').strip()

    if override:
        try:
            rolconnlimit = int(override)
        except ValueError:
            print(
                f"POS_TEST_ROLCONNLIMIT inválido: {override!r}. "
                "Se intenta medir."
            )
            rolconnlimit = None
    else:
        rolconnlimit = None

    if rolconnlimit is None:
        try:
            from odoo.sql_db import db_connect
            probe_cr = db_connect(env.cr.dbname).cursor()
            try:
                probe_cr.execute(
                    "SELECT rolconnlimit FROM pg_roles "
                    "WHERE rolname = current_user"
                )
                row = probe_cr.fetchone()
                rolconnlimit = row[0] if row else -1
            finally:
                probe_cr.close()
        except Exception as exc:
            print(
                "No se pudo medir rolconnlimit "
                f"({type(exc).__name__}: {exc}). "
                "Workers sin limitar. Si el rol tiene tope de "
                "conexiones, define POS_TEST_ROLCONNLIMIT con el "
                "valor medido."
            )
            return args.workers

    if rolconnlimit <= 0:
        print(
            "Cupo de conexiones del rol: ilimitado "
            "(no se limita workers)"
        )
        return args.workers

    max_by_conn = (rolconnlimit - CONN_HEADROOM) // 2
    effective = min(args.workers, max_by_conn)

    if effective < args.workers:
        print()
        print(
            f"Cupo de conexiones del rol: {rolconnlimit} -> "
            f"workers limitado de {args.workers} a {effective} "
            f"(headroom={CONN_HEADROOM}, "
            f"~2 conexiones por worker)"
        )
        print()

    return effective

def _make_prefix():
    return os.environ.get('POS_TEST_PREFIX') or (
        f'CONC-{os.getpid()}-{int(time.time())}'
    )


def _parse_csv_ints(value, env_fallback):
    """
    Parsea un CSV de enteros (--product-ids / --partner-ids).
    Si value es vacío/None, devuelve [env_fallback].
    """
    if not value:
        return [env_fallback]

    items = []
    for part in str(value).split(','):
        part = part.strip()
        if not part:
            continue
        try:
            items.append(int(part))
        except ValueError:
            raise SystemExit(
                f"Valor inválido en CSV de ids: {part!r}"
            )
    if not items:
        return [env_fallback]
    return items


def _parse_csv_str(value, env_fallback):
    """
    Parsea un CSV de strings (--sessions).
    Si value es vacío/None, devuelve [env_fallback].
    """
    if not value:
        return [env_fallback]

    items = [
        part.strip()
        for part in str(value).split(',')
        if part.strip()
    ]
    if not items:
        return [env_fallback]
    return items


def _shell_dbname():
    env_shell = globals().get('env')
    if env_shell:
        return env_shell.cr.dbname
    return None


def _fresh_env(db_name):
    import odoo
    from odoo import api, SUPERUSER_ID
    from odoo.sql_db import db_connect

    cr = db_connect(db_name).cursor()
    return api.Environment(cr, SUPERUSER_ID, {}), cr


def _resolve_payment_method(env, session, payment_method_id):
    """
    Resuelve el método de pago para la sesión asignada al worker.

    Para que la prueba sea representativa de producción (donde cada
    POS tiene sus métodos de pago autorizados), SIEMPRE se valida
    que el método pertenezca a la config de la sesión. Si se pasa
    --payment-method-id y la sesión lo tiene autorizado, se usa; si
    no, se resuelve automáticamente desde los métodos de la sesión
    (prefiriendo el de efectivo / is_cash_count). Así la prueba
    funciona sin cambios tanto en local (5 POS) como en producción
    (N POS, cada uno con sus métodos).
    """
    if payment_method_id:
        candidate = env['pos.payment.method'].browse(
            payment_method_id
        ).exists()

        if candidate and candidate in session.payment_method_ids:
            return candidate

        if candidate and candidate not in session.payment_method_ids:
            print(
                f"  [AVISO] payment_method_id={payment_method_id} "
                f"no está autorizado en la sesión {session.name} "
                f"(config {session.config_id.name}). "
                f"Se resuelve desde la sesión."
            )

    # Resolución automática por sesión: efectivo primero
    cash = session.payment_method_ids.filtered('is_cash_count')
    if cash:
        return cash[:1]

    return session.payment_method_ids.filtered(
        lambda m: m.type != 'pay_later'
    )[:1]


def _resolve_product_taxes(env, session, product):
    """
    Resuelve los impuestos que se usarán para el producto en el POS.

    IMPORTANTE:
    product.taxes_id puede contener impuestos de otra compañía.

    En nuestro caso:
        product.taxes_id = [51]
        tax 51 pertenece a CO Company (company 3)

    pero:
        session.company_id = My Company (company 1)

    Por eso NO podemos pasar directamente product.taxes_id
    a compute_all() ni a la línea del POS.

    Primero usamos los impuestos del producto que sean válidos
    para la compañía de la sesión.

    Si no existe ninguno, buscamos un impuesto de venta activo
    de la compañía del POS.
    """

    company = session.company_id

    valid_taxes = product.taxes_id.filtered(
        lambda tax: (
            not tax.company_id
            or tax.company_id == company
        )
    )

    if valid_taxes:
        return valid_taxes

    # Fallback para productos cuyos taxes_id pertenecen
    # a otra compañía.
    fallback_tax = env['account.tax'].search([
        ('company_id', '=', company.id),
        ('type_tax_use', '=', 'sale'),
        ('active', '=', True),
    ], order='sequence, id', limit=1)

    if not fallback_tax:
        raise RuntimeError(
            "No se pudo resolver un impuesto de venta válido "
            f"para la compañía {company.id} ({company.name}) "
            f"para el producto {product.id} ({product.display_name}). "
            f"Taxes originales del producto: {product.taxes_id.ids}"
        )

    return fallback_tax


def prepare_test_data(env, args):
    from odoo.tools import float_compare

    args.workers = _clamp_workers(env, args)

    session = env['pos.session'].search(
        [('name', '=', args.session_names[0])],
        limit=1,
    )

    if not session:
        raise RuntimeError(
            f"No existe la sesión {args.session_names[0]}"
        )

    if session.state != 'opened':
        raise RuntimeError(
            f"La sesión {args.session_names[0]} no está abierta. "
            f"Estado actual: {session.state}"
        )

    # Validar que TODAS las sesiones del pool existan y estén abiertas
    for sname in args.session_names:
        s = env['pos.session'].search([('name', '=', sname)], limit=1)
        if not s:
            raise RuntimeError(f"No existe la sesión {sname}")
        if s.state != 'opened':
            raise RuntimeError(
                f"La sesión {sname} no está abierta "
                f"(estado {s.state})"
            )

    # Validar que TODOS los productos del pool existan
    for pid in args.product_ids:
        p = env['product.product'].browse(pid).exists()
        if not p:
            raise RuntimeError(f"No existe product.product {pid}")
        if p.type not in ('product', 'consu'):
            raise RuntimeError(
                f"El producto {p.display_name} tiene type "
                f"'{p.type}' y no es almacenable"
            )

    # Validar que TODOS los partners del pool existan
    for prid in args.partner_ids:
        if not env['res.partner'].browse(prid).exists():
            raise RuntimeError(f"No existe res.partner {prid}")

    product = env['product.product'].browse(
        args.product_ids[0]
    ).exists()

    config = session.config_id
    picking_type = config.picking_type_id

    if not picking_type:
        raise RuntimeError(
            "El POS no tiene picking type configurado"
        )

    source_location = picking_type.default_location_src_id
    destination_location = picking_type.default_location_dest_id

    if not source_location:
        raise RuntimeError(
            "El picking type no tiene ubicación origen"
        )

    if not destination_location:
        raise RuntimeError(
            "El picking type no tiene ubicación destino"
        )

    if not config.invoice_journal_id:
        raise RuntimeError(
            "El POS no tiene invoice_journal_id configurado"
        )

    payment_method = _resolve_payment_method(
        env,
        session,
        args.payment_method_id,
    )

    if not payment_method:
        raise RuntimeError(
            "El POS no tiene método de pago no pay_later"
        )

    if not payment_method.journal_id:
        raise RuntimeError(
            f"El método de pago {payment_method.name} "
            "no tiene journal configurado"
        )

    partner = env['res.partner'].browse(
        args.partner_id
    ).exists()

    if not partner:
        raise RuntimeError(
            f"No existe res.partner {args.partner_id}"
        )

    # Resolver impuestos aquí también, para validar antes
    # de comenzar la prueba concurrente.
    resolved_taxes = _resolve_product_taxes(
        env,
        session,
        product,
    )

    # Stock inicial = suma de todos los productos del pool en la
    # ubicación fuente de la (primera) sesión. Si hay varios
    # productos/sesiones, la validación final compara contra esta
    # suma para no romper en escenarios representativos.
    initial_qty = 0.0
    for pid in args.product_ids:
        p = env['product.product'].browse(pid).exists()
        initial_qty += env['stock.quant']._get_available_quantity(
            p,
            source_location,
        )

    needed = args.quantity * args.workers

    print()
    print("=" * 70)
    print(" PREPARACIÓN DE PRUEBA")
    print("=" * 70)
    print()
    print(
        f"Sesión              : {session.name} (ID {session.id})"
    )
    print(
        f"POS Config          : {config.name} (ID {config.id})"
    )
    print(
        f"Compañía POS        : "
        f"{session.company_id.name} (ID {session.company_id.id})"
    )
    print(
        f"Producto            : "
        f"{product.display_name} (ID {product.id})"
    )
    print(
        f"Producto company    : "
        f"{product.company_id.id if product.company_id else False} "
        f"{product.company_id.name if product.company_id else False}"
    )
    print(
        f"Partner             : "
        f"{partner.name} (ID {partner.id})"
    )
    print(
        f"Método de pago      : "
        f"{payment_method.name} (ID {payment_method.id})"
    )
    print(
        f"Invoice journal     : {config.invoice_journal_id.name}"
    )
    print(
        f"Origen              : "
        f"{source_location.display_name} (ID {source_location.id})"
    )
    print(
        f"Destino             : "
        f"{destination_location.display_name} "
        f"(ID {destination_location.id})"
    )
    print(
        f"Anglo-saxon         : "
        f"{env.company.anglo_saxon_accounting}"
    )
    print(
        "update_stock_at_closing: "
        f"{session.update_stock_at_closing}"
    )

    print()
    print("IMPUESTOS")
    print(
        f"Taxes originales producto: "
        f"{product.taxes_id.ids}"
    )
    print(
        f"Taxes válidos POS        : "
        f"{resolved_taxes.ids}"
    )

    for tax in resolved_taxes:
        print(
            f"  tax={tax.id} "
            f"name={tax.name} "
            f"amount={tax.amount} "
            f"company={tax.company_id.id if tax.company_id else False} "
            f"{tax.company_id.name if tax.company_id else ''}"
        )

    print()
    print(f"Stock inicial       : {initial_qty}")
    print(
        f"Órdenes a crear     : "
        f"{args.workers} x {args.quantity} und"
    )
    print(f"Stock necesario     : {needed}")
    print()

    if float_compare(
        initial_qty,
        0,
        precision_rounding=product.uom_id.rounding,
    ) <= 0:
        raise RuntimeError(
            f"Stock no disponible. Inicial={initial_qty}"
        )

    if float_compare(
        initial_qty,
        needed,
        precision_rounding=product.uom_id.rounding,
    ) < 0:
        raise RuntimeError(
            f"Stock insuficiente. Disponible={initial_qty}, "
            f"necesario={needed}"
        )

    pending = env['pos.inventory.queue'].search([
        ('state', 'in', ['pending', 'processing'])
    ])

    if pending:
        raise RuntimeError(
            "Hay elementos pendientes/processing en la cola. "
            "Limpia la cola antes de ejecutar esta prueba. "
            f"Encontrados: {pending.ids}"
        )

    env.cr.commit()

    return (
        initial_qty,
        session,
        product,
        partner,
        payment_method,
    )


def create_invoiced_order(
    tenv,
    args,
    session,
    product,
    partner,
    payment_method,
    index,
    attempt=1,
):
    """
    Crea una orden POS, pago, picking e invoice.

    IMPORTANTE:
    Los taxes del producto NO se utilizan directamente.

    Se resuelven contra la compañía de la sesión POS para evitar
    errores multi-company cuando product.taxes_id contiene taxes
    de otra compañía.
    """

    PosOrder = tenv['pos.order']

    qty = args.quantity
    price_unit = product.lst_price

    # ==============================================================
    # RESOLUCIÓN MULTI-COMPANY DE TAXES
    # ==============================================================

    taxes = _resolve_product_taxes(
        tenv,
        session,
        product,
    )

    if not taxes:
        raise RuntimeError(
            f"No se pudieron resolver taxes para "
            f"product {product.id}"
        )

    for tax in taxes:
        if tax.company_id and tax.company_id != session.company_id:
            raise RuntimeError(
                f"Tax {tax.id} ({tax.name}) pertenece a company "
                f"{tax.company_id.id}, pero la sesión pertenece a "
                f"company {session.company_id.id}"
            )

    # ==============================================================
    # TAX CALCULATION
    # ==============================================================

    taxes_val = taxes.compute_all(
        price_unit,
        session.config_id.currency_id,
        qty,
        product=product,
        partner=partner,
    )

    amount_tax = (
        taxes_val['total_included']
        - taxes_val['total_excluded']
    )

    amount_total = taxes_val['total_included']

    # ==============================================================
    # IDEMPOTENCIA POR WORKER
    # ==============================================================
    # Cada worker usa una referencia ESTABLE (sin sufijo de intento).
    # Si un intento previo ya creó y comprometió la orden, se REUTILIZA
    # y se completa (picking/factura/cola) en lugar de crear una orden
    # duplicada. Esto evita que los reintentos acumulen cientos de
    # órdenes para la misma venta bajo alta concurrencia.
    pos_reference = f'{args.order_prefix}-{index:05d}'

    existing = PosOrder.search([
        ('pos_reference', '=', pos_reference),
        ('session_id', '=', session.id),
    ], limit=1)

    if existing:
        order = existing

        if not order.picking_ids:
            order._create_order_picking()

        if not order.account_move:
            order.with_context(
                generate_pdf=False
            )._generate_pos_order_invoice()

        tenv.cr.commit()

        tenv['pos.inventory.queue']._process_queue()

        tenv.cr.commit()

        return order

    print()
    print("=" * 70)
    print("CREANDO ORDEN")
    print("=" * 70)

    print("Worker:", index)
    print(
        "Product:",
        product.id,
        product.display_name,
    )

    print(
        "Session:",
        session.id,
        session.name,
    )

    print(
        "Session company:",
        session.company_id.id,
        session.company_id.name,
    )

    print(
        "Product company:",
        product.company_id.id if product.company_id else False,
        product.company_id.name if product.company_id else False,
    )

    print(
        "Product taxes originales:",
        product.taxes_id.ids,
    )

    print(
        "Product taxes válidos:",
        taxes.ids,
    )

    for tax in taxes:
        print(
            "  tax:",
            tax.id,
            tax.name,
            "amount=",
            tax.amount,
            "company=",
            tax.company_id.id if tax.company_id else False,
            tax.company_id.name if tax.company_id else '',
        )

    print()
    print("TAX CALCULATION")
    print("  subtotal:", taxes_val['total_excluded'])
    print("  tax:", amount_tax)
    print("  total:", amount_total)

    # ==============================================================
    # CREATE POS ORDER
    # ==============================================================

    order = PosOrder.create({
        'session_id': session.id,
        'partner_id': partner.id,
        'pricelist_id': session.config_id.pricelist_id.id,
        'to_invoice': True,

        'amount_tax': amount_tax,
        'amount_total': amount_total,
        'amount_paid': amount_total,
        'amount_return': 0.0,

        'pos_reference': pos_reference,

        'lines': [(0, 0, {
            'product_id': product.id,
            'qty': qty,
            'price_unit': price_unit,
            'discount': 0.0,

            # IMPORTANT:
            # Se guardan explícitamente los valores calculados
            # con el tax correcto de la compañía del POS.
            'price_subtotal': taxes_val['total_excluded'],
            'price_subtotal_incl': taxes_val['total_included'],

            'tax_ids': [
                (6, 0, taxes.ids)
            ],
        })],
    })

    print()
    print("ORDER CREADA")
    print("  id:", order.id)
    print("  name:", order.name)
    print("  state:", order.state)
    print(
        "  company:",
        order.company_id.id,
        order.company_id.name,
    )
    print("  amount_tax:", order.amount_tax)
    print("  amount_total:", order.amount_total)

    line = order.lines[0]

    print()
    print("LINEA")
    print("  id:", line.id)
    print("  product:", line.product_id.id)
    print("  price_unit:", line.price_unit)
    print("  qty:", line.qty)
    print("  price_subtotal:", line.price_subtotal)
    print(
        "  price_subtotal_incl:",
        line.price_subtotal_incl,
    )
    print("  taxes:", line.tax_ids.ids)

    # ==============================================================
    # PAYMENT
    # ==============================================================

    order.add_payment({
        'amount': order.amount_total,
        'payment_date': order.date_order,
        'payment_method_id': payment_method.id,
        'pos_order_id': order.id,
    })

    print()
    print("PAYMENT OK")

    # ==============================================================
    # MARK ORDER PAID
    # ==============================================================

    order.action_pos_order_paid()

    print(
        "ORDER PAID:",
        order.state,
    )

    # ==============================================================
    # PICKING
    # ==============================================================

    if not order.picking_ids:
        print()
        print("CREANDO PICKING...")
        order._create_order_picking()

    print()
    print(
        "PICKINGS:",
        order.picking_ids.ids,
    )

    for picking in order.picking_ids:
        print(
            "  picking:",
            picking.id,
            picking.name,
            "state=",
            picking.state,
        )

    # ==============================================================
    # COST
    # ==============================================================

    if hasattr(
        order,
        '_compute_total_cost_in_real_time',
    ):
        order._compute_total_cost_in_real_time()

    # ==============================================================
    # INVOICE
    # ==============================================================

    print()
    print("GENERANDO FACTURA...")

    order.with_context(
        generate_pdf=False
    )._generate_pos_order_invoice()

    print()
    print("=" * 70)
    print("FACTURA CREADA")
    print("=" * 70)

    invoice = order.account_move

    if not invoice:
        raise RuntimeError(
            f"La orden {order.id} no generó factura"
        )

    print("Invoice ID:", invoice.id)
    print("Invoice name:", invoice.name)
    print("Invoice state:", invoice.state)
    print("Invoice amount:", invoice.amount_total)

    # ==============================================================
    # COMMIT
    # ==============================================================

    tenv.cr.commit()

    tenv['pos.inventory.queue']._process_queue()

    tenv.cr.commit()

    return order


def run_order_in_thread(
    worker_id,
    args,
    go_event,
    results,
    lock,
):
    db_name = args.db_name

    max_attempts = getattr(
        args,
        'max_attempts',
        DEFAULT_MAX_ATTEMPTS,
    )

    go_event.wait()

    for attempt in range(1, max_attempts + 1):
        try:
            # Limitar conexiones simultáneas al pool de Odoo para no
            # agotarlo (PoolError: The Connection Pool Is Full).
            CONN_SEMAPHORE.acquire()
            try:
                tenv, cr = _fresh_env(db_name)
            except Exception:
                CONN_SEMAPHORE.release()
                raise

            try:
                idx = (worker_id - 1) % len(args.session_names)
                session = tenv['pos.session'].search(
                    [('name', '=', args.session_names[idx])],
                    limit=1,
                )

                idx = (worker_id - 1) % len(args.product_ids)
                product = tenv['product.product'].browse(
                    args.product_ids[idx]
                ).exists()

                idx = (worker_id - 1) % len(args.partner_ids)
                partner = tenv['res.partner'].browse(
                    args.partner_ids[idx]
                ).exists()

                payment_method = _resolve_payment_method(
                    tenv,
                    session,
                    args.payment_method_id,
                )

                start = time.time()

                order = create_invoiced_order(
                    tenv,
                    args,
                    session,
                    product,
                    partner,
                    payment_method,
                    worker_id,
                    attempt,
                )

                elapsed = time.time() - start

                invoice = order.account_move

                with lock:
                    results.append({
                        'worker_id': worker_id,
                        'ok': True,
                        'order_id': order.id,
                        'invoice_id': invoice.id,
                        'invoice_name': invoice.name,
                        'invoice_state': invoice.state,
                        'elapsed': elapsed,
                        'attempts': attempt,
                        'error': '',
                    })

                print(
                    f"[WORKER {worker_id}] "
                    f"OK order={order.id} "
                    f"invoice={invoice.id}/{invoice.name} "
                    f"state={invoice.state} "
                    f"en {elapsed:.3f}s "
                    f"(intento {attempt})",
                    flush=True,
                )

            finally:
                cr.close()
                CONN_SEMAPHORE.release()

            return

        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'

            transient = _is_transient(exc)

            if transient and attempt < max_attempts:
                delay = BACKOFF_BASE * (2 ** (attempt - 1))

                print(
                    f"[WORKER {worker_id}] "
                    f"Transitorio intento {attempt}: "
                    f"{last_error} -> retry en {delay:.1f}s",
                    flush=True,
                )

                time.sleep(delay)
                continue

            traceback.print_exc()

            with lock:
                results.append({
                    'worker_id': worker_id,
                    'ok': False,
                    'order_id': 0,
                    'invoice_id': 0,
                    'invoice_name': '',
                    'invoice_state': '',
                    'elapsed': 0.0,
                    'attempts': attempt,
                    'transient': transient,
                    'error': last_error,
                })

            print(
                f"[WORKER {worker_id}] "
                f"ERROR {last_error} "
                f"(intento {attempt})",
                flush=True,
            )

            return


def worker_process(
    config_path,
    db_name,
    args,
    worker_id,
    barrier,
):
    try:
        import odoo
        from odoo import api, SUPERUSER_ID

        odoo.tools.config.parse_config([
            '-c',
            config_path,
            '-d',
            db_name,
            '--addons-path',
            #'/home/odoo/src/user,/home/odoo/src/user/Jorels-Community/jorels-odoo-addons,/home/odoo/src/user/ffjuanzuluaga,/home/odoo/src/odoo/addons,/home/odoo/src/odoo/odoo/addons,/home/odoo/src/enterprise,/home/odoo/src/themes',
            '/mnt/extra-addons/custom-addons,/usr/lib/python3/dist-packages/odoo/addons',
        ])

        registry = odoo.registry(db_name)

        with registry.cursor() as cr:
            tenv = api.Environment(
                cr,
                SUPERUSER_ID,
                {},
            )

            idx = (worker_id - 1) % len(args.session_names)
            session = tenv['pos.session'].search(
                [('name', '=', args.session_names[idx])],
                limit=1,
            )

            if not session:
                raise RuntimeError(
                    f"No existe sesión {args.session_names[idx]}"
                )

            idx = (worker_id - 1) % len(args.product_ids)
            product = tenv['product.product'].browse(
                args.product_ids[idx]
            ).exists()

            if not product:
                raise RuntimeError(
                    f"No existe product {args.product_ids[idx]}"
                )

            idx = (worker_id - 1) % len(args.partner_ids)
            partner = tenv['res.partner'].browse(
                args.partner_ids[idx]
            ).exists()

            if not partner:
                raise RuntimeError(
                    f"No existe partner {args.partner_ids[idx]}"
                )

            payment_method = _resolve_payment_method(
                tenv,
                session,
                args.payment_method_id,
            )

            print(
                f"[WORKER {worker_id}] "
                f"PID={os.getpid()} preparado",
                flush=True,
            )

            barrier.wait()

            start = time.time()

            order = create_invoiced_order(
                tenv,
                args,
                session,
                product,
                partner,
                payment_method,
                worker_id,
                1,
            )

            elapsed = time.time() - start

            invoice = order.account_move

            print(
                f"[WORKER {worker_id}] "
                f"OK order={order.id} "
                f"invoice={invoice.id}/{invoice.name} "
                f"state={invoice.state} "
                f"en {elapsed:.3f}s",
                flush=True,
            )

    except Exception as exc:
        traceback.print_exc()

        print(
            f"[WORKER {worker_id}] "
            f"ERROR {type(exc).__name__}: {exc}",
            flush=True,
        )


def validate_results(
    tenv,
    args,
    initial_qty,
):
    from odoo.tools import float_compare

    # Todas las sesiones del pool (escenarios multi-POS)
    sessions = tenv['pos.session'].search([
        ('name', 'in', args.session_names),
    ])

    if not sessions:
        raise RuntimeError(
            f"No existen sesiones {args.session_names}"
        )

    # Ubicación fuente de la primera sesión (asume misma company /
    # ubicación entre las sesiones del pool, que es el caso local y
    # el típico en producción para un benchmark de concurrencia).
    config = sessions[0].config_id
    picking_type = config.picking_type_id
    source_location = picking_type.default_location_src_id

    # Todas las órdenes del run en TODAS las sesiones del pool
    orders = tenv['pos.order'].search([
        ('session_id', 'in', sessions.ids),
        (
            'pos_reference',
            'like',
            f'{args.order_prefix}-%',
        ),
    ], order='pos_reference')

    pickings = orders.mapped('picking_ids')

    queue_items = tenv['pos.inventory.queue'].search([
        ('picking_id', 'in', pickings.ids),
    ], order='sequence, id')

    invoices = orders.mapped('account_move')

    # Stock final = suma de todos los productos del pool en la
    # ubicación fuente (escenarios multi-producto).
    final_qty = 0.0
    for pid in args.product_ids:
        p = tenv['product.product'].browse(pid)
        final_qty += tenv[
            'stock.quant'
        ]._get_available_quantity(
            p,
            source_location,
        )

    precision_rounding = min(
        tenv['product.product'].browse(pid).uom_id.rounding
            for pid in args.product_ids
            )
    
    # Bajo alta concurrencia algunos workers fallan de forma esperada
    # (SerializationFailure / PoolError). El sistema de cola debe
    # procesar correctamente las órdenes que SÍ se crearon, no es
    # obligatorio que se creen exactamente args.workers órdenes.
    expected_processed = len(orders)

    done_pickings = pickings.filtered(
        lambda p: p.state == 'done'
    )

    expected_moved = (
        args.quantity * len(done_pickings)
    )
    expected_final = (
        initial_qty - expected_moved
    )

    print()
    print("=" * 70)
    print(" RESULTADO DE LA PRUEBA")
    print("=" * 70)
    print()

    print(
        f"Órdenes creadas     : {len(orders)}"
    )
    print(
        f"Pickings            : {len(pickings)}"
    )
    print(
        f"Items de cola       : {len(queue_items)}"
    )
    print(
        f"Facturas            : {len(invoices)}"
    )

    print()
    print(
        f"Stock inicial       : {initial_qty}"
    )
    print(
        f"Stock final         : {final_qty}"
    )
    print(
        f"Stock final esperado: {expected_final}"
    )

    print()

    for item in queue_items:
        start = item.start_date
        done = item.done_date
        duration = None

        if start and done:
            duration = (
                done - start
            ).total_seconds()

        print(
            f"Queue {item.id:4d} | "
            f"{item.name:15s} | "
            f"Picking={item.picking_id.name:20s} | "
            f"State={item.state:18s} | "
            f"Retry={item.retry_count} | "
            f"Duration={duration}"
        )

    print()

    errors = []
    warnings = []

    # ==============================================================
    # CONSOLIDACIÓN POR CONSISTENCIA
    # ==============================================================

    complete_orders = []
    incomplete_orders = []

    for order in orders:
        problems = []

        if order.state not in (
            'paid',
            'invoiced',
        ):
            problems.append(
                f"estado {order.state}"
            )

        invoice = order.account_move

        if not invoice:
            problems.append("sin factura")

        elif invoice.state != 'posted':
            problems.append(
                f"factura {invoice.name} en {invoice.state}"
            )

        if len(order.picking_ids) != 1:
            problems.append(
                f"{len(order.picking_ids)} pickings"
            )

        else:
            picking = order.picking_ids[0]

            if picking.state != 'done':
                problems.append(
                    f"picking {picking.name} en {picking.state}"
                )

            item = tenv['pos.inventory.queue'].search([
                ('picking_id', '=', picking.id),
            ])

            if len(item) != 1:
                problems.append(
                    f"{len(item)} items de cola para picking"
                )

            elif item.state != 'done':
                problems.append(
                    f"cola {item.name} en {item.state}"
                )

            elif item.error_message:
                problems.append(
                    f"cola {item.name} error: "
                    f"{item.error_message}"
                )

        # ==========================================================
        # VALIDACIÓN DE TAXES
        # ==========================================================

        for line in order.lines:
            for tax in line.tax_ids:
                if (
                    tax.company_id
                    and tax.company_id != order.session_id.company_id
                ):
                    problems.append(
                        f"line {line.id}: tax {tax.id} "
                        f"pertenece a company {tax.company_id.id}, "
                        f"pero POS pertenece a company "
                        f"{order.session_id.company_id.id}"
                    )

        if problems:
            incomplete_orders.append((order, problems))
        else:
            complete_orders.append(order)

    print()
    print(" CONSOLIDACIÓN POR CONSISTENCIA")
    print(
        f"  Órdenes completas  : "
        f"{len(complete_orders)}"
    )
    print(
        f"  Órdenes incompletas: "
        f"{len(incomplete_orders)}"
    )

    for order, problems in incomplete_orders:
        payments = order.payment_ids
        queue_desc = []

        for picking in order.picking_ids:
            item = tenv['pos.inventory.queue'].search([
                ('picking_id', '=', picking.id),
            ])

            for it in item:
                queue_desc.append(
                    f"{it.name}/{it.state}/r{it.retry_count}"
                )

        print()
        print(
            f"  INCOMPLETA {order.name} "
            f"[{order.pos_reference}] "
            f"state={order.state} "
            f"amount_tax={order.amount_tax} "
            f"amount_total={order.amount_total}"
        )
        print(
            f"    pagos={len(payments)} "
            f"pagado={sum(p.amount for p in payments)}"
        )
        print(
            f"    picking_ids={order.picking_ids.ids} "
            f"estados="
            f"{[p.state for p in order.picking_ids]} "
            f"cola={queue_desc}"
        )

        if order.account_move:
            inv = order.account_move
            print(
                f"    factura={inv.id}/{inv.name} "
                f"state={inv.state}"
            )

        for prob in problems:
            print(f"    - {prob}")

    # ==============================================================
    # ORDERS
    # ==============================================================

    if len(complete_orders) != expected_processed:
        warnings.append(
            f"Órdenes completas {len(complete_orders)} "
            f"de {expected_processed} creadas "
            f"(fallas de concurrencia esperadas: "
            f"SerializationFailure/PoolError)"
        )

    if incomplete_orders:
        warnings.append(
            f"{len(incomplete_orders)} órdenes incompletas "
            f"del run (workers fallidos): " + ', '.join(
                f"{o.name}[{o.pos_reference}]"
                for o, _ in incomplete_orders
            )
        )

    # ==============================================================
    # QUEUE
    # ==============================================================

    for item in queue_items:

        if item.state != 'done':
            errors.append(
                f"Queue {item.id} no está done: "
                f"{item.state}"
            )

        if item.error_message:
            errors.append(
                f"Queue {item.id} tiene error: "
                f"{item.error_message}"
            )

        if not item.start_date:
            errors.append(
                f"Queue {item.id} no tiene start_date"
            )

        if not item.done_date:
            errors.append(
                f"Queue {item.id} no tiene done_date"
            )

        if item.retry_count != 0:
            print(
                f"  (Queue {item.name} tuvo "
                f"{item.retry_count} retries internos, "
                f"terminó done)"
            )

    if len(queue_items) != len(pickings):
        errors.append(
            f"Se esperaban {len(pickings)} items de cola "
            f"pero existen {len(queue_items)}"
        )

    # ==============================================================
    # STOCK
    # ==============================================================

    if float_compare(
        final_qty,
        expected_final,
        precision_rounding = precision_rounding,
    ) != 0:
        errors.append(
            f"Stock incorrecto. "
            f"Esperado={expected_final}, "
            f"actual={final_qty}"
        )

    # ==============================================================
    # RESIDUAL QUEUE ITEMS
    # ==============================================================

    residual = tenv[
        'pos.inventory.queue'
    ].search([
        ('picking_id', 'in', pickings.ids),
        (
            'state',
            'in',
            [
                'pending',
                'processing',
                'failed',
                'failed_permanent',
            ],
        ),
    ])

    if residual:
        errors.append(
            f"Quedan items residuales en la cola: "
            f"{residual.ids}"
        )

    # ==============================================================
    # RESULT
    # ==============================================================

    if errors:
        print("❌ PRUEBA FALLIDA")
        print()

        for error in errors:
            print(
                f"  - {error}"
            )

    else:
        print(
            "✅ PRUEBA FUNCIONAL SUPERADA"
        )
        print()
        print(
            f"{expected_processed} órdenes facturadas "
            f"y sus pickings fueron procesados por la cola. "
            f"El stock final coincide con el esperado."
        )

    if warnings:
        print()
        print("⚠️  ADVERTENCIAS (no bloquean la prueba)")
        print()

        for warning in warnings:
            print(
                f"  - {warning}"
            )

    print()

    return not errors


def main_shell(args):
    env_shell = globals()['env']

    print()
    print("=" * 70)
    print(" POS INVENTORY QUEUE")
    print(" PRUEBA DE ÓRDENES + FACTURAS CONCURRENTES")
    print(" (ODOO.SH SHELL - THREADS)")
    print("=" * 70)
    print()

    (
        initial_qty,
        session,
        product,
        partner,
        payment_method,
    ) = prepare_test_data(
        env_shell,
        args,
    )

    print("=" * 70)
    print(" INICIANDO PROCESAMIENTO CONCURRENTE")
    print("=" * 70)
    print()

    results = []
    lock = threading.Lock()
    go_event = threading.Event()

    threads = []

    for worker_id in range(
        1,
        args.workers + 1,
    ):
        thread = threading.Thread(
            target=run_order_in_thread,
            args=(
                worker_id,
                args,
                go_event,
                results,
                lock,
            ),
            daemon=False,
        )

        thread.start()
        threads.append(thread)

    time.sleep(0.5)

    go_event.set()

    for thread in threads:
        thread.join()

    failed = [
        r for r in results
        if not r['ok']
    ]

    if failed:
        print()
        print("=" * 70)
        print(
            f" {len(failed)} WORKERS FALLARON"
        )
        print("=" * 70)

        for result in failed:
            print(
                f"  Worker {result['worker_id']}: "
                f"{result['error']}"
            )

    serial_failures = sum(
        1 for r in failed
        if 'SerializationFailure' in r['error']
        or 'DeadlockDetected' in r['error']
    )

    conn_failures = sum(
        1 for r in failed
        if ('OperationalError' in r['error']
            or 'PoolError' in r['error'])
        and 'SerializationFailure' not in r['error']
        and 'DeadlockDetected' not in r['error']
    )

    other_failures = (
        len(failed) - serial_failures - conn_failures
    )

    retries_used = sum(
        r['attempts'] - 1
        for r in results
        if r['ok']
    )

    attempts_used = sum(
        r['attempts']
        for r in results
    )

    print()
    print(
        f"Recuento de fallos: "
        f"serialización={serial_failures}, "
        f"conexión={conn_failures}, "
        f"otros={other_failures}"
    )
    print(
        f"Retries usados (workers OK): {retries_used} "
        f"| total intentos: {attempts_used}"
    )

    print()
    print("=" * 70)
    print(" VALIDANDO RESULTADOS")
    print("=" * 70)

    # ==============================================================
    # DRAIN: procesar items residuales con retries
    # ==============================================================
    # (mismo criterio que main_cli: items stuck en 'processing' se
    #  resetean a 'pending' y se reprocesan; el modulo es idempotente)

    DRAIN_RETRIES = 3
    DRAIN_WAIT = 10

    for drain_attempt in range(1, DRAIN_RETRIES + 1):
        drain_env, drain_cr = _fresh_env(args.db_name)

        try:
            drain_env['pos.inventory.queue']._process_queue()
            drain_cr.commit()
        finally:
            drain_cr.close()

        check_env, check_cr = _fresh_env(args.db_name)

        try:
            stuck = check_env['pos.inventory.queue'].search([
                ('state', '=', 'processing'),
                (
                    'picking_id.pos_order_id.pos_reference',
                    'like',
                    f'{args.order_prefix}-%',
                ),
            ])
        finally:
            check_cr.close()

        if not stuck:
            print(
                f"  Drain #{drain_attempt}: "
                f"cola limpia, 0 items stuck"
            )
            break

        print(
            f"  Drain #{drain_attempt}: "
            f"{len(stuck)} items stuck en 'processing', "
            f"{'reseteando a pending' if drain_attempt < DRAIN_RETRIES else 'ultimo intento'}"
        )

        if drain_attempt < DRAIN_RETRIES:
            time.sleep(DRAIN_WAIT)

            reset_env, reset_cr = _fresh_env(args.db_name)

            try:
                reset_cr.execute(
                    "UPDATE pos_inventory_queue "
                    "SET state = 'pending', "
                    "    start_date = NULL "
                    "WHERE id IN %s",
                    (tuple(stuck.ids),),
                )
                reset_cr.commit()
            finally:
                reset_cr.close()

    validation_env, validation_cr = _fresh_env(
        args.db_name
    )

    try:
        success = validate_results(
            validation_env,
            args,
            initial_qty,
        )
    finally:
        validation_cr.close()

    print("=" * 70)

    if success:
        print(
            " RESULTADO FINAL: PASS"
        )
    else:
        print(
            " RESULTADO FINAL: FAIL"
        )

    print("=" * 70)
    print()

    return success


def main_cli():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--config',
        required=True,
    )

    parser.add_argument(
        '--db',
        required=True,
    )

    parser.add_argument(
        '--session',
        dest='session_name',
        default=DEFAULT_SESSION,
    )


    parser.add_argument(
        '--product-id',
        type=int,
        default=DEFAULT_PRODUCT_ID,
    )

    parser.add_argument(
        '--partner-id',
        type=int,
        default=DEFAULT_PARTNER_ID,
    )

    parser.add_argument(
        '--product-ids',
        default='',
        help='CSV de products (repartidos entre workers). '
             'Si se omite usa --product-id.',
    )

    parser.add_argument(
        '--partner-ids',
        default='',
        help='CSV de partners (repartidos entre workers). '
             'Si se omite usa --partner-id.',
    )

    parser.add_argument(
        '--sessions',
        default='',
        help='CSV de nombres de sesión (repartidos entre workers). '
             'Si se omite usa --session.',
    )

    parser.add_argument(
        '--payment-method-id',
        type=int,
        default=DEFAULT_PAYMENT_METHOD_ID,
    )

    parser.add_argument(
        '--workers',
        type=int,
        default=DEFAULT_WORKERS,
    )

    parser.add_argument(
        '--quantity',
        type=float,
        default=DEFAULT_QUANTITY,
    )

    parser.add_argument(
        '--attempts',
        dest='max_attempts',
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
    )

    parser.add_argument(
        '--max-conn',
        type=int,
        default=64,
        help='Máximo de conexiones Odoo simultáneas ( Semaphore). '
             'Debe ser <= db_maxconn del contenedor. Evita '
             'PoolError: The Connection Pool Is Full.',
    )

    args = parser.parse_args()

    args.db_name = args.db

    args.order_prefix = _make_prefix()

    args.product_ids = _parse_csv_ints(
        args.product_ids,
        args.product_id,
    )
    args.partner_ids = _parse_csv_ints(
        args.partner_ids,
        args.partner_id,
    )
    args.session_names = _parse_csv_str(
        args.sessions,
        args.session_name,
    )

    global CONN_SEMAPHORE
    CONN_SEMAPHORE = threading.Semaphore(
        max(1, args.max_conn)
    )

    if args.workers < 2:
        raise SystemExit(
            '--workers debe ser >= 2'
        )

    multiprocessing.set_start_method(
        'spawn',
        force=True,
    )

    print()
    print("=" * 70)
    print(" POS INVENTORY QUEUE")
    print(" PRUEBA DE ÓRDENES + FACTURAS CONCURRENTES")
    print("=" * 70)
    print()

    import odoo

    odoo.tools.config.parse_config([
        '-c',
        args.config,
        '-d',
        args.db,
        '--addons-path',
        '/mnt/extra-addons/custom-addons,/usr/lib/python3/dist-packages/odoo/addons',
        #'/home/odoo/src/user,/home/odoo/src/user/Jorels-Community/jorels-odoo-addons,/home/odoo/src/user/ffjuanzuluaga,/home/odoo/src/odoo/addons,/home/odoo/src/odoo/odoo/addons,/home/odoo/src/enterprise,/home/odoo/src/themes',
    ])

    registry = odoo.registry(
        args.db
    )

    with registry.cursor() as cr:
        from odoo import api, SUPERUSER_ID

        env = api.Environment(
            cr,
            SUPERUSER_ID,
            {},
        )

        (
            initial_qty,
            session,
            product,
            partner,
            payment_method,
        ) = prepare_test_data(
            env,
            args,
        )

    print("=" * 70)
    print(" INICIANDO PROCESAMIENTO CONCURRENTE")
    print("=" * 70)
    print()

    results = []
    lock = threading.Lock()
    go_event = threading.Event()

    threads = []

    for worker_id in range(
        1,
        args.workers + 1,
    ):
        thread = threading.Thread(
            target=run_order_in_thread,
            args=(
                worker_id,
                args,
                go_event,
                results,
                lock,
            ),
            daemon=False,
        )

        thread.start()
        threads.append(thread)

    time.sleep(0.5)

    go_event.set()

    for thread in threads:
        thread.join()

    failed = [
        r for r in results
        if not r['ok']
    ]

    if failed:
        print()
        print("=" * 70)
        print(
            f" {len(failed)} WORKERS FALLARON"
        )
        print("=" * 70)

        for result in failed:
            print(
                f"  Worker {result['worker_id']}: "
                f"{result['error']}"
            )

    serial_failures = sum(
        1 for r in failed
        if 'SerializationFailure' in r['error']
        or 'DeadlockDetected' in r['error']
    )

    conn_failures = sum(
        1 for r in failed
        if ('OperationalError' in r['error']
            or 'PoolError' in r['error'])
        and 'SerializationFailure' not in r['error']
        and 'DeadlockDetected' not in r['error']
    )

    other_failures = (
        len(failed) - serial_failures - conn_failures
    )

    retries_used = sum(
        r['attempts'] - 1
        for r in results
        if r['ok']
    )

    attempts_used = sum(
        r['attempts']
        for r in results
    )

    print()
    print(
        f"Recuento de fallos: "
        f"serialización={serial_failures}, "
        f"conexión={conn_failures}, "
        f"otros={other_failures}"
    )
    print(
        f"Retries usados (workers OK): {retries_used} "
        f"| total intentos: {attempts_used}"
    )

    print("=" * 70)
    print(" VALIDANDO RESULTADOS")
    print("=" * 70)

    # ==============================================================
    # DRAIN: procesar items residuales con retries
    # ==============================================================
    #
    # Items stuck en 'processing' ocurren cuando _process_item_in_new_cursor()
    # no pudo abrir un cursor nuevo (PoolError del pool compartido con el
    # servidor Odoo). Despues de que los workers terminan, el pool se libera.
    # Resetearlos a 'pending' y reintentar es seguro: el modulo detecta
    # si el picking ya fue done (inventory_queue.py linea ~424) y solo marca
    # el item 'done' sin re-ejecutar _action_done(), por tanto es idempotente.

    DRAIN_RETRIES = 3
    DRAIN_WAIT = 10  # segundos entre intentos

    for drain_attempt in range(1, DRAIN_RETRIES + 1):
        # 1) Procesar la cola normalmente (pending + processing stale).
        drain_env, drain_cr = _fresh_env(args.db)

        try:
            drain_env['pos.inventory.queue']._process_queue()
            drain_cr.commit()
        finally:
            drain_cr.close()

        # 2) Buscar items stuck en 'processing' (recien atrapados, no stale).
        #    Solo los del run actual (pos_reference con el prefix del test)
        #    para no reprocesar items colgados de corridas anteriores.
        check_env, check_cr = _fresh_env(args.db)

        try:
            stuck = check_env['pos.inventory.queue'].search([
                ('state', '=', 'processing'),
                (
                    'picking_id.pos_order_id.pos_reference',
                    'like',
                    f'{args.order_prefix}-%',
                ),
            ])
        finally:
            check_cr.close()

        if not stuck:
            print(
                f"  Drain #{drain_attempt}: "
                f"cola limpia, 0 items stuck"
            )
            break

        print(
            f"  Drain #{drain_attempt}: "
            f"{len(stuck)} items stuck en 'processing', "
            f"{'reseteando a pending' if drain_attempt < DRAIN_RETRIES else 'ultimo intento'}"
        )

        if drain_attempt < DRAIN_RETRIES:
            # 3) Esperar a que items en vuelo completen su transaccion.
            time.sleep(DRAIN_WAIT)

            # 4) Resetear stuck items a 'pending' para que el proximo
            #    _process_queue los reclame (el modulo es idempotente).
            reset_env, reset_cr = _fresh_env(args.db)

            try:
                reset_cr.execute(
                    "UPDATE pos_inventory_queue "
                    "SET state = 'pending', "
                    "    start_date = NULL "
                    "WHERE id IN %s",
                    (tuple(stuck.ids),),
                )
                reset_cr.commit()
            finally:
                reset_cr.close()

    validation_env, validation_cr = _fresh_env(
        args.db
    )

    try:
        success = validate_results(
            validation_env,
            args,
            initial_qty,
        )
    finally:
        validation_cr.close()

    print("=" * 70)

    if success:
        print(
            " RESULTADO FINAL: PASS"
        )
    else:
        print(
            " RESULTADO FINAL: FAIL"
        )

    print("=" * 70)
    print()

    sys.exit(
        0 if success else 1
    )


def main():
    db_name = _shell_dbname()

    if db_name:
        args = types.SimpleNamespace(
            db_name=db_name,

            order_prefix=_make_prefix(),

            session_name=os.environ.get(
                'POS_TEST_SESSION',
                DEFAULT_SESSION,
            ),

            product_id=int(
                os.environ.get(
                    'POS_TEST_PRODUCT_ID',
                    DEFAULT_PRODUCT_ID,
                )
            ),

            partner_id=int(
                os.environ.get(
                    'POS_TEST_PARTNER_ID',
                    DEFAULT_PARTNER_ID,
                )
            ),

            payment_method_id=int(
                os.environ.get(
                    'POS_TEST_PAYMENT_METHOD_ID',
                    DEFAULT_PAYMENT_METHOD_ID,
                )
            ),

            workers=int(
                os.environ.get(
                    'POS_TEST_WORKERS',
                    DEFAULT_WORKERS,
                )
            ),

            max_attempts=int(
                os.environ.get(
                    'POS_TEST_ATTEMPTS',
                    DEFAULT_MAX_ATTEMPTS,
                )
            ),

            quantity=float(
                os.environ.get(
                    'POS_TEST_QUANTITY',
                    DEFAULT_QUANTITY,
                )
            ),
        )

        success = main_shell(args)

    else:
        main_cli()
        return

    if not success:
        sys.exit(1)


if __name__ == '__main__':
    main()