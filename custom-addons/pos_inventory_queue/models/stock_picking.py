import logging

from odoo import api, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_is_zero

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    @api.model
    def _create_picking_from_pos_order_lines(self, location_dest_id, lines, picking_type, partner=False):
        pickings = self.env['stock.picking']
        stockable_lines = lines.filtered(
            lambda l: l.product_id.type in ['product', 'consu']
            and not float_is_zero(l.qty, precision_rounding=l.product_id.uom_id.rounding)
        )
        if not stockable_lines:
            return pickings

        positive_lines = stockable_lines.filtered(lambda l: l.qty > 0)
        negative_lines = stockable_lines - positive_lines

        if positive_lines:
            location_id = picking_type.default_location_src_id.id
            positive_picking = self.env['stock.picking'].create(
                self._prepare_picking_vals(partner, picking_type, location_id, location_dest_id)
            )
            positive_picking._create_move_from_pos_order_lines(positive_lines)
            pickings |= positive_picking

        if negative_lines:
            if picking_type.return_picking_type_id:
                return_picking_type = picking_type.return_picking_type_id
                return_location_id = return_picking_type.default_location_dest_id.id
            else:
                return_picking_type = picking_type
                return_location_id = picking_type.default_location_src_id.id

            negative_picking = self.env['stock.picking'].create(
                self._prepare_picking_vals(partner, return_picking_type, location_dest_id, return_location_id)
            )
            negative_picking._create_move_from_pos_order_lines(negative_lines)
            pickings |= negative_picking

        # El contexto marca "picking POS en tiempo real" (lo pone
        # pos_order._create_order_picking). La cola solo interviene si ADEMAS
        # el interruptor GLOBAL esta activado. Apagado -> rama else ->
        # _action_done() sincronizado = comportamiento nativo, como si el
        # modulo no existiera (nada nuevo entra a la cola).
        if self.env.context.get('pos_inventory_queue') and \
                self.env['pos.inventory.queue']._is_queue_enabled():
            Queue = self.env['pos.inventory.queue']
            for picking in pickings:
                Queue.create({'picking_id': picking.id, 'state': 'pending'})
            self._trigger_queue_processing_after_commit()
        else:
            for picking in pickings:
                try:
                    with self.env.cr.savepoint():
                        picking._action_done()
                except (UserError, ValidationError):
                    pass

        return pickings

    @api.model
    def _trigger_queue_processing_after_commit(self):
        """
        Dispara el procesamiento de la cola después de que la
        transacción actual (orden + pago + picking + item) sea
        committeada.

        Un item recién encolado NO es visible para el procesador hasta
        que su transacción termina. Por eso el procesamiento se registra
        como hook post-commit.

        En Odoo 17 el dispatcher post-commit cuelga del Cursor
        (cr.postcommit), no de cr.transaction. Si no hubiera dispatcher
        (scripts crudos), se cae a transaction.add; si tampoco, quien
        ejecuta el script llama _process_queue() explícito tras su commit.
        """
        cr = self.env.cr
        postcommit = getattr(cr, 'postcommit', None)
        transaction = getattr(cr, 'transaction', None)

        db_name = cr.dbname
        uid = self.env.uid
        context = self.env.context

        def _after_commit():
            from .queue_connection import (
                queue_get_cursor,
                queue_put_cursor,
            )
            from psycopg2.pool import PoolError

            # El hook es best-effort: si el pool dedicado de la cola
            # está lleno, se omite y el CRON drena la cola. Así 100
            # órdenes concurrentes no compiten agresivamente por
            # conexiones del pool de la cola.
            try:
                cr2, env2 = queue_get_cursor(db_name, uid, context)
            except PoolError:
                _logger.debug(
                    'POS Queue: hook post-commit omitido '
                    '(pool de cola lleno); el cron drenará la cola'
                )
                return

            try:
                env2['pos.inventory.queue']._process_queue()
                # _process_queue() ya commitea el claim de cada item
                # (para liberar su row lock); este commit cierra el
                # cursor del hook, que es descartable.
                cr2.commit()
            finally:
                queue_put_cursor(cr2)

        if postcommit is not None:
            postcommit.add(_after_commit)
        elif transaction is not None:
            transaction.add(_after_commit)
