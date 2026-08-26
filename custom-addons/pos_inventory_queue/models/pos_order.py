from odoo import models


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def _create_order_picking(self):
        self.ensure_one()
        if self.picking_ids:
            return
        if self.shipping_date:
            self.sudo().lines._launch_stock_rule_from_pos_order_lines()
        else:
            if self._should_create_picking_real_time():
                picking_type = self.config_id.picking_type_id
                if self.partner_id.property_stock_customer:
                    destination_id = self.partner_id.property_stock_customer.id
                elif not picking_type or not picking_type.default_location_dest_id:
                    destination_id = self.env['stock.warehouse']._get_partner_locations()[0].id
                else:
                    destination_id = picking_type.default_location_dest_id.id

                pickings = self.env['stock.picking'].with_context(
                    pos_inventory_queue=True
                )._create_picking_from_pos_order_lines(destination_id, self.lines, picking_type, self.partner_id)
                pickings.write({
                    'pos_session_id': self.session_id.id,
                    'pos_order_id': self.id,
                    'origin': self.name,
                })
