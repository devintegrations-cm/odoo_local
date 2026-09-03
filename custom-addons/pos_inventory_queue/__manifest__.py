{
    'name': 'Pos Inventory Queue',
    'version': '17.0.1.2.1',
    'category': 'Point of Sale',
    'summary': 'Serializes POS real-time inventory operations to prevent concurrency',
    'description': """
        When multiple POS terminals invoice simultaneously with real-time inventory,
        the system processes stock.quant updates concurrently causing deadlocks and
        race conditions.

        This module intercepts the picking creation flow for POS real-time pickings
        and serializes their processing through a persistent queue using
        PostgreSQL FOR UPDATE SKIP LOCKED for atomic claim.
    """,
    "author": "Miguel Bolivar, Libertario Coffee",
    "website": "https://www.libertariocoffee.com",
    "license": "LGPL-3",
    'depends': ['point_of_sale'],
    'data': [
        'data/ir_sequence.xml',
        'data/ir_cron.xml',
        'data/ir_config_parameter.xml',
        'security/ir.model.access.csv',
        'views/pos_inventory_queue_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
