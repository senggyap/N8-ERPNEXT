frappe.query_reports["NKT Compliance Control Center"] = {
  filters: [
    {
      fieldname: "as_of_date",
      label: __("As-of Date"),
      fieldtype: "Date",
      default: frappe.datetime.get_today(),
      reqd: 1
    },
    {
      fieldname: "attention_only",
      label: __("Attention Needed Only"),
      fieldtype: "Check",
      default: 1
    },
    {
      fieldname: "due_within_days",
      label: __("Due Within Days"),
      fieldtype: "Int",
      description: __("Optional. Expired items remain included because they are already overdue.")
    },
    {
      fieldname: "company",
      label: __("Company"),
      fieldtype: "Link",
      options: "Company"
    },
    {
      fieldname: "document_category",
      label: __("Category"),
      fieldtype: "Select",
      options: "\nPermit\nLicense\nContract\nInsurance\nVehicle Registration\nLease\nCertification\nSOP\nInspection\nOther"
    },
    {
      fieldname: "record_state",
      label: __("Record State"),
      fieldtype: "Select",
      options: "\nActive\nSuperseded\nCancelled"
    },
    {
      fieldname: "responsible_user",
      label: __("Responsible Person"),
      fieldtype: "Link",
      options: "User"
    },
    {
      fieldname: "title_contains",
      label: __("Title Contains"),
      fieldtype: "Data"
    }
  ],

  onload(report) {
    report.page.add_inner_button(__("Compliance Documents"), () => {
      frappe.set_route("List", "NKT Compliance Document");
    }, __("Compliance"));

    report.page.add_inner_button(__("New Compliance Document"), () => {
      frappe.new_doc("NKT Compliance Document");
    }, __("Compliance"));

    report.page.add_inner_button(__("Owner Control Center"), () => {
      frappe.set_route("query-report", "NKT Owner Control Center");
    }, __("Compliance"));
  }
};
