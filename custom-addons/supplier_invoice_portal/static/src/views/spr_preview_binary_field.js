/** @odoo-module **/
// Campo binario con boton de vista previa al lado de editar, descargar y
// eliminar (DECISIONS.md #55). El PDF se abre con el visor de Odoo; el XML,
// con sangria y la factura embebida primero.

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Dialog } from "@web/core/dialog/dialog";
import { BinaryField, binaryField } from "@web/views/fields/binary/binary_field";
import { Component } from "@odoo/owl";

export class SprFilePreviewDialog extends Component {
    static template = "supplier_invoice_portal.FilePreviewDialog";
    static components = { Dialog };
    static props = {
        title: String,
        pdfUrl: { type: String, optional: true },
        xmlText: { type: String, optional: true },
        close: Function,
    };
}

export class SprPreviewBinaryField extends BinaryField {
    static template = "supplier_invoice_portal.PreviewBinaryField";
    static props = {
        ...BinaryField.props,
        previewType: { type: String, optional: true },
    };

    setup() {
        super.setup();
        this.dialog = useService("dialog");
        this.orm = useService("orm");
    }

    get canPreview() {
        const { record, name } = this.props;
        return Boolean(record.resId && record.data[name] && !record.dirty);
    }

    async onFilePreview() {
        const { resModel, resId } = this.props.record;
        const title = this.fileName || this.props.record.fields[this.props.name].string;
        if (this.props.previewType === "xml") {
            const xmlText = await this.orm.call(resModel, "spr_xml_preview", [[resId]]);
            this.dialog.add(SprFilePreviewDialog, { title, xmlText });
            return;
        }
        const fileUrl = `/web/content?model=${resModel}&id=${resId}&field=${this.props.name}`;
        this.dialog.add(SprFilePreviewDialog, {
            title,
            pdfUrl: `/web/static/lib/pdfjs/web/viewer.html?file=${encodeURIComponent(fileUrl)}`,
        });
    }
}

registry.category("fields").add("spr_preview_binary", {
    ...binaryField,
    component: SprPreviewBinaryField,
    extractProps: (fieldInfo) => ({
        ...binaryField.extractProps(fieldInfo),
        previewType: fieldInfo.options.preview || "pdf",
    }),
});
