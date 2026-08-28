/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { AbstractAwaitablePopup } from "@point_of_sale/app/popup/abstract_awaitable_popup";

export class CashMoveConfirmPopup extends AbstractAwaitablePopup {
    static template = "cash_in_out_message.CashMoveConfirmPopup";

    setup() {
        super.setup(...arguments);
        this.type = this.props.type || "out";
        this.amount = this.props.amount || 0;
        this.formattedAmount = this.props.formattedAmount || "";
        this.isLastMovement = this.props.isLastMovement || false;
        this.configMessage = this.props.configMessage || "";
    }

    get movementTypeLabel() {
        return this.type === "in" ? _t("entrada de efectivo") : _t("salida de efectivo");
    }

    get movementTypeClass() {
        return this.type === "in" ? "cash-in" : "cash-out";
    }

    get iconClass() {
        return this.type === "in"
            ? "fa fa-arrow-down"
            : "fa fa-arrow-up";
    }
}
