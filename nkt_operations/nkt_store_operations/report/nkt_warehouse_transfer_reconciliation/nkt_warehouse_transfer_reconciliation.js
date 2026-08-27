frappe.query_reports["NKT Warehouse Transfer Reconciliation"] = {
    filters: [
        {
            fieldname: "from_date",
            label: __("From Business Date"),
            fieldtype: "Date",
            default: frappe.datetime.add_days(frappe.datetime.get_today(), -7),
        },
        {
            fieldname: "to_date",
            label: __("To Business Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
        },
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
        },
        {
            fieldname: "view_status",
            label: __("Transfer View Status"),
            fieldtype: "Select",
            options:
                "\nDraft\nReleased / In Transit\nPartially Arrived\nCompleted\nDiscrepancy",
        },
        {
            fieldname: "source_warehouse",
            label: __("Source Warehouse"),
            fieldtype: "Link",
            options: "Warehouse",
        },
        {
            fieldname: "destination_warehouse",
            label: __("Destination Warehouse"),
            fieldtype: "Link",
            options: "Warehouse",
        },
        {
            fieldname: "item_code",
            label: __("Item"),
            fieldtype: "Link",
            options: "Item",
        },
        {
            fieldname: "only_overdue",
            label: __("Only Overdue In Transit"),
            fieldtype: "Check",
            default: 0,
        },
        {
            fieldname: "only_discrepancy",
            label: __("Only With Discrepancy"),
            fieldtype: "Check",
            default: 0,
        },
        {
            fieldname: "overdue_after_hours",
            label: __("Overdue Threshold (Hours)"),
            fieldtype: "Float",
            default: 24,
            description: __("Reporting threshold only; it does not block Release or Arrival."),
        },
    ],

    formatter(value, row, column, data, default_formatter) {
        value = default_formatter(value, row, column, data);
        if (!data) return value;

        if (column.fieldname === "overdue" && data.overdue) {
            value = `<strong>${value}</strong>`;
        }
        if (column.fieldname === "view_status" && data.view_status === "Discrepancy") {
            value = `<strong>${value}</strong>`;
        }
        return value;
    },
};
