frappe.query_reports["NKT Trucking Vehicle History"] = {
    filters: [
        {fieldname: "vehicle", label: __("Vehicle"), fieldtype: "Link", options: "NKT Vehicle"},
        {fieldname: "from_date", label: __("From Date"), fieldtype: "Date"},
        {fieldname: "to_date", label: __("To Date"), fieldtype: "Date"}
    ]
};
