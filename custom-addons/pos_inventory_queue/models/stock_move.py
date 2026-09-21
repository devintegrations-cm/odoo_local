import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _get_related_invoices(self):
        """Enlaza la factura de POS al movimiento de inventario.

        Con contabilidad anglosajona + valoración en tiempo real, la
        factura del pedido POS se asienta ANTES de que el picking se
        valide (la cola lo procesa después). Cuando el picking se valida,
        stock_valuation_layer._validate_accounting_entries() llama a
        moves._get_related_invoices() para conciliar la cuenta transitoria
        de salida. El core de POS solo devuelve los movimientos ya 'done'
        al momento de la factura, así que sin este override la transitoria
        quedaría sin conciliar para siempre.

        Mismo patrón que sale_stock: sumar la factura posted ligada al
        pedido POS del picking. Cubre factura y nota de crédito
        (devoluciones), porque ambos cuelgan de pos_order.account_move.

        DEFENSA: stock_account define _get_related_invoices(); si la
        cadena de herencia cambia en futuras versiones de Odoo, este
        fallback evita AttributeError silencioso.
        """
        parent = getattr(super(), '_get_related_invoices', None)
        if parent is None:
            _logger.debug(
                'pos_inventory_queue: _get_related_invoices no existe '
                'en super(); devolviendo vacío'
            )
            invoices = self.env['account.move']
        else:
            invoices = parent()
        pos_invoices = self.mapped(
            'picking_id.pos_order_id.account_move'
        ).filtered(lambda m: m.state == 'posted')
        return invoices | pos_invoices
