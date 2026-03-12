# Comments routes: create, list, and report issues.
# Delegates work to the comments service layer.

# api/comments.py

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional

from services.features.comments import comments_service

router = APIRouter(prefix="/api/comments", tags=["Comments"])


# =========================================================
# Schemas
# =========================================================
class IssueAttachment(BaseModel):
    name: str
    type: str
    size: int
    data_url: str


class IssueCreate(BaseModel):
    tour_id: str
    pano_id: str
    title: str
    description: Optional[str] = None
    department: Optional[str] = None
    created_by: Optional[str] = None
    completion_date: Optional[str] = None
    severity: Optional[str] = None
    yaw: Optional[float] = None
    pitch: Optional[float] = None

    issue_type: Optional[str] = None
    priority: Optional[str] = None
    problem_description: Optional[str] = None
    action_required: Optional[str] = None
    assigned_to: Optional[str] = None
    assigned_to_type: Optional[str] = None
    assigned_to_detail: Optional[str] = None
    target_completion_date: Optional[str] = None
    root_cause: Optional[str] = None
    reference_type: Optional[str] = None
    image_attachments: Optional[List[IssueAttachment]] = None
    document_attachments: Optional[List[IssueAttachment]] = None


# =========================================================
# Get ALL comments of a tour
# =========================================================
@router.get("/all/{tour_id}")
def get_all_comments(tour_id: str):
    return comments_service.get_all_comments(tour_id)


# =========================================================
# Generate Issue PDF Report
# =========================================================
@router.get("/report/{comment_id}")
def generate_comment_report(comment_id: str):
    pdf_path = comments_service.build_comment_report(comment_id)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=os.path.basename(pdf_path),
    )


# =========================================================
# Get comments for single pano
# =========================================================
@router.get("/{tour_id}/{pano_id}")
def get_comments(tour_id: str, pano_id: str, include_shared: bool = False, radius_px: int = 40):
    return comments_service.get_comments_for_pano(
        tour_id=tour_id,
        pano_id=pano_id,
        include_shared=include_shared,
        radius_px=radius_px,
    )


# =========================================================
# Save single comment
# =========================================================
@router.post("")
def save_comment(issue: IssueCreate):
    return comments_service.create_comment(issue.dict(exclude_none=True))


# =========================================================
# Delete single comment
# =========================================================
@router.delete("/{comment_id}")
def delete_comment_route(comment_id: str):
    return comments_service.delete_comment(comment_id)


# =========================================================
# Update comment
# =========================================================
@router.put("/update/{comment_id}")
def update_comment_route(comment_id: str, payload: dict):
    return comments_service.update_comment(comment_id, payload)
