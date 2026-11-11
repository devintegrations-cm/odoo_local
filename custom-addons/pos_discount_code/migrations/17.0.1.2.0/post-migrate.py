from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """discount_state pasó a guardarse con el estado AL CANJEAR: recalcularlo en las órdenes con canje."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    orders = env["pos.order"].search([("discount_code", "!=", False)])
    if orders:
        env.add_to_compute(orders._fields["discount_state"], orders)
        orders.flush_recordset(["discount_state"])
