import frappe
from frappe.utils import date_diff, getdate, today

def execute(filters=None):
    filters=filters or {}
    columns=[
        {"label":"SOA","fieldname":"soa","fieldtype":"Link","options":"NKT Trucking Customer SOA","width":150},
        {"label":"Customer","fieldname":"customer","fieldtype":"Link","options":"Customer","width":190},
        {"label":"Statement Date","fieldname":"statement_date","fieldtype":"Date","width":105},
        {"label":"SOA Total","fieldname":"grand_total","fieldtype":"Currency","width":115},
        {"label":"Collected","fieldname":"amount_collected","fieldtype":"Currency","width":115},
        {"label":"Outstanding","fieldname":"outstanding_amount","fieldtype":"Currency","width":120},
        {"label":"Collection Status","fieldname":"collection_status","fieldtype":"Data","width":115},
        {"label":"Statement Age (Days)","fieldname":"age_days","fieldtype":"Int","width":120},
        {"label":"Statement Age Bucket","fieldname":"age_bucket","fieldtype":"Data","width":125},
        {"label":"Last Collection","fieldname":"last_collection_datetime","fieldtype":"Datetime","width":145},
    ]
    where=["status='Finalized'"]; params={}
    if filters.get('customer'):
        where.append('customer=%(customer)s'); params['customer']=filters['customer']
    if filters.get('collection_status'):
        where.append('collection_status=%(collection_status)s'); params['collection_status']=filters['collection_status']
    rows=frappe.db.sql(f"""select name as soa, customer, statement_date, grand_total, amount_collected, outstanding_amount, collection_status, last_collection_datetime from `tabNKT Trucking Customer SOA` where {' and '.join(where)} order by statement_date asc, name asc""",params,as_dict=True)
    for r in rows:
        age=max(date_diff(getdate(today()),getdate(r.statement_date)),0) if r.statement_date else 0
        r.age_days=age
        r.age_bucket='0-30' if age<=30 else ('31-60' if age<=60 else ('61-90' if age<=90 else '91+'))
    return columns,rows
