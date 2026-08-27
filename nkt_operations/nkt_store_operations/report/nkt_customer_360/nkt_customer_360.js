frappe.query_reports["NKT Customer 360"] = {
  filters: [
    {
      fieldname: "customer",
      label: __("Customer"),
      fieldtype: "Link",
      options: "Customer",
      reqd: 1
    },
    {
      fieldname: "as_of_date",
      label: __("As-of Date"),
      fieldtype: "Date",
      default: frappe.datetime.get_today(),
      reqd: 1
    },
    {
      fieldname: "company",
      label: __("Company"),
      fieldtype: "Link",
      options: "Company"
    },
    {
      fieldname: "include_closed_receivables",
      label: __("Include Closed Receivables"),
      fieldtype: "Check",
      default: 0
    }
  ],

  onload(report) {
    const selected_customer = () => report.get_filter_value("customer");

    const require_customer = () => {
      const customer = selected_customer();
      if (!customer) {
        frappe.msgprint(__("Select a Customer first."));
        return null;
      }
      return customer;
    };

    report.page.add_inner_button(__("Open Customer"), () => {
      const customer = require_customer();
      if (!customer) return;
      frappe.set_route("Form", "Customer", customer);
    }, __("Customer Account"));

    report.page.add_inner_button(__("Customer Statements"), () => {
      const customer = require_customer();
      if (!customer) return;
      frappe.route_options = { customer };
      frappe.set_route("List", "NKT Customer Statement");
    }, __("Customer Account"));

    report.page.add_inner_button(__("Aging Alerts"), () => {
      const customer = require_customer();
      if (!customer) return;
      frappe.route_options = { customer };
      frappe.set_route("List", "NKT Account Aging Alert");
    }, __("Customer Account"));
  }
};
