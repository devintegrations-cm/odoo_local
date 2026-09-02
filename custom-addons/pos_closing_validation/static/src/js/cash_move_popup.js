/** @odoo-module */

import { onWillStart } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

import { CashMovePopup } from "@point_of_sale/app/navbar/cash_move_popup/cash_move_popup";
import { CashMoveWarningPopup } from "@pos_closing_validation/js/cash_move_warning_popup";

patch(CashMovePopup.prototype, {
    setup() {
        super.setup(...arguments);

        this.cashMoveControl = {
            count: 0,
            limit: this.pos.config.maximum_cash_in_out_moves || 0,
        };

        this.closingValidation = {
            is_rescue: false,
            expected_cash: 0,
            cash_move_count: 0,
        };

        this.state.isLimitReached = false;
        this.state.loadError = false;

        onWillStart(async () => {
            try {
                const [controlData, closingInfo] = await Promise.all([
                    this.orm.call(
                        "pos.session",
                        "get_cash_in_out_control_data",
                        [[this.pos.pos_session.id]]
                    ),
                    this.orm.call(
                        "pos.session",
                        "get_closing_validation_info",
                        [[this.pos.pos_session.id]]
                    ),
                ]);

                this.cashMoveControl = controlData;
                this.closingValidation = closingInfo;

                this.state.isLimitReached =
                    this.cashMoveControl.count >= this.cashMoveControl.limit;
            } catch (error) {
                // RPC failed — block the popup to prevent unsafe operations
                this.state.loadError = true;
                this.state.isLimitReached = true;
            }
        });
    },

    _isLastMovement() {
        const count = this.cashMoveControl.count;
        const limit = this.cashMoveControl.limit;
        return count + 1 === limit;
    },

    async confirm() {
        if (this.state.isLimitReached) {
            return;
        }

        if (this._isLastMovement() && !this._skipCashMoveWarning) {
            const { confirmed } = await this.popup.add(
                CashMoveWarningPopup,
                {
                    title: _t("Último movimiento de efectivo"),
                    body: _t(
                        "Este será el último movimiento Cash In/Out permitido para esta sesión.\n\n"
                        + "Movimientos: %(current)s/%(limit)s\n\n"
                        + "¿Desea continuar?",
                        {
                            current: this.cashMoveControl.count + 1,
                            limit: this.cashMoveControl.limit,
                        }
                    ),
                    confirmLabel: _t("Confirmar"),
                    cancelLabel: _t("Cancelar"),
                }
            );

            if (!confirmed) {
                return;
            }
        }

        return super.confirm();
    },
});
