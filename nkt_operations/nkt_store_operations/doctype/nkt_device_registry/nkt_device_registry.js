frappe.ui.form.on('NKT Device Registry', {
  onload(frm) {
    if (frm.is_new() && !frm.doc.device_id) {
      frm.set_value('device_id', nkt_new_device_uuid());
    }
  },

  refresh(frm) {
    if (frm.is_new() || !frm.doc.device_id) return;

    frm.add_custom_button(__('Bind This Browser'), () => {
      try {
        window.localStorage.setItem('nkt_device_id', frm.doc.device_id);
        window.localStorage.setItem('nkt_device_context', frm.doc.operational_context || '');
        window.localStorage.setItem('nkt_device_label', frm.doc.device_label || '');
        frappe.show_alert({ message: __('This browser is now bound to the selected NKT Device record.'), indicator: 'green' }, 5);
      } catch (e) {
        frappe.msgprint(__('Browser binding could not be saved on this workstation.'));
      }
    }, __('Device Setup'));

    frm.add_custom_button(__('Unbind This Browser'), () => {
      try {
        window.localStorage.removeItem('nkt_device_id');
        window.localStorage.removeItem('nkt_device_context');
        window.localStorage.removeItem('nkt_device_label');
        frappe.show_alert({ message: __('This browser binding was removed.'), indicator: 'orange' }, 5);
      } catch (e) {
        frappe.msgprint(__('Browser binding could not be removed on this workstation.'));
      }
    }, __('Device Setup'));

    frm.add_custom_button(__('Check Browser Binding'), () => {
      let bound = '';
      try { bound = window.localStorage.getItem('nkt_device_id') || ''; } catch (e) {}
      frappe.msgprint(bound && bound === frm.doc.device_id
        ? __('This browser is bound to this Device record.')
        : __('This browser is not bound to this Device record.'));
    }, __('Device Setup'));

    if (frm.doc.status === 'Active') {
      nkt_add_reason_action(frm, __('Restrict Device'), 'Device', false);
      if (frm.doc.assigned_user) {
        nkt_add_reason_action(frm, __('Restrict Assigned User'), 'User', false);
        nkt_add_reason_action(frm, __('Restrict User + Device'), 'Both', false);
      }
    }

    if (frm.doc.status === 'Restricted') {
      frm.add_custom_button(__('Restore Device'), () => nkt_restore_action(frm, 'Device'), __('Security Control'));
      if (frm.doc.assigned_user) {
        frm.add_custom_button(__('Restore User + Device'), () => nkt_restore_action(frm, 'Both'), __('Security Control'));
      }
    }

    if (frm.doc.assigned_user) {
      frm.add_custom_button(__('Restore Assigned User'), () => nkt_restore_action(frm, 'User'), __('Security Control'));
    }

    if (!['Revoked', 'Lost/Stolen', 'Retired'].includes(frm.doc.status)) {
      nkt_add_terminal_action(frm, __('Mark Lost/Stolen'), 'Lost/Stolen');
      nkt_add_terminal_action(frm, __('Revoke Device'), 'Revoked');
      nkt_add_terminal_action(frm, __('Retire Device'), 'Retired');
    }
  }
});

function nkt_new_device_uuid() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  const buf = new Uint8Array(16);
  window.crypto.getRandomValues(buf);
  buf[6] = (buf[6] & 0x0f) | 0x40;
  buf[8] = (buf[8] & 0x3f) | 0x80;
  const h = [...buf].map(x => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
}


function nkt_add_reason_action(frm, label, scope) {
  frm.add_custom_button(label, () => {
    frappe.prompt(
      [{ fieldname: 'reason', fieldtype: 'Small Text', label: __('Reason'), reqd: 1 }],
      values => {
        frappe.call({
          method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.owner_set_restriction',
          args: {
            scope,
            user: frm.doc.assigned_user || null,
            device_id: frm.doc.device_id,
            reason: values.reason
          }
        }).then(() => frm.reload_doc());
      },
      label
    );
  }, __('Security Control'));
}

function nkt_restore_action(frm, scope) {
  frappe.call({
    method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.owner_restore_restriction',
    args: {
      scope,
      user: frm.doc.assigned_user || null,
      device_id: frm.doc.device_id
    }
  }).then(() => frm.reload_doc());
}

function nkt_add_terminal_action(frm, label, status) {
  frm.add_custom_button(label, () => {
    frappe.prompt(
      [{ fieldname: 'reason', fieldtype: 'Small Text', label: __('Reason'), reqd: 1 }],
      values => {
        frappe.call({
          method: 'nkt_operations.nkt_store_operations.features.offline_edge.internal.device_controls.owner_mark_terminal_device',
          args: {
            device_id: frm.doc.device_id,
            status,
            reason: values.reason
          }
        }).then(() => frm.reload_doc());
      },
      label
    );
  }, __('Security Control'));
}
