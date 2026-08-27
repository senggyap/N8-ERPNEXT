(() => {
  const DOCTYPE = 'NKT Encoder Fast Screen';
  frappe.ui.form.on(DOCTYPE, {
    refresh(frm) {
      frm.page.add_inner_button(__('Warehouse Change / Recent Orders'), () => {
        frappe.set_route('Form', 'NKT Warehouse Change Fast Screen', 'NKT Warehouse Change Fast Screen');
      });
    }
  });
})();
