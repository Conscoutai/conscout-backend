"""Document authorization and immutable file handling, with no external services."""
import io
import os
import zipfile
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

import mongomock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from services import documents_service as service
from api.routes.documents import router

OWNER = AuthenticatedUser("owner", "owner@example.com")
VIEWER = AuthenticatedUser("viewer", "viewer@example.com", accessible_project_names=("Site A",))
OTHER = AuthenticatedUser("other", "other@example.com")
PDF = b"%PDF-1.7\nlocal document\n%%EOF"


@pytest.fixture
def library(monkeypatch, tmp_path):
    db = mongomock.MongoClient().db
    names = ["projects", "documents", "metadata", "groups", "events", "raw_schedule_baselines_collection", "raw_schedule_updates_collection", "raw_budget_boqs_collection", "raw_budget_invoices_collection", "raw_material_documents_collection", "raw_safety_records_collection", "raw_schedule_evidence_collection", "raw_tours_collection"]
    for name in names:
        monkeypatch.setattr(service, name, db[name])
    db.documents.create_index([("project_id", 1), ("client_reference", 1)], unique=True)
    db.documents.create_index([("group_id", 1), ("version", 1)], unique=True)
    db.groups.create_index("group_id", unique=True)
    monkeypatch.setattr(service, "site_storage_roots", lambda **kwargs: [str(tmp_path)])
    monkeypatch.setattr(service, "tour_storage_roots", lambda **kwargs: [str(tmp_path / "tours")])
    db.projects.insert_many([
        {"id": "p1", "site_name": "Site A", "owner_user_id": OWNER.user_id, "owner_email": OWNER.email},
        {"id": "p2", "site_name": "Site B", "owner_user_id": OTHER.user_id, "owner_email": OTHER.email},
    ])
    return db, tmp_path


def upload(content=PDF, **kwargs):
    return service.upload_document("Site A", kwargs.pop("filename", "contract.pdf"), content, kwargs.pop("category", "contract"), kwargs.pop("payload", {}), OWNER, client_reference=kwargs.pop("client_reference", uuid4().hex), **kwargs)


def fail(code, operation):
    with pytest.raises(HTTPException) as error:
        operation()
    assert error.value.status_code == code


def test_upload_revision_and_retry_preserve_original_bytes(library):
    db, root = library
    first = upload(client_reference="stable-reference", payload={"title": "Contract", "tags": ["signed"]})
    identity = first["document"]["id"]
    retry = upload(client_reference="stable-reference")
    assert retry["duplicate"] and retry["document"]["id"] == identity
    fail(409, lambda: upload(PDF + b"changed", client_reference="stable-reference"))
    duplicate = upload()
    assert duplicate["duplicate"] and db.documents.count_documents({}) == 1
    second = upload(PDF + b"\nrevision two", revision_of=identity)
    assert second["document"]["version"] == 2
    assert len(second["versions"]) == 2 and len(second["history"]) == 2
    assert service.document_file(identity, OWNER)[1].read_bytes() == PDF
    assert service.list_documents(OWNER)["documents"][0]["id"] == second["document"]["id"]
    assert service.list_documents(OWNER, latest=False)["total"] == 2
    fail(409, lambda: upload(PDF + b"stale", revision_of=identity))
    assert len(list((root / "p1" / "documents").iterdir())) == 2


def test_racing_upload_reference_cannot_return_a_different_file(library, monkeypatch):
    db, root = library
    upload(client_reference="racing-reference")
    original_lookup = service.documents.find_one
    first_lookup = True

    def lookup(query, *args, **kwargs):
        nonlocal first_lookup
        if first_lookup and query.get("client_reference") == "racing-reference":
            first_lookup = False
            return None  # Another request inserts the original after this lookup.
        return original_lookup(query, *args, **kwargs)

    monkeypatch.setattr(service.documents, "find_one", lookup)
    fail(409, lambda: upload(PDF + b"different file", client_reference="racing-reference"))
    assert db.documents.count_documents({}) == 1
    assert len(list((root / "p1" / "documents").iterdir())) == 1


def test_project_visibility_and_management(library):
    identity = upload()["document"]["id"]
    assert service.list_documents(VIEWER)["total"] == 1
    assert service.list_documents(OTHER)["total"] == 0
    fail(404, lambda: service.document_detail(identity, OTHER))
    fail(404, lambda: service.document_file(identity, OTHER))
    fail(404, lambda: service.list_documents(OWNER, project="p2"))
    fail(403, lambda: service.update_document(identity, {"title": "Changed"}, VIEWER))
    fail(403, lambda: service.upload_document("Site A", "x.pdf", PDF, "contract", {}, VIEWER, client_reference="reference-viewer"))
    assert service.list_documents(AuthenticatedUser("", ""))["total"] == 0
    library[0].raw_budget_invoices_collection.insert_one({"project_id": "p1", "owner_user_id": OTHER.user_id, "owner_email": OTHER.email, "invoice_id": "foreign-owner", "original_filename": "Secret.pdf"})
    fail(404, lambda: service.document_detail("invoice:foreign-owner", OWNER))


def test_sources_deduplicate_boq_and_archive_without_business_changes(library):
    db, _ = library
    db.raw_budget_boqs_collection.insert_one({"project_id": "p1", "boq_id": "b1", "source_sha256": "same", "is_active": True, "original_filename": "BOQ.pdf", "version": 1, "lines": [{"amount": 999}]})
    db.raw_material_documents_collection.insert_one({"project_id": "p1", "document_id": "m1", "source_sha256": "same", "document_type": "boq", "status": "confirmed", "original_filename": "BOQ.pdf"})
    listed = service.list_documents(OWNER)
    assert listed["total"] == 1 and len(listed["documents"][0]["links"]) == 2
    assert "lines" not in listed["documents"][0] and "storage_path" not in listed["documents"][0]
    service.update_document("boq:b1", {"archived": True, "title": "Approved quantities"}, OWNER)
    assert service.list_documents(OWNER)["total"] == 0
    assert service.list_documents(OWNER, archived=True, query="approved")["total"] == 1
    assert db.raw_budget_boqs_collection.find_one({})["is_active"] is True
    assert db.raw_material_documents_collection.find_one({})["status"] == "confirmed"
    fail(400, lambda: service.update_document("boq:b1", {"status": "active"}, OWNER))
    service.update_document("boq:b1", {"archived": False}, OWNER)
    assert service.list_documents(OWNER)["total"] == 1


def test_general_metadata_archive_history_and_filters(library):
    identity = upload(payload={"title": "Initial", "tags": ["signed"]})["document"]["id"]
    service.update_document(identity, {"title": "Main contract", "reference": "CT-001", "description": "Client agreement", "tags": ["signed", "signed"]}, OWNER)
    assert service.list_documents(OWNER, query="ct-001", category="contract", uploader=OWNER.email)["total"] == 1
    assert service.list_documents(OWNER, status="rejected")["total"] == 0
    assert service.document_detail(identity, OWNER)["document"]["tags"] == ["signed"]
    service.update_document(identity, {"archived": True}, OWNER)
    fail(409, lambda: upload(PDF + b"next", revision_of=identity))
    assert len(service.document_detail(identity, OWNER)["history"]) == 3
    fail(400, lambda: service.list_documents(OWNER, after="2026-02-30"))
    fail(400, lambda: service.list_documents(OWNER, after="2026-10-01", before="2026-01-01"))


@pytest.mark.parametrize("filename,content,category", [("x.exe", PDF, "contract"), ("x.pdf", b"html", "contract"), ("x.docx", b"PKfake", "contract"), ("x.png", b"\x89PNG\r\n\x1a\n", "report"), ("x.pdf", PDF, "invoice"), ("x.pdf", b"", "contract")])
def test_rejects_invalid_uploads(library, filename, content, category):
    fail(400, lambda: upload(content, filename=filename, category=category))
    assert library[0].documents.count_documents({}) == 0


def test_office_file_validation_and_sanitized_name(library):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("[Content_Types].xml", "<Types/>")
    result = upload(buffer.getvalue(), filename="../../contract.docx")
    assert result["document"]["filename"] == "contract.docx"
    assert service.document_file(result["document"]["id"], OWNER)[1].is_relative_to(library[1])


def test_missing_and_outside_project_paths_are_not_served(library):
    db, root = library
    outside = root / "p2" / "secret.pdf"
    outside.parent.mkdir();outside.write_bytes(PDF)
    db.raw_budget_invoices_collection.insert_many([
        {"project_id": "p1", "invoice_id": "outside", "original_filename": "invoice.pdf", "storage_path": str(outside)},
        {"project_id": "p1", "invoice_id": "traversal", "original_filename": "invoice.pdf", "source_url": "/sites/p1/../p2/secret.pdf"},
        {"project_id": "p1", "invoice_id": "missing", "original_filename": "invoice.pdf", "storage_path": str(root / "p1" / "missing.pdf")},
    ])
    for identity in ("outside", "traversal", "missing"):
        assert service.document_detail("invoice:" + identity, OWNER)
        fail(404, lambda: service.document_file("invoice:" + identity, OWNER))


def test_finalized_safety_only_and_dxf_index(library):
    db, root = library
    db.raw_safety_records_collection.insert_many([{"project_id": "p1", "record_id": "final", "record_type": "daily_report", "status": "finalized", "record_date": "2026-10-01"}, {"project_id": "p1", "record_id": "draft", "record_type": "daily_report", "status": "draft"}])
    dxf = root / "p1" / service.SITE_DXF_DIRNAME / "zones.dxf"
    dxf.parent.mkdir(parents=True);dxf.write_text("DXF")
    assert service.list_documents(OWNER)["total"] == 2
    assert service.list_documents(VIEWER)["total"] == 1
    assert service.document_file("dxf:p1:zones.dxf", OWNER)[1] == dxf


def test_http_upload_preview_auth_and_strict_patch(library):
    app = FastAPI();app.include_router(router)
    user = [OWNER]
    app.dependency_overrides[require_authenticated_user] = lambda: user[0]
    with TestClient(app) as client:
        response = client.post("/projects/Site%20A/documents", files={"file": ("contract.pdf", PDF, "application/pdf")}, data={"category": "contract", "tags": "signed,client", "client_reference": "http-reference"})
        assert response.status_code == 200, response.text
        identity = response.json()["document"]["id"]
        assert response.json()["document"]["tags"] == ["signed", "client"]
        preview = client.get(f"/workspace/documents/{identity}/file?preview=true")
        assert preview.status_code == 200 and preview.content == PDF
        assert preview.headers["content-type"] == "application/pdf" and preview.headers["cache-control"] == "private, no-store"
        assert client.patch(f"/workspace/documents/{identity}", json={"status": "paid"}).status_code == 422
        user[0] = VIEWER
        assert client.patch(f"/workspace/documents/{identity}", json={"title": "Forbidden"}).status_code == 403
        user[0] = OTHER
        assert client.get(f"/workspace/documents/{identity}/file").status_code == 404


def test_library_records_and_files_follow_project_deletion(library, monkeypatch):
    from services.project_setup import project_lifecycle_service as lifecycle
    db, root = library
    upload()
    bindings = {"floorplans_collection": service.projects, "project_documents_collection": service.documents, "document_metadata_collection": service.metadata, "document_groups_collection": service.groups, "document_events_collection": service.events}
    for name in vars(lifecycle):
        if name.endswith("_collection"):
            monkeypatch.setattr(lifecycle, name, bindings.get(name, db[name]))
    monkeypatch.setattr(lifecycle, "SITES_DIR", str(root))
    monkeypatch.setattr(lifecycle, "site_dir", lambda key, **kwargs: str(root / key))
    assert (root / "p1").resolve().is_relative_to(root.resolve())
    lifecycle.delete_project("p1")
    assert service.documents.count_documents({}) == 0
    assert service.groups.count_documents({}) == 0
    assert service.events.count_documents({}) == 0
    assert not (root / "p1").exists()


def test_activity_attachments_require_project_and_tour_authorization(library):
    db, root = library
    image = root / "tours" / "owner__tour1" / "capture.jpg"
    image.parent.mkdir(parents=True); image.write_bytes(b"local activity image")
    foreign = root / "tours" / "tour2" / "secret.jpg"
    foreign.parent.mkdir(); foreign.write_bytes(b"private image")
    db.raw_tours_collection.insert_many([
        {"tour_id": "tour1", "storage_key": "owner__tour1", "site_name": "Site A", "owner_user_id": OWNER.user_id},
        {"tour_id": "tour2", "storage_key": "tour2", "site_name": "Site B", "owner_user_id": OTHER.user_id},
    ])
    db.raw_schedule_evidence_collection.insert_many([
        {"project_id": "p1", "evidence_id": "e1", "tour_id": "tour1", "activity_name": "Concrete works", "activity_id": "A100", "image_url": "/streetview/owner__tour1/capture.jpg", "status": "approved"},
        {"project_id": "p1", "evidence_id": "foreign-tour", "tour_id": "tour2", "image_url": "/streetview/tour2/secret.jpg"},
        {"project_id": "p1", "evidence_id": "wrong-alias", "tour_id": "tour1", "image_url": "/streetview/tour2/secret.jpg"},
        {"project_id": "p1", "evidence_id": "traversal", "tour_id": "tour1", "image_url": "/streetview/owner__tour1/../tour2/secret.jpg"},
        {"project_id": "p2", "evidence_id": "foreign-project", "tour_id": "tour2", "image_url": "/streetview/tour2/secret.jpg"},
        {"project_id": "p1", "evidence_id": "no-file", "activity_name": "Manual progress"},
    ])
    for user in (OWNER, VIEWER):
        row, path = service.document_file("activity-evidence:e1", user)
        assert path.read_bytes() == b"local activity image"
        assert row["category"] == "schedule" and row["activity_name"] == "Concrete works"
        assert "_tour" not in service.document_detail(row["id"], user)["document"]
        fail(404, lambda: service.document_file("activity-evidence:traversal", user))
        for evidence in ("foreign-tour", "wrong-alias", "foreign-project", "no-file"):
            fail(404, lambda: service.document_detail("activity-evidence:" + evidence, user))
    fail(404, lambda: service.document_file("activity-evidence:e1", OTHER))
    image.unlink()
    fail(404, lambda: service.document_file("activity-evidence:e1", OWNER))
