from odoo import api, models, fields
from odoo.tools.misc import str2bool


class PosInventoryQueueConfig(models.TransientModel):
    _name = 'pos.inventory.queue.config'
    _description = 'POS Inventory Queue Configuration'

    pos_inventory_queue_enabled = fields.Boolean(
        string="POS Inventory Queue",
        default=True,
        help="Interruptor GLOBAL de la cola de inventario del POS.",
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'pos_inventory_queue_enabled' in fields_list:
            res['pos_inventory_queue_enabled'] = str2bool(
                self.env['ir.config_parameter'].sudo().get_param(
                    'pos_inventory_queue.enabled', default='True'),
                default=True,
            )
        return res

    def write(self, vals):
        res = super().write(vals)
        if 'pos_inventory_queue_enabled' in vals:
            ICP = self.env['ir.config_parameter'].sudo()
            value = 'True' if self.pos_inventory_queue_enabled else 'False'
            row = ICP.search(
                [('key', '=', 'pos_inventory_queue.enabled')], limit=1)
            if row:
                row.value = value
            else:
                ICP.set_param('pos_inventory_queue.enabled', value)
        return res

    def action_save(self):
        self.ensure_one()
        ICP = self.env['ir.config_parameter'].sudo()
        value = 'True' if self.pos_inventory_queue_enabled else 'False'
        row = ICP.search(
            [('key', '=', 'pos_inventory_queue.enabled')], limit=1)
        if row:
            row.value = value
        else:
            ICP.set_param('pos_inventory_queue.enabled', value)
        return {'type': 'ir.actions.act_window_close'}
