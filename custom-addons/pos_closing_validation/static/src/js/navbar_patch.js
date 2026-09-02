/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { Navbar } from "@point_of_sale/app/navbar/navbar";
import { CashMovePopup } from "@point_of_sale/app/navbar/cash_move_popup/cash_move_popup";
import { ClosePosPopup } from "@point_of_sale/app/navbar/closing_popup/closing_popup";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { RescueSessionWarningPopup } from "@pos_closing_validation/js/rescue_session_warning_popup";

const CLOSE_POS_POPUP_ALLOWED_PROPS = [
    "orders_details",
    "opening_notes",
    "default_cash_details",
    "other_payment_methods",
    "is_manager",
    "amount_authorized_diff",
];

function _pickClosePosProps(info) {
    const props = {};
    for (const key of CLOSE_POS_POPUP_ALLOWED_PROPS) {
        if (key in info) {
            props[key] = info[key];
        }
    }
    return props;
}

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
        let info;
        try {
            info = await _getClosingValidationInfo(
                this.orm,
                this.pos.pos_session.id
            );
        } catch (error) {
            await this.popup.add(ErrorPopup, {
                title: _t("Error de conexión"),
                body: _t(
                    "No se pudo obtener la información de la sesión. " +
                    "Verifique su conexión e intente de nuevo."
                ),
            });
            return;
        }

        if (!info) {
            await this.popup.add(ErrorPopup, {
                title: _t("Error"),
                body: _t(
                    "No se pudo obtener la información de la sesión. " +
                    "Intente de nuevo."
                ),
            });
            return;
        }

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

        this.hardwareProxy.openCashbox(_t("Cash in / out"));
        this.popup.add(CashMovePopup);
    },

    async closeSession() {
        let info;
        try {
            info = await this.pos.getClosePosInfo();
        } catch (error) {
            await this.popup.add(ErrorPopup, {
                title: _t("Error de conexión"),
                body: _t(
                    "No se pudo obtener la información de cierre. " +
                    "Verifique su conexión e intente de nuevo."
                ),
            });
            return;
        }

        if (!info || !info.default_cash_details) {
            await this.popup.add(ErrorPopup, {
                title: _t("Error"),
                body: _t(
                    "No se pudo obtener la información de cierre. " +
                    "Intente de nuevo."
                ),
            });
            return;
        }

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

        if (!info.can_close) {
            const reasons = info.blocking_reasons || [];
            await this.popup.add(RescueSessionWarningPopup, {
                title: _t("No se puede cerrar la sesión"),
                body: reasons.join("\n") || _t(
                    "Esta sesión no puede cerrarse en este momento."
                ),
            });
            return;
        }

        this.popup.add(ClosePosPopup, _pickClosePosProps(info));
    },
});
