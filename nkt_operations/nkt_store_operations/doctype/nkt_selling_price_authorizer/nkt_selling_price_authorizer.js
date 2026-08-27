frappe.ui.form.on("NKT Selling Price Authorizer", {
  refresh(frm) {
    frm.set_df_property("user", "read_only", !frm.is_new());

    if (frm.is_new() || !frm.doc.user) return;

    frm.add_custom_button(__("Set / Change 5-Digit PIN"), () => {
      const d = new frappe.ui.Dialog({
        title: __("Set Selling-Price Authorization PIN"),
        fields: [
          {
            fieldname: "pin",
            fieldtype: "Password",
            label: __("New 5-Digit PIN"),
            reqd: 1,
            description: __("Exactly five numeric digits. The PIN is sent only to the server for hashing and is never stored in this form.")
          },
          {
            fieldname: "confirm_pin",
            fieldtype: "Password",
            label: __("Confirm 5-Digit PIN"),
            reqd: 1
          }
        ],
        primary_action_label: __("Save PIN"),
        primary_action(values) {
          const pin = String(values.pin || "");
          const confirm = String(values.confirm_pin || "");
          if (!/^\d{5}$/.test(pin)) {
            frappe.msgprint(__("PIN must be exactly five numeric digits."));
            return;
          }
          if (pin !== confirm) {
            frappe.msgprint(__("The two PIN entries do not match."));
            return;
          }
          d.get_primary_btn().prop("disabled", true);
          frappe.call({
            method: "nkt_operations.nkt_store_operations.manager_authorization.set_authorizer_pin",
            args: { user: frm.doc.user, pin },
            freeze: true,
            freeze_message: __("Securing Manager PIN…")
          }).then(() => {
            d.set_value("pin", "");
            d.set_value("confirm_pin", "");
            d.hide();
            frappe.show_alert({ message: __("Manager PIN configured."), indicator: "green" });
            frm.reload_doc();
          }).finally(() => d.get_primary_btn().prop("disabled", false));
        }
      });
      d.show();
      const $pin = d.get_field("pin").$input;
      const $confirm = d.get_field("confirm_pin").$input;
      $pin.attr({ inputmode: "numeric", maxlength: "5", autocomplete: "new-password" });
      $confirm.attr({ inputmode: "numeric", maxlength: "5", autocomplete: "new-password" });
      $confirm.on("keydown", e => {
        if (e.key === "Enter") {
          e.preventDefault();
          d.get_primary_btn().trigger("click");
        }
      });
      setTimeout(() => $pin.trigger("focus"), 0);
    });

    if (frm.doc.can_authorize_selling_price_adjustments) {
      frm.add_custom_button(__("Disable Authorization"), () => {
        frappe.confirm(
          __("Disable this user's selling-price Manager PIN authorization?"),
          () => frappe.call({
            method: "nkt_operations.nkt_store_operations.manager_authorization.disable_authorizer",
            args: { user: frm.doc.user },
            freeze: true
          }).then(() => frm.reload_doc())
        );
      });
    }
  }
});
