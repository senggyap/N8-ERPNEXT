frappe.query_reports["NKT EOD Inventory"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "business_date",
			label: __("Business Date"),
			fieldtype: "Date",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: function () {
				return {
					filters: {
						company: frappe.query_report.get_filter_value("company"),
						is_group: 0,
						disabled: 0,
					},
				};
			},
		},
		{
			fieldname: "only_eod_selected",
			label: __("Only EOD-selected Items"),
			fieldtype: "Check",
			default: 1,
			description: __(
				"Uses the existing operational inventory-selection flag only to choose items. Quantities come from Stock Ledger history."
			),
		},
		{
			fieldname: "include_zero_balances",
			label: __("Include Zero Balances"),
			fieldtype: "Check",
			default: 1,
		},
	],
};
