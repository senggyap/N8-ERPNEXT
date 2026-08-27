import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

CARD_SURCHARGE_RATE = 0.02
LOCKED = {"Posted", "Reversed"}
REFERENCE_METHODS = {"GCash", "Maya", "Bank Transfer", "Card", "Online"}

class NKTTruckingCustomerCollection(Document):
    def validate(self):
        self._guard_locked_history()
        if not self.collection_datetime:
            self.collection_datetime = now_datetime()
        if not self.received_by:
            self.received_by = frappe.session.user
        self._calculate_payment_rows()
        self._fill_allocation_rows()
        self._calculate_totals()
        if self.status == "Posted":
            self._validate_for_posting()

    def on_update(self):
        if self.status in ("Posted", "Reversed"):
            self._sync_linked_soas()

    def _guard_locked_history(self):
        if self.is_new():
            return
        old = frappe.db.get_value(self.doctype, self.name, "status")
        if old == "Posted" and self.status == "Reversed" and getattr(self.flags, "allow_controlled_reversal", False):
            return
        if old in LOCKED:
            frappe.throw(f"{old} trucking collections are locked. Use the controlled reversal action instead of editing issued collection history.")

    def _calculate_payment_rows(self):
        for row in self.payments or []:
            if flt(row.base_amount) < 0:
                frappe.throw(f"Payment row {row.idx}: Amount Applied cannot be negative.")
            row.surcharge_amount = flt(row.base_amount) * CARD_SURCHARGE_RATE if row.payment_method == "Card" else 0
            row.total_received = flt(row.base_amount) + flt(row.surcharge_amount)

    def _posted_allocated_to_soa(self, soa_name):
        value = frappe.db.sql("""
            select coalesce(sum(a.allocated_amount),0)
            from `tabNKT Trucking Customer Collection Allocation` a
            inner join `tabNKT Trucking Customer Collection` c on c.name=a.parent
            where a.customer_soa=%s and c.status='Posted' and c.name<>%s
        """, (soa_name, self.name or ""))[0][0]
        return flt(value)

    def _fill_allocation_rows(self):
        seen=set()
        for row in self.allocations or []:
            if not row.customer_soa:
                continue
            if row.customer_soa in seen:
                frappe.throw(f"Allocation row {row.idx}: Customer SOA {row.customer_soa} is duplicated in this collection.")
            seen.add(row.customer_soa)
            soa=frappe.get_doc("NKT Trucking Customer SOA", row.customer_soa)
            if soa.customer != self.customer:
                frappe.throw(f"Allocation row {row.idx}: SOA {soa.name} belongs to customer {soa.customer}, not {self.customer}.")
            if soa.status != "Finalized":
                frappe.throw(f"Allocation row {row.idx}: SOA {soa.name} must be Finalized before collection can be posted.")
            row.statement_date=soa.statement_date
            row.soa_total=flt(soa.grand_total)
            prior=self._posted_allocated_to_soa(soa.name)
            row.outstanding_before=max(flt(soa.grand_total)-prior,0)
            row.outstanding_after=max(flt(row.outstanding_before)-flt(row.allocated_amount),0)

    def _calculate_totals(self):
        self.total_payment_base=sum(flt(r.base_amount) for r in (self.payments or []))
        self.total_card_surcharge=sum(flt(r.surcharge_amount) for r in (self.payments or []))
        self.total_received=sum(flt(r.total_received) for r in (self.payments or []))
        self.total_allocated=sum(flt(r.allocated_amount) for r in (self.allocations or []))
        self.unallocated_amount=max(flt(self.total_payment_base)-flt(self.total_allocated),0)

    def _validate_payment_references(self):
        for row in self.payments or []:
            method=row.payment_method
            if flt(row.base_amount) <= 0:
                frappe.throw(f"Payment row {row.idx}: Amount Applied must be greater than zero.")
            if method in REFERENCE_METHODS and not (row.reference_number or "").strip():
                frappe.throw(f"Payment row {row.idx}: Reference Number is required for {method}.")
            if method == "Bank Transfer" and not row.reference_datetime:
                frappe.throw(f"Payment row {row.idx}: Reference Date / Time is required for Bank Transfer.")
            if method == "Check":
                if not (row.bank_or_provider or "").strip() or not (row.check_number or "").strip() or not row.check_date:
                    frappe.throw(f"Payment row {row.idx}: Check requires Bank, Check Number, and Check Date.")
                key=(row.check_number or "").strip()
                duplicate=frappe.db.sql("""
                    select c.name from `tabNKT Trucking Customer Collection` c
                    inner join `tabNKT Trucking Customer Collection Payment` p on p.parent=c.name
                    where c.status='Posted' and c.name<>%s and p.payment_method='Check'
                      and trim(ifnull(p.check_number,''))=%s and trim(ifnull(p.bank_or_provider,''))=%s limit 1
                """, (self.name or "", key, (row.bank_or_provider or "").strip()), as_dict=True)
            elif method in REFERENCE_METHODS:
                key=(row.reference_number or "").strip()
                duplicate=frappe.db.sql("""
                    select c.name from `tabNKT Trucking Customer Collection` c
                    inner join `tabNKT Trucking Customer Collection Payment` p on p.parent=c.name
                    where c.status='Posted' and c.name<>%s and p.payment_method=%s
                      and trim(ifnull(p.reference_number,''))=%s limit 1
                """, (self.name or "", method, key), as_dict=True)
            else:
                duplicate=[]
            if duplicate:
                frappe.throw(f"Payment row {row.idx}: duplicate {method} reference already exists on collection {duplicate[0].name}.")

    def _validate_for_posting(self):
        if not self.payments:
            frappe.throw("At least one payment row is required before Posting a trucking collection.")
        if not self.allocations:
            frappe.throw("At least one Finalized Customer SOA allocation is required before Posting a trucking collection.")
        self._validate_payment_references()
        for row in self.allocations or []:
            if flt(row.allocated_amount) <= 0:
                frappe.throw(f"Allocation row {row.idx}: Allocated Amount must be greater than zero.")
            if flt(row.allocated_amount) - flt(row.outstanding_before) > 0.005:
                frappe.throw(f"Allocation row {row.idx}: allocation exceeds the SOA outstanding balance. Trucking collections cannot over-allocate or create an advance implicitly.")
        if abs(flt(self.total_payment_base)-flt(self.total_allocated)) > 0.005:
            frappe.throw("Payment Applied to SOAs must exactly equal Total Allocated. Card surcharge is separate and is never allocated to receivable/aging.")

    @frappe.whitelist()
    def post_collection(self):
        if self.status != "Draft":
            frappe.throw("Only a Draft trucking collection can be Posted.")
        self.status="Posted"
        self.posted_at=now_datetime()
        self.save()
        return {"status":self.status,"total_allocated":self.total_allocated,"card_surcharge":self.total_card_surcharge,"total_received":self.total_received}

    @frappe.whitelist()
    def reverse_collection(self, reason=None):
        if self.status != "Posted":
            frappe.throw("Only a Posted trucking collection can be Reversed.")
        if not (reason or "").strip():
            frappe.throw("Reversal Reason is required.")
        self.flags.allow_controlled_reversal=True
        self.status="Reversed"
        self.reversed_at=now_datetime()
        self.reversed_by=frappe.session.user
        self.reversal_reason=reason.strip()
        self.save()
        return self.status

    def _sync_linked_soas(self):
        names={r.customer_soa for r in (self.allocations or []) if r.customer_soa}
        for name in names:
            _sync_trucking_soa_collection_summary(name)

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft trucking collections may be deleted.")


def _sync_trucking_soa_collection_summary(soa_name):
    soa=frappe.db.get_value("NKT Trucking Customer SOA",soa_name,["grand_total","status"],as_dict=True)
    if not soa or soa.status != "Finalized":
        return
    row=frappe.db.sql("""
        select coalesce(sum(a.allocated_amount),0) as collected, max(c.posted_at) as last_collection
        from `tabNKT Trucking Customer Collection Allocation` a
        inner join `tabNKT Trucking Customer Collection` c on c.name=a.parent
        where a.customer_soa=%s and c.status='Posted'
    """, (soa_name,), as_dict=True)[0]
    collected=flt(row.collected)
    outstanding=max(flt(soa.grand_total)-collected,0)
    status="Paid" if outstanding <= 0.005 else ("Partially Paid" if collected > 0.005 else "Unpaid")
    frappe.db.set_value("NKT Trucking Customer SOA",soa_name,{"amount_collected":collected,"outstanding_amount":outstanding,"collection_status":status,"last_collection_datetime":row.last_collection},update_modified=False)
