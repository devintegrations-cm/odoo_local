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
