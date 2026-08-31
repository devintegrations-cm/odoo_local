import hashlib
import logging
import random
import time
import traceback

import psycopg2
from psycopg2 import errors as psycopg2_errors
from psycopg2.pool import PoolError

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class PosInventoryQueue(models.Model):
    _name = 'pos.inventory.queue'
    _description = 'POS Inventory Queue'
    _order = 'sequence, id'

    _sql_constraints = [
        (
            'picking_unique',
            'UNIQUE(picking_id)',
            'A queue item already exists for this picking.',
        ),
    ]

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        default='New',
        readonly=True,
    )

    sequence = fields.Integer(
        string='Sequence',
        default=10,
        index=True,
    )

    picking_id = fields.Many2one(
        'stock.picking',
        string='Picking',
        required=True,
        ondelete='cascade',
        readonly=True,
    )

    pos_order_id = fields.Many2one(
        'pos.order',
        string='POS Order',
        related='picking_id.pos_order_id',
        store=True,
        readonly=True,
    )

    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('processing', 'Processing'),
            ('done', 'Done'),
            ('failed', 'Failed'),
            ('failed_permanent', 'Failed Permanent'),
        ],
        string='State',
        default='pending',
        copy=False,
        readonly=True,
        index=True,
    )

    retry_count = fields.Integer(
        string='Retry Count',
        default=0,
        readonly=True,
    )

    start_date = fields.Datetime(
        string='Start Date',
        readonly=True,
    )

    done_date = fields.Datetime(
        string='Done Date',
        readonly=True,
    )

    error_date = fields.Datetime(
        string='Error Date',
        readonly=True,
    )

    error_message = fields.Text(
        string='Error Message',
        readonly=True,
    )

    MAX_RETRIES = 5
    CLAIM_MAX_RETRIES = 10

    # Reclama items de nuevo si quedaron 'processing' por un crash
    # del procesador que los tenía asignados.
    STALE_PROCESSING_MINUTES = 5

    # Timeout de contención de filas para los mensajes de la cola.
    # Evita que un procesador quede colgado esperando (sin límite)
    # un lock de fila retenido por otro que quedó 'idle in transaction'.
    # A los N segundos PostgreSQL lanza lock_not_available (SQLSTATE 55P03)
    # que cae en retry/backoff y finalmente en 'failed_permanent' con
    # diagnóstico, en lugar de colgarse indefinidamente.
    LOCK_TIMEOUT_SECONDS = 5

    # -------------------------------------------------------------------------
    # CREATE
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        to_create = []
        created = self.browse()

        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code(
                        'pos.inventory.queue'
                    )
                    or 'New'
                )

            if vals.get('picking_id'):
                existing = self.env['pos.inventory.queue'].search(
                    [('picking_id', '=', vals['picking_id'])],
                    limit=1,
                )
                if existing:
                    created |= existing
                    continue

            to_create.append(vals)

        if to_create:
            try:
                with self.env.cr.savepoint():
                    created |= super().create(to_create)
            except psycopg2_errors.UniqueViolation:
                # Dos requests concurrentes pueden pasar el search
                # anterior a la vez y chocar con UNIQUE(picking_id).
                # El savepoint acota el rollback a este INSERT: no
                # afecta al picking/orden que se está creando en la
                # misma transacción. Re-devolvemos los items ya en
                # cola. Idempotente.
                for vals in to_create:
                    picking_id = vals.get('picking_id')
                    if not picking_id:
                        continue
                    existing = self.env['pos.inventory.queue'].search(
                        [('picking_id', '=', picking_id)],
                        limit=1,
                    )
                    if existing:
                        created |= existing

        return created

    # -------------------------------------------------------------------------
    # QUEUE CLAIM — raw SQL with SerializationFailure resilience
    # -------------------------------------------------------------------------

    @api.model
    def _claim_next_item(self):
        """
        Atomically claim one queue item using FOR UPDATE SKIP LOCKED.

        Returns the ID of the claimed item, or None.

        IMPORTANTE:
        - NO commitea: el marcado 'processing' (con su start_date) queda
          en la transacción del cursor actual y se persiste cuando el
          dueño del cursor decide commitear.
        - En caso de conflicto transitorio durante el claim
          (SerializationFailure, poco frecuente) hace rollback del cursor
          actual e intenta de nuevo internamente. Por eso _process_queue
          debe invocarse sobre un cursor cuyo contenido descartable no
          importe (transacción post-commit, cron, o el cursor del test);
          la transacción ya committeada de la orden POS no se afecta.

        El diseño de la cola exige que el item ya esté COMMITTEADO para
        ser visible al procesador: la orden POS commitea
        orden + pago + picking + item como una sola unidad atómica y
        recién después dispara el procesamiento (post-commit, cron o
        llamada explícita desde el test).

        Dado que el lock del claim se libera al commitear, los items que
        quedaron 'processing' por un crash del procesador (más de
        STALE_PROCESSING_MINUTES minutos) se vuelven a reclamar.
        """
        self.env.cr.flush()

        for attempt in range(self.CLAIM_MAX_RETRIES):
            try:
                # El claim corre en la transacción del llamador, que
                # puede quedar 'idle in transaction' reteniendo la fila.
                # Limitar el lock_timeout evita colgarse esperando una
                # fila retenida por otro procesador que quedó colgado.
                # Se repite en cada intento porque un rollback interno
                # revierte el SET LOCAL.
                self.env.cr.execute(
                    "SET LOCAL lock_timeout = %s",
                    ("%d s" % self.LOCK_TIMEOUT_SECONDS,),
                )
                self.env.cr.execute(
                    """
                        SELECT id
                          FROM pos_inventory_queue
                         WHERE (
                                state IN ('pending', 'failed')
                                OR (
                                    state = 'processing'
                                    AND start_date < (
                                        now() AT TIME ZONE 'UTC'
                                        - %s::interval
                                    )
                                )
                              )
                           AND retry_count < %s
                         ORDER BY
                            CASE
                                WHEN state = 'pending' THEN 0
                                ELSE 1
                            END,
                            sequence, id
                         FOR UPDATE SKIP LOCKED
                         LIMIT 1
                    """,
                    (
                        "%d minutes" % self.STALE_PROCESSING_MINUTES,
                        self.MAX_RETRIES,
                    ),
                )

                row = self.env.cr.fetchone()
                if not row:
                    return None

                item_id = row[0]

                self.env.cr.execute(
                    """
                        UPDATE pos_inventory_queue
                           SET state = 'processing',
                               start_date = now() AT TIME ZONE 'UTC'
                         WHERE id = %s
                    """,
                    (item_id,),
                )

                _logger.info(
                    'POS Queue: claimed item %s',
                    item_id,
                )

                return item_id

            except psycopg2.errors.SerializationFailure:
                self.env.cr.rollback()
                self.env.cr.flush()
                _logger.debug(
                    'POS Queue: claim conflict on attempt %d, retrying',
                    attempt + 1,
                )
                continue

        _logger.warning(
            'POS Queue: could not claim any item after %d attempts',
            self.CLAIM_MAX_RETRIES,
        )
        return None

    # -------------------------------------------------------------------------
    # QUEUE PROCESSOR
    # -------------------------------------------------------------------------

    def _format_error(self, exc):
        """
        Formatea un error para persistir en error_message.
        Incluye la clase de excepción (con SQLSTATE si aplica) y el
        traceback completo, recortado a un tope razonable para
        facilitar el diagnóstico sin perder detalle.
        """
        exc_type = type(exc).__name__
        if isinstance(exc, psycopg2.Error):
            diagnostic = getattr(exc, 'diag', None)
            sqlstate = (
                getattr(diagnostic, 'sqlstate', None)
                if diagnostic
                else None
            )
            if sqlstate:
                exc_type = '%s (SQLSTATE %s)' % (exc_type, sqlstate)

        combined = '%s: %s' % (exc_type, exc)
        tb = traceback.format_exc()
        if tb and tb.strip():
            combined += '\n%s' % tb

        return combined[:4000]

    @api.model
    def _process_queue(self):
        """
        Process available queue items.

        Corre sobre el CURSOR CON EL QUE SE INVOCA. No commitea el
        trabajo pesado de cada item (ese se hace en un cursor aislado
        con retry + backoff en _process_item_in_new_cursor), pero SÍ
        commitea el claim de cada item: el claim marca 'processing' en
        la transacción de este cursor y, si no se liberara su row lock
        antes de procesar, el cursor aislado que marca 'done' sobre la
        MISMA fila en otra conexión quedaría bloqueado contra ese mismo
        lock (deadlock estructural). Commitear el claim suelta ese lock.

        VARIOS procesadores pueden drenar en paralelo: el claim usa
        FOR UPDATE SKIP LOCKED (ver _claim_next_item), así que cada item
        es reclamado por un solo procesador. No hay advisory lock global
        (eso serializaba incluso productos independientes): la
        serialización por recurso de stock la aplica cada item con
        pg_advisory_xact_lock en _process_item_in_new_cursor.

        CONTRATO:
        - Los items deben estar COMMITTEADOS para ser visibles al
          procesador (ver _claim_next_item).
        - El claim de cada item se commitea AQUÍ (self.env.cr.commit),
          de modo que este cursor NO debe contener trabajo del llamador
          que se quiera preservar sin committear. Todos los llamadores
          (hook post-commit, cron, script de prueba) corren sobre un
          cursor descartable, así que es seguro.
        - El trabajo pesado de cada item queda committeado por su propio
          cursor aislado (_process_item_in_new_cursor).

        Esto garantiza que la cola NUNCA commitea ni revierte la
        transacción de la orden POS: orden + pago + picking + item se
        committean como una sola unidad atómica antes de procesar.
        """
        # VARIOS procesadores pueden drenar la cola en paralelo. El claim
        # usa FOR UPDATE SKIP LOCKED (ver _claim_next_item) así que cada
        # item es reclamado por un solo procesador. NO hay advisory lock
        # global: eso serializaba incluso productos independientes. La
        # serialización por recurso de stock la aplica cada item con
        # pg_advisory_xact_lock (ver _process_item_in_new_cursor).
        while True:
            item_id = self._claim_next_item()
            if item_id is None:
                break
            # El claim marcó el item 'processing' en la TRANSACCIÓN
            # DEL LLAMADOR (self.env.cr); commitear aquí suelta el row
            # lock del claim para que el cursor aislado de
            # _process_item_in_new_cursor pueda marcar 'done' sobre la
            # MISMA fila en otra conexión sin deadlock estructural.
            self.env.cr.commit()
            try:
                self._process_item_in_new_cursor(item_id)
            except PoolError as exc:
                # No se pudo obtener una conexión del pool DEDICADO
                # de la cola para procesar el item. No dejamos el item
                # en 'processing' (eso sería un stuck permanente hasta
                # el reclaim por STALE_PROCESSING_MINUTES): lo
                # revertimos a 'pending' en el cursor del drenador
                # (que YA tiene la conexión y la commitea) para que el
                # CRON lo retome automáticamente, sin intervención
                # manual ni drain manual.
                self.env.cr.execute(
                    """
                        UPDATE pos_inventory_queue
                           SET state = 'pending',
                               start_date = NULL,
                               error_date = now() AT TIME ZONE 'UTC',
                               error_message = %s
                         WHERE id = %s
                    """,
                    (
                        'PoolError: sin conexion del pool de cola; '
                        'revertido a pending para reclaim por cron',
                        item_id,
                    ),
                )
                self.env.cr.commit()
                _logger.warning(
                    'POS Queue: item %s revertido a pending '
                    '(PoolError): %s',
                    item_id,
                    exc,
                )
                break

    # -------------------------------------------------------------------------
    # STOCK RESOURCE LOCKS — per (product, location, company)
    # -------------------------------------------------------------------------

    @staticmethod
    def _stock_lock_key(
        product_id,
        location_id,
        company_id,
        lot_id=0,
        package_id=0,
        owner_id=0,
    ):
        """Clave int8 estable para pg_advisory_xact_lock.

        Cubre el recurso de stock real que _action_done() modificará.
        Se incluyen lot/package/owner en el hash para mayor precisión
        (paraleliza lotes distintos en la misma ubicación); en POS sin
        trazabilidad quedan en 0 y la granularidad es
        (producto, ubicación, compañía), que ya es correcta.
        """
        return int(hashlib.sha1(
            ('pos.inventory.queue.stock.%d.%d.%d.%d.%d.%d' % (
                product_id,
                location_id,
                company_id,
                lot_id,
                package_id,
                owner_id,
            )).encode()
        ).hexdigest()[:15], 16)

    def _stock_lock_keys(self, picking):
        """Claves int8 ordenadas de pg_advisory_xact_lock para un picking.

        Por cada move se bloquea ORIGEN y DESTINO, porque en Odoo 17
        stock_move_line._action_done() escribe stock.quant en ambas
        ubicaciones (stock_move_line.py:408 y :416). Cubrir solo el
        origen permitiría que dos pickings compitan por el quant de
        destino (p.ej. la ubicación de clientes común).

        Odoo ya row-lockea la fila de quant exacta
        (stock_quant.py:1118, FOR NO KEY UPDATE SKIP LOCKED), así que
        este advisory lock es preventivo de contención, no la única
        garantía de integridad.

        Se devuelve ordenado ascendentemente para evitar deadlocks al
        adquirir múltiples locks en procesadores distintos.
        """
        keys = set()
        for move in picking.move_ids:
            company = move.company_id.id
            keys.add(self._stock_lock_key(
                move.product_id.id, move.location_id.id, company))
            keys.add(self._stock_lock_key(
                move.product_id.id, move.location_dest_id.id, company))
        return sorted(keys)

    # -------------------------------------------------------------------------
    # ITEM PROCESSOR — each item gets its own cursor
    # -------------------------------------------------------------------------

    def _process_item_in_new_cursor(self, item_id):
        """
        Open a brand-new database cursor and process the queue item there.

        The new cursor borrows a connection from the QUEUE'S OWN dedicated
        pool (models/queue_connection), NOT from Odoo's shared connection
        pool (db_maxconn). That isolation is what prevents the queue from
        competing with the 100 POS workers for connections and hitting
        PoolError under load.

        Processing is retried inside the cursor with savepoints.
        Between retries an exponential backoff gives competing workers
        time to release their locks on shared stock rows.
        """
        from .queue_connection import (
            queue_get_cursor,
            queue_put_cursor,
        )

        # Calcular las claves de lock en el entorno EXTERNO (self.env.cr),
        # ANTES de abrir new_cr, para no leer stock.quant/moves sobre
        # new_cr antes de adquirir el advisory lock (evita snapshot viejo
        # -> SerializationFailure 40001 bajo contención).
        item = self.browse(item_id)
        if not item.exists():
            return
        picking = item.picking_id
        lock_keys = self._stock_lock_keys(picking) if picking else ()

        new_cr = None
        new_cr = queue_get_cursor(
            self.env.cr.dbname,
            self.env.uid,
            self.env.context,
        )[0]
        new_cr.execute(
            "SET LOCAL lock_timeout = %s",
            ("%d s" % self.LOCK_TIMEOUT_SECONDS,),
        )
        try:
            env = api.Environment(new_cr, self.env.uid, self.env.context)
            item_new = env['pos.inventory.queue'].browse(item_id)

            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    # ADQUIRIR locks por recurso de stock COMO PRIMER
                    # COMANDO sobre new_cr. El snapshot de la transacción
                    # se fija en la primera lectura (el reclaim de abajo),
                    # que ocurre DESPUÉS de tomar el lock, así el worker
                    # concurrente ve el quant ya comprometido por quien
                    # poseía el lock -> 0 SerializationFailure entre
                    # workers de la cola. pg_advisory_xact_lock se libera
                    # solo al commit/rollback; tras new_cr.rollback() por
                    # contención el lock se libera y se re-adquiere aquí.
                    for key in lock_keys:
                        new_cr.execute(
                            "SELECT pg_advisory_xact_lock(%s)",
                            (key,),
                        )

                    # Reclaim seguro: si este item quedó 'processing'
                    # por un crash previo (> STALE_PROCESSING_MINUTES)
                    # pero el picking ya fue procesado por otro worker
                    # mientras tanto, marcarlo 'done' sin re-ejecutar
                    # _action_done() para no duplicar quants.
                    if item_new.picking_id.state == 'done':
                        new_cr.execute(
                            """
                                UPDATE pos_inventory_queue
                                   SET state = 'done',
                                       done_date = now() AT TIME ZONE 'UTC',
                                       retry_count = 0,
                                       error_date = NULL,
                                       error_message = NULL
                                 WHERE id = %s
                            """,
                            (item_id,),
                        )
                        new_cr.commit()

                        _logger.info(
                            'POS Queue: Picking %s ya estaba done, '
                            'item %s marcado done sin reprocesar',
                            item_new.picking_id.name,
                            item_new.name,
                        )
                        return

                    with new_cr.savepoint():
                        item_new.picking_id._action_done()

                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET state = 'done',
                                   done_date = now() AT TIME ZONE 'UTC',
                                   retry_count = 0,
                                   error_date = NULL,
                                   error_message = NULL
                             WHERE id = %s
                        """,
                        (item_id,),
                    )
                    new_cr.commit()

                    _logger.info(
                            'POS Queue: Picking %s processed successfully '
                            '(item %s, attempt %d/%d)',
                            item_new.picking_id.name,
                            item_new.name,
                            attempt,
                            self.MAX_RETRIES,
                        )
                    return

                except (
                    psycopg2.errors.SerializationFailure,
                    psycopg2_errors.LockNotAvailable,
                ) as exc:
                    new_cr.rollback()

                    if attempt >= self.MAX_RETRIES:
                        # Contención (no error de lógica): ceder el item a
                        # 'pending' para que el cron/otro drenador lo
                        # retome, en lugar de failed_permanent. Esto evita
                        # que una contención esperada se vuelva un fallo
                        # permanente y libera la conexión del pool.
                        new_cr.execute(
                            """
                                UPDATE pos_inventory_queue
                                   SET state = 'pending',
                                       start_date = NULL,
                                       retry_count = retry_count + 1,
                                       error_date = now() AT TIME ZONE 'UTC',
                                       error_message = %s
                                 WHERE id = %s
                            """,
                            (
                                self._format_error(exc)
                                + ' | contención: revertido a pending '
                                'para retry por cron',
                                item_id,
                            ),
                        )
                        new_cr.commit()

                        _logger.warning(
                            'POS Queue: Picking %s cede a pending por '
                            'contención (intentos agotados, item %s): %s',
                            item_new.picking_id.name,
                            item_new.name,
                            exc,
                        )
                        return

                    base = 0.05 * (2 ** (attempt - 1))
                    backoff = random.uniform(0, base * 2)
                    time.sleep(backoff)

                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET retry_count = retry_count + 1,
                                   error_date = now() AT TIME ZONE 'UTC',
                                   error_message = %s
                             WHERE id = %s
                        """,
                        (self._format_error(exc), item_id),
                    )
                    new_cr.commit()

                    _logger.warning(
                            'POS Queue: transient conflict for Picking %s '
                            '(item %s, attempt %d/%d, backoff %.2fs): %s',
                            item_new.picking_id.name,
                            item_new.name,
                            attempt,
                        self.MAX_RETRIES,
                        backoff,
                        exc,
                    )

                except Exception as exc:
                    new_cr.rollback()

                    if attempt >= self.MAX_RETRIES:
                        new_cr.execute(
                            """
                                UPDATE pos_inventory_queue
                                   SET state = 'failed_permanent',
                                       retry_count = retry_count + 1,
                                       error_date = now() AT TIME ZONE 'UTC',
                                       error_message = %s
                                 WHERE id = %s
                            """,
                            (self._format_error(exc), item_id),
                        )
                        new_cr.commit()

                        _logger.error(
                            'POS Queue: Picking %s permanently failed '
                            'after %d attempts (item %s): %s',
                            item_new.picking_id.name,
                            attempt,
                            item_new.name,
                            exc,
                        )
                        return

                    base = 0.05 * (2 ** (attempt - 1))
                    backoff = random.uniform(0, base * 2)
                    time.sleep(backoff)

                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET retry_count = retry_count + 1,
                                   error_date = now() AT TIME ZONE 'UTC',
                                   error_message = %s
                             WHERE id = %s
                        """,
                        (self._format_error(exc), item_id),
                    )
                    new_cr.commit()

                    _logger.warning(
                            'POS Queue: Picking %s failed '
                            '(item %s, attempt %d/%d): %s',
                            item_new.picking_id.name,
                            item_new.name,
                            attempt,
                        self.MAX_RETRIES,
                        exc,
                    )

        finally:
            if new_cr is not None:
                queue_put_cursor(new_cr)

    # -------------------------------------------------------------------------
    # ACTIONS
    # -------------------------------------------------------------------------

    def action_retry(self):
        retried = self.browse()
        for record in self:
            if record.state not in ('failed', 'failed_permanent'):
                continue

            record.sudo().write({
                'state': 'pending',
                'retry_count': 0,
                'error_date': False,
                'error_message': False,
            })
            retried |= record

        if retried:
            retried._process_queue()

    # -------------------------------------------------------------------------
    # CRON CLEANUP
    # -------------------------------------------------------------------------

    @api.model
    def _cron_cleanup_done_items(self, days=30):
        """Remove queue items in 'done' state older than *days*."""
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=days)
        old_items = self.search([
            ('state', '=', 'done'),
            ('done_date', '<', cutoff),
        ])
        if old_items:
            old_items.unlink()
            _logger.info(
                'POS Queue: cleaned up %d done items older than %d days',
                len(old_items),
                days,
            )
