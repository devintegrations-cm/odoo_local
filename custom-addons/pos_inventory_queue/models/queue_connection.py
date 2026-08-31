import atexit
import logging
import os
import threading

from odoo.sql_db import Connection, ConnectionPool, connection_info_for
from psycopg2.pool import PoolError

_logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Pool de conexiones PROPIO y aislado de la cola.
#
# No comparte el ConnectionPool global de Odoo (el que respeta db_maxconn),
# de modo que el procesamiento de la cola NUNCA compite por esas conexiones
# ni provoca PoolError bajo alta concurrencia de workers del POS.
#
# Tamaño pequeño y configurable (ver _sync_maxconn_from_param / env
# POS_QUEUE_POOL_MAXCONN). No se sube a 16/32: el objetivo es aislar la cola,
# no crear otro cuello de botella.
# -----------------------------------------------------------------------------

_QUEUE_POOL = None
_QUEUE_POOL_LOCK = threading.Lock()
_QUEUE_INFO = {}  # dbname -> connection_info (cacheado)
_QUEUE_MAXCONN_DEFAULT = int(os.environ.get('POS_QUEUE_POOL_MAXCONN') or 8)
_PARAM_SYNCED = {}


def _queue_connection_info(dbname):
    info = _QUEUE_INFO.get(dbname)
    if info is None:
        _, info = connection_info_for(dbname)
        _QUEUE_INFO[dbname] = info
    return info


def _get_pool(dbname):
    global _QUEUE_POOL
    if _QUEUE_POOL is None:
        with _QUEUE_POOL_LOCK:
            if _QUEUE_POOL is None:
                _QUEUE_POOL = ConnectionPool(_QUEUE_MAXCONN_DEFAULT)
                atexit.register(_close_queue_pool)
    return _QUEUE_POOL


def _sync_maxconn_from_param(env):
    """Ajusta el tope del pool desde ir.config_parameter (solo hacia arriba).

    Se ejecuta una vez por base usando el cursor ya abierto del pool, para
    no necesitar una conexión adicional solo para leer la configuración.
    """
    db = env.cr.dbname
    if _PARAM_SYNCED.get(db):
        return
    try:
        val = env['ir.config_parameter'].sudo().get_param(
            'pos_inventory_queue.pool_maxconn', '0'
        )
        val = int(val or 0)
        if val > 0 and _QUEUE_POOL is not None:
            _QUEUE_POOL._maxconn = max(_QUEUE_POOL._maxconn, val)
    except Exception:
        _logger.debug(
            'POS Queue: no se pudo leer pool_maxconn',
            exc_info=True,
        )
    _PARAM_SYNCED[db] = True


def queue_get_cursor(dbname, uid, context):
    """Abre un cursor Odoo desde el POOL DEDICADO de la cola.

    Lanza psycopg2.pool.PoolError si el pool está lleno. El llamador decide
    el comportamiento:
      - hook post-commit: best-effort -> return, el cron recupera.
      - procesamiento de item: el item debe revertirse a 'pending' para que
        el cron lo retome (ver inventory_queue._process_queue).
    """
    from odoo import api, SUPERUSER_ID

    info = _queue_connection_info(dbname)
    pool = _get_pool(dbname)
    conn = Connection(pool, dbname, info)
    cr = conn.cursor()
    env = api.Environment(cr, uid or SUPERUSER_ID, context or {})
    _sync_maxconn_from_param(env)
    return cr, env


def queue_put_cursor(cr):
    """Devuelve el cursor (y su conexión) al pool dedicado."""
    try:
        cr.close()
    except Exception:
        _logger.debug(
            'POS Queue: error al cerrar cursor de cola',
            exc_info=True,
        )


def _close_queue_pool():
    global _QUEUE_POOL
    pool = _QUEUE_POOL
    _QUEUE_POOL = None
    if pool is not None:
        try:
            pool.close_all()
        except Exception:
            _logger.debug(
                'POS Queue: error al cerrar pool de cola',
                exc_info=True,
            )
