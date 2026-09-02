{
    "name": "Pos Closing Validation",
    "version": "17.0.4.1.0",
    "category": "Point of Sale",
    "summary": "Controls POS cash movements and session closing",
    "description": """
POS Closing Validation
======================

Extends the standard Odoo Point of Sale cash control workflow with:

- Configurable maximum Cash In/Out movements per POS.
- Backend validation of the cash movement limit.
- Cash In/Out blocked on rescue sessions (backend + frontend).
- Last-movement warning in the Cash In/Out popup.
- Cash movement counter in the popup.
- Maximum authorized cash difference validation when closing a session.
- Opening balance continuity audit between sessions.
- Blocking new session opening when rescue sessions are pending.
- Unified closing snapshot (single source of truth).
- Transactional closing with row-level lock to prevent race conditions.
- Enhanced closing popup with explicit expected cash summary.
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
            "pos_closing_validation/static/src/css/error_popup.css",
            "pos_closing_validation/static/src/js/cash_move_warning_popup.js",
            "pos_closing_validation/static/src/xml/cash_move_warning_popup.xml",
            "pos_closing_validation/static/src/js/rescue_session_warning_popup.js",
            "pos_closing_validation/static/src/xml/rescue_session_warning_popup.xml",
            "pos_closing_validation/static/src/js/cash_move_popup.js",
            "pos_closing_validation/static/src/xml/cash_move_popup.xml",
            "pos_closing_validation/static/src/js/closing_popup_patch.js",
            "pos_closing_validation/static/src/xml/closing_popup_extension.xml",
            "pos_closing_validation/static/src/js/navbar_patch.js",
        ],
    },
    "installable": True,
    "application": False,
}
