import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

DISCOUNT_FIELDS = [
    "discount_program_id",
    "discount_name",
    "discount_code",
    "discount_amount",
    "discount_vigencia",
    "discount_doc_type",
    "discount_doc_number",
    "discount_redeem_date",
]

DISCOUNT_DOC_TYPES = [
    ("nit", "NIT"),
    ("cc", "Cédula de ciudadanía"),
    ("ti", "Tarjeta de Identidad"),
    ("te", "Tarjeta de extranjería"),
    ("ce", "Cédula de extranjería"),
    ("passport", "Pasaporte"),
    ("niup", "NIUP"),
    ("external_id", "Nit de otro país"),
]

NUMERIC_DOC_TYPES = {"nit", "cc", "ti", "te", "ce", "niup"}


class PosOrder(models.Model):
    _inherit = "pos.order"

    discount_program_id = fields.Many2one(
        "loyalty.program",
        string="Programa de descuento",
        copy=False,
        ondelete="set null",
    )
    discount_name = fields.Char(string="Nombre del descuento", copy=False)
    discount_code = fields.Char(string="Código de descuento", copy=False)
    # Calculado en el servidor desde las líneas de recompensa reales de la
    # orden: el valor que envía el POS se ignora porque queda desactualizado
    # si el cajero cambia cantidades o productos después de aplicar el código.
    discount_amount = fields.Monetary(
        string="Valor del descuento",
        copy=False,
        currency_field="currency_id",
        compute="_compute_discount_amount",
        store=True,
        readonly=True,
    )
    # Estado del bono en la fecha del canje (almacenado).
    discount_state = fields.Selection(
        [("vigente", "Vigente"), ("vencido", "Vencido")],
        string="Estado al canjear",
        compute="_compute_discount_state",
        store=True,
        copy=False,
    )
    discount_vigencia = fields.Date(string="Fecha de vigencia", copy=False)
    discount_doc_type = fields.Selection(
        DISCOUNT_DOC_TYPES,
        string="Tipo de documento",
        copy=False,
    )
    discount_doc_number = fields.Char(string="Número de documento", copy=False)
    discount_redeem_date = fields.Datetime(string="Fecha de canje", copy=False)

    @api.depends("discount_code", "discount_vigencia", "discount_redeem_date")
    def _compute_discount_state(self):
        for order in self:
            if not order.discount_code:
                order.discount_state = False
                continue
            if not order.discount_vigencia or not order.discount_redeem_date:
                order.discount_state = "vigente"
                continue
            # La fecha de canje está en UTC: se pasa al día local del cajero
            # para compararla con la vigencia (que es una fecha, sin hora).
            tz = order.user_id.tz or order.env.context.get("tz") or order.env.user.tz
            redeem_day = fields.Date.context_today(
                order.with_context(tz=tz), order.discount_redeem_date
            )
            order.discount_state = (
                "vencido" if order.discount_vigencia < redeem_day else "vigente"
            )

    @api.depends("discount_program_id", "lines.reward_id", "lines.price_subtotal_incl")
    def _compute_discount_amount(self):
        for order in self:
            program = order.discount_program_id
            if not program:
                order.discount_amount = 0.0
                continue
            reward_lines = order.lines.filtered(
                lambda line: line.reward_id and line.reward_id.program_id == program
            )
            amount = abs(sum(reward_lines.mapped("price_subtotal_incl")))
            order.discount_amount = order.currency_id.round(amount) if order.currency_id else amount

    def _order_fields(self, ui_order):
        vals = super()._order_fields(ui_order)
        # El POS siempre envía la llave (con False si no hay código), así una
        # orden en borrador que se re-sincroniza sin el código lo limpia.
        # Órdenes de clientes viejos sin la llave no se tocan.
        if "discount_code" in ui_order:
            vals.update(self._prepare_discount_code_vals(ui_order, vals.get("lines") or []))
        return vals

    @api.model
    def _prepare_discount_code_vals(self, ui_order, line_commands):
        """Valores del canje a guardar en la orden.

        Solo se registra el canje si el bono quedó aplicado de verdad: el
        programa existe, el código pertenece a ese programa y la orden trae
        al menos una línea de recompensa del programa. Si no, se guardan los
        campos vacíos. Nunca se lanza un error: un dato inválido no debe
        impedir que la orden se sincronice.
        """
        empty = {field: False for field in DISCOUNT_FIELDS if field != "discount_amount"}
        code = (ui_order.get("discount_code") or "").strip()
        if not code:
            return empty
        program = self._discount_applied_program(
            ui_order.get("discount_program_id"), code, line_commands
        )
        if not program:
            return empty
        doc_type = ui_order.get("discount_doc_type")
        if doc_type not in dict(DISCOUNT_DOC_TYPES):
            doc_type = False
        return {
            "discount_program_id": program.id,
            "discount_name": program.name,
            "discount_code": code,
            "discount_vigencia": program.date_to or False,
            "discount_doc_type": doc_type,
            "discount_doc_number": self._clean_doc_number(doc_type, ui_order.get("discount_doc_number")),
            "discount_redeem_date": self._parse_discount_redeem_date(ui_order),
        }

    @api.model
    def _discount_applied_program(self, program_id, code, line_commands):
        if not isinstance(program_id, int) or isinstance(program_id, bool):
            return self.env["loyalty.program"]
        program = self.env["loyalty.program"].sudo().browse(program_id).exists()
        if not program:
            return program
        reward_ids = {
            command[2].get("reward_id")
            for command in line_commands
            if isinstance(command, (list, tuple)) and len(command) == 3
            and isinstance(command[2], dict) and command[2].get("reward_id")
        }
        rewards = self.env["loyalty.reward"].sudo().browse(list(reward_ids)).exists()
        if program not in rewards.program_id:
            return self.env["loyalty.program"]
        code_is_rule = code in program.rule_ids.filtered(lambda r: r.mode == "with_code").mapped("code")
        code_is_card = code_is_rule or bool(
            self.env["loyalty.card"].sudo().search_count(
                [("code", "=", code), ("program_id", "=", program.id)], limit=1
            )
        )
        return program if code_is_card else self.env["loyalty.program"]

    @api.model
    def _parse_discount_redeem_date(self, ui_order):
        """El POS envía la fecha en UTC; si falta o no es válida se usa la de la orden."""
        for value in (ui_order.get("discount_redeem_date"), ui_order.get("date_order")):
            if not value:
                continue
            try:
                return fields.Datetime.to_datetime(str(value).replace("T", " ")[:19])
            except ValueError:
                continue
        return fields.Datetime.now()

    @api.model
    def _clean_doc_number(self, doc_type, doc_number):
        """Quita espacios, puntos y guiones en los tipos numéricos; nunca lanza error."""
        doc_number = (doc_number or "").strip()
        if doc_type in NUMERIC_DOC_TYPES:
            doc_number = re.sub(r"[\s.\-]", "", doc_number)
        return doc_number or False

    def write(self, vals):
        if any(field in vals for field in DISCOUNT_FIELDS):
            if any(order.state != "draft" for order in self):
                raise UserError(
                    _("Las órdenes validadas no permiten modificar los datos de descuento.")
                )
        return super().write(vals)
