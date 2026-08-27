import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

LOCKED = {"Finalized", "Superseded"}


def _nkt_c15e_layout_for_values(van_no=None, eir_no=None):
    return "A" if ((van_no or "").strip() or (eir_no or "").strip()) else "B"


def _nkt_c15e_line_layout(row):
    return _nkt_c15e_layout_for_values(row.get("van_no"), row.get("eir_no"))

class NKTTruckingCustomerSOA(Document):
    def validate(self):
        self._guard_locked_history()
        if self.period_start and self.period_end and self.period_end < self.period_start:
            frappe.throw("Period End / Cutoff Date cannot be before Period Start.")
        if not self.prepared_by_user:
            self.prepared_by_user = frappe.session.user
        self._fill_lines_from_sources()
        self._recalculate()
        if self.status in ("Prepared", "Finalized"):
            self._validate_billable_lines()

    def on_update(self):
        if self.status == "Finalized":
            self._sync_finalized_customer_charges()
            self._sync_collection_summary()

    def _guard_locked_history(self):
        if self.is_new():
            return
        old_status = frappe.db.get_value(self.doctype, self.name, "status")
        if old_status in LOCKED:
            frappe.throw(f"{old_status} customer SOAs are locked. Create a controlled correction/replacement rather than silently editing issued history.")

    def _fill_lines_from_sources(self):
        for row in self.lines or []:
            if row.source_charge:
                ch = frappe.get_doc("NKT Trucking Customer Charge", row.source_charge)
                if ch.customer != self.customer:
                    frappe.throw(f"Row {row.idx}: Source Charge {ch.name} belongs to customer {ch.customer}, not {self.customer}.")
                if ch.status not in ("Ready to Bill", "Billed"):
                    frappe.throw(f"Row {row.idx}: Source Charge {ch.name} must be Ready to Bill before it can be used on an SOA.")
                if ch.status == "Billed" and ch.linked_customer_soa != self.name:
                    frappe.throw(f"Row {row.idx}: Source Charge {ch.name} is already billed on {ch.linked_customer_soa}.")
                row.source_trip = ch.source_trip
                row.charge_type = ch.charge_type
                if not row.charge_description:
                    row.charge_description = ch.reference_no or ch.notes or ch.charge_type
                mapping = {
                    "txn_date": ch.charge_date,
                    "dr_no": ch.dr_no,
                    "eir_no": ch.eir_no,
                    "driver_name": ch.driver_name,
                    "plate_no": ch.plate_no,
                    "destination": ch.destination,
                }
                for field, value in mapping.items():
                    if not row.get(field) and value:
                        row.set(field, value)
                if not row.qty and ch.qty:
                    row.qty = ch.qty
                if not row.rate and ch.rate:
                    row.rate = ch.rate
                if ch.manual_amount_override and not row.manual_amount_override:
                    row.manual_amount_override = 1
                    row.amount = ch.amount
                    row.override_reason = ch.override_reason or "Source charge manual override"
                continue

            if not row.source_trip:
                continue
            trip = frappe.get_doc("NKT Trucking Trip", row.source_trip)
            if trip.job_type == "External Customer" and trip.customer and trip.customer != self.customer:
                frappe.throw(f"Row {row.idx}: Source Trip {trip.name} belongs to customer {trip.customer}, not {self.customer}.")
            row.charge_type = row.charge_type or "Primary Haul"
            mapping = {
                "txn_date": trip.trip_date,
                "van_no": trip.van_no,
                "dr_no": trip.dr_no,
                "eir_no": trip.eir_no,
                "driver_name": trip.driver_name,
                "plate_no": trip.plate_no,
                "destination": trip.destination,
            }
            for field, value in mapping.items():
                if not row.get(field) and value:
                    row.set(field, value)

    def _recalculate(self):
        total = 0
        layouts = set()
        for row in self.lines or []:
            if not row.manual_amount_override:
                row.amount = flt(row.qty) * flt(row.rate)
            elif not row.override_reason:
                frappe.throw(f"Row {row.idx}: Override Reason is required when Manual Amount Override is enabled.")
            total += flt(row.amount)
            layouts.add(_nkt_c15e_line_layout(row))

        self.grand_total = total

        # New/draft SOAs are always automatic. Legacy Prepared/Finalized
        # statements retain their historical stored manual layout value.
        if self.is_new() or self.status == "Draft":
            self.print_layout = "Auto"

        if len(layouts) > 1:
            frappe.throw(
                "Container/van/EIR trips and ordinary truck-load/driver trips "
                "cannot be combined in one SOA. C15E automatically creates "
                "separate Format A and Format B SOAs."
            )

        self.resolved_layout = next(iter(layouts), "B")

    def _validate_billable_lines(self):
        if not self.lines:
            frappe.throw("At least one SOA line is required.")
        seen_base_trips = set()
        seen_charges = set()
        for row in self.lines:
            if not row.txn_date:
                frappe.throw(f"Row {row.idx}: Date is required.")
            if not row.destination:
                frappe.throw(f"Row {row.idx}: Destination is required.")
            if not row.driver_name:
                frappe.throw(f"Row {row.idx}: Driver is required for every billed trucking line because the actual driver remains part of trip history and profitability controls.")

            if row.source_charge:
                if row.source_charge in seen_charges:
                    frappe.throw(f"Row {row.idx}: Source Charge {row.source_charge} is already used in this SOA.")
                seen_charges.add(row.source_charge)
                ch = frappe.get_doc("NKT Trucking Customer Charge", row.source_charge)
                if ch.customer != self.customer:
                    frappe.throw(f"Row {row.idx}: Source Charge {ch.name} belongs to a different customer.")
                if ch.status not in ("Ready to Bill", "Billed"):
                    frappe.throw(f"Row {row.idx}: Source Charge {ch.name} is not Ready to Bill.")
                duplicate = frappe.db.sql("""
                    select s.name
                    from `tabNKT Trucking Customer SOA` s
                    inner join `tabNKT Trucking Customer SOA Line` l on l.parent=s.name
                    where l.source_charge=%s and s.name<>%s and s.status in ('Prepared','Finalized')
                    limit 1
                """, (row.source_charge, self.name or ""), as_dict=True)
                if duplicate:
                    frappe.throw(f"Row {row.idx}: Source Charge {row.source_charge} is already billed/prepared on {duplicate[0].name}.")
            elif row.source_trip:
                if row.source_trip in seen_base_trips:
                    frappe.throw(f"Row {row.idx}: Primary-haul Source Trip {row.source_trip} is already used in this SOA. Use a Source Backload / Additional Charge record for another legitimate billable line from the same Trip.")
                seen_base_trips.add(row.source_trip)
                trip = frappe.get_doc("NKT Trucking Trip", row.source_trip)
                if trip.status not in ("Delivered", "Closed"):
                    frappe.throw(f"Row {row.idx}: Source Trip {row.source_trip} must be Delivered or Closed before the SOA can be Prepared/Finalized.")
                duplicate = frappe.db.sql("""
                    select s.name
                    from `tabNKT Trucking Customer SOA` s
                    inner join `tabNKT Trucking Customer SOA Line` l on l.parent=s.name
                    where l.source_trip=%s and ifnull(l.source_charge,'')='' and s.name<>%s and s.status in ('Prepared','Finalized')
                    limit 1
                """, (row.source_trip, self.name or ""), as_dict=True)
                if duplicate:
                    frappe.throw(f"Row {row.idx}: Primary haul for Source Trip {row.source_trip} is already billed on {duplicate[0].name}.")
            if not row.manual_amount_override:
                if flt(row.qty) <= 0:
                    frappe.throw(f"Row {row.idx}: Qty must be greater than zero. Use Qty = 1 for a flat/per-trip charge only when that is the real billing basis.")
                if flt(row.rate) < 0:
                    frappe.throw(f"Row {row.idx}: Rate cannot be negative.")

    @frappe.whitelist()
    def pull_unbilled_items(self):
        if self.status != "Draft":
            frappe.throw("Unbilled items can only be pulled into a Draft SOA.")
        if not self.customer:
            frappe.throw("Select Customer first.")

        # Sources already claimed by another Draft/Prepared/Finalized SOA are
        # skipped so repeated automatic split runs do not duplicate billing.
        claimed = frappe.db.sql(
            """
            select l.source_trip, l.source_charge
            from `tabNKT Trucking Customer SOA` s
            inner join `tabNKT Trucking Customer SOA Line` l on l.parent=s.name
            where s.customer=%s
              and s.name<>%s
              and s.status in ('Draft','Prepared','Finalized')
            """,
            (self.customer, self.name or ""),
            as_dict=True,
        )
        claimed_base = {r.source_trip for r in claimed if r.source_trip and not r.source_charge}
        claimed_charges = {r.source_charge for r in claimed if r.source_charge}

        existing_base = {r.source_trip for r in (self.lines or []) if r.source_trip and not r.source_charge}
        existing_charges = {r.source_charge for r in (self.lines or []) if r.source_charge}
        cutoff = self.period_end or self.statement_date
        period_start = self.period_start

        groups = {"A": [], "B": []}
        counts = {"A": {"base": 0, "charge": 0}, "B": {"base": 0, "charge": 0}}

        filters = {
            "job_type": "External Customer",
            "customer": self.customer,
            "status": ["in", ["Delivered", "Closed"]],
        }
        if cutoff:
            filters["trip_date"] = ["<=", cutoff]
        trips = frappe.get_all(
            "NKT Trucking Trip",
            filters=filters,
            fields=["name","trip_date","van_no","dr_no","eir_no","driver_name","plate_no","destination"],
            order_by="trip_date asc, name asc",
        )
        for t in trips:
            if period_start and t.trip_date and t.trip_date < period_start:
                continue
            if t.name in existing_base or t.name in claimed_base:
                continue
            layout = _nkt_c15e_layout_for_values(t.van_no, t.eir_no)
            groups[layout].append({
                "source_trip": t.name,
                "charge_type": "Primary Haul",
                "txn_date": t.trip_date,
                "van_no": t.van_no,
                "dr_no": t.dr_no,
                "eir_no": t.eir_no,
                "driver_name": t.driver_name,
                "plate_no": t.plate_no,
                "destination": t.destination,
            })
            counts[layout]["base"] += 1

        cfilters = {"customer": self.customer, "status": "Ready to Bill"}
        if cutoff:
            cfilters["charge_date"] = ["<=", cutoff]
        charges = frappe.get_all(
            "NKT Trucking Customer Charge",
            filters=cfilters,
            fields=["name","charge_date","source_trip","eir_no"],
            order_by="charge_date asc, name asc",
        )
        trip_layout_cache = {}
        for c in charges:
            if period_start and c.charge_date and c.charge_date < period_start:
                continue
            if c.name in existing_charges or c.name in claimed_charges:
                continue

            van_no = None
            eir_no = c.eir_no
            if c.source_trip:
                if c.source_trip not in trip_layout_cache:
                    trip_layout_cache[c.source_trip] = frappe.db.get_value(
                        "NKT Trucking Trip",
                        c.source_trip,
                        ["van_no", "eir_no"],
                        as_dict=True,
                    ) or {}
                tv = trip_layout_cache[c.source_trip]
                van_no = tv.get("van_no")
                eir_no = eir_no or tv.get("eir_no")

            layout = _nkt_c15e_layout_for_values(van_no, eir_no)
            groups[layout].append({"source_charge": c.name})
            counts[layout]["charge"] += 1

        existing_layouts = {_nkt_c15e_line_layout(r) for r in (self.lines or [])}
        if len(existing_layouts) > 1:
            frappe.throw("This Draft SOA already contains mixed A/B lines. Separate them before pulling more sources.")

        nonempty = [layout for layout in ("A", "B") if groups[layout]]
        if not nonempty:
            self._fill_lines_from_sources()
            self._recalculate()
            self.save()
            return {
                "primary_haul_lines_added": 0,
                "backload_additional_charge_lines_added": 0,
                "resolved_layout": self.resolved_layout,
                "companion_soas": [],
            }

        current_layout = next(iter(existing_layouts), None)
        if not current_layout:
            current_layout = "A" if groups["A"] else "B"

        for values in groups[current_layout]:
            self.append("lines", values)

        self._fill_lines_from_sources()
        self._recalculate()
        self.save()

        companion_names = []
        other_layout = "B" if current_layout == "A" else "A"
        if groups[other_layout]:
            companion = frappe.get_doc({
                "doctype": "NKT Trucking Customer SOA",
                "business_name": self.business_name,
                "customer": self.customer,
                "statement_date": self.statement_date,
                "company": self.company,
                "print_layout": "Auto",
                "period_start": self.period_start,
                "period_end": self.period_end,
                "prepared_by": self.prepared_by,
                "default_qty": self.default_qty,
                "default_rate": self.default_rate,
                "notes": (
                    f"Automatically split from {self.name}: "
                    + ("ordinary truck-load/driver Format B." if other_layout == "B"
                       else "container/van/EIR Format A.")
                ),
                "lines": groups[other_layout],
            })
            companion.insert()
            companion_names.append(companion.name)

        return {
            "primary_haul_lines_added": counts["A"]["base"] + counts["B"]["base"],
            "backload_additional_charge_lines_added": counts["A"]["charge"] + counts["B"]["charge"],
            "resolved_layout": self.resolved_layout,
            "companion_soas": companion_names,
            "format_a_lines_added": len(groups["A"]),
            "format_b_lines_added": len(groups["B"]),
        }

    @frappe.whitelist()
    def apply_defaults(self):
        if self.status not in ("Draft", "Prepared"):
            frappe.throw("Defaults can only be applied while the SOA is Draft or Prepared.")
        for row in self.lines or []:
            if not row.manual_amount_override:
                if not row.qty and self.default_qty:
                    row.qty = self.default_qty
                if not row.rate and self.default_rate:
                    row.rate = self.default_rate
                row.amount = flt(row.qty) * flt(row.rate)
        self.save()
        return {"grand_total": self.grand_total}

    @frappe.whitelist()
    def mark_prepared(self):
        if self.status != "Draft":
            frappe.throw("Only a Draft SOA can be marked Prepared.")
        self.status = "Prepared"
        self.prepared_at = now_datetime()
        self.save()
        return self.status

    @frappe.whitelist()
    def finalize_statement(self):
        if self.status != "Prepared":
            frappe.throw("Only a Prepared SOA can be Finalized.")
        self.status = "Finalized"
        self.finalized_at = now_datetime()
        self.save()
        return self.status

    def _sync_finalized_customer_charges(self):
        for row in self.lines or []:
            if not row.source_charge:
                continue
            ch = frappe.db.get_value("NKT Trucking Customer Charge", row.source_charge, ["status","linked_customer_soa"], as_dict=True) or {}
            if ch.get("status") == "Billed" and ch.get("linked_customer_soa") == self.name:
                continue
            if ch.get("status") == "Billed" and ch.get("linked_customer_soa") != self.name:
                frappe.throw(f"Source Charge {row.source_charge} is already billed on {ch.get('linked_customer_soa')}.")
            frappe.db.set_value("NKT Trucking Customer Charge", row.source_charge, {
                "status":"Billed","linked_customer_soa":self.name,"billed_amount":flt(row.amount),"billed_at":now_datetime()
            }, update_modified=True)


    def _sync_collection_summary(self):
        if not frappe.db.exists("DocType", "NKT Trucking Customer Collection"):
            collected = 0
            last_collection = None
        else:
            row = frappe.db.sql("""
                select coalesce(sum(a.allocated_amount),0) as collected, max(c.posted_at) as last_collection
                from `tabNKT Trucking Customer Collection Allocation` a
                inner join `tabNKT Trucking Customer Collection` c on c.name=a.parent
                where a.customer_soa=%s and c.status='Posted'
            """, (self.name,), as_dict=True)[0]
            collected = flt(row.collected)
            last_collection = row.last_collection
        outstanding = max(flt(self.grand_total) - collected, 0)
        collection_status = "Paid" if outstanding <= 0.005 else ("Partially Paid" if collected > 0.005 else "Unpaid")
        frappe.db.set_value(self.doctype, self.name, {
            "amount_collected": collected,
            "outstanding_amount": outstanding,
            "collection_status": collection_status,
            "last_collection_datetime": last_collection,
        }, update_modified=False)

    def on_trash(self):
        if self.status != "Draft":
            frappe.throw("Only Draft customer SOAs may be deleted.")
