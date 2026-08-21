# Database module: Mongo client and collection handles.
# Shared by services and routes.

from __future__ import annotations

from pymongo import MongoClient

from core.auth_context import merge_owner_filter, stamp_owned_document
from core.config import ADMIN_DB_NAME, DB_NAME, MONGO_URI


class ScopedCollection:
    def __init__(self, raw_collection):
        self._raw = raw_collection

    def _stamp_upsert_update(self, update):
        if not isinstance(update, dict):
            return update

        set_on_insert = update.get("$setOnInsert")
        if isinstance(set_on_insert, dict):
            stamped_insert = stamp_owned_document(set_on_insert)
        else:
            stamped_insert = stamp_owned_document({})

        if set_on_insert == stamped_insert:
            return update

        merged_update = dict(update)
        merged_update["$setOnInsert"] = stamped_insert
        return merged_update

    def find_one(self, filter=None, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.find_one(scoped_filter, *args, **kwargs)

    def find(self, filter=None, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.find(scoped_filter, *args, **kwargs)

    def update_one(self, filter, update, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        if kwargs.get("upsert"):
            update = self._stamp_upsert_update(update)
        return self._raw.update_one(scoped_filter, update, *args, **kwargs)

    def update_many(self, filter, update, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        if kwargs.get("upsert"):
            update = self._stamp_upsert_update(update)
        return self._raw.update_many(scoped_filter, update, *args, **kwargs)

    def delete_one(self, filter, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.delete_one(scoped_filter, *args, **kwargs)

    def delete_many(self, filter, *args, **kwargs):
        scoped_filter = merge_owner_filter(filter)
        return self._raw.delete_many(scoped_filter, *args, **kwargs)

    def insert_one(self, document, *args, **kwargs):
        return self._raw.insert_one(stamp_owned_document(document), *args, **kwargs)

    def insert_many(self, documents, *args, **kwargs):
        return self._raw.insert_many(
            [stamp_owned_document(document) for document in documents],
            *args,
            **kwargs,
        )

    def aggregate(self, pipeline, *args, **kwargs):
        owner_match = merge_owner_filter(None)
        if owner_match:
            pipeline = [{"$match": owner_match}, *pipeline]
        return self._raw.aggregate(pipeline, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._raw, name)


client = MongoClient(MONGO_URI)
db = client[DB_NAME]
admin_db = client[ADMIN_DB_NAME]

raw_floorplans_collection = db["sites"]
raw_tours_collection = db["tours"]
raw_work_schedules_collection = db["work_schedules"]
raw_schedule_baselines_collection = db["schedule_baselines"]
raw_schedule_activities_collection = db["schedule_activities"]
raw_schedule_relationships_collection = db["schedule_relationships"]
raw_schedule_assignments_collection = db["schedule_assignments"]
raw_schedule_evidence_collection = db["schedule_evidence"]
raw_schedule_progress_snapshots_collection = db["schedule_progress_snapshots"]
raw_material_documents_collection = db["material_documents"]
raw_project_materials_collection = db["project_materials"]
raw_material_audit_events_collection = db["material_audit_events"]
raw_budget_boqs_collection = db["budget_boqs"]
raw_budget_boq_items_collection = db["budget_boq_items"]
raw_budget_variations_collection = db["budget_variations"]
raw_budget_invoices_collection = db["budget_invoices"]
raw_budget_verification_runs_collection = db["budget_verification_runs"]
raw_budget_audit_events_collection = db["budget_audit_events"]
raw_users_collection = db["users"]
raw_admins_collection = admin_db["admins"]
raw_inspections_collection = db["inspections"]
raw_notifications_collection = db["notifications"]
raw_notification_devices_collection = db["notification_devices"]
raw_safety_records_collection = db["safety_records"]
raw_safety_analysis_jobs_collection = db["safety_analysis_jobs"]
raw_safety_audit_events_collection = db["safety_audit_events"]
raw_subscription_requests_collection = db["subscription_requests"]
raw_subscription_checkout_sessions_collection = db["subscription_checkout_sessions"]
raw_subscription_payments_collection = db["subscription_payments"]


def ensure_admin_directory_indexes() -> None:
    """Create the indexes required by the isolated administrator directory."""
    raw_admins_collection.create_index("email", unique=True, name="unique_admin_email")
    raw_admins_collection.create_index(
        "user_id", unique=True, name="unique_admin_user_id"
    )
    raw_admins_collection.create_index(
        "auth_sessions.access_token", sparse=True, name="admin_access_token"
    )
    raw_admins_collection.create_index(
        "auth_sessions.refresh_token", sparse=True, name="admin_refresh_token"
    )


def ensure_schedule_indexes() -> None:
    """Create indexes used by versioned baselines and tour evidence."""
    raw_schedule_baselines_collection.create_index(
        "baseline_id", unique=True, name="unique_schedule_baseline_id"
    )
    raw_schedule_baselines_collection.create_index(
        [("project_id", 1), ("version", -1)], name="schedule_project_versions"
    )
    raw_schedule_activities_collection.create_index(
        [("baseline_id", 1), ("activity_id", 1)],
        unique=True,
        name="unique_baseline_activity_id",
    )
    raw_schedule_relationships_collection.create_index(
        [("baseline_id", 1), ("activity_internal_id", 1)],
        name="schedule_relationship_activity",
    )
    raw_schedule_assignments_collection.create_index(
        [("baseline_id", 1), ("activity_internal_id", 1)],
        name="schedule_assignment_activity",
    )
    raw_schedule_evidence_collection.create_index(
        [("baseline_id", 1), ("activity_internal_id", 1), ("tour_id", 1)],
        unique=True,
        name="unique_schedule_tour_evidence",
    )
    raw_schedule_progress_snapshots_collection.create_index(
        [("baseline_id", 1), ("snapshot_date", 1)],
        unique=True,
        name="unique_schedule_snapshot_date",
    )


def ensure_material_indexes() -> None:
    """Create project-isolated material document, ledger, and audit indexes."""
    raw_material_documents_collection.create_index(
        "document_id", unique=True, name="unique_material_document_id"
    )
    raw_material_documents_collection.create_index(
        [("owner_user_id", 1), ("project_id", 1), ("source_sha256", 1)],
        unique=True,
        name="unique_project_material_document_hash",
    )
    raw_material_documents_collection.create_index(
        [("project_id", 1), ("document_type", 1), ("status", 1), ("uploaded_at", -1)],
        name="material_project_type_status",
    )
    raw_project_materials_collection.create_index(
        [("owner_user_id", 1), ("project_id", 1), ("material_id", 1)],
        unique=True,
        name="unique_project_material_id",
    )
    raw_project_materials_collection.create_index(
        [("project_id", 1), ("status", 1), ("description", 1)],
        name="material_ledger_lookup",
    )
    raw_material_audit_events_collection.create_index(
        [("project_id", 1), ("created_at", -1)],
        name="material_audit_project_created",
    )


def ensure_budget_indexes() -> None:
    """Create project-isolated BOQ, invoice, verification, and audit indexes."""
    raw_budget_boqs_collection.create_index(
        "boq_id", unique=True, name="unique_budget_boq_id"
    )
    raw_budget_boqs_collection.create_index(
        [("project_id", 1), ("version", -1)], name="budget_project_boq_versions"
    )
    raw_budget_boqs_collection.create_index(
        [("owner_user_id", 1), ("project_id", 1), ("source_sha256", 1)],
        unique=True,
        name="unique_project_budget_boq_hash",
    )
    raw_budget_boqs_collection.create_index(
        [("owner_user_id", 1), ("project_id", 1), ("is_active", 1)],
        unique=True,
        partialFilterExpression={"is_active": True},
        name="unique_active_project_budget_boq",
    )
    raw_budget_boq_items_collection.create_index(
        [("owner_user_id", 1), ("project_id", 1), ("boq_id", 1), ("boq_item_id", 1)],
        unique=True,
        name="unique_budget_boq_item",
    )
    raw_budget_boq_items_collection.create_index(
        [("project_id", 1), ("item_number", 1), ("normalized_description", 1)],
        name="budget_boq_item_lookup",
    )
    raw_budget_variations_collection.create_index(
        "variation_id", unique=True, name="unique_budget_variation_id"
    )
    raw_budget_variations_collection.create_index(
        [("project_id", 1), ("status", 1), ("effective_date", 1)],
        name="budget_variation_effective",
    )
    raw_budget_invoices_collection.create_index(
        "invoice_id", unique=True, name="unique_budget_invoice_id"
    )
    raw_budget_invoices_collection.create_index(
        [("project_id", 1), ("billing_cutoff_date", 1), ("sequence", 1)],
        name="budget_invoice_history",
    )
    raw_budget_invoices_collection.create_index(
        [("owner_user_id", 1), ("project_id", 1), ("source_sha256", 1)],
        unique=True,
        name="unique_project_budget_invoice_hash",
    )
    raw_budget_verification_runs_collection.create_index(
        "verification_run_id",
        unique=True,
        name="unique_budget_verification_run_id",
    )
    raw_budget_verification_runs_collection.create_index(
        [("project_id", 1), ("invoice_id", 1), ("version", -1)],
        name="budget_invoice_verification_versions",
    )
    raw_budget_audit_events_collection.create_index(
        [("project_id", 1), ("created_at", -1)],
        name="budget_audit_project_created",
    )


def ensure_safety_indexes() -> None:
    """Create the Phase 1 safety/manpower query and idempotency indexes."""
    raw_safety_records_collection.create_index(
        "record_id", unique=True, name="unique_safety_record_id"
    )
    raw_safety_records_collection.create_index(
        [("project_id", 1), ("record_type", 1), ("record_date", -1)],
        name="safety_project_type_date",
    )
    raw_safety_records_collection.create_index(
        [("project_id", 1), ("record_type", 1), ("status", 1)],
        name="safety_project_type_status",
    )
    raw_safety_records_collection.create_index(
        [("project_id", 1), ("record_type", 1), ("record_date", 1), ("revision", 1)],
        unique=True,
        partialFilterExpression={"record_type": "daily_report", "revision": {"$exists": True}},
        name="unique_safety_daily_report_revision",
    )
    raw_safety_records_collection.create_index(
        [("project_id", 1), ("record_type", 1), ("client_reference_id", 1)],
        unique=True,
        partialFilterExpression={"client_reference_id": {"$exists": True}},
        name="unique_safety_client_reference",
    )
    raw_safety_analysis_jobs_collection.create_index(
        "job_id", unique=True, name="unique_safety_analysis_job_id"
    )
    raw_safety_analysis_jobs_collection.create_index(
        [("project_id", 1), ("tour_id", 1), ("analysis_version", 1)],
        unique=True,
        name="unique_safety_tour_analysis_version",
    )
    raw_safety_audit_events_collection.create_index(
        [("project_id", 1), ("created_at", -1)],
        name="safety_audit_project_created",
    )

# Store site-related data in a single collection.
# This replaces the old floorplans collection name.
floorplans_collection = ScopedCollection(raw_floorplans_collection)
tours_collection = ScopedCollection(raw_tours_collection)
work_schedules_collection = ScopedCollection(raw_work_schedules_collection)
schedule_baselines_collection = ScopedCollection(raw_schedule_baselines_collection)
schedule_activities_collection = ScopedCollection(raw_schedule_activities_collection)
schedule_relationships_collection = ScopedCollection(raw_schedule_relationships_collection)
schedule_assignments_collection = ScopedCollection(raw_schedule_assignments_collection)
schedule_evidence_collection = ScopedCollection(raw_schedule_evidence_collection)
schedule_progress_snapshots_collection = ScopedCollection(
    raw_schedule_progress_snapshots_collection
)
material_documents_collection = ScopedCollection(raw_material_documents_collection)
project_materials_collection = ScopedCollection(raw_project_materials_collection)
material_audit_events_collection = ScopedCollection(raw_material_audit_events_collection)
budget_boqs_collection = ScopedCollection(raw_budget_boqs_collection)
budget_boq_items_collection = ScopedCollection(raw_budget_boq_items_collection)
budget_variations_collection = ScopedCollection(raw_budget_variations_collection)
budget_invoices_collection = ScopedCollection(raw_budget_invoices_collection)
budget_verification_runs_collection = ScopedCollection(
    raw_budget_verification_runs_collection
)
budget_audit_events_collection = ScopedCollection(raw_budget_audit_events_collection)
users_collection = raw_users_collection
inspections_collection = ScopedCollection(raw_inspections_collection)
notifications_collection = raw_notifications_collection
notification_devices_collection = raw_notification_devices_collection
safety_records_collection = ScopedCollection(raw_safety_records_collection)
safety_analysis_jobs_collection = ScopedCollection(raw_safety_analysis_jobs_collection)
safety_audit_events_collection = ScopedCollection(raw_safety_audit_events_collection)
