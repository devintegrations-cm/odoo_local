/** @odoo-module **/

import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { Order } from "@point_of_sale/app/store/models";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { patch } from "@web/core/utils/patch";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { useService } from "@web/core/utils/hooks";
import { Component } from "@odoo/owl";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";
import { _t } from "@web/core/l10n/translation";
import { DiscountCodePopup } from "./discount_code_popup";
import { checkLoyaltyCompatibility, getProgramDiscountAmount } from "./loyalty_adapter";

const { DateTime } = luxon;

export class DiscountCodeButton extends Component {
    static template = "pos_discount_code.DiscountCodeButton";

    setup() {
        this.pos = usePos();
        this.popup = useService("popup");
    }

    async click() {
        // Si una actualización de Odoo cambió pos_loyalty, se detiene aquí con
        // un aviso claro en vez de aplicar descuentos a medias.
        const problems = checkLoyaltyCompatibility(this.pos.get_order());
        if (problems.length) {
            console.error("[pos_discount_code] pos_loyalty incompatible:", problems);
            await this.popup.add(ErrorPopup, {
                title: _t("Código de descuento no disponible"),
                body: _t(
                    "Este botón no es compatible con la versión actual de la lealtad de Odoo. " +
                        "Avise a soporte. Mientras tanto, un administrador puede desmarcar " +
                        "“Código de descuento (bono)” en los Ajustes del punto de venta para " +
                        "volver al botón de códigos de Odoo.\n\nDetalle: %s",
                    problems.join("; ")
                ),
            });
            return;
        }
        await this.popup.add(DiscountCodePopup, {});
    }
}

patch(PosStore.prototype, {
    /**
     * Tipos de documento del canje, enviados por el servidor
     * (pos.session._pos_data_process). La única fuente es el Selection de
     * pos.order.discount_doc_type en Python.
     */
    async _processData(loadedData) {
        await super._processData(...arguments);
        this.discountDocTypes = loadedData["pos_discount_code.doc_types"] || [];
    },
});

patch(Order.prototype, {
    setup(_defaultObj, options) {
        super.setup(...arguments);
        // super.setup() ya ejecuta init_from_JSON() cuando la orden se
        // restaura; solo se inicializan los campos en órdenes nuevas.
        if (!options?.json) {
            this.clearDiscountCode();
        }
    },
    /**
     * Valor actual del descuento: suma de las líneas de recompensa del
     * programa del código. Se recalcula cada vez porque el cajero puede
     * cambiar cantidades o productos después de aplicar el código.
     */
    getDiscountCodeAmount() {
        return getProgramDiscountAmount(this, this.discount_program_id);
    },
    /** Datos del canje en la orden (los valida el servidor al sincronizar). */
    setDiscountCode({ program, code, docType, docNumber }) {
        this.discount_program_id = program.id;
        this.discount_name = program.name;
        this.discount_code = code;
        this.discount_amount = this.getDiscountCodeAmount();
        this.discount_vigencia = program.date_to ? program.date_to.toFormat("yyyy-MM-dd") : false;
        this.discount_doc_type = docType;
        this.discount_doc_number = docNumber;
        // Odoo almacena los Datetime en UTC.
        this.discount_redeem_date = DateTime.now().toUTC().toFormat("yyyy-MM-dd HH:mm:ss");
    },
    clearDiscountCode() {
        this.discount_program_id = false;
        this.discount_name = false;
        this.discount_code = false;
        this.discount_amount = 0;
        this.discount_vigencia = false;
        this.discount_doc_type = false;
        this.discount_doc_number = false;
        this.discount_redeem_date = false;
    },
    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        // Las llaves se envían siempre (con false si no hay código): así el
        // servidor limpia el canje de una orden en borrador si se quitó el código.
        json.discount_program_id = this.discount_program_id || false;
        json.discount_name = this.discount_name || false;
        json.discount_code = this.discount_code || false;
        json.discount_amount = this.getDiscountCodeAmount();
        json.discount_vigencia = this.discount_vigencia || false;
        json.discount_doc_type = this.discount_doc_type || false;
        json.discount_doc_number = this.discount_doc_number || false;
        json.discount_redeem_date = this.discount_redeem_date || false;
        return json;
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.discount_program_id = json.discount_program_id || false;
        this.discount_name = json.discount_name || false;
        this.discount_code = json.discount_code || false;
        this.discount_amount = json.discount_amount || 0;
        this.discount_vigencia = json.discount_vigencia || false;
        this.discount_doc_type = json.discount_doc_type || false;
        this.discount_doc_number = json.discount_doc_number || false;
        this.discount_redeem_date = json.discount_redeem_date || false;
    },
});

ProductScreen.addControlButton({
    component: DiscountCodeButton,
    condition: function () {
        return (
            this.pos.config.show_discount_code_button &&
            this.pos.programs.some((p) =>
                ["coupons", "promotion", "gift_card", "promo_code", "next_order_coupons"].includes(
                    p.program_type
                )
            )
        );
    },
});
