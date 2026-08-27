frappe.ui.form.on("NKT Oil Tank Close", {
    refresh(frm) {
        frm.set_intro(
            __("Owner/Admin only. Submit only when the combined physical Palm Olein tanks are truly empty and every checklist item is confirmed."),
            "orange"
        );
    },
});
