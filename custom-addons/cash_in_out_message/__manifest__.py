{
    "name": "POS Cash In/Out Message",
    "version": "17.0.1.0.0",
    "category": "Point of Sale",
    "summary": "Display a configurable message in the Cash In/Out popup",
    "description": """
POS Cash In/Out Message
=======================

Adds a configurable plain-text message to the Cash In/Out popup
in the Point of Sale interface.

The message is configured per POS configuration.
""",
    "author": "Miguel Bolivar, Libertario Coffee",
    "website": "https://www.libertariocoffee.com",
    "license": "LGPL-3",
    "depends": [
        "point_of_sale",
    ],
    "data": [
        "views/pos_config_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "cash_in_out_message/static/src/js/cash_move_popup.js",
            "cash_in_out_message/static/src/xml/cash_move_popup.xml",
        ],
    },
    "installable": True,
    "application": True,
}