import frappe
from frappe.utils import getdate

def _in_range(value, filters):
    if not value:
        return True
    d = getdate(value)
    if filters.get("from_date") and d < getdate(filters["from_date"]):
        return False
    if filters.get("to_date") and d > getdate(filters["to_date"]):
        return False
    return True

def execute(filters=None):
    filters = filters or {}
    vehicle = filters.get("vehicle")
    columns = [
        {"label":"Event Date","fieldname":"event_date","fieldtype":"Date","width":100},
        {"label":"Event Type","fieldname":"event_type","fieldtype":"Data","width":105},
        {"label":"Record","fieldname":"record_name","fieldtype":"Dynamic Link","options":"record_doctype","width":155},
        {"label":"Vehicle","fieldname":"vehicle","fieldtype":"Link","options":"NKT Vehicle","width":140},
        {"label":"Plate","fieldname":"plate_no","fieldtype":"Data","width":100},
        {"label":"Driver","fieldname":"driver_name","fieldtype":"Data","width":130},
        {"label":"Status","fieldname":"status","fieldtype":"Data","width":110},
        {"label":"Trip / Description","fieldname":"description","fieldtype":"Data","width":260},
        {"label":"Cost","fieldname":"cost","fieldtype":"Currency","width":110},
        {"label":"Profit Treatment","fieldname":"profit_treatment","fieldtype":"Data","width":180},
        {"label":"record_doctype","fieldname":"record_doctype","fieldtype":"Data","hidden":1},
    ]
    data = []

    trip_filters = {}
    if vehicle:
        trip_filters["vehicle"] = vehicle
    for row in frappe.get_all("NKT Trucking Trip", filters=trip_filters,
                              fields=["name","trip_date","vehicle","plate_no","driver_name","status","origin","destination","dr_no"],
                              order_by="trip_date asc, name asc"):
        if not _in_range(row.trip_date, filters):
            continue
        route = " → ".join([x for x in [row.origin, row.destination] if x])
        if row.dr_no:
            route = (route + " | DR " + row.dr_no).strip(" |")
        data.append({
            "event_date": row.trip_date, "event_type":"Trip", "record_name":row.name,
            "record_doctype":"NKT Trucking Trip", "vehicle":row.vehicle, "plate_no":row.plate_no,
            "driver_name":row.driver_name, "status":row.status, "description":route,
            "cost":0, "profit_treatment":""
        })

    maintenance_filters = {}
    if vehicle:
        maintenance_filters["vehicle"] = vehicle
    for row in frappe.get_all("NKT Trucking Vehicle Maintenance", filters=maintenance_filters,
                              fields=["name","maintenance_date","vehicle","plate_no","driver_name_snapshot","status","maintenance_type","work_description","actual_cost","profit_treatment","source_trip"],
                              order_by="maintenance_date asc, name asc"):
        if not _in_range(row.maintenance_date, filters):
            continue
        desc = row.maintenance_type or "Maintenance"
        if row.source_trip:
            desc += " | Trip " + row.source_trip
        if row.work_description:
            desc += " | " + row.work_description
        data.append({
            "event_date":row.maintenance_date, "event_type":"Maintenance", "record_name":row.name,
            "record_doctype":"NKT Trucking Vehicle Maintenance", "vehicle":row.vehicle, "plate_no":row.plate_no,
            "driver_name":row.driver_name_snapshot, "status":row.status, "description":desc,
            "cost":row.actual_cost, "profit_treatment":row.profit_treatment
        })

    incident_filters = {}
    if vehicle:
        incident_filters["vehicle"] = vehicle
    for row in frappe.get_all("NKT Trucking Incident", filters=incident_filters,
                              fields=["name","incident_datetime","vehicle","plate_no","driver_name_snapshot","status","incident_type","description","actual_cost","profit_treatment","source_trip"],
                              order_by="incident_datetime asc, name asc"):
        if not _in_range(row.incident_datetime, filters):
            continue
        desc = row.incident_type or "Incident"
        if row.source_trip:
            desc += " | Trip " + row.source_trip
        if row.description:
            desc += " | " + row.description
        data.append({
            "event_date":getdate(row.incident_datetime), "event_type":"Incident", "record_name":row.name,
            "record_doctype":"NKT Trucking Incident", "vehicle":row.vehicle, "plate_no":row.plate_no,
            "driver_name":row.driver_name_snapshot, "status":row.status, "description":desc,
            "cost":row.actual_cost, "profit_treatment":row.profit_treatment
        })

    data.sort(key=lambda x: (str(x.get("event_date") or ""), x.get("event_type") or "", x.get("record_name") or ""))
    return columns, data
