from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PosSession(models.Model):
    _inherit = 'pos.session'

    queue_pending_count = fields.Integer(
        string='Cola de Inventario pendiente',
        compute='_compute_queue_pending_count',
        help='Pickings POS de esta sesión aún sin procesar en la cola de '
             'inventario (pending/processing/failed). La guarda de cierre '
             '(P0-7) impide cerrar la sesión si este contador no llega a 0.',
    )

    @api.depends('order_ids')
    def _compute_queue_pending_count(self):
        Queue = self.env['pos.inventory.queue'].sudo()
        for session in self:
            session.queue_pending_count = Queue.search_count([
                ('pos_order_id.session_id', '=', session.id),
                ('state', '!=', 'done'),
            ])

    def action_view_queue_items(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Cola de Inventario'),
            'res_model': 'pos.inventory.queue',
            'view_mode': 'tree,form',
            'domain': [
                ('pos_order_id.session_id', '=', self.id),
                ('state', '!=', 'done'),
            ],
            'context': {'search_default_needs_attention': 1},
        }

    def _validate_session(self):
        """Guarda de cierre (P0-7).

        Antes de cerrar la sesión, drena en línea los pickings POS de esa
        sesión que quedaron pendientes en la cola. Si aun así queda
        alguno sin validar, bloquea el cierre con un mensaje claro: cerrar
        con pickings pendientes dejaría el asiento de cierre y la
        conciliación de la cuenta transitoria (anglosajona) sin datos
        completos.

        Solo interviene si el módulo de cola está instalado y el
        interruptor global está activo o existe trabajo pendiente (puede
        haber backlog de cuando estaba activo).
        """
        self.ensure_one()
        Queue = self.env['pos.inventory.queue'].sudo()
        pending_before = Queue.search_count([
            ('pos_order_id.session_id', '=', self.id),
            ('state', '!=', 'done'),
        ])
        if pending_before:
            remaining = Queue._process_session_items(self)
            if remaining:
                refs = ', '.join(
                    item.picking_id.name or item.name for item in remaining
                )
                raise UserError(_(
                    "No se puede cerrar la sesión '%(session)s': quedan "
                    "%(count)s movimiento(s) de inventario sin procesar en "
                    "la cola (ref: %(refs)s). Reintenta desde "
                    "Punto de Venta > Configuración > Cola de Inventario "
                    "antes de cerrar.",
                    session=self.name,
                    count=len(remaining),
                    refs=refs,
                ))
        return super()._validate_session()
