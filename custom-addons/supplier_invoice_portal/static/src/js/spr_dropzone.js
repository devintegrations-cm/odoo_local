/** @odoo-module **/
// Zona de arrastrar y soltar del portal (DECISIONS.md #45). Eventos delegados en
// el documento: sirven para cualquier .o_spr_dropzone sin importar cuando se
// dibuje. El input transparente ya recibe el archivo soltado; esto solo resalta
// la zona y muestra el nombre del archivo.

function zoneOf(event) {
    return event.target.closest && event.target.closest(".o_spr_dropzone");
}

function showName(zone) {
    const input = zone.querySelector('input[type="file"]');
    const label = zone.querySelector(".o_spr_dropzone_name");
    const file = input && input.files && input.files[0];
    zone.classList.toggle("o_spr_has_file", Boolean(file));
    if (label) {
        label.textContent = file ? file.name : "";
    }
}

for (const type of ["dragenter", "dragover"]) {
    document.addEventListener(type, (event) => {
        const zone = zoneOf(event);
        if (zone) {
            zone.classList.add("o_spr_dragover");
        }
    });
}
for (const type of ["dragleave", "drop"]) {
    document.addEventListener(type, (event) => {
        const zone = zoneOf(event);
        if (zone) {
            zone.classList.remove("o_spr_dragover");
        }
    });
}
document.addEventListener("change", (event) => {
    const zone = zoneOf(event);
    if (zone && event.target.matches('input[type="file"]')) {
        showName(zone);
    }
});
