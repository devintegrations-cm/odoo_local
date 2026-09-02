/** @odoo-module */

import { ClosePosPopup } from "@point_of_sale/app/navbar/closing_popup/closing_popup";

/**
 * Extend ClosePosPopup's accepted props with the fields our template
 * extension actually consumes.
 *
 * The rest of the validation data returned by the backend
 * (``can_close``, ``blocking_reasons``, ``pending_rescue``, etc.) is
 * consumed by ``navbar_patch.js`` BEFORE the popup opens, and is not
 * forwarded as props thanks to ``_pickClosePosProps``.
 *
 * This keeps OWL 2's strict props validation happy while minimizing
 * the surface area the standard popup has to be aware of.
 */
const EXTRA_PROPS = [
    "expected_cash",
];

ClosePosPopup.props = [...ClosePosPopup.props, ...EXTRA_PROPS];
