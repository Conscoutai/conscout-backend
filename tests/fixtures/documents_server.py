"""Loopback-only Documents preview using real routes and an in-memory database.

PYTHONPATH must include the backend root and requirements.test.txt dependencies.
Run from the backend: python tests/fixtures/documents_server.py
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

os.environ["MONGO_URI"] = "mongodb://127.0.0.1:27017"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import mongomock
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fpdf import FPDF
from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from services import documents_service as service
from api.routes.documents import router

storage = tempfile.TemporaryDirectory(prefix="conscout-documents-preview-")
root = Path(storage.name)
db = mongomock.MongoClient().documents_preview
for name in ("projects", "documents", "metadata", "groups", "events", "raw_schedule_baselines_collection", "raw_schedule_updates_collection", "raw_budget_boqs_collection", "raw_budget_invoices_collection", "raw_material_documents_collection", "raw_safety_records_collection"):
    setattr(service, name, db[name])
service.site_storage_roots = lambda **kwargs: [str(root)]
db.documents.create_index([("project_id", 1), ("client_reference", 1)], unique=True)
db.documents.create_index([("group_id", 1), ("version", 1)], unique=True)
db.groups.create_index("group_id", unique=True)

project = {"id": "local-project", "site_name": "Report fixture", "owner_user_id": "local-user", "owner_email": "local@example.com", "created_by_email": "local@example.com"}
db.projects.insert_many([project, {"id": "foreign-project", "site_name": "Private project", "owner_user_id": "another-user", "owner_email": "another@example.com"}])
pdf = FPDF();pdf.add_page();pdf.set_font("Arial", size=12);pdf.cell(0, 12, "ConScout local Documents verification")
rendered = pdf.output(dest="S")
pdf_bytes = rendered.encode("latin-1") if isinstance(rendered, str) else bytes(rendered)
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ1sAAAAASUVORK5CYII=")


def seed(collection, identifier, identity, filename, **extra):
    path = root / "local-project" / "fixture" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes if filename.endswith(".pdf") else b"ERMHDR\tPrimavera local fixture\n")
    getattr(service, collection).insert_one({"project_id": "local-project", identifier: identity, "original_filename": filename, "source_filename": filename, "storage_path": str(path), "source_size_bytes": path.stat().st_size, "uploaded_at": "2026-10-01T08:00:00Z", "uploaded_by_email": "local@example.com", **extra})


seed("raw_schedule_baselines_collection", "baseline_id", "baseline-local", "Approved baseline.xer", version=1, is_active=True)
seed("raw_schedule_updates_collection", "update_id", "latest", "client-sep20.xer", status="accepted")
seed("raw_budget_boqs_collection", "boq_id", "local-boq", "Priced BOQ.pdf", version=1, is_active=True, source_sha256="shared-boq")
seed("raw_material_documents_collection", "document_id", "material-boq", "Priced BOQ.pdf", document_type="boq", status="confirmed", source_sha256="shared-boq")
seed("raw_budget_invoices_collection", "invoice_id", "local-invoice", "Invoice 001.pdf", invoice_number="INV-001", status="needs_review")
seed("raw_material_documents_collection", "document_id", "delivery-1", "Delivery note 001.pdf", document_type="delivery_note", status="needs_review")
seed("raw_budget_invoices_collection", "invoice_id", "missing", "Missing original.pdf", status="held")
Path(db.raw_budget_invoices_collection.find_one({"invoice_id": "missing"})["storage_path"]).unlink()
image = root / "local-project" / "fixture" / "floorplan.png";image.write_bytes(png)
db.projects.update_one({"id": "local-project"}, {"$set": {"imageUrl": "/sites/local-project/fixture/floorplan.png"}})
db.raw_budget_invoices_collection.insert_one({"project_id": "foreign-project", "invoice_id": "secret", "original_filename": "Private invoice.pdf", "status": "paid"})

app = FastAPI(title="Isolated local Documents verification")
app.include_router(router)


def fixture_user(authorization: str = Header("")):
    if authorization == "Bearer local-customer-token":
        return AuthenticatedUser("local-user", "local@example.com")
    if authorization == "Bearer local-viewer-token":
        return AuthenticatedUser("local-viewer", "viewer@example.com", accessible_project_names=("Report fixture",))
    raise HTTPException(401, "Invalid local fixture token")


app.dependency_overrides[require_authenticated_user] = fixture_user

if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=4312)
    finally:
        storage.cleanup()
