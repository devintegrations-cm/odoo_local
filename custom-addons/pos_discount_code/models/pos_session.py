from odoo import models
from .pos_order import NUMERIC_DOC_TYPES

DOC_TYPES_KEY = "pos_discount_code.doc_types"


class PosSession(models.Model):
    _inherit = "pos.session"

    def _pos_data_process(self, loaded_data):
        """Envía al POS los tipos de documento del canje.

        La lista vive en un solo lugar: el Selection de
        ``pos.order.discount_doc_type`` (``DISCOUNT_DOC_TYPES`` en
        pos_order.py). El popup la lee de aquí al abrir la sesión, así que
        agregar o quitar un tipo es un cambio solo en Python.
        """
        super()._pos_data_process(loaded_data)
        field = self.env["pos.order"]._fields["discount_doc_type"]
        loaded_data[DOC_TYPES_KEY] = [
            {"value": value, "label": label, "numeric": value in NUMERIC_DOC_TYPES}
            for value, label in field._description_selection(self.env)
        ]
