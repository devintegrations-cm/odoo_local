{
    "name": "Cash In/Out Message",
    "version": "17.0.2.0.0",
    "category": "Point of Sale",
    "summary": "Display a configurable message and confirmation popup in the Cash In/Out popup",
    "description": """
POS Cash In/Out Message
=======================

Adds a configurable plain-text message and a professional confirmation
popup to the Cash In/Out workflow in the Point of Sale interface.

Features:
- Configurable message displayed in the Cash In/Out popup.
- Confirmation popup before registering any cash movement.
- Shows movement type (in/out), amount, and last-movement warning.
- Touch-friendly and professional design.
""",
    "author": "Miguel Bolivar, Libertario Coffee",
    "website": "https://www.libertariocoffee.com",
    "license": "LGPL-3",
    "depends": [
        "pos_closing_validation",
    ],
    "data": [
        "views/pos_config_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "cash_in_out_message/static/src/css/cash_move_confirm_popup.css",
            "cash_in_out_message/static/src/js/cash_move_confirm_popup.js",
            "cash_in_out_message/static/src/xml/cash_move_confirm_popup.xml",
            "cash_in_out_message/static/src/js/cash_move_popup.js",
            "cash_in_out_message/static/src/xml/cash_move_popup.xml",
        ],
    },
    "installable": True,
    "application": False,
}