frappe.ui.form.on("NKT Oil Daily Repack", {
    refresh(frm) {
        frm.set_intro(
            __("Owner/Admin daily paper entry. Physical Repacking Date is preserved; Created At remains the true ERP encoding time."),
            "blue"
        );
        nkt_oil_repack_math(frm);
    },
    finished_containers(frm) { nkt_oil_repack_math(frm); },
    reported_spillage_kg(frm) { nkt_oil_repack_math(frm); },
});

function nkt_oil_repack_math(frm) {
    const containers = Math.max(parseInt(frm.doc.finished_containers || 0, 10), 0);
    const spill = Math.max(flt(frm.doc.reported_spillage_kg || 0), 0);
    frm.set_value("nominal_kg_per_container", 17);
    frm.set_value("nominal_finished_kg", containers * 17);
    frm.set_value("bulk_consumption_kg", containers * 17 + spill);
    frm.set_value("empty_containers_consumed", containers);
}
