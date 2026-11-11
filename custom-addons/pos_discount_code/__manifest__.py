{
    "name": "POS Discount Code",
    "version": "17.0.1.2.0",
    "category": "Point of Sale",
    "summary": "Capture discount codes on POS orders, also save discount information in the order for traceability.",
    "description": """
POS Discount Code
=================

Allows cashiers to apply a discount code (bono) from the POS order screen,
capturing the document type, document number and the code itself.

The discount is applied through the native loyalty machinery and the
captured data is stored on the order for traceability.
""",
    "author": "Miguel Bolivar, Libertario Coffee",
    "license": "LGPL-3",
    "depends": [
        "point_of_sale",
        "pos_loyalty",
    ],
    "data": [
        "views/pos_config_views.xml",
        "views/pos_order_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_discount_code/static/src/js/loyalty_adapter.js",
            "pos_discount_code/static/src/js/discount_code_popup.js",
            "pos_discount_code/static/src/js/discount_code_button.js",
            "pos_discount_code/static/src/xml/discount_code_popup.xml",
            "pos_discount_code/static/src/xml/hide_promo_code.xml",
        ],
    },
    "installable": True,
    "application": False,
}
