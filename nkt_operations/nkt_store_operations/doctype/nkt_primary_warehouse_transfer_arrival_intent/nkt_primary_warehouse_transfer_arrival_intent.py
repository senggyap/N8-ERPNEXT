from frappe.model.document import Document


class NKTPrimaryWarehouseTransferArrivalIntent(Document):
    """Immutable preserved Arrival intent.

    Downstream materialization fields are server-updated with db.set_value by the
    dedicated Primary materializer; ordinary document saves may not rewrite history.
    """

    def validate(self):
        before = None if self.is_new() else self.get_doc_before_save()
        if not before:
            return
        immutable = (
            "event_uuid","event_family","event_action","origin_device","origin_user",
            "operational_context","business_date","settled_at","client_created_at",
            "warehouse_transfer","company","transfer_date","source_warehouse",
            "destination_warehouse","outgoing_stock_entry","transit_warehouse",
            "total_arrival_quantity","envelope_sha256","payload_sha256",
            "canonical_envelope_json","canonical_payload_json","preservation_state",
        )
        changed = [
            field for field in immutable
            if (before.get(field) or None) != (self.get(field) or None)
        ]
        if changed:
            raise ValueError(
                "Immutable Primary Transfer Arrival Intent cannot be changed: "
                + ", ".join(changed)
            )
