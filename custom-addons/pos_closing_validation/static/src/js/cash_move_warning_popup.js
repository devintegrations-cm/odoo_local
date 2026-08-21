/** @odoo-module */

import { AbstractAwaitablePopup } from "@point_of_sale/app/popup/abstract_awaitable_popup";

export class CashMoveWarningPopup extends AbstractAwaitablePopup {
    static template = "pos_closing_validation.CashMoveWarningPopup";
}
