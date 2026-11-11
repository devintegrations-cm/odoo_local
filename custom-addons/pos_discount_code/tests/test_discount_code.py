from datetime import date, datetime

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.point_of_sale.tests.common import TestPoSCommon


@tagged("post_install", "-at_install")
class TestPosDiscountCode(TestPoSCommon):
    """Registro del canje de bonos en pos.order (lógica del servidor)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.basic_config
        cls.product = cls.env["product.product"].create({
            "name": "Café de prueba",
            "type": "consu",
            "list_price": 100.0,
            "available_in_pos": True,
            "taxes_id": [Command.clear()],
        })
        # Programa con código en la regla (promo_code).
        cls.program = cls.env["loyalty.program"].create({
            "name": "Bono 10%",
            "program_type": "promo_code",
            "trigger": "with_code",
            "applies_on": "current",
            "date_to": date(2099, 12, 31),
            "pos_ok": True,
            "rule_ids": [Command.create({"mode": "with_code", "code": "BONO10"})],
            "reward_ids": [Command.create({
                "reward_type": "discount",
                "discount": 10,
                "discount_mode": "percent",
                "discount_applicability": "order",
            })],
        })
        cls.reward = cls.program.reward_ids
        # Programa de cupones: el código está en la tarjeta (loyalty.card).
        cls.coupon_program = cls.env["loyalty.program"].create({
            "name": "Cupón 5.000",
            "program_type": "coupons",
            "trigger": "with_code",
            "applies_on": "current",
            "pos_ok": True,
            "rule_ids": [Command.create({"mode": "with_code"})],
            "reward_ids": [Command.create({
                "reward_type": "discount",
                "discount": 20,
                "discount_mode": "per_order",
                "discount_applicability": "order",
            })],
        })
        cls.coupon_reward = cls.coupon_program.reward_ids
        cls.coupon = cls.env["loyalty.card"].create({
            "program_id": cls.coupon_program.id,
            "code": "CUPON-ABC",
            "points": 1,
        })

    def setUp(self):
        super().setUp()
        self.open_new_session()

    # ------------------------------------------------------------------ helpers
    def _reward_line(self, reward, amount):
        return (0, 0, {
            "id": 999999,
            "pack_lot_ids": [],
            "product_id": reward.discount_line_product_id.id,
            "price_unit": -amount,
            "qty": 1,
            "price_subtotal": -amount,
            "price_subtotal_incl": -amount,
            "tax_ids": [(6, 0, [])],
            "reward_id": reward.id,
            "is_reward_line": True,
        })

    def _ui_order(self, qty=2, reward=None, reward_amount=0.0, uid=None, **discount):
        data = self.create_ui_order_data([(self.product, qty)], uid=uid)
        order = data["data"]
        if reward:
            order["lines"].append(self._reward_line(reward, reward_amount))
            total = order["amount_total"] - reward_amount
            order["amount_total"] = order["amount_paid"] = total
            order["statement_ids"][0][2]["amount"] = total
        order.update(discount)
        return data

    def _sync(self, data, draft=False):
        res = self.env["pos.order"].create_from_ui([data], draft=draft)
        return self.env["pos.order"].browse(res[0]["id"])

    def _discount(self, **overrides):
        vals = {
            "discount_program_id": self.program.id,
            "discount_name": "nombre enviado por el POS",
            "discount_code": "BONO10",
            "discount_amount": 1.0,  # valor viejo: el servidor debe ignorarlo
            "discount_vigencia": "2000-01-01",
            "discount_doc_type": "cc",
            "discount_doc_number": " 1032456789 ",
            "discount_redeem_date": "2026-11-10 19:30:00",
        }
        vals.update(overrides)
        return vals

    # ------------------------------------------------------------------- tests
    def test_applied_code_is_registered(self):
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount()))
        self.assertEqual(order.state, "paid")
        self.assertEqual(order.discount_program_id, self.program)
        self.assertEqual(order.discount_code, "BONO10")
        self.assertEqual(order.discount_name, "Bono 10%", "El nombre debe salir del programa, no del POS")
        self.assertEqual(order.discount_vigencia, date(2099, 12, 31), "La vigencia debe salir del programa")
        self.assertEqual(order.discount_amount, 20.0, "El valor sale de las líneas de recompensa")
        self.assertEqual(order.discount_doc_type, "cc")
        self.assertEqual(order.discount_doc_number, "1032456789")
        self.assertEqual(order.discount_redeem_date, datetime(2026, 11, 10, 19, 30, 0),
                         "La fecha llega en UTC y se guarda sin desplazarla")
        self.assertEqual(order.discount_state, "vigente")

    def test_code_without_reward_line_is_not_registered(self):
        order = self._sync(self._ui_order(**self._discount()))
        self.assertEqual(order.state, "paid")
        self.assertFalse(order.discount_code)
        self.assertFalse(order.discount_program_id)
        self.assertFalse(order.discount_doc_number)
        self.assertEqual(order.discount_amount, 0.0)

    def test_reward_line_of_other_program_is_not_registered(self):
        data = self._ui_order(reward=self.coupon_reward, reward_amount=20.0, **self._discount())
        order = self._sync(data)
        self.assertFalse(order.discount_code)

    def test_missing_program_does_not_block_sync(self):
        missing_id = self.env["loyalty.program"].search([], order="id desc", limit=1).id + 1000
        data = self._ui_order(reward=self.reward, reward_amount=20.0,
                              **self._discount(discount_program_id=missing_id))
        order = self._sync(data)
        self.assertEqual(order.state, "paid", "La orden debe sincronizar aunque el programa no exista")
        self.assertFalse(order.discount_code)

    def test_code_not_belonging_to_program_is_not_registered(self):
        data = self._ui_order(reward=self.reward, reward_amount=20.0,
                              **self._discount(discount_code="OTRO"))
        order = self._sync(data)
        self.assertFalse(order.discount_code)

    def test_coupon_code_is_registered(self):
        data = self._ui_order(reward=self.coupon_reward, reward_amount=20.0, **self._discount(
            discount_program_id=self.coupon_program.id, discount_code="CUPON-ABC"))
        order = self._sync(data)
        self.assertEqual(order.discount_program_id, self.coupon_program)
        self.assertEqual(order.discount_code, "CUPON-ABC")
        self.assertEqual(order.discount_amount, 20.0)

    def test_invalid_values_are_sanitized(self):
        data = self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount(
            discount_doc_type="no-existe", discount_redeem_date="fecha-mala"))
        order = self._sync(data)
        self.assertEqual(order.discount_code, "BONO10")
        self.assertFalse(order.discount_doc_type)
        self.assertEqual(order.discount_redeem_date, order.date_order,
                         "Con una fecha inválida se usa la fecha de la orden")

    def test_amount_follows_order_changes(self):
        uid = self.create_random_uid()
        order = self._sync(self._ui_order(qty=2, reward=self.reward, reward_amount=20.0, uid=uid,
                                          **self._discount()), draft=True)
        self.assertEqual(order.state, "draft")
        self.assertEqual(order.discount_amount, 20.0)
        # El cajero sube la cantidad: el descuento porcentual cambia.
        order = self._sync(self._ui_order(qty=5, reward=self.reward, reward_amount=50.0, uid=uid,
                                          **self._discount()), draft=True)
        self.assertEqual(order.discount_amount, 50.0)

    def test_draft_resync_without_code_clears_registration(self):
        uid = self.create_random_uid()
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, uid=uid,
                                          **self._discount()), draft=True)
        self.assertEqual(order.discount_code, "BONO10")
        cleared = self._discount(discount_program_id=False, discount_code=False,
                                 discount_doc_type=False, discount_doc_number=False,
                                 discount_redeem_date=False)
        order = self._sync(self._ui_order(uid=uid, **cleared), draft=True)
        self.assertFalse(order.discount_code)
        self.assertFalse(order.discount_program_id)
        self.assertFalse(order.discount_redeem_date)
        self.assertEqual(order.discount_amount, 0.0)

    def test_order_without_discount_keys_is_untouched(self):
        order = self._sync(self._ui_order())
        self.assertFalse(order.discount_code)
        self.assertEqual(order.discount_amount, 0.0)

    def test_paid_order_discount_is_locked(self):
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount()))
        with self.assertRaises(UserError):
            order.write({"discount_code": "CAMBIADO"})
        # Escribir otros campos sigue permitido.
        order.write({"note": "ok"})

    def test_redeem_date_parsing(self):
        PosOrder = self.env["pos.order"]
        self.assertEqual(
            PosOrder._parse_discount_redeem_date({"discount_redeem_date": "2026-11-10T19:30:00.123Z"}),
            datetime(2026, 11, 10, 19, 30, 0),
        )
        fallback = PosOrder._parse_discount_redeem_date({})
        self.assertLessEqual(abs((fields.Datetime.now() - fallback).total_seconds()), 5)

    # ------------------------------------------------------- estado al canjear
    def test_state_is_frozen_at_redeem_time(self):
        """Un bono válido al canjearlo sigue "vigente" aunque hoy ya esté vencido."""
        self.program.date_to = date(2020, 1, 15)
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount(
            discount_redeem_date="2020-01-10 15:00:00")))
        self.assertEqual(order.discount_vigencia, date(2020, 1, 15))
        self.assertEqual(order.discount_state, "vigente",
                         "El estado corresponde a la fecha del canje, no a la fecha actual")

    def test_state_expired_when_redeemed_after_validity(self):
        self.program.date_to = date(2020, 1, 15)
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount(
            discount_redeem_date="2020-01-20 15:00:00")))
        self.assertEqual(order.discount_state, "vencido")

    def test_state_uses_cashier_local_day(self):
        """03:00 UTC del 16 = 22:00 del 15 en Bogotá: el último día de vigencia sigue vigente."""
        self.env.user.tz = "America/Bogota"
        self.program.date_to = date(2020, 1, 15)
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount(
            discount_redeem_date="2020-01-16 03:00:00")))
        self.assertEqual(order.discount_state, "vigente")

    def test_state_empty_without_code(self):
        order = self._sync(self._ui_order())
        self.assertFalse(order.discount_state)

    # ------------------------------------------------------ tipos de documento
    def test_doc_types_are_sent_to_pos_from_python(self):
        loaded = self.pos_session.load_pos_data()
        sent = loaded["pos_discount_code.doc_types"]
        selection = self.env["pos.order"]._fields["discount_doc_type"].selection
        self.assertEqual([d["value"] for d in sent], [value for value, _label in selection],
                         "El POS recibe exactamente los tipos del Selection de Python")
        numeric = {d["value"] for d in sent if d["numeric"]}
        self.assertIn("cc", numeric)
        self.assertNotIn("passport", numeric)
        self.assertTrue(all(d["label"] for d in sent))

    def test_numeric_doc_number_is_cleaned(self):
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount(
            discount_doc_type="cc", discount_doc_number="1.032.456-789")))
        self.assertEqual(order.discount_doc_number, "1032456789")

    def test_passport_keeps_letters(self):
        order = self._sync(self._ui_order(reward=self.reward, reward_amount=20.0, **self._discount(
            discount_doc_type="passport", discount_doc_number="AB123456")))
        self.assertEqual(order.discount_doc_number, "AB123456")


