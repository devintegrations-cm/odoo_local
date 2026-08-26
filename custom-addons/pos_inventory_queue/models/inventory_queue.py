import logging
import time
import psycopg2

from odoo import api, fields, models
from odoo.sql_db import db_connect

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
            created |= super().create(to_create)

        return created

    # -------------------------------------------------------------------------
    # QUEUE CLAIM — raw SQL with SerializationFailure resilience
    # -------------------------------------------------------------------------

    @api.model
    def _claim_next_item(self):
        """
        Atomically claim one pending queue item using FOR UPDATE SKIP LOCKED.

        Returns the ID of the claimed item, or None.

        The SELECT FOR UPDATE SKIP LOCKED itself can fail with
        SerializationFailure when:
          - cr.flush() triggers pending ORM writes that collide with
            concurrent transactions.
          - Our REPEATABLE READ snapshot is stale after cr.commit().

        Both cases are transient: rollback gives us a fresh snapshot
        and the next attempt succeeds.
        """
        self.env.cr.flush()

        for attempt in range(self.CLAIM_MAX_RETRIES):
            try:
                self.env.cr.execute(
                    """
                        SELECT id
                          FROM pos_inventory_queue
                         WHERE state IN ('pending', 'failed')
                           AND retry_count < %s
                         ORDER BY
                            CASE WHEN state = 'pending' THEN 0 ELSE 1 END,
                            sequence, id
                         FOR UPDATE SKIP LOCKED
                         LIMIT 1
                    """,
                    (self.MAX_RETRIES,),
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

                self.env.cr.commit()

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

    @api.model
    def _process_queue(self):
        """
        Process all available queue items.

        Each item is claimed atomically and processed in its own database
        cursor (separate PostgreSQL transaction). A SerializationFailure
        in one item never poisons the transaction of another item.
        """
        while True:
            item_id = self._claim_next_item()
            if item_id is None:
                break
            self._process_item_in_new_cursor(item_id)

    # -------------------------------------------------------------------------
    # ITEM PROCESSOR — each item gets its own cursor
    # -------------------------------------------------------------------------

    def _process_item_in_new_cursor(self, item_id):
        """
        Open a brand-new database cursor and process the queue item there.

        The new cursor borrows a connection from Odoo's connection pool but
        does NOT go through Registry.cursor(), so it never acquires the
        registry lock.

        Processing is retried inside the cursor with savepoints.
        Between retries an exponential backoff gives competing workers
        time to release their locks on shared stock rows.
        """
        new_cr = db_connect(self.env.cr.dbname).cursor()
        try:
            env = api.Environment(new_cr, self.env.uid, self.env.context)
            item = env['pos.inventory.queue'].browse(item_id)

            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    with new_cr.savepoint():
                        item.picking_id._action_done()

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
                        item.picking_id.name,
                        item.name,
                        attempt,
                        self.MAX_RETRIES,
                    )
                    return

                except psycopg2.errors.SerializationFailure as exc:
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
                            (str(exc)[:2000], item_id),
                        )
                        new_cr.commit()

                        _logger.error(
                            'POS Queue: Picking %s permanently failed '
                            'after %d attempts (item %s)',
                            item.picking_id.name,
                            attempt,
                            item.name,
                        )
                        return

                    backoff = 0.05 * (2 ** (attempt - 1))
                    time.sleep(backoff)

                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET retry_count = retry_count + 1,
                                   error_date = now() AT TIME ZONE 'UTC',
                                   error_message = %s
                             WHERE id = %s
                        """,
                        (str(exc)[:2000], item_id),
                    )
                    new_cr.commit()

                    _logger.warning(
                        'POS Queue: transient conflict for Picking %s '
                        '(item %s, attempt %d/%d, backoff %.2fs): %s',
                        item.picking_id.name,
                        item.name,
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
                            (str(exc)[:2000], item_id),
                        )
                        new_cr.commit()

                        _logger.error(
                            'POS Queue: Picking %s permanently failed '
                            'after %d attempts (item %s): %s',
                            item.picking_id.name,
                            attempt,
                            item.name,
                            exc,
                        )
                        return

                    backoff = 0.05 * (2 ** (attempt - 1))
                    time.sleep(backoff)

                    new_cr.execute(
                        """
                            UPDATE pos_inventory_queue
                               SET retry_count = retry_count + 1,
                                   error_date = now() AT TIME ZONE 'UTC',
                                   error_message = %s
                             WHERE id = %s
                        """,
                        (str(exc)[:2000], item_id),
                    )
                    new_cr.commit()

                    _logger.warning(
                        'POS Queue: Picking %s failed '
                        '(item %s, attempt %d/%d): %s',
                        item.picking_id.name,
                        item.name,
                        attempt,
                        self.MAX_RETRIES,
                        exc,
                    )

        finally:
            new_cr.close()

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
