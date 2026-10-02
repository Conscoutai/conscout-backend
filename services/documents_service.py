"""Project-authorized file catalog. Operational records remain authoritative."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core.auth_context import AuthenticatedUser
from core.config import SITE_DXF_DIRNAME, site_storage_roots, tour_storage_roots
from core.database import (
    raw_floorplans_collection as projects, raw_schedule_baselines_collection,
    raw_schedule_updates_collection, raw_budget_boqs_collection,
    raw_budget_invoices_collection, raw_material_documents_collection,
    raw_safety_records_collection, raw_project_documents_collection as documents,
    raw_document_metadata_collection as metadata, raw_document_groups_collection as groups,
    raw_document_events_collection as events,
    raw_schedule_evidence_collection, raw_tours_collection,
)

GENERAL_TYPES = {"contract", "drawing", "specification", "correspondence", "other", "report"}
MAX_FILE_BYTES = 25 * 1024 * 1024
SOURCE_FIELDS = {field: 1 for field in (
    "project_id", "site_name", "baseline_id", "update_id", "boq_id", "invoice_id", "document_id",
    "record_id", "record_type", "record_date", "source_filename", "original_filename", "source_url",
    "storage_path", "source_size_bytes", "source_sha256", "document_type", "version", "revision",
    "status", "is_active", "uploaded_at", "created_at", "uploaded_by_email", "uploaded_by",
    "invoice_number", "extracted_header.document_number", "reviewed_header.document_number",
    "source_material_document_id",
    "finalized_at", "finalized_by.email",
)}


def text(value) -> str:
    return str(value or "").strip()


def iso(value) -> str:
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()
    return text(value)


def project_id(project) -> str:
    return text(project.get("project_id") or project.get("id") or project.get("dxf_project_id") or project.get("site_name"))


def owns(project, user: AuthenticatedUser) -> bool:
    return bool((project.get("owner_user_id") and project["owner_user_id"] == user.user_id) or
                (text(project.get("owner_email")).lower() and text(project.get("owner_email")).lower() == user.email.lower()))


def authorized_projects(user: AuthenticatedUser) -> list[dict]:
    clauses = []
    if user.user_id:
        clauses.append({"owner_user_id": user.user_id})
    if user.email:
        clauses.append({"owner_email": user.email.lower()})
    if user.accessible_project_names:
        clauses.extend([{key: {"$in": list(user.accessible_project_names)}} for key in ("site_name", "dxf_project_id")])
    if user.accessible_floorplan_ids:
        clauses.append({"id": {"$in": list(user.accessible_floorplan_ids)}})
    fields = {key: 1 for key in ("project_id", "id", "dxf_project_id", "site_name", "owner_user_id", "owner_email", "image_url", "imageUrl", "floorplan_url", "created_at")}
    for key in ("schedule_zone_plan", "proposed_schedule_zone_plan"):
        fields.update({f"{key}.{field}": 1 for field in ("zone_plan_id", "source_url", "source_filename", "version", "confirmation_status", "uploaded_at", "uploaded_by_email")})
    return list(projects.find({"$or": clauses}, fields)) if clauses else []


def require_project(reference, user, manage=False):
    project = next((p for p in authorized_projects(user) if text(reference).lower() in {
        project_id(p).lower(), text(p.get("site_name")).lower(), text(p.get("id")).lower()}), None)
    if not project:
        raise HTTPException(404, "Project not found")
    if manage and not owns(project, user):
        raise HTTPException(403, "Only the project admin can manage documents")
    return project


def source_scope(allowed):
    """Include owner checks for legacy deployments that reused project names as IDs."""
    clauses = []
    for project in allowed:
        owners = [{"owner_user_id": {"$in": [None, ""]}, "owner_email": {"$in": [None, ""]}}]
        if project.get("owner_user_id"):
            owners.append({"owner_user_id": project["owner_user_id"]})
        if project.get("owner_email"):
            owners.append({"owner_email": project["owner_email"]})
        clauses.append({"project_id": project_id(project), "$or": owners})
    return {"$or": clauses} if clauses else {"project_id": {"$in": []}}


def _row(record, project, source, category, identity, group=None):
    pid = project_id(project)
    filename = text(record.get("source_filename") or record.get("original_filename"))
    if not filename:
        filename = Path(text(record.get("storage_path")) or unquote(urlparse(text(record.get("source_url"))).path)).name
    header = record.get("reviewed_header") or record.get("extracted_header") or {}
    return {
        "id": source + ":" + text(identity), "project_id": pid,
        "project": text(project.get("site_name") or pid), "source": source,
        "source_id": text(identity), "category": category, "filename": filename,
        "title": filename, "reference": text(record.get("invoice_number") or header.get("document_number")),
        "version": record.get("version") or record.get("revision") or "",
        "status": "active" if record.get("is_active") else text(record.get("status")) or "available",
        "uploaded_at": iso(record.get("uploaded_at") or record.get("created_at")),
        "uploaded_by": text(record.get("uploaded_by_email") or record.get("uploaded_by")),
        "size": record.get("source_size_bytes"), "group_id": group or source + ":" + text(identity),
        "description": "", "tags": [], "archived": False,
        "_path": text(record.get("storage_path")), "_url": text(record.get("source_url")),
        "_hash": text(record.get("source_sha256")), "_project": project,
    }


def catalog_rows(user):
    allowed = authorized_projects(user)
    by_id = {project_id(p): p for p in allowed}
    rows = []
    sources = [
        (raw_schedule_baselines_collection, "baseline", "schedule", "baseline_id"),
        (raw_schedule_updates_collection, "schedule-update", "schedule", "update_id"),
        (raw_budget_boqs_collection, "boq", "boq", "boq_id"),
        (raw_budget_invoices_collection, "invoice", "invoice", "invoice_id"),
        (raw_material_documents_collection, "material", "materials", "document_id"),
        (raw_safety_records_collection, "safety-report", "report", "record_id"),
    ]
    for collection, source, category, key in sources:
        query = source_scope(allowed)
        if source == "baseline":
            query["removed_at"] = None
        if source == "safety-report":
            query.update(record_type="daily_report", status="finalized")
        for record in collection.find(query, SOURCE_FIELDS):
            project = by_id.get(text(record.get("project_id")))
            if not project or not record.get(key):
                continue
            group = source + ":" + project_id(project) if source in {"baseline", "boq", "schedule-update"} else None
            row = _row(record, project, source, category, record[key], group)
            if source != "safety-report" and not row["filename"]:
                continue
            row["document_type"] = text(record.get("document_type"))
            if source == "material" and record.get("document_type") == "boq":
                row.update(category="boq", group_id="material-boq:" + project_id(project))
            if source == "safety-report":
                # Match the existing audit-report download permission.
                if not owns(project, user):
                    continue
                row.update(filename=f"Safety-{record.get('record_date')}-r{record.get('revision', 1)}.pdf", group_id=f"safety-report:{project_id(project)}:{record.get('record_date')}")
                row["title"] = row["filename"]
                row["uploaded_at"] = iso(record.get("finalized_at") or record.get("created_at"))
                row["uploaded_by"] = text((record.get("finalized_by") or {}).get("email")) or row["uploaded_by"]
            rows.append(row)
    evidence = list(raw_schedule_evidence_collection.find(source_scope(allowed), {
        field: 1 for field in ("project_id", "evidence_id", "image_url", "activity_name", "activity_id", "tour_id", "uploaded_at", "created_at", "captured_at", "status", "uploaded_by_email")
    }))
    tours = {text(t.get("tour_id")): t for t in raw_tours_collection.find({"tour_id": {"$in": list({text(e.get("tour_id")) for e in evidence if e.get("tour_id")})}}, {field: 1 for field in ("tour_id", "storage_key", "project_id", "site_name", "site", "floorplan_id", "owner_user_id", "owner_email")})}
    for record in evidence:
        project = by_id.get(text(record.get("project_id")))
        image = text(record.get("image_url"))
        if not project or not record.get("evidence_id") or not image:
            continue
        path = unquote(urlparse(image).path)
        tour = tours.get(text(record.get("tour_id")))
        if path.startswith("/streetview/"):
            if not _evidence_tour_matches(tour, project):
                continue
            key = path[len("/streetview/"):].split("/")[0]
            if key not in {text(tour.get("storage_key")), text(tour.get("tour_id"))}:
                continue
        elif not path.startswith("/sites/"):
            continue
        row = _row({**record, "source_url": image}, project, "activity-evidence", "schedule", record["evidence_id"])
        row.update(activity_name=text(record.get("activity_name")), activity_id=text(record.get("activity_id")), _tour=tour)
        row["title"] = (row["activity_name"] + " · " if row["activity_name"] else "") + "Site capture"
        rows.append(row)
    for record in documents.find(source_scope(allowed), {"_id": 0}):
        row = _row(record, by_id[record["project_id"]], "library", record["category"], record["document_id"], record["group_id"])
        row.update({key: record.get(key, row.get(key)) for key in ("title", "description", "reference", "tags", "archived")})
        rows.append(row)
    for project in allowed:
        for key in ("schedule_zone_plan", "proposed_schedule_zone_plan"):
            plan = project.get(key) or {}
            if plan.get("source_url"):
                row = _row({**plan, "status": plan.get("confirmation_status")}, project, "zone-plan", "drawing", plan.get("zone_plan_id") or key + ":" + project_id(project), "zone-plan:" + project_id(project))
                rows.append(row)
        image = text(project.get("image_url") or project.get("imageUrl") or project.get("floorplan_url"))
        if image and urlparse(image).path.startswith("/sites/"):
            filename = Path(unquote(urlparse(image).path)).name
            rows.append(_row({"source_filename": filename, "source_url": image, "created_at": project.get("created_at")}, project, "floorplan", "drawing", project_id(project)))
        # Asset uploads retain extracted DXFs, not the original ZIP package.
        found = set()
        for storage_root in site_storage_roots(owner_email=project.get("owner_email"), owner_user_id=project.get("owner_user_id")):
            root = Path(storage_root).resolve()
            for key in dict.fromkeys((project_id(project), text(project.get("id")), text(project.get("dxf_project_id")), text(project.get("site_name")))):
                if not key or key in {".", ".."} or any(c in key for c in ("/", "\\", "\x00", ":")):
                    continue
                directory = root / key / SITE_DXF_DIRNAME
                if not directory.is_dir():
                    continue
                for path in directory.glob("*.dxf"):
                    resolved = path.resolve()
                    if not resolved.is_relative_to((root / key).resolve()) or path.name in found:
                        continue
                    found.add(path.name)
                    stat = resolved.stat()
                    rows.append(_row({"source_filename": path.name, "storage_path": str(resolved), "source_size_bytes": stat.st_size, "uploaded_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)}, project, "dxf", "drawing", project_id(project) + ":" + path.name))
    overlays = {m["document_id"]: m for m in metadata.find({"document_id": {"$in": [r["id"] for r in rows]}}, {"_id": 0})}
    for row in rows:
        overlay = overlays.get(row["id"], {})
        row.update({key: overlay[key] for key in ("title", "reference", "description", "tags", "archived") if key in overlay})
        row["can_manage"] = owns(row["_project"], user)
        row["extension"] = Path(row["filename"]).suffix.lower().lstrip(".")
    # A material BOQ imported into Budget can share one physical source file.
    combined = []
    seen = {}
    for row in rows:
        identity = (row["project_id"], row["_hash"], row["category"])
        existing = seen.get(identity) if row["_hash"] and row["category"] == "boq" else None
        row["links"] = [{"source": row["source"], "id": row["source_id"], "status": row["status"]}]
        if existing and existing["source"] != row["source"]:
            existing["links"].extend(row["links"])
        else:
            combined.append(row)
            if row["_hash"]:
                seen[identity] = row
    newest = {}
    for row in combined:
        rank = (int(row["version"]) if str(row["version"]).isdigit() else 0, row["uploaded_at"], row["id"])
        if row["group_id"] not in newest or rank > newest[row["group_id"]][0]:
            newest[row["group_id"]] = (rank, row["id"])
    for row in combined:
        row["latest"] = row["id"] == newest[row["group_id"]][1]
    return allowed, combined


def public(row):
    return {key: value for key, value in row.items() if not key.startswith("_") and key != "group_id"}


def _evidence_tour_matches(tour, project):
    if not tour:
        return False
    keys = {project_id(project), text(project.get("id")), text(project.get("site_name")), text(project.get("dxf_project_id"))} - {""}
    if not any(text(tour.get(key)) in keys for key in ("project_id", "site_name", "site", "floorplan_id")):
        return False
    # A tour linked by an activity must belong to the same project owner.
    if tour.get("owner_user_id") or tour.get("owner_email"):
        return bool((tour.get("owner_user_id") and tour.get("owner_user_id") == project.get("owner_user_id")) or
                    (text(tour.get("owner_email")).lower() and text(tour.get("owner_email")).lower() == text(project.get("owner_email")).lower()))
    return True


def list_documents(user, *, project="", category="", query="", status="", uploader="", after="", before="", latest=True, archived=False, page=1, limit=30):
    if page < 1 or not 1 <= limit <= 100:
        raise HTTPException(400, "Choose a valid document page")
    for date in (after, before):
        if date:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError()
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Choose a valid upload date")
    if after and before and after > before:
        raise HTTPException(400, "The start date must be before the end date")
    allowed, rows = catalog_rows(user)
    if project:
        selected = require_project(project, user)
        rows = [r for r in rows if r["project_id"] == project_id(selected)]
    rows = [r for r in rows if bool(r["archived"]) == archived and (not latest or r["latest"])]
    counts = {"all": len(rows)}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    rows = [r for r in rows if (not category or r["category"] == category) and
            (not status or r["status"] == status) and (not uploader or r["uploaded_by"] == uploader) and
            (not after or r["uploaded_at"][:10] >= after) and (not before or r["uploaded_at"][:10] <= before) and
            (not query or query.casefold() in " ".join([r["filename"], r["title"], r["project"], r["reference"], r["description"], *r["tags"]]).casefold())]
    rows.sort(key=lambda r: (r["uploaded_at"], r["id"]), reverse=True)
    total = len(rows)
    return {"documents": [public(r) for r in rows[(page-1)*limit:page*limit]], "total": total, "page": page, "limit": limit, "counts": counts,
            "projects": [{"id": project_id(p), "name": text(p.get("site_name") or project_id(p)), "can_manage": owns(p, user)} for p in allowed]}


def get_document(identity, user):
    _, rows = catalog_rows(user)
    row = next((r for r in rows if r["id"] == identity or any(link["source"] + ":" + link["id"] == identity for link in r["links"])), None)
    if not row:
        raise HTTPException(404, "Document not found")
    return row, rows


def document_detail(identity, user):
    row, rows = get_document(identity, user)
    versions = sorted([public(r) for r in rows if r["group_id"] == row["group_id"]], key=lambda r: (r["latest"], r["uploaded_at"]), reverse=True)
    history = [{"action": e["action"], "by": e["by"], "at": iso(e["created_at"])} for e in events.find({"group_id": row["group_id"], "project_id": row["project_id"]}, {"_id": 0}).sort("created_at", -1).limit(50)]
    return {"document": public(row), "versions": versions, "history": history}


def document_file(identity, user):
    row, _ = get_document(identity, user)
    if row["source"] == "safety-report":
        return row, None
    project = row["_project"]
    url = unquote(urlparse(row["_url"]).path)
    if row["source"] == "activity-evidence" and url.startswith("/streetview/"):
        tour = row.get("_tour")
        if not _evidence_tour_matches(tour, project):
            raise HTTPException(404, "Activity attachment not found")
        relative = url[len("/streetview/"):]
        key, _, rest = relative.partition("/")
        keys = {text(tour.get("storage_key")), text(tour.get("tour_id"))} - {""}
        if key in keys and rest:
            for storage_root in tour_storage_roots(owner_email=tour.get("owner_email"), owner_user_id=tour.get("owner_user_id"), site_name=tour.get("site_name") or tour.get("site") or project.get("site_name")):
                root = Path(storage_root).resolve()
                for alias in keys:
                    directory = (root / alias).resolve()
                    path = (directory / rest).resolve()
                    if directory.is_relative_to(root) and directory != root and path.is_relative_to(directory) and path.is_file():
                        return row, path
        raise HTTPException(404, "The activity attachment is unavailable. Open the activity to view its context.")
    roots = [Path(p).resolve() for p in site_storage_roots(owner_email=project.get("owner_email"), owner_user_id=project.get("owner_user_id"))]
    candidates = [Path(row["_path"])] if row["_path"] else []
    if url.startswith("/sites/"):
        relative = url[len("/sites/"):]
        candidates.extend(root / relative for root in roots)
    keys = {project_id(project), text(project.get("site_name")), text(project.get("id")), text(project.get("dxf_project_id"))}
    for candidate in candidates:
        path = candidate.resolve()
        for root in roots:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] in keys and path.is_file():
                return row, path
    raise HTTPException(404, "The original file is unavailable. Its project record is still available.")


def clean_metadata(payload):
    result = {}
    for key, max_length in (("title", 250), ("reference", 120), ("description", 2000)):
        if key in payload:
            value = text(payload[key])
            if len(value) > max_length or key == "title" and not value:
                raise HTTPException(400, f"Invalid document {key}")
            result[key] = value
    if "tags" in payload:
        if not isinstance(payload["tags"], list) or len(payload["tags"]) > 12 or any(not isinstance(t, str) or not t.strip() or len(t) > 50 for t in payload["tags"]):
            raise HTTPException(400, "Use at most 12 tags of 50 characters")
        result["tags"] = list(dict.fromkeys(t.strip() for t in payload["tags"]))
    if "archived" in payload:
        if not isinstance(payload["archived"], bool):
            raise HTTPException(400, "Invalid archive status")
        result["archived"] = payload["archived"]
    if not result:
        raise HTTPException(400, "Choose document details to update")
    return result


def update_document(identity, payload, user):
    row, _ = get_document(identity, user)
    identity = row["id"]
    require_project(row["project_id"], user, True)
    if not isinstance(payload, dict) or set(payload) - {"title", "reference", "description", "tags", "archived"}:
        raise HTTPException(400, "Only library details can be updated here")
    values = clean_metadata(payload)
    now = datetime.now(timezone.utc)
    if row["source"] == "library":
        documents.update_many({"project_id": row["project_id"], "group_id": row["group_id"]}, {"$set": values})
    else:
        metadata.update_one({"document_id": identity}, {"$set": {**values, "project_id": row["project_id"], "owner_user_id": row["_project"].get("owner_user_id"), "owner_email": row["_project"].get("owner_email")}}, upsert=True)
    events.insert_one({"project_id": row["project_id"], "owner_user_id": row["_project"].get("owner_user_id"), "owner_email": row["_project"].get("owner_email"), "group_id": row["group_id"], "document_id": identity, "action": "restored" if values.get("archived") is False else "archived" if values.get("archived") else "details_updated", "by": user.email, "created_at": now})
    return document_detail(identity, user)


def upload_document(project_ref, filename, content, category, payload, user, *, revision_of="", client_reference=""):
    project = require_project(project_ref, user, True)
    pid = project_id(project)
    if category not in GENERAL_TYPES:
        raise HTTPException(400, "Upload operational documents through their project workflow")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", client_reference):
        raise HTTPException(400, "A valid upload reference is required")
    existing = documents.find_one({"project_id": pid, "client_reference": client_reference})
    if existing:
        if existing["source_sha256"] != hashlib.sha256(content).hexdigest() or existing["category"] != category:
            raise HTTPException(409, "This upload reference belongs to a different file. Choose the file again to start a new upload.")
        return {**document_detail("library:" + existing["document_id"], user), "duplicate": True}
    safe_name = re.sub(r"[\x00-\x1f\x7f]", "_", Path(text(filename).replace("\\", "/")).name)
    extension = Path(safe_name).suffix.lower()
    if extension not in {".pdf", ".png", ".jpg", ".jpeg", ".docx", ".xlsx"} or not content or len(content) > MAX_FILE_BYTES:
        raise HTTPException(400, "Choose a PDF, image, DOCX or XLSX file of at most 25 MB")
    signatures = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".docx": b"PK", ".xlsx": b"PK"}
    if not content.startswith(signatures[extension]) or category == "report" and extension != ".pdf":
        raise HTTPException(400, "The file contents do not match its document type")
    if extension in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                required = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
                if required not in archive.namelist() or "[Content_Types].xml" not in archive.namelist():
                    raise ValueError()
        except (ValueError, zipfile.BadZipFile):
            raise HTTPException(400, "The file is not a valid Office document")
    values = clean_metadata({**payload, "title": text(payload.get("title")) or safe_name})
    parent = None
    if revision_of:
        parent, _ = get_document(revision_of, user)
        if parent["project_id"] != pid or parent["source"] != "library" or parent["category"] != category:
            raise HTTPException(400, "Choose a general document in this project and category to revise")
        if not parent["latest"]:
            raise HTTPException(409, "A newer revision exists. Open the latest revision first.")
        if parent["archived"]:
            raise HTTPException(409, "Restore this document before uploading a revision")
    digest = hashlib.sha256(content).hexdigest()
    duplicate = documents.find_one({"project_id": pid, "source_sha256": digest, "category": category, **({"group_id": parent["group_id"]} if parent else {})})
    if duplicate:
        return {**document_detail("library:" + duplicate["document_id"], user), "duplicate": True}
    identity = "doc_" + uuid4().hex
    group = parent["group_id"] if parent else identity
    counter = groups.find_one_and_update({"group_id": group}, {"$inc": {"next_version": 1}, "$setOnInsert": {"project_id": pid, "owner_user_id": project.get("owner_user_id"), "owner_email": project.get("owner_email")}}, upsert=True, return_document=ReturnDocument.AFTER)
    version = counter["next_version"]
    root = Path(site_storage_roots(owner_email=project.get("owner_email"), owner_user_id=project.get("owner_user_id"))[0]).resolve()
    directory = (root / pid / "documents").resolve()
    if directory.parent.parent != root:
        raise HTTPException(400, "Invalid project storage identifier")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (identity + extension)
    now = datetime.now(timezone.utc)
    record = {"document_id": identity, "group_id": group, "project_id": pid, "category": category, "version": version,
              "original_filename": safe_name, "storage_path": str(path), "source_size_bytes": len(content), "source_sha256": digest,
              "status": "available", "uploaded_at": now, "uploaded_by_email": user.email, "owner_user_id": project.get("owner_user_id"),
              "owner_email": project.get("owner_email"), "client_reference": client_reference, "archived": False, **values}
    path.write_bytes(content)
    try:
        documents.insert_one(record)
    except DuplicateKeyError:
        path.unlink(missing_ok=True)
        existing = documents.find_one({"project_id": pid, "client_reference": client_reference})
        if not existing:
            raise HTTPException(409, "This revision changed while uploading. Refresh and retry.")
        if existing["source_sha256"] != digest or existing["category"] != category:
            raise HTTPException(409, "This upload reference belongs to a different file. Choose the file again to start a new upload.")
        return {**document_detail("library:" + existing["document_id"], user), "duplicate": True}
    except Exception:
        path.unlink(missing_ok=True)
        raise
    events.insert_one({"project_id": pid, "owner_user_id": project.get("owner_user_id"), "owner_email": project.get("owner_email"), "group_id": group, "document_id": "library:" + identity, "action": "revision_uploaded" if parent else "uploaded", "by": user.email, "created_at": now})
    return document_detail("library:" + identity, user)
