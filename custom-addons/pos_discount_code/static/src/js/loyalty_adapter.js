/** @odoo-module **/

/**
 * Adaptador de pos_loyalty (Odoo 17).
 *
 * Único archivo del módulo que accede al estado interno de pos_loyalty
 * en la orden (`codeActivatedProgramRules`, `codeActivatedCoupons`,
 * `couponPointChanges`, `disabledRewards`) y sus métodos internos
 * (`_activateCode`, `_applyReward`, `_updateRewards`...). El popup y el resto
 * del módulo solo llaman las funciones exportadas aquí.
 *
 * Ese estado no es una interfaz pública y puede cambiar entre versiones de
 * Odoo. `checkLoyaltyCompatibility` verifica su estructura antes de abrir el
 * popup y detiene el flujo si no coincide.
 *
 * Al migrar a Odoo 19 este archivo se reescribe contra el nuevo pos_loyalty;
 * la firma de las funciones exportadas puede mantenerse.
 */

import { _t } from "@web/core/l10n/translation";

// ---------------------------------------------------------------- contrato

/** Métodos de la orden (pos_loyalty) que usa este adaptador. */
const ORDER_METHODS = [
    "_activateCode",
    "_applyReward",
    "_updateRewards",
    "_updateLoyaltyPrograms",
    "getClaimableRewards",
    "_get_reward_lines",
    "get_orderlines",
];

/** Estado interno de la orden (pos_loyalty) y la forma que debe tener. */
const ORDER_STATE = {
    codeActivatedProgramRules: (v) => Array.isArray(v),
    codeActivatedCoupons: (v) => Array.isArray(v),
    couponPointChanges: (v) => Boolean(v) && typeof v === "object",
    disabledRewards: (v) => v instanceof Set,
};

/** Datos de lealtad cargados en el PosStore. */
const POS_STATE = {
    rules: (v) => Array.isArray(v),
    program_by_id: (v) => Boolean(v) && typeof v === "object",
    reward_by_id: (v) => Boolean(v) && typeof v === "object",
};

/**
 * Revisa que pos_loyalty tenga la forma que este adaptador espera.
 * @returns {string[]} lista de problemas; vacía si todo es compatible.
 */
export function checkLoyaltyCompatibility(order) {
    const problems = [];
    if (!order) {
        return [_t("No hay una orden abierta.")];
    }
    for (const method of ORDER_METHODS) {
        if (typeof order[method] !== "function") {
            problems.push(`Order.${method}() no existe`);
        }
    }
    for (const [key, isValid] of Object.entries(ORDER_STATE)) {
        if (!isValid(order[key])) {
            problems.push(`Order.${key} no tiene la forma esperada`);
        }
    }
    for (const [key, isValid] of Object.entries(POS_STATE)) {
        if (!isValid(order.pos?.[key])) {
            problems.push(`pos.${key} no tiene la forma esperada`);
        }
    }
    return problems;
}

// ------------------------------------------------------------- consultas

/** Regla con código del programa (promociones). Igual criterio que pos_loyalty. */
export function findCodeRule(order, code) {
    return order.pos.rules.find(
        (rule) => rule.mode === "with_code" && (rule.promo_barcode === code || rule.code === code)
    );
}

/**
 * Programa del código si ya está activado en la orden (por regla o por
 * cupón/tarjeta), o null.
 * @returns {{program: object, couponId: number|false}|null}
 */
export function getActivatedProgram(order, code) {
    const rule = findCodeRule(order, code);
    if (rule && order.codeActivatedProgramRules.includes(rule.id)) {
        return { program: rule.program_id, couponId: false };
    }
    const coupon = order.codeActivatedCoupons.find((c) => c.code === code);
    if (coupon) {
        const program = order.pos.program_by_id[coupon.program_id];
        return program ? { program, couponId: coupon.id } : null;
    }
    return null;
}

/** Líneas de recompensa (descuento) de un programa en la orden. */
export function getProgramRewardLines(order, programId) {
    return order.get_orderlines().filter((line) => {
        if (!line.is_reward_line || !line.reward_id) {
            return false;
        }
        const reward = order.pos.reward_by_id[line.reward_id];
        return reward?.program_id?.id === programId;
    });
}

export function programHasRewardLine(order, programId) {
    return getProgramRewardLines(order, programId).length > 0;
}

/** Valor (positivo, con impuestos) que descuenta un programa en la orden. */
export function getProgramDiscountAmount(order, programId) {
    if (!programId) {
        return 0;
    }
    const total = getProgramRewardLines(order, programId).reduce(
        (sum, line) => sum + line.get_price_with_tax(),
        0
    );
    return Math.abs(total);
}

/**
 * Valida un cupón / tarjeta de regalo contra el servidor SIN gastarlo
 * (pos.config.use_coupon_code solo consulta; el cupón se gasta al cobrar).
 * @returns {Promise<{program: object, points: number}|{error: string}>}
 */
export async function checkCouponCode(orm, order, code) {
    let result;
    try {
        result = await orm.call("pos.config", "use_coupon_code", [
            [order.pos.config.id],
            code,
            order.date_order,
            order.get_partner()?.id || false,
            order.pricelist?.id || false,
        ]);
    } catch {
        return { error: _t("No se pudo validar el código: revise la conexión e intente de nuevo.") };
    }
    if (!result.successful) {
        return { error: result.payload.error_message || _t("Este código no es válido.") };
    }
    const program = order.pos.program_by_id[result.payload.program_id];
    if (!program) {
        return { error: _t("El programa de este código no está disponible en este punto de venta.") };
    }
    return { program, points: result.payload.points };
}

// ------------------------------------------------------------- acciones

/**
 * Activa un código en la orden y devuelve su programa.
 * - Si el código ya estaba activado pero el cajero quitó su descuento, vuelve
 *   a aplicar la recompensa (para que el cliente no pierda el bono).
 * - Si no, usa `_activateCode` de pos_loyalty (regla o cupón).
 * @returns {Promise<{program: object}|{error: string}>}
 */
export async function activateCode(order, code) {
    const activated = getActivatedProgram(order, code);
    if (activated) {
        if (!programHasRewardLine(order, activated.program.id)) {
            const reapplied = await reenableActivatedProgram(order, activated);
            if (reapplied !== true) {
                return { error: reapplied };
            }
        }
        return { program: activated.program };
    }
    const res = await order._activateCode(code);
    // `_activateCode` puede devolver un mensaje aunque el código haya quedado
    // activado (p.ej. saldo de tarjeta de regalo): se decide por el estado.
    const nowActivated = getActivatedProgram(order, code);
    if (!nowActivated) {
        return { error: (res !== true && res) || _t("No fue posible aplicar el código.") };
    }
    return { program: nowActivated.program };
}

/**
 * Quita un código activado y sus líneas de descuento de la orden.
 * pos_loyalty no tiene un método público para desactivar un código: se
 * deshace lo que hizo `_activateCode` y se recalculan las recompensas.
 */
export async function deactivateCode(order, code, programId) {
    const rule = code ? findCodeRule(order, code) : null;
    if (rule) {
        const index = order.codeActivatedProgramRules.indexOf(rule.id);
        if (index >= 0) {
            order.codeActivatedProgramRules.splice(index, 1);
        }
    }
    const couponIndex = code ? order.codeActivatedCoupons.findIndex((c) => c.code === code) : -1;
    if (couponIndex >= 0) {
        const [coupon] = order.codeActivatedCoupons.splice(couponIndex, 1);
        delete order.couponPointChanges[coupon.id];
    }
    const rewardLines = getProgramRewardLines(order, programId);
    if (rewardLines.length) {
        order.orderlines.remove(rewardLines);
    }
    await order._updateLoyaltyPrograms();
    order._updateRewards();
}

/**
 * Vuelve a aplicar la recompensa de un código activado cuyo descuento quitó el
 * cajero (pos_loyalty la deja en `disabledRewards`). Replica lo que hace
 * `_activateCode` al activar un código: si hay una sola recompensa
 * reclamable, la aplica.
 * @returns {Promise<true|string>} true o el mensaje de error.
 */
async function reenableActivatedProgram(order, activated) {
    for (const reward of activated.program.rewards) {
        order.disabledRewards.delete(reward.id);
    }
    await order._updateLoyaltyPrograms();
    const claimable = activated.couponId
        ? order.getClaimableRewards(activated.couponId)
        : order.getClaimableRewards(false, activated.program.id);
    if (claimable.length !== 1) {
        return _t("No fue posible volver a aplicar el código: revise las recompensas de la orden.");
    }
    const { reward, coupon_id } = claimable[0];
    if (reward.reward_type === "product" && reward.multi_product) {
        return _t("Este bono requiere elegir el producto: use el botón Recompensas.");
    }
    const res = order._applyReward(reward, coupon_id);
    if (res !== true) {
        return res || _t("No fue posible aplicar el código.");
    }
    order._updateRewards();
    return true;
}
