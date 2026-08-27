import frappe
from frappe.utils import date_diff, nowdate

def execute(filters=None):
    filters = filters or {}
    conditions = ["a.status not in ('Settled','Cancelled')"]
    params = {}
    if filters.get("driver_name"):
        conditions.append("a.driver_name = %(driver_name)s")
        params["driver_name"] = filters["driver_name"]
    if filters.get("status"):
        conditions.append("a.status = %(status)s")
        params["status"] = filters["status"]

    rows = frappe.db.sql(
        f"""
        select
            a.name as cash_advance,
            a.advance_date,
            a.driver_name,
            a.scope,
            a.source_trip,
            a.vehicle,
            a.plate_no,
            a.amount,
            a.status,
            a.liquidation,
            a.outstanding_amount,
            l.expense_total,
            l.unused_cash_expected,
            l.reimbursement_due,
            l.status as liquidation_status
        from `tabNKT Trucking Driver Cash Advance` a
        left join `tabNKT Trucking Driver Liquidation` l on l.name = a.liquidation
        where {' and '.join(conditions)}
        order by a.advance_date asc, a.name asc
        """,
        params,
        as_dict=True,
    )
    for row in rows:
        row["age_days"] = date_diff(nowdate(), row.advance_date) if row.advance_date else None

    columns = [
        {"label":"Cash Advance","fieldname":"cash_advance","fieldtype":"Link","options":"NKT Trucking Driver Cash Advance","width":170},
        {"label":"Advance Date","fieldname":"advance_date","fieldtype":"Date","width":100},
        {"label":"Age (Days)","fieldname":"age_days","fieldtype":"Int","width":90},
        {"label":"Driver","fieldname":"driver_name","fieldtype":"Data","width":150},
        {"label":"Scope","fieldname":"scope","fieldtype":"Data","width":130},
        {"label":"Trip","fieldname":"source_trip","fieldtype":"Link","options":"NKT Trucking Trip","width":150},
        {"label":"Vehicle","fieldname":"vehicle","fieldtype":"Link","options":"NKT Vehicle","width":150},
        {"label":"Plate No.","fieldname":"plate_no","fieldtype":"Data","width":100},
        {"label":"Advance","fieldname":"amount","fieldtype":"Currency","width":110},
        {"label":"Advance Status","fieldname":"status","fieldtype":"Data","width":130},
        {"label":"Liquidation","fieldname":"liquidation","fieldtype":"Link","options":"NKT Trucking Driver Liquidation","width":160},
        {"label":"Liquidation Status","fieldname":"liquidation_status","fieldtype":"Data","width":120},
        {"label":"Expense Total","fieldname":"expense_total","fieldtype":"Currency","width":110},
        {"label":"Cash Expected Back","fieldname":"unused_cash_expected","fieldtype":"Currency","width":130},
        {"label":"Reimbursement Due","fieldname":"reimbursement_due","fieldtype":"Currency","width":130},
        {"label":"Outstanding Cash","fieldname":"outstanding_amount","fieldtype":"Currency","width":120},
    ]
    return columns, rows
