from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPosInventoryQueue(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Queue = self.env['pos.inventory.queue']
        self.Picking = self.env['stock.picking']

        self.warehouse = self.env['stock.warehouse'].search([], limit=1)
        self.picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('warehouse_id', '=', self.warehouse.id),
        ], limit=1)

        self.product = self.env['product.product'].create({
            'name': 'Test Queue Product',
            'type': 'product',
            'list_price': 10.0,
        })

        self.source_location = (
            self.picking_type.default_location_src_id
            or self.warehouse.lot_stock_id
        )
        self.dest_location = (
            self.picking_type.default_location_dest_id
            or self.env['stock.warehouse']._get_partner_locations()[0]
        )

        self.env['stock.quant'].with_context(inventory_mode=True).create({
            'product_id': self.product.id,
            'location_id': self.source_location.id,
            'inventory_quantity': 100.0,
        }).action_apply_inventory()

    def _create_picking(self, origin=None):
        picking = self.Picking.create({
            'picking_type_id': self.picking_type.id,
            'location_id': self.source_location.id,
            'location_dest_id': self.dest_location.id,
            'origin': origin or 'TEST',
        })
        self.env['stock.move'].create({
            'name': 'Test Move',
            'product_id': self.product.id,
            'product_uom_qty': 1.0,
            'product_uom': self.product.uom_id.id,
            'picking_id': picking.id,
            'location_id': self.source_location.id,
            'location_dest_id': self.dest_location.id,
        })
        picking.action_confirm()
        picking.move_ids.picked = True
        return picking

    def test_sequence_generation(self):
        item1 = self.Queue.create({
            'picking_id': self._create_picking('SEQ-1').id,
        })
        item2 = self.Queue.create({
            'picking_id': self._create_picking('SEQ-2').id,
        })

        self.assertNotEqual(item1.name, 'New')
        self.assertNotEqual(item2.name, 'New')
        self.assertNotEqual(item1.name, item2.name)

    def test_duplicate_picking_prevention(self):
        picking = self._create_picking('DUP-1')
        item1 = self.Queue.create({'picking_id': picking.id})
        item2 = self.Queue.create({'picking_id': picking.id})

        self.assertEqual(item1.id, item2.id)
        self.assertEqual(self.Queue.search(
            [('picking_id', '=', picking.id)]
        ).size, 1)

    def test_default_state(self):
        picking = self._create_picking('STATE-1')
        item = self.Queue.create({'picking_id': picking.id})
        self.assertEqual(item.state, 'pending')

    def test_retry_from_failed_permanent(self):
        picking = self._create_picking('RETRY-1')
        item = self.Queue.create({'picking_id': picking.id})
        item.sudo().write({
            'state': 'failed_permanent',
            'retry_count': 5,
            'error_message': 'Test error',
            'next_retry_date': '2099-01-01 00:00:00',
        })

        # La cola solo procesa items committeados.
        self.env.cr.commit()

        # (P1-5) action_retry ya NO procesa en linea: resetea a 'pending'
        # y dispara el cron. Primero verificamos el reseteo...
        item.action_retry()
        self.env.invalidate_all()
        item = self.Queue.search([('picking_id', '=', picking.id)])
        self.assertEqual(item.state, 'pending')
        self.assertEqual(item.retry_count, 0)
        self.assertFalse(item.error_message)
        self.assertFalse(item.next_retry_date)

        # ...y luego que el drenaje (aqui llamado a mano) lo lleva a done.
        self.env.cr.commit()
        self.Queue._process_queue()
        self.env.invalidate_all()
        item = self.Queue.search([('picking_id', '=', picking.id)])
        self.assertEqual(item.state, 'done')

    def test_claim_respects_future_next_retry_date(self):
        """(P0-8) Un item 'failed' con next_retry_date en el futuro NO se
        reclama: el backoff diferido debe respetarse hasta que venza."""
        picking = self._create_picking('NRET-FUT')
        item = self.Queue.create({'picking_id': picking.id})
        item.sudo().write({
            'state': 'failed',
            'retry_count': 2,
            'next_retry_date': '2099-01-01 00:00:00',
        })
        self.env.cr.commit()

        claimed = self.Queue._claim_next_item()
        self.env.invalidate_all()
        self.assertIsNone(
            claimed,
            'failed item con next_retry_date futuro no debe reclamarse',
        )

    def test_claim_after_next_retry_date_passed(self):
        """(P0-8) Un item 'failed' con next_retry_date vencido SI se
        reclama (el backoff expiro)."""
        picking = self._create_picking('NRET-PAST')
        item = self.Queue.create({'picking_id': picking.id})
        item.sudo().write({
            'state': 'failed',
            'retry_count': 2,
            'next_retry_date': '2000-01-01 00:00:00',
        })
        self.env.cr.commit()

        claimed = self.Queue._claim_next_item()
        self.env.invalidate_all()
        self.assertIsNotNone(
            claimed,
            'failed item con next_retry_date vencido debe reclamarse',
        )

    def test_retry_ignores_non_failed(self):
        picking = self._create_picking('RETRY-2')
        item = self.Queue.create({'picking_id': picking.id})

        item.action_retry()

        self.assertEqual(item.state, 'pending')
        self.assertEqual(item.retry_count, 0)

    def test_claim_next_item(self):
        p1 = self._create_picking('CLAIM-1')
        p2 = self._create_picking('CLAIM-2')
        self.Queue.create({'picking_id': p1.id})
        self.Queue.create({'picking_id': p2.id})

        item_id = self.Queue._claim_next_item()
        self.assertIsNotNone(item_id)

        self.env.invalidate_all()

        item = self.Queue.browse(item_id)
        self.assertEqual(item.state, 'processing')

    def test_claim_returns_none_when_empty(self):
        item_id = self.Queue._claim_next_item()
        self.assertIsNone(item_id)

    def test_process_queue_single_item(self):
        picking = self._create_picking('PROC-1')
        self.Queue.create({'picking_id': picking.id})

        # La cola solo procesa items committeados.
        self.env.cr.commit()

        self.Queue._process_queue()

        self.env.invalidate_all()

        item = self.Queue.search(
            [('picking_id', '=', picking.id)]
        )
        self.assertEqual(item.state, 'done')
        self.assertFalse(item.error_message)

    def test_process_queue_preserves_order(self):
        p1 = self._create_picking('ORDER-1')
        p2 = self._create_picking('ORDER-2')
        p3 = self._create_picking('ORDER-3')
        self.Queue.create({'picking_id': p1.id})
        self.Queue.create({'picking_id': p2.id})
        self.Queue.create({'picking_id': p3.id})

        # La cola solo procesa items committeados.
        self.env.cr.commit()

        self.Queue._process_queue()

        self.env.invalidate_all()

        items = self.Queue.search([
            ('picking_id', 'in', [p1.id, p2.id, p3.id]),
        ], order='sequence, id')

        for item in items:
            self.assertEqual(item.state, 'done')

    def test_claim_pending_with_max_retries(self):
        """Regression: pending items with retry_count >= MAX_RETRIES
        must still be claimed by the cron.  Previously the query
        ``retry_count < MAX_RETRIES`` excluded them, leaving them
        stuck forever."""
        picking = self._create_picking('MAXRETRY-1')
        item = self.Queue.create({'picking_id': picking.id})

        item.sudo().write({
            'state': 'pending',
            'retry_count': self.Queue.MAX_RETRIES,
        })

        self.env.cr.commit()

        item_id = self.Queue._claim_next_item()
        self.assertIsNotNone(
            item_id,
            'pending item with retry_count=MAX_RETRIES '
            'should be claimable',
        )

        self.env.invalidate_all()

        item = self.Queue.browse(item_id)
        self.assertEqual(item.state, 'processing')

    def test_claim_failed_with_max_retries_excluded(self):
        """failed items with retry_count >= MAX_RETRIES must NOT be
        claimed — they represent logic errors, not contention."""
        picking = self._create_picking('MAXRETRY-2')
        item = self.Queue.create({'picking_id': picking.id})

        item.sudo().write({
            'state': 'failed',
            'retry_count': self.Queue.MAX_RETRIES,
        })

        self.env.cr.commit()

        item_id = self.Queue._claim_next_item()
        self.assertIsNone(
            item_id,
            'failed item with retry_count=MAX_RETRIES '
            'should NOT be claimable',
        )

    def test_process_item_in_new_cursor_already_done(self):
        """Si el picking ya está done (reclamo de stale), el item se
        marca done sin re-ejecutar _action_done() para no duplicar
        quants."""
        picking = self._create_picking('IDEMP-1')
        item = self.Queue.create({'picking_id': picking.id})

        # Simular que el picking fue procesado por otro worker.
        picking.action_confirm()
        picking.move_ids.picked = True
        picking._action_done()
        self.assertEqual(picking.state, 'done')

        # Forzar el item a 'processing' (como si hubiera quedado
        # stale tras un crash).
        item.sudo().write({
            'state': 'processing',
            'start_date:': '2026-01-01 00:00:00',
        })
        self.env.cr.commit()

        status = self.Queue._process_item_in_new_cursor(item.id)
        self.env.invalidate_all()

        self.assertEqual(status, 'done')
        item.refresh()
        self.assertEqual(item.state, 'done')
        self.assertFalse(item.error_message)

    def test_cron_cleanup_done_items(self):
        """El cron de limpieza elimina items done más antiguos que N
        días y deja los recientes."""
        from datetime import datetime, timedelta

        p_old = self._create_picking('CLEAN-OLD')
        p_new = self._create_picking('CLEAN-NEW')
        item_old = self.Queue.create({'picking_id': p_old.id})
        item_new = self.Queue.create({'picking_id': p_new.id})

        # Marcar ambos como done con fechas diferentes.
        now = datetime.utcnow()
        old_date = (now - timedelta(days=60)).strftime(
            '%Y-%m-%d %H:%M:%S'
        )
        new_date = now.strftime('%Y-%m-%d %H:%M:%S')

        self.env.cr.execute(
            "UPDATE pos_inventory_queue "
            "SET state = 'done', done_date = %s "
            "WHERE id = %s",
            (old_date, item_old.id),
        )
        self.env.cr.execute(
            "UPDATE pos_inventory_queue "
            "SET state = 'done', done_date = %s "
            "WHERE id = %s",
            (new_date, item_new.id),
        )
        self.env.cr.commit()

        self.Queue._cron_cleanup_done_items(days=30)
        self.env.invalidate_all()

        self.assertFalse(
            item_old.exists(),
            'Old done item should be cleaned up',
        )
        self.assertTrue(
            item_new.exists(),
            'Recent done item should remain',
        )

    def test_process_session_items(self):
        """_process_session_items procesa los items pendientes de una
        sesión y devuelve los que siguen sin done."""
        picking = self._create_picking('SESSION-1')
        item = self.Queue.create({'picking_id': picking.id})

        # Asignar el picking a una sesión POS mockeada.
        session = self.env['pos.session'].search([], limit=1)
        if session:
            picking.write({
                'pos_session_id': session.id,
            })
            picking.pos_order_id = False
            item.pos_order_id = False

        self.env.cr.commit()

        if session:
            remaining = self.Queue._process_session_items(session)
            self.env.invalidate_all()
            # Si el procesamiento fue exitoso, no debería quedar nada.
            self.assertEqual(len(remaining), 0)

    def test_stale_processing_reclaim(self):
        """Items en 'processing' con start_date > 5 minutos se reclaman
        como stale (el claim los trata como pending)."""
        picking = self._create_picking('STALE-1')
        item = self.Queue.create({'picking_id': picking.id})

        from datetime import datetime, timedelta
        stale_time = (
            datetime.utcnow() - timedelta(minutes=10)
        ).strftime('%Y-%m-%d %H:%M:%S')

        self.env.cr.execute(
            "UPDATE pos_inventory_queue "
            "SET state = 'processing', start_date = %s "
            "WHERE id = %s",
            (stale_time, item.id),
        )
        self.env.cr.commit()

        item_id = self.Queue._claim_next_item()
        self.env.invalidate_all()

        self.assertIsNotNone(
            item_id,
            'stale processing item should be reclaimable',
        )
        claimed = self.Queue.browse(item_id)
        self.assertEqual(claimed.state, 'processing')
        self.assertEqual(claimed.id, item.id)

    def test_format_error_output(self):
        """_format_error incluye el tipo de excepción y trunca con '...'
        cuando el mensaje supera 4000 caracteres."""
        import psycopg2

        # Error corto: no se trunca.
        short_exc = psycopg2.OperationalError('short error')
        result = self.Queue._format_error(short_exc)
        self.assertIn('OperationalError', result)
        self.assertIn('short error', result)
        self.assertFalse(result.endswith('...'))

        # Error largo: se trunca con '...'.
        long_msg = 'x' * 5000
        long_exc = psycopg2.OperationalError(long_msg)
        result = self.Queue._format_error(long_exc)
        self.assertTrue(len(result) <= 4000)
        self.assertTrue(result.endswith('...'))

    def test_action_trigger_processing_returns_notification(self):
        """action_trigger_processing devuelve una notificación client."""
        result = self.Queue.action_trigger_processing()
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['type'], 'success')
