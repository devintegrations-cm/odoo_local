import hashlib
import logging

from odoo import models

_logger = logging.getLogger(__name__)

# Prefijo estable para derivar la clave del advisory lock de numeración.
_SEQ_LOCK_PREFIX = 'pos.inventory.queue.account.move.seq'


class AccountMove(models.Model):
    _inherit = 'account.move'

    def _set_next_sequence(self):
        self.ensure_one()
        # Odoo asigna el número definitivo de cada factura/asiento aquí,
        # en un único punto de todo el módulo 'account'. Bajo facturación
        # concurrente (p. ej. varios POS generando facturas a la vez) dos
        # transacciones leen el mismo "último número" y calculan el mismo
        # siguiente, chocando con la restricción única
        # (account_move_unique_name sobre name + journal_id).
        #
        # Odoo 17 ya lo corrige por sí solo (bucle con savepoint que
        # reintenta con el siguiente número ante UniqueViolation), pero
        # eso genera ruido en el log ("duplicate key ...") y reintentos
        # innecesarios. Este advisory lock transaccional serializa la
        # asignación del número POR JOURNAL: el segundo proceso espera al
        # primero y asigna el siguiente número directamente, sin colisión
        # ni reintento.
        #
        # pg_advisory_xact_lock se libera automáticamente al commit o
        # rollback de la transacción, de modo que no deja locks colgados.
        # Solo serializa el instante de numerar (microsegundos), no el
        # resto de la transacción ni la cola de inventario.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (self._pos_inventory_queue_seq_lock_key(),),
        )
        return super(AccountMove, self)._set_next_sequence()

    def _pos_inventory_queue_seq_lock_key(self):
        """Clave de advisory lock estable por journal.

        Se deriva en 60 bits (15 hex) sobre un prefijo propio y distinto
        del de la cola (ADVISORY_LOCK_KEY usa 32 bits), de modo que el
        lock de numeración nunca colisiona con el de procesamiento de la
        cola y queda dentro del rango de bigint positivo de PostgreSQL
        (pg_advisory_xact_lock acepta bigint, 64 bits con signo).
        """
        return int(
            hashlib.sha1(
                ('%s.%s' % (_SEQ_LOCK_PREFIX, self.journal_id.id)).encode()
            ).hexdigest()[:15],
            16,
        )
