/** @odoo-module */

import { AbstractAwaitablePopup } from "@point_of_sale/app/popup/abstract_awaitable_popup";

export class RescueSessionWarningPopup extends AbstractAwaitablePopup {
    static template = "pos_closing_validation.RescueSessionWarningPopup";

    onReload() {
        window.location.reload();
    }
}
