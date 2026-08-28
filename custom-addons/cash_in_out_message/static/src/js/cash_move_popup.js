/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { CashMovePopup } from "@point_of_sale/app/navbar/cash_move_popup/cash_move_popup";
import { patch } from "@web/core/utils/patch";
import { CashMoveConfirmPopup } from "@cash_in_out_message/js/cash_move_confirm_popup";

patch(CashMovePopup.prototype, {
    setup() {
        super.setup(...arguments);

        this.cashInOutMessage = this.pos.config.cash_in_out_message_enabled
            ? (this.pos.config.cash_in_out_message || "")
            : "";
    },

    async confirm() {
        if (this.state.isLimitReached) {
            return;
        }

        const amount = parseFloat(this.state.amount);
        if (!amount || amount === 0) {
            return super.confirm();
        }

        const type = this.state.type;
        const isLastMovement = this._isLastMovement
            ? this._isLastMovement()
            : false;

        const formattedAmount = this.env.utils.formatCurrency(amount);

        const { confirmed } = await this.popup.add(
            CashMoveConfirmPopup,
            {
                title: _t("Confirmar movimiento"),
                type: type,
                amount: amount,
                formattedAmount: formattedAmount,
                isLastMovement: isLastMovement,
                configMessage: this.cashInOutMessage,
                confirmLabel: _t("Sí, registrar"),
                cancelLabel: _t("Cancelar"),
            }
        );

        if (confirmed) {
            this._skipCashMoveWarning = true;
            await super.confirm();
            this._skipCashMoveWarning = false;
        }
    },
});