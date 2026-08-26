#!/usr/bin/env python3

import argparse
import multiprocessing
import os
import sys
import time
import traceback


def load_odoo(config_path, db_name):
    import odoo

    odoo.tools.config.parse_config([
        "-c",
        config_path,
        "-d",
        db_name,
    ])

    return odoo


def prepare_test_data(
    config_path,
    db_name,
    session_name,
    template_id,
    quantity,
):
    """
    Crea N pickings reales y sus registros de cola.

    IMPORTANTE:
    Aquí NO se procesan los pickings.

    Solamente se crean:
        stock.picking
        stock.move
        pos.inventory.queue

    Después los workers competirán por procesar la cola.
    """

    import odoo
    from odoo import api, SUPERUSER_ID

    registry = odoo.registry(db_name)

    with registry.cursor() as cr:

        env = api.Environment(
            cr,
            SUPERUSER_ID,
            {},
        )

        session = env["pos.session"].search(
            [
                ("name", "=", session_name),
            ],
            limit=1,
        )

        if not session:
            raise RuntimeError(
                f"No existe la sesión {session_name}"
            )

        if session.state != "opened":
            raise RuntimeError(
                f"La sesión {session_name} no está abierta. "
                f"Estado actual: {session.state}"
            )

        template = env[
            "product.template"
        ].browse(template_id).exists()

        if not template:
            raise RuntimeError(
                f"No existe product.template {template_id}"
            )

        product = template.product_variant_id

        if not product:
            raise RuntimeError(
                f"El template {template_id} no tiene variante"
            )

        picking_type = session.config_id.picking_type_id

        if not picking_type:
            raise RuntimeError(
                "El POS no tiene picking type configurado"
            )

        source_location = (
            picking_type.default_location_src_id
        )

        destination_location = (
            picking_type.default_location_dest_id
        )

        if not source_location:
            raise RuntimeError(
                "El picking type no tiene ubicación origen"
            )

        if not destination_location:
            raise RuntimeError(
                "El picking type no tiene ubicación destino"
            )

        # ---------------------------------------------------------
        # STOCK INICIAL
        # ---------------------------------------------------------

        initial_qty = env[
            "stock.quant"
        ]._get_available_quantity(
            product,
            source_location,
        )

        print()
        print("=" * 70)
        print(" PREPARACIÓN DE PRUEBA")
        print("=" * 70)
        print()
        print(f"Sesión              : {session.name} (ID {session.id})")
        print(
            f"Producto             : "
            f"{product.display_name} (ID {product.id})"
        )
        print(
            f"Origen               : "
            f"{source_location.display_name} "
            f"(ID {source_location.id})"
        )
        print(
            f"Destino              : "
            f"{destination_location.display_name} "
            f"(ID {destination_location.id})"
        )
        print(f"Stock inicial        : {initial_qty}")
        print(f"Pickings a procesar  : {quantity}")
        print()

        if initial_qty < quantity:
            raise RuntimeError(
                f"Stock insuficiente. "
                f"Disponible={initial_qty}, "
                f"necesario={quantity}"
            )

        # ---------------------------------------------------------
        # LIMPIEZA:
        #
        # No reutilizamos registros pendientes de pruebas anteriores.
        # ---------------------------------------------------------

        old_pending = env[
            "pos.inventory.queue"
        ].search([
            ("state", "in", ["pending", "processing"])
        ])

        if old_pending:
            raise RuntimeError(
                "Hay elementos pendientes/processing en la cola. "
                "Limpia la cola antes de ejecutar esta prueba. "
                f"Encontrados: {old_pending.ids}"
            )

        created_pickings = []

        # ---------------------------------------------------------
        # CREAR PICKINGS SIN PROCESARLOS
        # ---------------------------------------------------------

        for index in range(quantity):

            picking = env[
                "stock.picking"
            ].create({
                "partner_id": session.config_id.company_id.partner_id.id,
                "picking_type_id": picking_type.id,
                "location_id": source_location.id,
                "location_dest_id": destination_location.id,
                "origin": (
                    f"QUEUE-CONCURRENCY-"
                    f"{session.name}-"
                    f"{index + 1}"
                ),
                "pos_session_id": session.id,
            })

            move = env[
                "stock.move"
            ].create({
                "name": (
                    f"QUEUE-CONCURRENCY "
                    f"{index + 1}"
                ),
                "product_id": product.id,
                "product_uom_qty": 1.0,
                "product_uom": product.uom_id.id,
                "picking_id": picking.id,
                "picking_type_id": picking_type.id,
                "location_id": source_location.id,
                "location_dest_id": destination_location.id,
                "company_id": session.config_id.company_id.id,
            })

            picking.action_confirm()

            # POS normalmente marca los movimientos como picked.
            move.picked = True

            # -----------------------------------------------------
            # CREAR REGISTRO REAL DE LA COLA
            # -----------------------------------------------------

            queue_item = env[
                "pos.inventory.queue"
            ].create({
                "picking_id": picking.id,
                "state": "pending",
            })

            created_pickings.append(
                (
                    picking.id,
                    picking.name,
                    queue_item.id,
                    queue_item.name,
                    queue_item.sequence,
                )
            )

        cr.commit()

        print("Pickings creados:")
        print()

        for data in created_pickings:
            (
                picking_id,
                picking_name,
                queue_id,
                queue_name,
                sequence,
            ) = data

            print(
                f"  Queue {queue_id:4d} | "
                f"{queue_name:15s} | "
                f"Picking {picking_name:20s} | "
                f"sequence={sequence}"
            )

        print()
        print(
            "Todos los pickings están PENDIENTES. "
            "Todavía no se ha actualizado el stock."
        )
        print()

        picking_ids = [p[0] for p in created_pickings]

        return initial_qty, product.id, source_location.id, picking_ids


def worker(
    config_path,
    db_name,
    worker_id,
    barrier,
):
    """
    Cada worker tiene su propio proceso y su propia conexión.

    Todos llaman al método REAL:
        pos.inventory.queue._process_queue()
    """

    try:

        import odoo
        from odoo import api, SUPERUSER_ID

        odoo.tools.config.parse_config([
            "-c",
            config_path,
            "-d",
            db_name,
        ])

        registry = odoo.registry(db_name)

        print(
            f"[WORKER {worker_id}] "
            f"PID={os.getpid()} preparado",
            flush=True,
        )

        with registry.cursor() as cr:

            env = api.Environment(
                cr,
                SUPERUSER_ID,
                {},
            )

            Queue = env[
                "pos.inventory.queue"
            ]

            pending = Queue.search(
                [
                    ("state", "=", "pending"),
                ],
                order="sequence, id",
            )

            print(
                f"[WORKER {worker_id}] "
                f"Pendientes antes de comenzar: "
                f"{pending.ids}",
                flush=True,
            )

            print(
                f"[WORKER {worker_id}] "
                f"ESPERANDO BARRERA",
                flush=True,
            )

            barrier.wait()

            start = time.time()

            print(
                f"[WORKER {worker_id}] "
                f"ENTRANDO A _process_queue() "
                f"{start:.6f}",
                flush=True,
            )

            try:

                # =================================================
                # ESTE ES EL PUNTO CLAVE.
                #
                # Estamos ejecutando TU método real.
                #
                # _process_queue() adquiere:
                #
                # pg_advisory_xact_lock(54321)
                #
                # y después procesa los pending.
                # =================================================

                Queue._process_queue()

                elapsed = time.time() - start

                print(
                    f"[WORKER {worker_id}] "
                    f"_process_queue() FINALIZÓ "
                    f"en {elapsed:.3f}s",
                    flush=True,
                )

                cr.commit()

                print(
                    f"[WORKER {worker_id}] "
                    f"COMMIT OK",
                    flush=True,
                )

            except Exception as exc:

                print(
                    f"[WORKER {worker_id}] "
                    f"ERROR DURANTE _process_queue(): "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

                traceback.print_exc()

                cr.rollback()

    except Exception as exc:

        print(
            f"[WORKER {worker_id}] "
            f"ERROR GENERAL: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        traceback.print_exc()


def validate_results(
    config_path,
    db_name,
    session_name,
    template_id,
    initial_qty,
    expected_processed,
    created_picking_ids,
):
    """
    Valida el resultado final desde una conexión nueva.
    """

    import odoo
    from odoo import api, SUPERUSER_ID

    registry = odoo.registry(db_name)

    with registry.cursor() as cr:

        env = api.Environment(
            cr,
            SUPERUSER_ID,
            {},
        )

        session = env[
            "pos.session"
        ].search(
            [
                ("name", "=", session_name),
            ],
            limit=1,
        )

        template = env[
            "product.template"
        ].browse(template_id)

        product = template.product_variant_id

        picking_type = session.config_id.picking_type_id

        source_location = (
            picking_type.default_location_src_id
        )

        Queue = env[
            "pos.inventory.queue"
        ]

        items = Queue.search(
            [
                ('picking_id', 'in', created_picking_ids),
            ],
            order="sequence, id",
        )

        final_qty = env[
            "stock.quant"
        ]._get_available_quantity(
            product,
            source_location,
        )

        print()
        print("=" * 70)
        print(" RESULTADO DE LA PRUEBA")
        print("=" * 70)
        print()

        print(
            f"Stock inicial esperado : {initial_qty}"
        )

        print(
            f"Stock final             : {final_qty}"
        )

        expected_final = (
            initial_qty - expected_processed
        )

        print(
            f"Stock final esperado    : {expected_final}"
        )

        print()

        print(
            f"Elementos de cola      : {len(items)}"
        )

        print()

        errors = []

        # ---------------------------------------------------------
        # VALIDAR CADA ITEM
        # ---------------------------------------------------------

        for item in items:

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
                f"Start={start} | "
                f"Done={done} | "
                f"Duration={duration}"
            )

            if item.state != "done":
                errors.append(
                    f"Queue {item.id} no está done: "
                    f"{item.state}"
                )

            if item.retry_count != 0:
                errors.append(
                    f"Queue {item.id} tuvo retries: "
                    f"{item.retry_count}"
                )

            if item.error_message:
                errors.append(
                    f"Queue {item.id} tiene error: "
                    f"{item.error_message}"
                )

            if not start:
                errors.append(
                    f"Queue {item.id} no tiene start_date"
                )

            if not done:
                errors.append(
                    f"Queue {item.id} no tiene done_date"
                )

        # ---------------------------------------------------------
        # VALIDAR CANTIDAD
        # ---------------------------------------------------------

        if len(items) != expected_processed:

            errors.append(
                f"Se esperaban {expected_processed} "
                f"elementos de cola pero existen "
                f"{len(items)}"
            )

        # ---------------------------------------------------------
        # VALIDAR STOCK
        # ---------------------------------------------------------

        if final_qty != expected_final:

            errors.append(
                f"Stock incorrecto. "
                f"Esperado={expected_final}, "
                f"actual={final_qty}"
            )

        print()

        if errors:

            print("❌ PRUEBA FALLIDA")
            print()

            for error in errors:
                print(f"  - {error}")

        else:

            print(
                "✅ PRUEBA FUNCIONAL SUPERADA"
            )

            print()
            print(
                "Los pickings fueron procesados "
                "por la cola y el stock final "
                "coincide con el esperado."
            )

        print()

        cr.commit()

        return not errors


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--config",
        required=True,
    )

    parser.add_argument(
        "--db",
        required=True,
    )

    parser.add_argument(
        "--session",
        default="POS/00148",
    )

    parser.add_argument(
        "--template-id",
        type=int,
        default=62,
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=5,
    )

    args = parser.parse_args()

    if args.workers < 2:
        raise SystemExit(
            "--workers debe ser >= 2"
        )

    # -------------------------------------------------------------
    # IMPORTANTE:
    # Cada worker empieza desde cero.
    # NO hereda conexiones PostgreSQL.
    # -------------------------------------------------------------

    multiprocessing.set_start_method(
        "spawn",
        force=True,
    )

    print()
    print("=" * 70)
    print(" POS INVENTORY QUEUE")
    print(" PRUEBA REAL DE CONCURRENCIA")
    print("=" * 70)
    print()

    print(
        f"BD             : {args.db}"
    )
    print(
        f"Sesión         : {args.session}"
    )
    print(
        f"Producto       : template {args.template_id}"
    )
    print(
        f"Workers        : {args.workers}"
    )
    print()

    # -------------------------------------------------------------
    # PASO 1
    # Crear pickings + queue.
    # -------------------------------------------------------------

    initial_qty, product_id, location_id, picking_ids = (
        prepare_test_data(
            args.config,
            args.db,
            args.session,
            args.template_id,
            args.workers,
        )
    )

    # -------------------------------------------------------------
    # PASO 2
    # Lanzar workers concurrentes.
    # -------------------------------------------------------------

    print("=" * 70)
    print(" INICIANDO PROCESAMIENTO CONCURRENTE")
    print("=" * 70)
    print()

    barrier = multiprocessing.Barrier(
        args.workers
    )

    processes = []

    for worker_id in range(
        1,
        args.workers + 1,
    ):

        process = multiprocessing.Process(
            target=worker,
            args=(
                args.config,
                args.db,
                worker_id,
                barrier,
            ),
        )

        process.start()

        processes.append(process)

    for process in processes:

        process.join()

    # -------------------------------------------------------------
    # PASO 3
    # Validar resultados.
    # -------------------------------------------------------------

    success = validate_results(
        args.config,
        args.db,
        args.session,
        args.template_id,
        initial_qty,
        args.workers,
        picking_ids,
    )

    print("=" * 70)

    if success:
        print(" RESULTADO FINAL: PASS")
    else:
        print(" RESULTADO FINAL: FAIL")

    print("=" * 70)
    print()

    sys.exit(
        0 if success else 1
    )


if __name__ == "__main__":
    main()



"""#!/usr/bin/env python3

import argparse
import multiprocessing
import os
import sys
import time
import traceback


def worker(
    db_name,
    config_path,
    session_name,
    template_id,
    worker_id,
    barrier,
):
    

    try:
        import odoo
        from odoo import api, SUPERUSER_ID

        # ---------------------------------------------------------
        # Cada proceso configura Odoo independientemente.
        # ---------------------------------------------------------

        odoo.tools.config.parse_config([
            "-c",
            config_path,
            "-d",
            db_name,
        ])

        registry = odoo.registry(db_name)

        print(
            f"[WORKER {worker_id}] "
            f"PID={os.getpid()} preparado",
            flush=True,
        )

        # ---------------------------------------------------------
        # Cada worker obtiene su propia conexión.
        # ---------------------------------------------------------

        with registry.cursor() as cr:

            env = api.Environment(
                cr,
                SUPERUSER_ID,
                {},
            )

            session = env["pos.session"].search(
                [
                    ("name", "=", session_name),
                ],
                limit=1,
            )

            if not session:
                raise RuntimeError(
                    f"No se encontró la sesión {session_name}"
                )

            template = env[
                "product.template"
            ].browse(template_id).exists()

            if not template:
                raise RuntimeError(
                    f"No existe product.template "
                    f"ID {template_id}"
                )

            product = template.product_variant_id

            if not product:
                raise RuntimeError(
                    f"El template {template.display_name} "
                    f"no tiene product.product"
                )

            print(
                f"[WORKER {worker_id}] "
                f"Sesión={session.name} "
                f"(ID {session.id}) "
                f"Producto={product.display_name} "
                f"(ID {product.id})",
                flush=True,
            )

            # -----------------------------------------------------
            # Todos los workers esperan aquí.
            # -----------------------------------------------------

            print(
                f"[WORKER {worker_id}] "
                f"ESPERANDO BARRERA",
                flush=True,
            )

            barrier.wait()

            start = time.time()

            print(
                f"[WORKER {worker_id}] "
                f"INICIO CONCURRENTE "
                f"{time.time():.6f}",
                flush=True,
            )

            # -----------------------------------------------------
            # PRUEBA ACTUAL
            #
            # Todavía NO modificamos inventario.
            #
            # Solamente hacemos una lectura dentro de una
            # transacción independiente para comprobar que
            # los workers realmente están trabajando con
            # conexiones separadas.
            # -----------------------------------------------------

            orders = env["pos.order"].search(
                [
                    ("session_id", "=", session.id),
                    ("lines.product_id", "=", product.id),
                ],
                order="id desc",
                limit=1,
            )

            if orders:
                order = orders[0]

                print(
                    f"[WORKER {worker_id}] "
                    f"Orden encontrada: "
                    f"{order.name} "
                    f"(ID {order.id})",
                    flush=True,
                )
            else:
                print(
                    f"[WORKER {worker_id}] "
                    f"No existe todavía una orden "
                    f"para el producto",
                    flush=True,
                )

            elapsed = time.time() - start

            print(
                f"[WORKER {worker_id}] "
                f"FINALIZADO en {elapsed:.3f}s",
                flush=True,
            )

            cr.commit()

    except Exception as exc:

        print(
            f"[WORKER {worker_id}] "
            f"ERROR: {type(exc).__name__}: {exc}",
            flush=True,
        )

        traceback.print_exc()


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Prueba de conexiones concurrentes "
            "para POS Inventory Queue"
        )
    )

    parser.add_argument(
        "--db",
        required=True,
    )

    parser.add_argument(
        "--config",
        required=True,
    )

    parser.add_argument(
        "--session",
        default="POS/00148",
    )

    parser.add_argument(
        "--template-id",
        type=int,
        default=62,
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=5,
    )

    args = parser.parse_args()

    # -------------------------------------------------------------
    # IMPORTANTE:
    #
    # Usamos spawn en lugar de fork.
    #
    # Así cada worker empieza limpio y NO hereda conexiones
    # PostgreSQL del proceso padre.
    # -------------------------------------------------------------

    multiprocessing.set_start_method(
        "spawn",
        force=True,
    )

    print()
    print("=" * 70)
    print(" POS INVENTORY QUEUE - PRUEBA DE CONCURRENCIA")
    print("=" * 70)
    print()

    print(f"Base de datos : {args.db}")
    print(f"Sesión        : {args.session}")
    print(f"Template ID   : {args.template_id}")
    print(f"Workers       : {args.workers}")
    print()

    # -------------------------------------------------------------
    # Barrera compartida.
    #
    # Todos los procesos deben llegar aquí antes de continuar.
    # -------------------------------------------------------------

    barrier = multiprocessing.Barrier(
        args.workers
    )

    processes = []

    for worker_id in range(
        1,
        args.workers + 1,
    ):

        process = multiprocessing.Process(
            target=worker,
            args=(
                args.db,
                args.config,
                args.session,
                args.template_id,
                worker_id,
                barrier,
            ),
        )

        process.start()

        processes.append(process)

    # -------------------------------------------------------------
    # Esperar workers.
    # -------------------------------------------------------------

    for process in processes:
        process.join()

    print()
    print("=" * 70)
    print(" PRUEBA FINALIZADA")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
"""