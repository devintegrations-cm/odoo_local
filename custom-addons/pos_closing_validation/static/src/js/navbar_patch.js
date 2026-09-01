/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { Navbar } from "@point_of_sale/app/navbar/navbar";
import { CashMovePopup } from "@point_of_sale/app/navbar/cash_move_popup/cash_move_popup";
import { ClosePosPopup } from "@point_of_sale/app/navbar/closing_popup/closing_popup";
import { RescueSessionWarningPopup } from "@pos_closing_validation/js/rescue_session_warning_popup";

async function _getClosingValidationInfo(orm, posSessionId) {
    return await orm.call(
        "pos.session",
        "get_closing_validation_info",
        [[posSessionId]]
    );
}

patch(Navbar.prototype, {
    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
    },

    async onCashMoveButtonClick() {
        const info = await _getClosingValidationInfo(
            this.orm,
            this.pos.pos_session.id
        );

        // Rescue session with no orders: block everything
        if (info.is_rescue && !info.has_orders) {
            await this.popup.add(RescueSessionWarningPopup, {
                title: _t("Sesión de rescate vacía"),
                body: _t(
                    "Esta sesión de rescate no tiene órdenes registradas. " +
                    "Actualice la página para iniciar una nueva sesión."
                ),
            });
            return;
        }

        // Non-rescue session in closing/closed state: block
        if (!info.is_rescue && info.must_block) {
            await this.popup.add(RescueSessionWarningPopup, {
                title: _t("Sesión no disponible"),
                body: _t(
                    "Esta sesión ya no está disponible para operaciones de efectivo. " +
                    "Actualice la página para continuar."
                ),
            });
            return;
        }

        // Rescue session WITH orders: allow cash in/out
        this.hardwareProxy.openCashbox(_t("Cash in / out"));
        this.popup.add(CashMovePopup);
    },

    async closeSession() {
        const info = await _getClosingValidationInfo(
            this.orm,
            this.pos.pos_session.id
        );

        // Any rescue session: always block close
        if (info.is_rescue) {
            await this.popup.add(RescueSessionWarningPopup, {
                title: _t("Actualice la página"),
                body: _t(
                    "Esta sesión es de rescate y tiene órdenes pendientes. " +
                    "Actualice la página para sincronizar los datos correctamente."
                ),
            });
            return;
        }

        // Non-rescue session in closing/closed state: block
        if (info.must_block) {
            await this.popup.add(RescueSessionWarningPopup, {
                title: _t("Sesión no disponible"),
                body: _t(
                    "Esta sesión ya no está disponible para cerrar. " +
                    "Actualice la página para continuar."
                ),
            });
            return;
        }

        const closeInfo = await this.pos.getClosePosInfo();
        this.popup.add(ClosePosPopup, { ...closeInfo });
    },
});
