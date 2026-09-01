/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { AbstractAwaitablePopup } from "@point_of_sale/app/popup/abstract_awaitable_popup";

export class RescueSessionWarningPopup extends AbstractAwaitablePopup {
    static template = "pos_closing_validation.RescueSessionWarningPopup";
    static defaultProps = {
        title: _t("Sesión no disponible"),
        body: _t("Actualice la página para continuar con esta operación."),
    };

    onReload() {
        window.location.reload();
    }
}
