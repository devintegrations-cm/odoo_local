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
        })

        # La cola solo procesa items committeados.
        self.env.cr.commit()

        item.action_retry()

        self.env.invalidate_all()

        item = self.Queue.search(
            [('picking_id', '=', picking.id)]
        )
        self.assertEqual(item.state, 'done')
        self.assertEqual(item.retry_count, 0)
        self.assertFalse(item.error_message)

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
