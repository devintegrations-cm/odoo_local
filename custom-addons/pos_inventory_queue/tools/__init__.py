"""Herramientas de testing para pos_inventory_queue.

Estos scripts se ejecutan FUERA del entorno de tests de Odoo
(odoo-bin --test-enable). Son herramientas manuales para validar
concurrencia real con multiprocessing/threads.

Uso:
    python3 tools/test_pos_inventory_concurrency.py --config /etc/odoo/odoo.conf --db pruebas
    python3 tools/test_pos_invoice_concurrency.py --config /etc/odoo/odoo.conf --db pruebas --workers 50

Ver README.md > Herramientas de Testing para documentación completa.
"""
