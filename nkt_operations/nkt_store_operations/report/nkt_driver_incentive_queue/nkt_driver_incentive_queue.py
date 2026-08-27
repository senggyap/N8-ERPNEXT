import frappe

def execute(filters=None):
    columns=[
      {"label":"Trip","fieldname":"name","fieldtype":"Link","options":"NKT Trucking Trip","width":150},
      {"label":"Trip Date","fieldname":"trip_date","fieldtype":"Date","width":100},
      {"label":"Driver","fieldname":"driver_name","fieldtype":"Data","width":140},
      {"label":"Truck","fieldname":"vehicle","fieldtype":"Link","options":"NKT Vehicle","width":140},
      {"label":"Plate","fieldname":"plate_no","fieldtype":"Data","width":100},
      {"label":"DR No.","fieldname":"dr_no","fieldtype":"Data","width":100},
      {"label":"EIR No.","fieldname":"eir_no","fieldtype":"Data","width":100},
      {"label":"Papers Verified","fieldname":"paperwork_verified_at","fieldtype":"Datetime","width":145},
      {"label":"Incentive Status","fieldname":"incentive_tracking_status","fieldtype":"Data","width":180},
      {"label":"Batch","fieldname":"incentive_batch","fieldtype":"Link","options":"NKT Driver Incentive Batch","width":140},
      {"label":"Paid Amount","fieldname":"incentive_paid_amount","fieldtype":"Currency","width":110},
    ]
    data=frappe.get_all("NKT Trucking Trip", filters={"fleet_ownership_snapshot":"ENT-Owned"}, fields=[c["fieldname"] for c in columns], order_by="trip_date asc, name asc")
    return columns,data
