import frappe
from nkt_operations.nkt_store_operations.features.trucking.access import require_external_carrier_access

def execute(filters=None):
    require_external_carrier_access()
    columns=[
      {"label":"Trip","fieldname":"name","fieldtype":"Link","options":"NKT Trucking Trip","width":150},
      {"label":"Trip Date","fieldname":"trip_date","fieldtype":"Date","width":100},
      {"label":"Carrier / Trucker","fieldname":"carrier_account_snapshot","fieldtype":"Link","options":"Supplier","width":170},
      {"label":"Truck","fieldname":"vehicle","fieldtype":"Link","options":"NKT Vehicle","width":140},
      {"label":"Plate","fieldname":"plate_no","fieldtype":"Data","width":100},
      {"label":"Driver","fieldname":"driver_name","fieldtype":"Data","width":130},
      {"label":"DR No.","fieldname":"dr_no","fieldtype":"Data","width":100},
      {"label":"EIR No.","fieldname":"eir_no","fieldtype":"Data","width":100},
      {"label":"Destination","fieldname":"destination","fieldtype":"Data","width":150},
      {"label":"Carrier Payable Status","fieldname":"carrier_payable_status","fieldtype":"Data","width":160},
      {"label":"Trucker SOA","fieldname":"linked_trucker_soa","fieldtype":"Link","options":"NKT Trucker SOA","width":140},
    ]
    trips=frappe.get_all("NKT Trucking Trip", filters={"fleet_ownership_snapshot":"External Carrier"}, fields=[x["fieldname"] for x in columns if x["fieldname"]!="linked_trucker_soa"], order_by="trip_date asc, name asc")
    for row in trips:
        hit=frappe.db.sql("""select p.name from `tabNKT Trucker SOA Line` l join `tabNKT Trucker SOA` p on p.name=l.parent where l.custom_c14_trucking_trip=%s and p.status!='Superseded' order by p.creation desc limit 1""", row.name, as_dict=True)
        row["linked_trucker_soa"]=hit[0].name if hit else None
    return columns,trips
