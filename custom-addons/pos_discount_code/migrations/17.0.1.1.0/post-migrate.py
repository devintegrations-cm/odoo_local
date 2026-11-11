from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """discount_amount pasó a calculado en el servidor: recalcularlo en las órdenes existentes."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    orders = env["pos.order"].search([("discount_program_id", "!=", False)])
    if orders:
        env.add_to_compute(orders._fields["discount_amount"], orders)
        orders.flush_recordset(["discount_amount"])
