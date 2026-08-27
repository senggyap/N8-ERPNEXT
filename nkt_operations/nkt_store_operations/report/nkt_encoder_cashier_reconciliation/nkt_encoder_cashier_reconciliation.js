frappe.query_reports["NKT Encoder Cashier Reconciliation"] = {
    filters: [
        {fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company", reqd: 1, default: frappe.defaults.get_user_default("Company")},
        {fieldname: "business_date", label: __("Business Date"), fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today()},
        {fieldname: "status", label: __("Status"), fieldtype: "Select", options: "\nMatched\nMatched with Customer Warning\nMatched with Warehouse Warning\nMatched with Customer and Warehouse Warning\nUnmatched\nAmbiguous"}
    ]
};
