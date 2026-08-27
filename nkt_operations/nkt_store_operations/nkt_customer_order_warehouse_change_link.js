(() => {
  const PRELOAD_KEY = 'nkt_wch_preload_order';
  frappe.ui.form.on('NKT Customer Order', {
    refresh(frm) {
      if (frm.doc.docstatus !== 1) return;
      const roles = new Set(frappe.user_roles || []);
      const allowed = roles.has('NKT Encoder') || roles.has('System Manager') || roles.has('NKT OWNER') || roles.has('NKT ADMINISTRATOR');
      if (!allowed) return;
      frm.add_custom_button(__('Change Warehouse'), () => {
        sessionStorage.setItem(PRELOAD_KEY, frm.doc.name);
        frappe.set_route('Form', 'NKT Warehouse Change Fast Screen', 'NKT Warehouse Change Fast Screen');
      }, __('Actions'));
    }
  });
})();
