frappe.query_reports["NKT Owner Control Center"] = {
    filters: [
        {
            fieldname: "business_date",
            label: __("Business Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
            reqd: 1,
        },
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
        },
        {
            fieldname: "category",
            label: __("Attention Category"),
            fieldtype: "Select",
            options:
                "\nDaily Control\nCashier / Encoder Reconciliation\nCashier Shift\nCredit Control / Receivables\nReturns / Exchanges\nPhysical Inventory\nSupplier / Purchasing\nWarehouse Customer Release\nInternal Warehouse Transfer",
        },
        {
            fieldname: "severity",
            label: __("Severity"),
            fieldtype: "Select",
            options: "\nCritical\nAttention\nInfo",
        },
        {
            fieldname: "search_text",
            label: __("Search"),
            fieldtype: "Data",
            description: __("Search reference, customer, user, warehouse, status, or summary."),
        },
    ],

    formatter(value, row, column, data, default_formatter) {
        value = default_formatter(value, row, column, data);
        if (!data) return value;

        if (column.fieldname === "severity") {
            if (data.severity === "Critical") {
                value = `<strong>${value}</strong>`;
            } else if (data.severity === "Attention") {
                value = `<strong>${value}</strong>`;
            }
        }

        if (column.fieldname === "category") {
            value = `<strong>${value}</strong>`;
        }

        return value;
    },
};
