frappe.ui.form.on("NKT Compliance Document", {
  refresh(frm) {
    if (frm.is_new()) return;

    const status = frm.doc.compliance_status || "";
    const indicator =
      status === "Expired" ? "red" :
      status === "Expiring Soon" ? "orange" :
      status === "Active" ? "green" :
      status === "No Expiry" ? "blue" :
      "gray";

    if (status) {
      frm.dashboard.set_headline_alert(
        __("Compliance Status: {0}", [status]),
        indicator
      );
    }

    frm.add_custom_button(__("Control Center"), () => {
      frappe.set_route("query-report", "NKT Compliance Control Center");
    }, __("Compliance"));

    frm.add_custom_button(__("Owner Control Center"), () => {
      frappe.set_route("query-report", "NKT Owner Control Center");
    }, __("Compliance"));
  }
});
