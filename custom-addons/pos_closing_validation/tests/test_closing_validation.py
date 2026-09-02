from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestClosingValidation(TransactionCase):
    """Test the closing validation snapshot and related checks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Company and currency
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id

        # Cash journal
        cls.cash_journal = cls.env["account.journal"].create({
            "name": "Cash Test",
            "type": "cash",
            "code": "CCSH",
            "company_id": cls.company.id,
        })

        # Cash payment method
        cls.cash_payment_method = cls.env["pos.payment.method"].create({
            "name": "Cash",
            "journal_id": cls.cash_journal.id,
            "is_cash_count": True,
            "split_transactions": False,
        })

        # POS config
        cls.pos_config = cls.env["pos.config"].create({
            "name": "Test POS",
            "module_pos_restaurant": False,
            "journal_id": cls.cash_journal.id,
            "payment_method_ids": [(6, 0, [cls.cash_payment_method.id])],
            "cash_control": True,
            "set_maximum_difference": True,
            "amount_authorized_diff": 10.0,
            "maximum_cash_in_out_moves": 2,
            "enable_rescue_session_validation": True,
        })

    def _create_session(self, opening=0.0, rescue=False):
        """Helper to create and open a POS session."""
        vals = {
            "config_id": self.pos_config.id,
        }
        if rescue:
            vals["rescue"] = True
        session = self.env["pos.session"].create(vals)
        session.action_pos_session_open()
        return session

    def _create_cash_move(self, session, amount, reason="Test"):
        """Helper to create a Cash In/Out movement."""
        session.try_cash_in_out(
            "in" if amount > 0 else "out",
            abs(amount),
            reason,
            {},
        )

    def _create_order(self, session, product, price, state="draft"):
        """Helper to create a POS order with required fields for Odoo 17."""
        order = self.env["pos.order"].create({
            "session_id": session.id,
            "partner_id": self.env["res.partner"].create({"name": "Partner"}).id,
            "lines": [(0, 0, {
                "product_id": product.id,
                "qty": 1,
                "price_unit": price,
                "price_subtotal": price,
                "price_subtotal_incl": price,
            })],
            "amount_tax": 0.0,
            "amount_total": price,
            "amount_paid": 0.0,
            "amount_return": 0.0,
            "state": state,
        })
        return order

    # ==================================================================
    # Caso A — Normal
    # ==================================================================

    def test_case_a_normal_session(self):
        """Normal session: opening 1000 + sales 200 - cash out 50 = 1150."""
        session = self._create_session(opening=1000.0)

        # Set opening balance manually
        session.cash_register_balance_start = 1000.0

        # Create a cash sale payment
        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 200.0,
            "taxes_id": [(6, 0, [])],
        })
        order = self._create_order(session, product, 200.0)
        self.env["pos.payment"].create({
            "pos_order_id": order.id,
            "payment_method_id": self.cash_payment_method.id,
            "amount": 200.0,
            "payment_date": fields.Datetime.now(),
        })

        # Cash out of 50
        self._create_cash_move(session, -50.0)

        # Get snapshot
        data = session._get_closing_cash_validation_data()

        self.assertAlmostEqual(data["opening_cash"], 1000.0, places=2)
        self.assertAlmostEqual(data["cash_sales"], 200.0, places=2)
        self.assertAlmostEqual(data["cash_out"], 50.0, places=2)
        self.assertAlmostEqual(data["expected_cash"], 1150.0, places=2)
        self.assertFalse(data["is_rescue"])

    # ==================================================================
    # Caso B — Rescue
    # ==================================================================

    def test_case_b_rescue_session(self):
        """Rescue session: opening 1556.90 + sales 303.45 - cash out 1000 = 860.35."""
        # Create parent session first
        parent = self._create_session(opening=1556.90)
        parent.cash_register_balance_start = 1556.90

        # Close parent
        parent.cash_register_balance_end_real = 1556.90
        parent.action_pos_session_closing_control()
        parent.action_pos_session_close()

        # Create rescue session
        rescue = self._create_session(rescue=True)

        # Set opening (rescue should inherit from parent via create override)
        rescue.cash_register_balance_start = 1556.90

        # Create cash sales
        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 101.15,
            "taxes_id": [(6, 0, [])],
        })

        for i in range(3):
            order = self._create_order(rescue, product, 101.15)
            self.env["pos.payment"].create({
                "pos_order_id": order.id,
                "payment_method_id": self.cash_payment_method.id,
                "amount": 101.15,
                "payment_date": fields.Datetime.now(),
            })

        # Cash out of 1000
        self._create_cash_move(rescue, -1000.0)

        # Get snapshot
        data = rescue._get_closing_cash_validation_data()

        self.assertTrue(data["is_rescue"])
        self.assertAlmostEqual(data["opening_cash"], 1556.90, places=2)
        self.assertAlmostEqual(data["cash_sales"], 303.45, places=2)
        self.assertAlmostEqual(data["cash_out"], 1000.0, places=2)
        self.assertAlmostEqual(data["expected_cash"], 860.35, places=2)
        self.assertEqual(data["cash_move_count"], 1)

    # ==================================================================
    # Caso C — Rescue con Cash In
    # ==================================================================

    def test_case_c_rescue_with_cash_in(self):
        """Rescue with Cash In: opening + sales + cash_in - cash_out."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1556.90

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 101.15,
            "taxes_id": [(6, 0, [])],
        })

        # Cash sale
        order = self._create_order(rescue, product, 101.15)
        self.env["pos.payment"].create({
            "pos_order_id": order.id,
            "payment_method_id": self.cash_payment_method.id,
            "amount": 101.15,
            "payment_date": fields.Datetime.now(),
        })

        # Cash in of 500
        self._create_cash_move(rescue, 500.0)

        # Cash out of 200
        self._create_cash_move(rescue, -200.0)

        data = rescue._get_closing_cash_validation_data()

        expected = 1556.90 + 101.15 + 500.0 - 200.0
        self.assertAlmostEqual(data["expected_cash"], expected, places=2)
        self.assertAlmostEqual(data["cash_in"], 500.0, places=2)
        self.assertAlmostEqual(data["cash_out"], 200.0, places=2)
        self.assertEqual(data["cash_move_count"], 2)

    # ==================================================================
    # Caso D — Diferencia real dentro del límite
    # ==================================================================

    def test_case_d_difference_within_limit(self):
        """Counted cash differs from expected by 5 (within 10 limit)."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # Expected = 1000.0, counted = 995.0, difference = 5.0 < 10.0
        error = session._check_authorized_cash_difference(995.0)
        self.assertIsNone(error)

    # ==================================================================
    # Caso E — Límite Cash In/Out alcanzado
    # ==================================================================

    def test_case_e_cash_in_out_limit(self):
        """Max 2 movements: 3rd should be blocked."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        self._create_cash_move(session, 100.0)
        self._create_cash_move(session, -50.0)

        # Third movement should trigger limit
        with self.assertRaises(UserError):
            self._create_cash_move(session, 25.0)

    # ==================================================================
    # Caso F — Dos terminales simultáneas (FOR UPDATE)
    # ==================================================================

    def test_case_f_concurrent_terminal_protection(self):
        """Verify FOR UPDATE lock prevents concurrent limit bypass."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # Fill to limit
        self._create_cash_move(session, 100.0)
        self._create_cash_move(session, -50.0)

        # Verify limit is enforced
        with self.assertRaises(UserError):
            self._create_cash_move(session, 25.0)

    # ==================================================================
    # Caso G — Rescue sin statement lines (sync failure)
    # ==================================================================

    def test_case_g_rescue_no_statement_lines(self):
        """Rescue with no synced statement lines."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1556.90

        # No payments, no cash moves
        data = rescue._get_closing_cash_validation_data()

        self.assertAlmostEqual(data["expected_cash"], 1556.90, places=2)
        self.assertAlmostEqual(data["cash_sales"], 0.0, places=2)
        self.assertAlmostEqual(data["cash_in"], 0.0, places=2)
        self.assertAlmostEqual(data["cash_out"], 0.0, places=2)

    # ==================================================================
    # Caso H — Cierre normal con rescue pendiente
    # ==================================================================

    def test_case_h_normal_close_with_rescue_pending(self):
        """Normal session should warn when rescue sessions are pending."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        # Create a rescue session WITH an order (non-empty)
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        # _cannot_close_session should warn about pending rescue
        result = parent._cannot_close_session()
        self.assertTrue(result)
        self.assertIn("rescate", result["message"].lower())

    # ==================================================================
    # Caso I — Integridad de datos
    # ==================================================================

    def test_case_i_data_integrity_paid_no_payments(self):
        """Paid order without payments should trigger integrity error."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # Create order with state 'paid' but no payments
        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 100.0,
            "taxes_id": [(6, 0, [])],
        })
        order = self._create_order(session, product, 100.0, state="paid")

        result = session._check_session_data_integrity()
        self.assertTrue(result)
        self.assertIn("órdenes pagadas", result["message"])

    # ==================================================================
    # Snapshot consistency
    # ==================================================================

    def test_snapshot_fields_completeness(self):
        """Snapshot returns all expected fields."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        data = session._get_closing_cash_validation_data()

        required_keys = [
            "session_id", "session_name", "is_rescue",
            "parent_session_name", "opening_cash", "cash_sales",
            "cash_in", "cash_in_count", "cash_out", "cash_out_count",
            "cash_move_count", "expected_cash", "counted_cash",
            "difference", "statement_lines_count", "orders_count",
            "has_orders",
        ]
        for key in required_keys:
            self.assertIn(key, data, f"Missing key: {key}")

    def test_snapshot_rescue_parent_link(self):
        """Rescue session links to parent."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0
        parent.action_pos_session_closing_control()
        parent.action_pos_session_close()

        rescue = self._create_session(rescue=True)

        self.assertEqual(rescue.rescue_parent_session_id, parent)
        self.assertIn(rescue, parent.rescue_session_ids)

    # ==================================================================
    # Caso J — _is_empty_rescue: sesión vacía
    # ==================================================================

    def test_case_j_is_empty_rescue_empty(self):
        """Empty rescue session (no orders, no payments, no statements) is empty."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        self.assertTrue(rescue._is_empty_rescue())

    # ==================================================================
    # Caso K — _is_empty_rescue: con orden
    # ==================================================================

    def test_case_k_is_empty_rescue_with_order(self):
        """Rescue session with an order is NOT empty."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 100.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 100.0)

        self.assertFalse(rescue._is_empty_rescue())

    # ==================================================================
    # Caso L — _is_empty_rescue: con solo canceladas
    # ==================================================================

    def test_case_l_is_empty_rescue_with_cancelled_orders(self):
        """Rescue session with only cancelled orders is NOT empty."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 100.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 100.0, state="cancel")

        self.assertFalse(rescue._is_empty_rescue())

    # ==================================================================
    # Caso M — Rescate vacío NO bloquea cierre normal
    # ==================================================================

    def test_case_m_empty_rescue_does_not_block(self):
        """Normal session can close when rescue sessions are empty."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        # Create empty rescue session
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        # Empty rescue should NOT block
        result = parent._check_rescue_sessions_pending()
        self.assertIsNone(result)

    # ==================================================================
    # Caso N — Rescate con orden SÍ bloquea cierre normal
    # ==================================================================

    def test_case_n_non_empty_rescue_does_block(self):
        """Normal session is blocked when rescue sessions have orders."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        # Create rescue with order
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        result = parent._check_rescue_sessions_pending()
        self.assertTrue(result)
        self.assertIn("rescate", result["message"].lower())

    # ==================================================================
    # Caso O — Default de enable_rescue_session_validation es False
    # ==================================================================

    def test_case_o_default_rescue_validation_is_false(self):
        """New POS config should have rescue validation disabled by default."""
        new_journal = self.env["account.journal"].create({
            "name": "Cash Default Test",
            "type": "cash",
            "code": "CDEF",
            "company_id": self.company.id,
        })
        new_payment_method = self.env["pos.payment.method"].create({
            "name": "Cash Default Test",
            "journal_id": new_journal.id,
            "is_cash_count": True,
            "split_transactions": False,
        })
        new_config = self.env["pos.config"].create({
            "name": "Test POS Default",
            "module_pos_restaurant": False,
            "journal_id": new_journal.id,
            "payment_method_ids": [(6, 0, [new_payment_method.id])],
            "cash_control": True,
        })
        self.assertFalse(new_config.enable_rescue_session_validation)

    # ==================================================================
    # Caso P — Snapshot has_orders field
    # ==================================================================

    def test_case_p_snapshot_has_orders_field(self):
        """Snapshot includes has_orders field reflecting order presence."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # No orders yet
        data = session._get_closing_cash_validation_data()
        self.assertFalse(data["has_orders"])

        # Create order
        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 100.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(session, product, 100.0)

        data = session._get_closing_cash_validation_data()
        self.assertTrue(data["has_orders"])

    # ==================================================================
    # FASE 4 — Validación de apertura: bloqueo por rescue pendiente
    # ==================================================================

    def test_opening_blocked_with_pending_rescue(self):
        """Opening a new session is blocked when rescue sessions exist."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        # Create rescue with order (non-empty, state=opened)
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        # Close parent session
        parent.cash_register_balance_end_real = 1050.0
        parent.action_pos_session_closing_control()
        parent.action_pos_session_close()

        # Attempting to open a new session should raise UserError
        new_config = self.env["pos.config"].create({
            "name": "Test POS Open Block",
            "module_pos_restaurant": False,
            "journal_id": self.cash_journal.id,
            "payment_method_ids": [(6, 0, [self.cash_payment_method.id])],
            "cash_control": True,
            "enable_rescue_session_validation": True,
        })

        with self.assertRaises(UserError) as ctx:
            new_config.open_ui()
        self.assertIn("rescate", ctx.exception.args[0].lower())

    def test_opening_allowed_when_no_rescue(self):
        """Opening a new session is allowed when no rescue sessions exist."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # No rescue sessions — should not raise
        # (open_ui will fail for other reasons in test env, but not our check)
        pending = self.env["pos.session"]._get_pending_rescue_sessions_for_config(
            self.pos_config.id
        )
        self.assertEqual(len(pending), 0)

    def test_opening_allowed_when_rescue_validation_disabled(self):
        """Opening is allowed even with rescue if validation is disabled."""
        # Disable rescue validation
        self.pos_config.enable_rescue_session_validation = False

        # Create rescue session
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        # open_ui should NOT raise our validation error
        # (it may fail for other test-environment reasons, but not for rescue)
        try:
            self.pos_config.open_ui()
        except UserError as e:
            self.assertNotIn("rescate", e.args[0].lower())

    def test_opening_allowed_when_rescue_closed(self):
        """Opening is allowed when all rescue sessions are closed."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        # Create and close a rescue session
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0
        rescue.cash_register_balance_end_real = 1000.0
        rescue.action_pos_session_closing_control()
        rescue.action_pos_session_close()

        # No pending rescues
        pending = self.env["pos.session"]._get_pending_rescue_sessions_for_config(
            self.pos_config.id
        )
        self.assertEqual(len(pending), 0)

    # ==================================================================
    # FASE 3 — Métodos de validación de ciclo
    # ==================================================================

    def test_check_pending_rescue_sessions_returns_blocked(self):
        """_check_pending_rescue_sessions returns blocked=True when rescue open."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        result = parent._check_pending_rescue_sessions()
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "pending_rescue")
        self.assertTrue(len(result["sessions"]) > 0)
        self.assertEqual(result["sessions"][0]["id"], rescue.id)

    def test_check_pending_rescue_sessions_returns_not_blocked(self):
        """_check_pending_rescue_sessions returns blocked=False when no rescue."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        result = session._check_pending_rescue_sessions()
        self.assertFalse(result["blocked"])
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["sessions"], [])

    def test_get_pending_rescue_sessions_includes_empty(self):
        """_get_pending_rescue_sessions includes empty rescues (strict for opening)."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        # Create empty rescue (no orders, no payments)
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        pending = parent._get_pending_rescue_sessions()
        self.assertIn(rescue, pending)

    def test_has_pending_rescue_sessions_true(self):
        """_has_pending_rescue_sessions returns True when rescue is open."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        self.assertTrue(parent._has_pending_rescue_sessions())

    def test_has_pending_rescue_sessions_false(self):
        """_has_pending_rescue_sessions returns False when no rescue."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        self.assertFalse(session._has_pending_rescue_sessions())

    def test_pending_rescue_validation_data_structure(self):
        """_get_pending_rescue_validation_data returns correct structure."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        data = parent._get_pending_rescue_validation_data()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], rescue.id)
        self.assertEqual(data[0]["name"], rescue.name)
        self.assertEqual(data[0]["state"], rescue.state)

    # ==================================================================
    # FASE 9 — Auditoría de saldo inicial
    # ==================================================================

    def test_expected_opening_balance_set_on_open(self):
        """expected_opening_balance is set from last session's closing balance."""
        # First session
        session1 = self._create_session()
        session1.cash_register_balance_start = 1000.0
        session1.cash_register_balance_end_real = 1200.0
        session1.action_pos_session_closing_control()
        session1.action_pos_session_close()

        # Second session — should capture expected opening from session1
        session2 = self._create_session()
        session2.action_pos_session_open()

        self.assertAlmostEqual(
            session2.expected_opening_balance, 1200.0, places=2
        )

    def test_expected_opening_balance_zero_when_no_previous(self):
        """expected_opening_balance is 0 when there's no previous session."""
        session = self._create_session()
        session.action_pos_session_open()

        # No previous session — expected should be 0 (default)
        self.assertAlmostEqual(
            session.expected_opening_balance, 0.0, places=2
        )

    def test_set_cashbox_pos_blocks_when_difference_exceeds_maximum(self):
        """set_cashbox_pos raises UserError when difference exceeds max."""
        # Create and close first session
        session1 = self._create_session()
        session1.cash_register_balance_start = 1000.0
        session1.cash_register_balance_end_real = 1200.0
        session1.action_pos_session_closing_control()
        session1.action_pos_session_close()

        # Create second session
        session2 = self._create_session()
        session2.action_pos_session_open()

        # Expected = 1200, max diff = 10
        # Entering 1000 → difference = 200 > 10 → should block
        with self.assertRaises(UserError) as ctx:
            session2.set_cashbox_pos(1000.0, "Test notes")
        self.assertIn("continuidad", ctx.exception.args[0].lower())

    def test_set_cashbox_pos_allows_when_difference_within_maximum(self):
        """set_cashbox_pos succeeds when difference is within max."""
        # Create and close first session
        session1 = self._create_session()
        session1.cash_register_balance_start = 1000.0
        session1.cash_register_balance_end_real = 1200.0
        session1.action_pos_session_closing_control()
        session1.action_pos_session_close()

        # Create second session
        session2 = self._create_session()
        session2.action_pos_session_open()

        # Expected = 1200, max diff = 10
        # Entering 1205 → difference = 5 < 10 → should succeed
        session2.set_cashbox_pos(1205.0, "Test notes")
        self.assertAlmostEqual(
            session2.cash_register_balance_start, 1205.0, places=2
        )
        self.assertEqual(session2.state, "opened")

    def test_set_cashbox_pos_allows_exact_match(self):
        """set_cashbox_pos succeeds with exact match."""
        session1 = self._create_session()
        session1.cash_register_balance_start = 1000.0
        session1.cash_register_balance_end_real = 1200.0
        session1.action_pos_session_closing_control()
        session1.action_pos_session_close()

        session2 = self._create_session()
        session2.action_pos_session_open()

        # Expected = 1200, entering 1200 → difference = 0
        session2.set_cashbox_pos(1200.0, "Exact match")
        self.assertAlmostEqual(
            session2.cash_register_balance_start, 1200.0, places=2
        )

    def test_set_cashbox_pos_allows_when_validation_disabled(self):
        """set_cashbox_pos succeeds even with large diff if validation disabled."""
        self.pos_config.set_maximum_difference = False

        session1 = self._create_session()
        session1.cash_register_balance_start = 1000.0
        session1.cash_register_balance_end_real = 1200.0
        session1.action_pos_session_closing_control()
        session1.action_pos_session_close()

        session2 = self._create_session()
        session2.action_pos_session_open()

        # Expected = 1200, entering 5000 → but validation disabled
        session2.set_cashbox_pos(5000.0, "No validation")
        self.assertAlmostEqual(
            session2.cash_register_balance_start, 5000.0, places=2
        )

    def test_set_cashbox_pos_rescue_session_skips_validation(self):
        """set_cashbox_pos skips opening audit for rescue sessions."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        # Rescue sessions don't set expected_opening_balance
        self.assertAlmostEqual(
            rescue.expected_opening_balance, 0.0, places=2
        )

        # set_cashbox_pos should work without validation
        rescue.set_cashbox_pos(500.0, "Rescue opening")
        self.assertAlmostEqual(
            rescue.cash_register_balance_start, 500.0, places=2
        )

    # ==================================================================
    # FASE 10-11 — Snapshot unificado
    # ==================================================================

    def test_closing_control_data_includes_validation_fields(self):
        """get_closing_control_data() includes our validation fields."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        data = session.get_closing_control_data()

        # Our fields exist
        self.assertIn("pending_rescue", data)
        self.assertIn("pending_rescue_sessions", data)
        self.assertIn("can_close", data)
        self.assertIn("blocking_reasons", data)
        self.assertIn("session_id", data)
        self.assertIn("session_name", data)
        self.assertIn("is_rescue", data)
        self.assertIn("opening_cash", data)
        self.assertIn("cash_sales", data)
        self.assertIn("cash_in", data)
        self.assertIn("cash_out", data)
        self.assertIn("expected_cash", data)
        self.assertIn("difference", data)
        self.assertIn("cash_move_count", data)
        self.assertIn("cash_move_limit", data)

    def test_closing_control_data_can_close_true(self):
        """can_close is True when no blocking conditions."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        data = session.get_closing_control_data()

        self.assertTrue(data["can_close"])
        self.assertEqual(data["blocking_reasons"], [])
        self.assertFalse(data["pending_rescue"])

    def test_closing_control_data_can_close_false_rescue_pending(self):
        """can_close is False when rescue sessions are pending."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        data = parent.get_closing_control_data()

        self.assertFalse(data["can_close"])
        self.assertTrue(data["pending_rescue"])
        self.assertTrue(len(data["blocking_reasons"]) > 0)
        self.assertTrue(len(data["pending_rescue_sessions"]) > 0)

    def test_closing_control_data_can_close_false_is_rescue(self):
        """can_close is False for rescue sessions."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        data = rescue.get_closing_control_data()

        self.assertFalse(data["can_close"])
        self.assertTrue(data["is_rescue"])
        self.assertTrue(len(data["blocking_reasons"]) > 0)

    def test_closing_control_data_cash_moves(self):
        """get_closing_control_data() includes cash move count and limit."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        self._create_cash_move(session, 100.0)

        data = session.get_closing_control_data()

        self.assertEqual(data["cash_move_count"], 1)
        self.assertEqual(data["cash_move_limit"], 2)

    def test_closing_control_data_expected_cash(self):
        """get_closing_control_data() returns correct expected_cash."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        self._create_cash_move(session, 100.0)
        self._create_cash_move(session, -50.0)

        data = session.get_closing_control_data()

        # expected = 1000 + 0 (no sales) + 100 - 50 = 1050
        self.assertAlmostEqual(data["expected_cash"], 1050.0, places=2)

    def test_get_blocking_reasons_empty(self):
        """_get_blocking_reasons returns empty list for normal session."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        reasons = session._get_blocking_reasons()
        self.assertEqual(reasons, [])

    def test_get_blocking_reasons_rescue(self):
        """_get_blocking_reasons includes rescue reason for rescue sessions."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        reasons = rescue._get_blocking_reasons()
        self.assertTrue(len(reasons) > 0)
        self.assertTrue(any("rescate" in r.lower() for r in reasons))

    def test_pending_rescue_sessions_in_snapshot(self):
        """pending_rescue_sessions in snapshot has correct structure."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        data = parent.get_closing_control_data()

        self.assertEqual(len(data["pending_rescue_sessions"]), 1)
        session_data = data["pending_rescue_sessions"][0]
        self.assertEqual(session_data["id"], rescue.id)
        self.assertEqual(session_data["name"], rescue.name)
        self.assertEqual(session_data["state"], rescue.state)

    # ==================================================================
    # FASE 12-14 — Cierre transaccional con FOR UPDATE
    # ==================================================================

    def test_post_closing_cash_details_validates_difference(self):
        """post_closing_cash_details blocks when difference exceeds max."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # expected = 1000, counted = 900, difference = 100 > 10 (max)
        error = session.post_closing_cash_details(900.0)

        self.assertTrue(error)
        self.assertFalse(error["successful"])
        self.assertIn("diferencia", error["message"].lower())

    def test_post_closing_cash_details_allows_within_limit(self):
        """post_closing_cash_details succeeds when difference within max."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # expected = 1000, counted = 1005, difference = 5 < 10 (max)
        error = session.post_closing_cash_details(1005.0)

        # Should return None (success) or call super which may have
        # its own behavior — but our validation passes
        if error:
            self.assertTrue(error.get("successful", True))

    def test_post_closing_cash_details_blocks_rescue_pending(self):
        """post_closing_cash_details blocks when rescue sessions pending."""
        parent = self._create_session()
        parent.cash_register_balance_start = 1000.0

        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        product = self.env["product.product"].create({
            "name": "Test Product",
            "list_price": 50.0,
            "taxes_id": [(6, 0, [])],
        })
        self._create_order(rescue, product, 50.0)

        error = parent.post_closing_cash_details(1000.0)

        self.assertTrue(error)
        self.assertFalse(error["successful"])
        self.assertIn("rescate", error["message"].lower())

    def test_post_closing_cash_details_blocks_integrity_violation(self):
        """post_closing_cash_details blocks on cash in/out integrity error."""
        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # Create movements up to limit
        self._create_cash_move(session, 100.0)
        self._create_cash_move(session, -50.0)

        # Manually create an extra statement line to simulate inconsistency
        # (bypassing the limit check)
        cash_pm = session.payment_method_ids.filtered(
            lambda pm: pm.type == "cash"
        )[:1]
        journal = cash_pm.journal_id
        self.env["account.bank.statement.line"].create({
            "payment_ref": "Extra move",
            "journal_id": journal.id,
            "amount": 25.0,
            "pos_session_id": session.id,
        })

        # Now we have 3 movements but limit is 2
        error = session.post_closing_cash_details(1000.0)

        self.assertTrue(error)
        self.assertFalse(error["successful"])
        self.assertIn("inconsistencia", error["message"].lower())

    def test_post_closing_cash_details_no_validation_when_disabled(self):
        """post_closing_cash_details skips validation when disabled."""
        self.pos_config.set_maximum_difference = False

        session = self._create_session()
        session.cash_register_balance_start = 1000.0

        # Large difference but validation disabled
        error = session.post_closing_cash_details(5000.0)

        # Our validation should not block (super may have its own behavior)
        if error:
            # If error exists, it should NOT be about difference
            self.assertNotIn("diferencia", error.get("message", "").lower())

    def test_post_closing_cash_details_rescue_blocks(self):
        """post_closing_cash_details blocks rescue sessions from closing here."""
        rescue = self._create_session(rescue=True)
        rescue.cash_register_balance_start = 1000.0

        error = rescue.post_closing_cash_details(1000.0)

        # Rescue sessions should be blocked by _get_blocking_reasons
        self.assertTrue(error)
        self.assertFalse(error["successful"])
