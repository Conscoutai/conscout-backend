from __future__ import annotations

import mimetypes
from typing import Optional
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from core.auth import require_authenticated_user
from core.auth_context import AuthenticatedUser
from services import documents_service as service

router = APIRouter(tags=["Documents"])


@router.get("/workspace/documents")
def list_documents(project: str = "", category: str = "", q: str = Query("", max_length=250), status: str = "", uploader: str = "", after: str = "", before: str = "", latest: bool = True, archived: bool = False, page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100), user: AuthenticatedUser = Depends(require_authenticated_user)):
    return service.list_documents(user, project=project, category=category, query=q, status=status, uploader=uploader, after=after, before=before, latest=latest, archived=archived, page=page, limit=limit)


@router.get("/workspace/documents/{identity}")
def detail(identity: str, user: AuthenticatedUser = Depends(require_authenticated_user)):
    return service.document_detail(identity, user)


class DocumentDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, max_length=250)
    reference: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    tags: Optional[list[str]] = None
    archived: Optional[bool] = None


@router.patch("/workspace/documents/{identity}")
def update(identity: str, payload: DocumentDetails, user: AuthenticatedUser = Depends(require_authenticated_user)):
    return service.update_document(identity, payload.model_dump(exclude_unset=True), user)


@router.post("/projects/{project}/documents")
async def upload(project: str, file: UploadFile = File(...), category: str = Form("other"), title: str = Form(""), reference: str = Form(""), description: str = Form(""), tags: str = Form(""), revision_of: str = Form(""), client_reference: str = Form(...), user: AuthenticatedUser = Depends(require_authenticated_user)):
    content = await file.read(service.MAX_FILE_BYTES + 1)
    return service.upload_document(project, file.filename, content, category, {"title": title or file.filename, "reference": reference, "description": description, "tags": [t.strip() for t in tags.split(",") if t.strip()]}, user, revision_of=revision_of, client_reference=client_reference)


@router.get("/workspace/documents/{identity}/file")
def download(identity: str, preview: bool = False, user: AuthenticatedUser = Depends(require_authenticated_user)):
    row, path = service.document_file(identity, user)
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if row["source"] == "safety-report":
        from services.safety.safety_service import render_daily_report_pdf
        content, filename = render_daily_report_pdf(row["project_id"], row["source_id"])
        headers["Content-Disposition"] = f'inline; filename="{filename}"' if preview else f'attachment; filename="{filename}"'
        return Response(content=content, media_type="application/pdf", headers=headers)
    mime = mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"
    inline = preview and row["extension"] in {"pdf", "png", "jpg", "jpeg"}
    return FileResponse(path, media_type=mime, filename=row["filename"], content_disposition_type="inline" if inline else "attachment", headers=headers)
