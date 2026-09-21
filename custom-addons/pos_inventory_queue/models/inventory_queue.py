import hashlib
import logging
import random
import time
import traceback

import psycopg2
from psycopg2 import errors as psycopg2_errors
from psycopg2.pool import PoolError

from odoo import api, fields, models, _, SUPERUSER_ID
from odoo.tools.misc import str2bool

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
        (
            'sequence_non_negative',
            'CHECK(sequence >= 0)',
            'Sequence must be non-negative.',
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
        index=True,
    )

    error_date = fields.Datetime(
        string='Error Date',
        readonly=True,
        index=True,
    )

    error_message = fields.Text(
        string='Error Message',
        readonly=True,
    )

    next_retry_date = fields.Datetime(
        string='Next Retry',
        readonly=True,
        help='Cuando el cron podra re-clamar un item en estado "failed" '
             '(backoff diferido por ciclos: now + 2^n minutos). Vacio si '
             'aplica ya o si el item esta en otro estado.',
    )

    active = fields.Boolean(
        string='Active',
        default=True,
        index=True,
        help='Desarchivado por defecto. Los items se archivan en '
             'lugar de eliminarse para conservar el historial.',
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
    # SCHEMA
    # -------------------------------------------------------------------------

    def init(self):
        """Indice parcial para el claim del cron (P1-2).

        El claim (_claim_next_item) busca los items ACTIVOS ordenados por
        (sequence, id). Con el tiempo la tabla acumula miles de items
        'done' (p. ej. ~740k historicos en prod); sin indice el SELECT
        escanea todo y el 'FOR UPDATE SKIP LOCKED' se encarece cada minuto.

        Un indice PARCIAL que solo cubre los estados reclamables mantiene
        el indice pequeno e independiente del tamano del historico, y el
        orden (sequence, id) coincide con el ORDER BY del claim (menos el
        CASE pending-first, que resuelve barato sobre este subconjunto).

        init() corre en el install y en cada -u; CREATE INDEX IF NOT EXISTS
        lo hace idempotente.
        """
        self.env.cr.execute(
            """
            CREATE INDEX IF NOT EXISTS pos_inventory_queue_claim_idx
                ON pos_inventory_queue (sequence, id)
             WHERE state IN ('pending', 'failed', 'processing')
               AND active = True
            """
        )

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
                                 state = 'pending'
                                 OR (
                                     state = 'failed'
                                     AND retry_count < %s
                                     AND (
                                         next_retry_date IS NULL
                                         OR next_retry_date
                                            <= (now() AT TIME ZONE 'UTC')
                                     )
                                 )
                                 OR (
                                     state = 'processing'
                                     AND start_date < (
                                         now() AT TIME ZONE 'UTC'
                                         - %s::interval
                                     )
                                 )
                               )
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
                        self.MAX_RETRIES,
                        "%d minutes" % self.STALE_PROCESSING_MINUTES,
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
    # SESSION CLOSE DRAIN
    # -------------------------------------------------------------------------

    @api.model
    def _process_session_items(self, session):
        """Procesar en línea los ítems pendientes de una sesión antes del cierre.

        Se invoca desde pos.session._validate_session (guarda de cierre,
        P0-7). Cada ítem se procesa en su propio cursor aislado
        (_process_item_in_new_cursor), que commitea/revierte de forma
        independiente: NO se contamina ni se commitea antes de tiempo la
        transacción del cierre de sesión que corre en el cursor HTTP.

        Procesa pending / processing (stale) / failed (reintentables). NO
        reintenta failed_permanent: esos exigen intervención manual y
        deben bloquear el cierre.

        Returns el recordset de ítems que siguen sin 'done' tras el
        drenaje (para que la guarda decida si bloquea).
        """
        remaining = self.sudo().search([
            ('pos_order_id.session_id', '=', session.id),
            ('state', '!=', 'done'),
        ])
        for item in remaining:
            if item.state == 'failed_permanent':
                continue
            if item.picking_id.state == 'done':
                continue
            try:
                self._process_item_in_new_cursor(item.id)
            except Exception:
                _logger.exception(
                    'POS Queue: session close inline drain failed item %s',
                    item.id,
                )
        return self.sudo().search([
            ('pos_order_id.session_id', '=', session.id),
            ('state', '!=', 'done'),
        ])

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

        return combined[:3997] + '...' if len(combined) > 4000 else combined

    @api.model
    def _trigger_processing(self):
        """Pide al worker de cron que drene la cola apenas se commitee.

        Reutilizado por el encolado (stock_picking) y por el presupuesto de
        tiempo del propio _process_queue. Best-effort: si falla, el cron
        periodico (cada 1 min) retoma igual.
        """
        try:
            self.env.ref(
                'pos_inventory_queue.ir_cron_process_pending_pos_inventory_queue'
            )._trigger()
        except Exception:
            _logger.exception(
                'POS Queue: no se pudo disparar el cron via _trigger(); '
                'el cron periodico drenara la cola'
            )

    @api.model
    def _notify_permanent_failure(self, item_id):
        """Alerta al responsable de inventario cuando un item pasa a
        'failed_permanent' (P1-3).

        Best-effort y totalmente aislado: usa SU PROPIO cursor del registry
        (el item ya quedo commiteado en failed_permanent por el procesador),
        y envuelve TODO en try/except para que un fallo de notificacion jamas
        afecte al drenaje ni al worker de cron.

        Mecanismo: mail.activity a los gestores de inventario de la compania
        del picking. Se evita acoplar el modulo a 'telegram_alerts' (modulo
        de terceros no presente en todos los entornos) para no romper
        installs en staging/dev; si se quiere Telegram, se engancha aqui
        mismo de forma guardada (if 'telegram.xxx' in self.env).
        """
        try:
            if 'mail.activity' not in self.env:
                return
            alert_cr = self.env.registry.cursor()
            try:
                alert_env = api.Environment(alert_cr, SUPERUSER_ID, {})
                item = alert_env['pos.inventory.queue'].browse(item_id)
                if not item.exists() or item.state != 'failed_permanent':
                    return
                activity_type = alert_env.ref(
                    'mail.mail_activity_data_todo',
                    raise_if_not_found=False,
                )
                if not activity_type:
                    return
                company = item.picking_id.company_id or self.env.company
                stock_mgr = alert_env.ref('stock.group_stock_manager')
                users = alert_env['res.users'].sudo().search([
                    ('groups_id', 'in', stock_mgr.id),
                    ('company_ids', 'in', company.id),
                    ('active', '=', True),
                ])
                if not users:
                    return
                label = item.picking_id.name or item.name
                alert_env['mail.activity'].sudo().create([{
                    'res_model_id': alert_env['ir.model']._get_id(
                        'pos.inventory.queue'),
                    'res_id': item.id,
                    'user_id': user.id,
                    'activity_type_id': activity_type.id,
                    'summary': 'POS Inventory Queue: fallo permanente en %s'
                               % label,
                    'note': _(
                        'El picking %(picking)s de la cola de inventario '
                        'del POS quedo en FAILED PERMANENT tras %(count)s '
                        'ciclos. Requiere revision manual en Punto de '
                        'Venta > Configuracion > Cola de Inventario.\n'
                        'Ultimo error: %(err)s',
                        picking=label,
                        count=item.retry_count,
                        err=(item.error_message or '')[:1500],
                    ),
                } for user in users])
                alert_cr.commit()
            except Exception:
                alert_cr.rollback()
                raise
            finally:
                alert_cr.close()
        except Exception:
            _logger.exception(
                'POS Queue: PERMANENT no se pudo crear la alerta de '
                'actividad para el item %s', item_id,
            )

    @api.model
    def _process_queue(self, time_budget=240):
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
        start = time.monotonic()
        # (P1-4) Contadores por pasada de drenaje para monitoreo con
        # analyze_logs.py: una linea 'summary' estable por ejecucion.
        counts = {'done': 0, 'contention': 0, 'failed': 0, 'permanent': 0}

        def _summary_log(reason):
            _logger.info(
                'POS Queue: summary reason=%s elapsed=%.1fs done=%d '
                'contention=%d failed=%d permanent=%d',
                reason,
                time.monotonic() - start,
                counts['done'],
                counts['contention'],
                counts['failed'],
                counts['permanent'],
            )

        while True:
            # (P1-1) Presupuesto de tiempo: Odoo.sh mata el worker de cron
            # a los ~330s. Si el drenaje superase ese limite, el cron
            # moriria dejando items en 'processing' huerfanos (reclamables
            # solo tras STALE_PROCESSING_MINUTES). Salimos LIMPIOS antes y
            # re-disparamos el trigger para continuar en la siguiente
            # pasada, sin perder el trabajo ya hecho. time_budget=0 lo
            # desactiva (scripts/tests que quieren drenar todo).
            if time_budget and (time.monotonic() - start) >= time_budget:
                _logger.info(
                    'POS Queue: presupuesto de %ds agotado; re-disparando '
                    'el cron para continuar drenando la cola', time_budget,
                )
                self.env.cr.commit()
                _summary_log('budget')
                self._trigger_processing()
                return
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
                status = self._process_item_in_new_cursor(item_id)
                if status in counts:
                    counts[status] += 1
            except PoolError as exc:
                # No se pudo obtener una conexion del pool de Odoo para
                # abrir el cursor aislado (pool agotado bajo carga). No
                # dejamos el item en 'processing' (eso seria un stuck
                # permanente hasta el reclaim por
                # STALE_PROCESSING_MINUTES): lo revertimos a 'pending' en
                # el cursor del drenador (que YA tiene la conexion y la
                # commitea) para que el CRON lo retome automaticamente,
                # sin intervencion manual ni drain manual.
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
                        'PoolError: pool de conexiones agotado; '
                        'revertido a pending para reclaim por cron',
                        item_id,
                    ),
                )
                self.env.cr.commit()
                counts['contention'] += 1
                _logger.warning(
                    'POS Queue: contention item %s revertido a pending '
                    '(PoolError): %s',
                    item_id,
                    exc,
                )
                break

        _summary_log('empty')

    # -------------------------------------------------------------------------
    # STOCK RESOURCE LOCKS — per (product, company)
    # -------------------------------------------------------------------------

    @staticmethod
    def _stock_lock_key(
        product_id,
        company_id,
    ):
        """Clave int8 estable para pg_advisory_xact_lock.

        Bloquea por (producto, compañía). NO incluye ubicación porque
        stock_valuation_layer es por producto+compañía, sin ubicación:
        el vacuum AVCO de un producto vendido a la vez en dos tiendas
        distintas reescribe las MISMAS capas de valoración, aunque sus
        quants estén en ubicaciones diferentes.
        """
        return int(hashlib.sha1(
            ('pos.inventory.queue.stock.%d.%d' % (
                product_id,
                company_id,
            )).encode()
        ).hexdigest()[:15], 16)

    def _stock_lock_keys(self, picking):
        """Claves int8 ordenadas de pg_advisory_xact_lock para un picking.

        Una sola clave por move: (producto, compañía). Se omite la
        ubicación porque stock_valuation_layer no la tiene; el recurso
        compartido real es la capa de valoración, no el quant (que Odoo
        ya row-lockea en stock_quant.py:1118 con FOR NO KEY UPDATE SKIP
        LOCKED). Este advisory lock es preventivo de contención del
        vacuum, no la única garantía de integridad.

        Se devuelve ordenado ascendentemente para evitar deadlocks al
        adquirir múltiples locks en procesadores distintos.
        """
        keys = set()
        for move in picking.move_ids:
            keys.add(self._stock_lock_key(
                move.product_id.id, move.company_id.id))
        return sorted(keys)

    # -------------------------------------------------------------------------
    # ITEM PROCESSOR — each item gets its own cursor
    # -------------------------------------------------------------------------

    def _process_item_in_new_cursor(self, item_id):
        """
        Open a brand-new database cursor and process the queue item there.

        The new cursor comes from Odoo's normal registry pool
        (`self.env.registry.cursor()`). El drenaje corre en el worker de
        cron (invocado via ir.cron._trigger), donde solo hay esta petición
        por worker: no compite con los requests HTTP del POS, así que ya
        no hace falta un pool de conexiones dedicado.

        Processing is retried inside the cursor with savepoints.
        Between retries an exponential backoff gives competing workers
        time to release their locks on shared stock rows.

        Retorna un codigo de resultado para los contadores del drenador
        (P1-4): 'done', 'contention', 'failed' (retry diferido) o
        'permanent'.
        """
        # Calcular las claves de lock en el entorno EXTERNO (self.env.cr),
        # ANTES de abrir new_cr, para no leer stock.quant/moves sobre
        # new_cr antes de adquirir el advisory lock (evita snapshot viejo
        # -> SerializationFailure 40001 bajo contención).
        item = self.browse(item_id)
        if not item.exists():
            return
        picking = item.picking_id
        lock_keys = self._stock_lock_keys(picking) if picking else ()

        new_cr = self.env.registry.cursor()
        try:
            env = api.Environment(new_cr, SUPERUSER_ID, {})
            item_new = env['pos.inventory.queue'].browse(item_id)

            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    # (P0-9) SET LOCAL es TRANSACTION-scoped: tras cada
                    # new_cr.rollback() por contencion la transaccion
                    # termina y el timeout se pierde, asi que se repite al
                    # inicio de CADA intento (no solo del primero). Sin el,
                    # los locks de fila de stock.quant / stock_valuation_layer
                    # de un intento posterior podrian esperar sin limite a
                    # un 'idle in transaction' ajeno en vez de saltar
                    # lock_not_available (55P03) y caer en backoff.
                    new_cr.execute(
                        "SET LOCAL lock_timeout = %s",
                        ("%d s" % self.LOCK_TIMEOUT_SECONDS,),
                    )
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
                            'POS Queue: processed Picking %s ya estaba '
                            'done, item %s marcado done sin reprocesar',
                            item_new.picking_id.name,
                            item_new.name,
                        )
                        return 'done'

                    with new_cr.savepoint():
                        item_new.picking_id.with_company(
                            item_new.picking_id.company_id,
                        )._action_done()

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
                            'POS Queue: processed Picking %s '
                            '(item %s, attempt %d/%d)',
                            item_new.picking_id.name,
                            item_new.name,
                            attempt,
                            self.MAX_RETRIES,
                        )
                    return 'done'

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
                            'POS Queue: contention Picking %s cede a '
                            'pending por contencion (intentos agotados, '
                            'item %s): %s',
                            item_new.picking_id.name,
                            item_new.name,
                            exc,
                        )
                        return 'contention'

                    base = 0.05 * (2 ** (attempt - 1))
                    backoff = random.uniform(0, base * 2)
                    time.sleep(backoff)

                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET error_date = now() AT TIME ZONE 'UTC',
                                   error_message = %s
                             WHERE id = %s
                        """,
                        (self._format_error(exc), item_id),
                    )
                    new_cr.commit()

                    _logger.warning(
                            'POS Queue: contention transient conflict for '
                            'Picking %s '
                            '(item %s, attempt %d/%d, backoff %.2fs): %s',
                            item_new.picking_id.name,
                            item_new.name,
                            attempt,
                        self.MAX_RETRIES,
                        backoff,
                        exc,
                    )

                except (
                    psycopg2.OperationalError,
                    psycopg2.InterfaceError,
                ) as exc:
                    # Error de CONEXION (caida de BD, reset, etc.). new_cr
                    # ya no sirve: no se puede UPDATE ni sobre este cursor,
                    # ni reintentar aqui (todos los reintentos fallarian
                    # sobre la misma conexion muerta). No lo marcamos
                    # failed_permanent (no es un error de logica). Se deja
                    # en 'processing' y el CRON lo reclamar como item
                    # stale tras STALE_PROCESSING_MINUTES (ver
                    # _claim_next_item). Nada se propaga (P0-5).
                    try:
                        new_cr.rollback()
                    except Exception:
                        pass
                    _logger.warning(
                        'POS Queue: contention Picking %s fallo por error '
                        'de conexion '
                        '(item %s); queda en processing para reclaim del '
                        'cron (stale > %d min): %s',
                        item_new.picking_id.name if item_new.exists() else item_id,
                        item_new.name if item_new.exists() else item_id,
                        self.STALE_PROCESSING_MINUTES,
                        exc,
                    )
                    return 'contention'

                except Exception as exc:
                    new_cr.rollback()

                    # Error NO transitorio (logica: producto con lote,
                    # constraint, UserError del picking, etc.). Reintentar
                    # en el mismo ciclo no sirve: el error se repite igual.
                    # (D4) se marca 'failed' con backoff diferido
                    # (next_retry_date = now + 2^n minutos) y el CRON lo
                    # re-clama cuando vence, dando tiempo a que se corrija
                    # la causa raiz. 'failed_permanent' solo al agotar
                    # MAX_RETRIES ciclos (~1 h acumulada).
                    cycles = item.retry_count + 1
                    permanent = cycles >= self.MAX_RETRIES
                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET state = %s,
                                   retry_count = %s,
                                   error_date = now() AT TIME ZONE 'UTC',
                                   error_message = %s,
                                   next_retry_date = CASE
                                       WHEN %s THEN NULL
                                       ELSE (now() AT TIME ZONE 'UTC')
                                            + (%s * interval '1 minute')
                                   END
                             WHERE id = %s
                        """,
                        (
                            'failed_permanent' if permanent else 'failed',
                            cycles,
                            self._format_error(exc),
                            str(permanent),
                            int(2 ** cycles),
                            item_id,
                        ),
                    )
                    new_cr.commit()

                    if permanent:
                        _logger.error(
                            'POS Queue: PERMANENT Picking %s fallo '
                            'definitivo tras %d ciclos (item %s): %s',
                            item_new.picking_id.name,
                            cycles,
                            item_new.name,
                            exc,
                        )
                        # (P1-3) Alerta best-effort al responsable de
                        # inventario. Usa su propio cursor; nunca propaga.
                        self._notify_permanent_failure(item_id)
                    else:
                        _logger.warning(
                            'POS Queue: failed Picking %s (item %s, '
                            'ciclo %d/%d); reintento en %d min: %s',
                            item_new.picking_id.name,
                            item_new.name,
                            cycles,
                            self.MAX_RETRIES,
                            2 ** cycles,
                            exc,
                        )
                    return 'permanent' if permanent else 'failed'

        finally:
            new_cr.close()

    # -------------------------------------------------------------------------
    # HELPER: global on/off switch (NOT per POS)
    # -------------------------------------------------------------------------

    @api.model
    def _is_queue_enabled(self):
        """
        Interruptor GLOBAL de la cola de inventario del POS.

        Se guarda en ir.config_parameter ('pos_inventory_queue.enabled'),
        no en pos.config: la cola es un mecanismo global (cron + advisory
        locks por recurso de stock), asi que el toggle tampoco es por
        terminal. Default activado si el parametro no existe.

        IMPORTANTE: este switch solo decide si una orden encolada NUEVA usa
        la cola o se valida de forma sincronizada (comportamiento nativo).
        El procesador (_claim_next_item/_process_queue) SIEMPRE drena, para
        que al apagar el modulo lo ya pendiente termine y no quede un
        picking sin validar en el limbo.
        """
        return str2bool(
            self.env['ir.config_parameter'].sudo().get_param(
                'pos_inventory_queue.enabled',
                default='True',
            ),
            default=True,
        )

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
                'next_retry_date': False,
            })
            retried |= record

        if retried:
            # (P1-5) NO drenar en linea desde el request del usuario:
            # _process_queue() hace cr.commit() por item y podria bloquear
            # el navegador o commitear trabajo del request. Se resetea a
            # 'pending' y se dispara el cron para que el worker de cron
            # procese, igual que el resto del flujo.
            self._trigger_processing()
        return True

    def action_trigger_processing(self):
        """Boton 'Procesar ahora': pide al cron drenar ya (managers).

        No procesa en el request; solo registra el trigger del cron, que
        drena en su propio worker. Devuelve una notificacion al cliente.
        """
        self._trigger_processing()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Cola de Inventario'),
                'message': _(
                    'Procesamiento solicitado. El planificador de tareas '
                    'drenara la cola en unos segundos.'),
                'type': 'success',
                'sticky': False,
            },
        }

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
