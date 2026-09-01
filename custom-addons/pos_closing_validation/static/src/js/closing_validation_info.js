/** @odoo-module */

import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

export class ClosingValidationInfo extends Component {
    setup() {
        this.info = this.props.info || {};
    }

    get isRescue() {
        return this.info.is_rescue || false;
    }

    get rescueLabel() {
        return this.info.parent_session_name
            ? _t("RESCATE DE %(parent)s", { parent: this.info.parent_session_name })
            : _t("SESIÓN DE RECUPERACIÓN");
    }

    get expectedCash() {
        return this.env.utils.formatCurrency(this.info.expected_cash || 0);
    }

    get openingCash() {
        return this.env.utils.formatCurrency(this.info.opening_cash || 0);
    }

    get cashSales() {
        return this.env.utils.formatCurrency(this.info.cash_sales || 0);
    }

    get cashIn() {
        return this.env.utils.formatCurrency(this.info.cash_in || 0);
    }

    get cashOut() {
        return this.env.utils.formatCurrency(this.info.cash_out || 0);
    }

    get cashMoveCount() {
        return this.info.cash_move_count || 0;
    }
}

ClosingValidationInfo.template = "pos_closing_validation.ClosingValidationInfo";
