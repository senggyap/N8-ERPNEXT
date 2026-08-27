import frappe

def execute(filters=None):
    columns=[
      {"label":"Source Type","fieldname":"source_type","fieldtype":"Data","width":115},
      {"label":"Date","fieldname":"txn_date","fieldtype":"Date","width":95},
      {"label":"Customer","fieldname":"customer","fieldtype":"Link","options":"Customer","width":180},
      {"label":"Trip","fieldname":"source_trip","fieldtype":"Link","options":"NKT Trucking Trip","width":140},
      {"label":"Charge","fieldname":"source_charge","fieldtype":"Link","options":"NKT Trucking Customer Charge","width":140},
      {"label":"Type","fieldname":"charge_type","fieldtype":"Data","width":120},
      {"label":"Driver","fieldname":"driver_name","fieldtype":"Data","width":140},
      {"label":"Plate No.","fieldname":"plate_no","fieldtype":"Data","width":100},
      {"label":"DR No.","fieldname":"dr_no","fieldtype":"Data","width":100},
      {"label":"EIR No.","fieldname":"eir_no","fieldtype":"Data","width":100},
      {"label":"Destination","fieldname":"destination","fieldtype":"Data","width":180},
      {"label":"Qty","fieldname":"qty","fieldtype":"Float","width":80},
      {"label":"Rate","fieldname":"rate","fieldtype":"Currency","width":100},
      {"label":"Amount","fieldname":"amount","fieldtype":"Currency","width":110},
    ]
    rows=[]
    # Base hauling revenue remains rate-unset until restricted SOA preparation; do not guess Qty/Rate.
    trips=frappe.get_all("NKT Trucking Trip",filters={"job_type":"External Customer","status":["in",["Delivered","Closed"]]},fields=["name","trip_date","customer","driver_name","plate_no","dr_no","eir_no","destination"],order_by="trip_date asc,name asc")
    for t in trips:
        billed=frappe.db.sql("""select s.name from `tabNKT Trucking Customer SOA` s inner join `tabNKT Trucking Customer SOA Line` l on l.parent=s.name where l.source_trip=%s and ifnull(l.source_charge,'')='' and s.status in ('Prepared','Finalized') limit 1""",t.name,as_dict=True)
        if not billed:
            rows.append({"source_type":"Primary Haul","txn_date":t.trip_date,"customer":t.customer,"source_trip":t.name,"charge_type":"Primary Haul","driver_name":t.driver_name,"plate_no":t.plate_no,"dr_no":t.dr_no,"eir_no":t.eir_no,"destination":t.destination,"qty":None,"rate":None,"amount":None})
    charges=frappe.get_all("NKT Trucking Customer Charge",filters={"status":"Ready to Bill"},fields=["name","charge_date","customer","source_trip","charge_type","driver_name","plate_no","dr_no","eir_no","destination","qty","rate","amount"],order_by="charge_date asc,name asc")
    for c in charges:
        rows.append({"source_type":"Backload / Addl","txn_date":c.charge_date,"customer":c.customer,"source_trip":c.source_trip,"source_charge":c.name,"charge_type":c.charge_type,"driver_name":c.driver_name,"plate_no":c.plate_no,"dr_no":c.dr_no,"eir_no":c.eir_no,"destination":c.destination,"qty":c.qty,"rate":c.rate,"amount":c.amount})
    return columns, rows
