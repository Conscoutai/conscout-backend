from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.auth import (
    ADMIN_ACCOUNT_ROLES,
    account_role_for_user,
    ensure_subscription_admin_user,
    require_authenticated_user,
)
from core.auth_context import AuthenticatedUser
from core.config import APP_SURFACE, DATA_DIR
from core.database import (
    raw_admins_collection,
    raw_ai_quality_flags_collection,
    raw_helpdesk_messages_collection,
    raw_helpdesk_tickets_collection,
)


router = APIRouter(tags=["Helpdesk"])

TICKET_CATEGORIES = {
    "technical_issue",
    "ai_response",
    "account_billing",
    "feature_request",
    "complaint",
    "other",
}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}
TICKET_PRIORITY_RANKS = {"low": 1, "normal": 2, "high": 3, "urgent": 4}
TICKET_STATUSES = {
    "open",
    "in_progress",
    "waiting_for_user",
    "resolved",
    "closed",
}
TICKET_APPS = {"web", "mobile", "lite"}
AI_FLAG_REASONS = {"incorrect", "unhelpful", "unsafe", "outdated", "other"}
AI_FLAG_STATUSES = {"open", "in_review", "resolved"}
AI_RESOLUTION_LABELS = {
    "accepted",
    "rejected",
    "knowledge_base_fix",
    "model_review",
}
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
}
MAX_ATTACHMENTS_PER_MESSAGE = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_TICKETS_PER_PAGE = 500


class AdminTicketUpdate(BaseModel):
    status: str = ""
    priority: str = ""
    assign_to_me: bool = False
    unassign: bool = False
    assigned_admin_user_id: str = ""


class AIFlagCreate(BaseModel):
    reason: str
    prompt: str = ""
    response: str = Field(min_length=1, max_length=50000)
    feedback: str = Field(default="", max_length=5000)
    project_context: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    model_version: str = ""
    app: str = "web"
    source_message_id: str = ""
    create_support_ticket: bool = True


class AIFlagResolution(BaseModel):
    status: str = "resolved"
    resolution_label: str
    resolution_notes: str = Field(default="", max_length=5000)
    knowledge_base_reference: str = Field(default="", max_length=500)
    model_task_reference: str = Field(default="", max_length=500)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalized_email(value: Any) -> str:
    return _clean(value).lower()


def _normalize_choice(value: Any, allowed: set[str], field_name: str) -> str:
    normalized = _clean(value).lower().replace("-", "_").replace(" ", "_")
    if normalized not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}. Expected one of: {', '.join(sorted(allowed))}.",
        )
    return normalized


def _normalize_app(value: Any) -> str:
    normalized = _clean(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "conscout_web": "web",
        "main_web": "web",
        "conscout_mobile": "mobile",
        "main_mobile": "mobile",
        "conscout_lite": "lite",
    }
    normalized = aliases.get(normalized, normalized)
    return _normalize_choice(normalized, TICKET_APPS, "app")


def _display_name(user: AuthenticatedUser) -> str:
    if user.name.strip():
        return user.name.strip()
    email = user.email.strip().lower()
    return email.split("@", 1)[0] if "@" in email else "ConScout user"


def _require_helpdesk_admin(current_user: AuthenticatedUser) -> str:
    return ensure_subscription_admin_user(current_user, required_role="admin")


def _is_helpdesk_admin(current_user: AuthenticatedUser) -> bool:
    admin = raw_admins_collection.find_one(
        {"user_id": current_user.user_id},
        {"account_role": 1, "is_subscription_admin": 1, "email": 1},
    )
    return bool(admin and account_role_for_user(admin) in ADMIN_ACCOUNT_ROLES)


def _ticket_number() -> str:
    for _ in range(5):
        candidate = f"CS-{uuid.uuid4().hex[:8].upper()}"
        if not raw_helpdesk_tickets_collection.find_one(
            {"ticket_number": candidate}, {"_id": 1}
        ):
            return candidate
    return f"CS-{uuid.uuid4().hex.upper()}"


def _safe_filename(value: str) -> str:
    name = os.path.basename(value.replace("\\", "/"))
    stem, extension = os.path.splitext(name)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    safe_extension = re.sub(r"[^A-Za-z0-9.]", "", extension).lower()
    return f"{safe_stem or 'attachment'}{safe_extension}"


def _ticket_storage_dir(ticket_id: str) -> str:
    return os.path.join(DATA_DIR, "helpdesk", ticket_id)


async def _save_attachments(
    *, ticket_id: str, uploads: Optional[list[UploadFile]]
) -> list[dict[str, Any]]:
    files = list(uploads or [])
    if len(files) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail=f"A maximum of {MAX_ATTACHMENTS_PER_MESSAGE} attachments is allowed.",
        )

    saved: list[dict[str, Any]] = []
    storage_dir = _ticket_storage_dir(ticket_id)
    try:
        for upload in files:
            original_name = _safe_filename(upload.filename or "attachment")
            extension = os.path.splitext(original_name)[1].lower()
            if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported attachment type: {extension or 'unknown'}.",
                )
            content = await upload.read(MAX_ATTACHMENT_BYTES + 1)
            if len(content) > MAX_ATTACHMENT_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"{original_name} exceeds the 10 MB attachment limit.",
                )
            attachment_id = uuid.uuid4().hex
            stored_name = f"{attachment_id}-{original_name}"
            os.makedirs(storage_dir, exist_ok=True)
            path = os.path.join(storage_dir, stored_name)
            with open(path, "wb") as output:
                output.write(content)
            saved.append(
                {
                    "attachment_id": attachment_id,
                    "name": original_name,
                    "content_type": _clean(upload.content_type)
                    or "application/octet-stream",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "stored_name": stored_name,
                }
            )
    except Exception:
        for attachment in saved:
            try:
                os.remove(os.path.join(storage_dir, attachment["stored_name"]))
            except FileNotFoundError:
                pass
        raise
    finally:
        for upload in files:
            await upload.close()
    return saved


def _public_attachment(ticket_id: str, attachment: dict[str, Any]) -> dict[str, Any]:
    attachment_id = _clean(attachment.get("attachment_id"))
    return {
        "id": attachment_id,
        "name": _clean(attachment.get("name")),
        "content_type": _clean(attachment.get("content_type")),
        "size": int(attachment.get("size") or 0),
        "download_url": (
            f"/helpdesk/tickets/{ticket_id}/attachments/{attachment_id}"
        ),
    }


def _serialize_message(message: dict[str, Any]) -> dict[str, Any]:
    ticket_id = _clean(message.get("ticket_id"))
    return {
        "id": _clean(message.get("message_id")),
        "author_type": _clean(message.get("author_type")),
        "author_name": _clean(message.get("author_name")),
        "body": _clean(message.get("body")),
        "event_type": _clean(message.get("event_type")),
        "event_from": _clean(message.get("event_from")),
        "event_to": _clean(message.get("event_to")),
        "internal_note": message.get("internal_note") is True,
        "automated": message.get("automated") is True,
        "attachments": [
            _public_attachment(ticket_id, attachment)
            for attachment in message.get("attachments", [])
            if isinstance(attachment, dict)
        ],
        "created_at": int(message.get("created_at") or 0),
    }


def _activity_message(
    *,
    ticket_id: str,
    actor: AuthenticatedUser,
    event_type: str,
    event_from: str,
    event_to: str,
    body: str,
    created_at: int,
) -> dict[str, Any]:
    return {
        "message_id": uuid.uuid4().hex,
        "ticket_id": ticket_id,
        "author_type": "system",
        "author_user_id": actor.user_id,
        "author_email": _normalized_email(actor.email),
        "author_name": _display_name(actor),
        "body": body,
        "event_type": event_type,
        "event_from": event_from,
        "event_to": event_to,
        "internal_note": False,
        "automated": True,
        "attachments": [],
        "created_at": created_at,
    }


def _ticket_activity_messages(
    *,
    ticket: dict[str, Any],
    update: dict[str, Any],
    actor: AuthenticatedUser,
    starting_at: int,
    include_assignment: bool = True,
) -> list[dict[str, Any]]:
    ticket_id = _clean(ticket.get("ticket_id"))
    actor_name = _display_name(actor)
    messages: list[dict[str, Any]] = []

    previous_status = _clean(ticket.get("status")) or "open"
    next_status = _clean(update.get("status")) or previous_status
    if next_status != previous_status:
        status_bodies = {
            "open": f"{actor_name} reopened this ticket.",
            "in_progress": f"{actor_name} moved this ticket to In progress.",
            "waiting_for_user": f"{actor_name} is waiting for your reply.",
            "resolved": f"{actor_name} marked this ticket as resolved.",
            "closed": f"{actor_name} closed this ticket.",
        }
        messages.append(
            _activity_message(
                ticket_id=ticket_id,
                actor=actor,
                event_type="status_changed",
                event_from=previous_status,
                event_to=next_status,
                body=status_bodies[next_status],
                created_at=starting_at + len(messages),
            )
        )

    previous_priority = _clean(ticket.get("priority")) or "normal"
    next_priority = _clean(update.get("priority")) or previous_priority
    if next_priority != previous_priority:
        messages.append(
            _activity_message(
                ticket_id=ticket_id,
                actor=actor,
                event_type="priority_changed",
                event_from=previous_priority,
                event_to=next_priority,
                body=(
                    f"{actor_name} changed the ticket priority to "
                    f"{next_priority.replace('_', ' ').title()}."
                ),
                created_at=starting_at + len(messages),
            )
        )

    previous_assignee_id = _clean(ticket.get("assigned_admin_user_id"))
    next_assignee_id = _clean(
        update.get("assigned_admin_user_id", previous_assignee_id)
    )
    if include_assignment and next_assignee_id != previous_assignee_id:
        previous_name = _clean(ticket.get("assigned_admin_name"))
        next_name = _clean(update.get("assigned_admin_name"))
        if not next_assignee_id:
            body = f"{actor_name} returned this ticket to the Technical Support queue."
        elif not previous_assignee_id and next_assignee_id == actor.user_id:
            body = f"{next_name or actor_name} joined this ticket as your Technical Admin."
        elif previous_name:
            body = (
                f"{actor_name} reassigned this ticket from {previous_name} "
                f"to {next_name or 'Technical Support'}."
            )
        else:
            body = (
                f"{actor_name} assigned this ticket to "
                f"{next_name or 'Technical Support'}."
            )
        messages.append(
            _activity_message(
                ticket_id=ticket_id,
                actor=actor,
                event_type="assignment_changed",
                event_from=previous_assignee_id,
                event_to=next_assignee_id,
                body=body,
                created_at=starting_at + len(messages),
            )
        )

    return messages


def _message_map(
    tickets: list[dict[str, Any]], *, include_internal_notes: bool
) -> dict[str, list[dict[str, Any]]]:
    ticket_ids = [_clean(ticket.get("ticket_id")) for ticket in tickets]
    query: dict[str, Any] = {"ticket_id": {"$in": ticket_ids}}
    if not include_internal_notes:
        query["internal_note"] = {"$ne": True}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not ticket_ids:
        return grouped
    messages = raw_helpdesk_messages_collection.find(query).sort("created_at", 1)
    for message in messages:
        ticket_id = _clean(message.get("ticket_id"))
        grouped[ticket_id].append(_serialize_message(message))
    return grouped


def _serialize_ticket(
    ticket: dict[str, Any], *, messages: Optional[list[dict[str, Any]]] = None
) -> dict[str, Any]:
    return {
        "id": _clean(ticket.get("ticket_id")),
        "ticket_number": _clean(ticket.get("ticket_number")),
        "subject": _clean(ticket.get("subject")),
        "description": _clean(ticket.get("description")),
        "category": _clean(ticket.get("category")),
        "priority": _clean(ticket.get("priority")),
        "app": _clean(ticket.get("app")),
        "status": _clean(ticket.get("status")) or "open",
        "user": {
            "id": _clean(ticket.get("owner_user_id")),
            "name": _clean(ticket.get("owner_name")),
            "email": _clean(ticket.get("owner_email")),
        },
        "assigned_admin": {
            "id": _clean(ticket.get("assigned_admin_user_id")),
            "name": _clean(ticket.get("assigned_admin_name")),
            "email": _clean(ticket.get("assigned_admin_email")),
        },
        "attachment_count": int(ticket.get("attachment_count") or 0),
        "message_count": int(ticket.get("message_count") or 0),
        "created_at": int(ticket.get("created_at") or 0),
        "updated_at": int(ticket.get("updated_at") or 0),
        "resolved_at": int(ticket.get("resolved_at") or 0),
        "closed_at": int(ticket.get("closed_at") or 0),
        "messages": messages or [],
    }


def _find_owned_ticket(ticket_id: str, current_user: AuthenticatedUser) -> dict:
    normalized_id = _clean(ticket_id)
    ticket = raw_helpdesk_tickets_collection.find_one(
        {
            "ticket_id": normalized_id,
            "deleted_at": {"$in": [0, None]},
            "$or": [
                {"owner_user_id": current_user.user_id},
                {"owner_email": _normalized_email(current_user.email)},
            ],
        }
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found.")
    return ticket


def _find_admin_ticket(ticket_id: str, current_user: AuthenticatedUser) -> dict:
    _require_helpdesk_admin(current_user)
    ticket = raw_helpdesk_tickets_collection.find_one(
        {"ticket_id": _clean(ticket_id), "deleted_at": {"$in": [0, None]}}
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found.")
    return ticket


async def _create_ticket_record(
    *,
    current_user: AuthenticatedUser,
    subject: str,
    description: str,
    category: str,
    priority: str,
    app: str,
    uploads: Optional[list[UploadFile]] = None,
    linked_ai_flag_id: str = "",
) -> dict[str, Any]:
    normalized_subject = subject.strip()
    normalized_description = description.strip()
    if len(normalized_subject) < 4 or len(normalized_subject) > 200:
        raise HTTPException(
            status_code=400, detail="Subject must be between 4 and 200 characters."
        )
    if len(normalized_description) < 10 or len(normalized_description) > 10000:
        raise HTTPException(
            status_code=400,
            detail="Description must be between 10 and 10,000 characters.",
        )

    normalized_category = _normalize_choice(
        category, TICKET_CATEGORIES, "category"
    )
    normalized_priority = _normalize_choice(
        priority, TICKET_PRIORITIES, "priority"
    )
    normalized_app = _normalize_app(app)
    ticket_id = uuid.uuid4().hex
    attachments = await _save_attachments(ticket_id=ticket_id, uploads=uploads)
    now = _now_ms()
    acknowledgement_at = now + 1
    ticket_number = _ticket_number()
    ticket = {
        "ticket_id": ticket_id,
        "ticket_number": ticket_number,
        "subject": normalized_subject,
        "description": normalized_description,
        "category": normalized_category,
        "priority": normalized_priority,
        "priority_rank": TICKET_PRIORITY_RANKS[normalized_priority],
        "app": normalized_app,
        "product": APP_SURFACE,
        "status": "open",
        "owner_user_id": current_user.user_id,
        "owner_email": _normalized_email(current_user.email),
        "owner_name": _display_name(current_user),
        "assigned_admin_user_id": "",
        "assigned_admin_email": "",
        "assigned_admin_name": "",
        "attachment_count": len(attachments),
        "message_count": 2,
        "linked_ai_flag_id": linked_ai_flag_id,
        "created_at": now,
        "updated_at": now,
        "last_user_reply_at": now,
        "last_admin_reply_at": 0,
        "last_automated_reply_at": acknowledgement_at,
        "resolved_at": 0,
        "closed_at": 0,
    }
    message = {
        "message_id": uuid.uuid4().hex,
        "ticket_id": ticket_id,
        "author_type": "user",
        "author_user_id": current_user.user_id,
        "author_email": _normalized_email(current_user.email),
        "author_name": _display_name(current_user),
        "body": normalized_description,
        "internal_note": False,
        "automated": False,
        "attachments": attachments,
        "created_at": now,
    }
    acknowledgement = {
        "message_id": uuid.uuid4().hex,
        "ticket_id": ticket_id,
        "author_type": "support",
        "author_user_id": "system",
        "author_email": "support@conscout.com",
        "author_name": "ConScout Technical Support",
        "body": (
            f"Thanks for contacting ConScout Technical Support. We've received "
            f"your request as {ticket_number}. A Technical Admin will review it "
            "and respond in this conversation. You can add any further details "
            "here while our team investigates."
        ),
        "internal_note": False,
        "automated": True,
        "attachments": [],
        "created_at": acknowledgement_at,
    }
    try:
        raw_helpdesk_tickets_collection.insert_one(ticket)
        raw_helpdesk_messages_collection.insert_one(message)
        raw_helpdesk_messages_collection.insert_one(acknowledgement)
    except Exception:
        raw_helpdesk_tickets_collection.delete_one({"ticket_id": ticket_id})
        raw_helpdesk_messages_collection.delete_many({"ticket_id": ticket_id})
        shutil.rmtree(_ticket_storage_dir(ticket_id), ignore_errors=True)
        raise
    return _serialize_ticket(
        ticket,
        messages=[
            _serialize_message(message),
            _serialize_message(acknowledgement),
        ],
    )


@router.post("/helpdesk/tickets", status_code=201)
async def create_ticket(
    subject: str = Form(...),
    description: str = Form(...),
    category: str = Form(...),
    priority: str = Form("normal"),
    app: str = Form("web"),
    attachments: Optional[list[UploadFile]] = File(default=None),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = await _create_ticket_record(
        current_user=current_user,
        subject=subject,
        description=description,
        category=category,
        priority=priority,
        app=app,
        uploads=attachments,
    )
    return {"message": "Support ticket created.", "ticket": ticket}


@router.get("/helpdesk/tickets")
def list_tickets(
    status: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=MAX_TICKETS_PER_PAGE),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    query: dict[str, Any] = {
        "deleted_at": {"$in": [0, None]},
        "$or": [
            {"owner_user_id": current_user.user_id},
            {"owner_email": _normalized_email(current_user.email)},
        ]
    }
    normalized_status = status.strip().lower().replace("-", "_")
    if normalized_status and normalized_status != "all":
        query["status"] = _normalize_choice(
            normalized_status, TICKET_STATUSES, "status"
        )
    tickets = list(
        raw_helpdesk_tickets_collection.find(query)
        .sort("updated_at", -1)
        .limit(limit)
    )
    messages = _message_map(tickets, include_internal_notes=False)
    return {
        "tickets": [
            _serialize_ticket(
                ticket, messages=messages.get(_clean(ticket.get("ticket_id")), [])
            )
            for ticket in tickets
        ]
    }


@router.get("/helpdesk/tickets/{ticket_id}")
def get_ticket(
    ticket_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = _find_owned_ticket(ticket_id, current_user)
    messages = _message_map([ticket], include_internal_notes=False)
    return {
        "ticket": _serialize_ticket(
            ticket, messages=messages.get(_clean(ticket.get("ticket_id")), [])
        )
    }


@router.delete("/helpdesk/tickets/{ticket_id}")
def delete_ticket(
    ticket_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = _find_owned_ticket(ticket_id, current_user)
    now = _now_ms()
    raw_helpdesk_tickets_collection.update_one(
        {
            "ticket_id": _clean(ticket.get("ticket_id")),
            "deleted_at": {"$in": [0, None]},
        },
        {
            "$set": {
                "deleted_at": now,
                "deleted_by_user_id": current_user.user_id,
                "deleted_by_email": _normalized_email(current_user.email),
                "updated_at": now,
            }
        },
    )
    return {"message": "Support ticket deleted."}


@router.post("/helpdesk/tickets/{ticket_id}/replies", status_code=201)
async def reply_to_ticket(
    ticket_id: str,
    body: str = Form(...),
    attachments: Optional[list[UploadFile]] = File(default=None),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = _find_owned_ticket(ticket_id, current_user)
    if _clean(ticket.get("status")) == "closed":
        raise HTTPException(
            status_code=409,
            detail="Closed tickets cannot receive new replies. Create a new ticket instead.",
        )
    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 10000:
        raise HTTPException(
            status_code=400, detail="Reply must be between 1 and 10,000 characters."
        )
    saved = await _save_attachments(ticket_id=ticket_id, uploads=attachments)
    now = _now_ms()
    message = {
        "message_id": uuid.uuid4().hex,
        "ticket_id": _clean(ticket.get("ticket_id")),
        "author_type": "user",
        "author_user_id": current_user.user_id,
        "author_email": _normalized_email(current_user.email),
        "author_name": _display_name(current_user),
        "body": normalized_body,
        "internal_note": False,
        "automated": False,
        "attachments": saved,
        "created_at": now,
    }
    raw_helpdesk_messages_collection.insert_one(message)
    raw_helpdesk_tickets_collection.update_one(
        {"ticket_id": _clean(ticket.get("ticket_id"))},
        {
            "$set": {
                "status": "open",
                "updated_at": now,
                "last_user_reply_at": now,
                "resolved_at": 0,
            },
            "$inc": {"message_count": 1, "attachment_count": len(saved)},
        },
    )
    updated = raw_helpdesk_tickets_collection.find_one(
        {"ticket_id": _clean(ticket.get("ticket_id"))}
    ) or ticket
    messages = _message_map([updated], include_internal_notes=False)
    return {
        "message": "Reply sent.",
        "ticket": _serialize_ticket(
            updated,
            messages=messages.get(_clean(updated.get("ticket_id")), []),
        ),
    }


@router.get(
    "/helpdesk/tickets/{ticket_id}/attachments/{attachment_id}",
    response_class=FileResponse,
)
def download_attachment(
    ticket_id: str,
    attachment_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = raw_helpdesk_tickets_collection.find_one(
        {"ticket_id": _clean(ticket_id), "deleted_at": {"$in": [0, None]}}
    )
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found.")
    owns_ticket = (
        _clean(ticket.get("owner_user_id")) == current_user.user_id
        or _normalized_email(ticket.get("owner_email"))
        == _normalized_email(current_user.email)
    )
    is_admin = _is_helpdesk_admin(current_user)
    if not owns_ticket and not is_admin:
        raise HTTPException(status_code=403, detail="Attachment access denied.")

    message = raw_helpdesk_messages_collection.find_one(
        {
            "ticket_id": _clean(ticket_id),
            "attachments.attachment_id": _clean(attachment_id),
        }
    )
    if not message:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if message.get("internal_note") is True and not is_admin:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    attachment = next(
        (
            item
            for item in message.get("attachments", [])
            if isinstance(item, dict)
            and _clean(item.get("attachment_id")) == _clean(attachment_id)
        ),
        None,
    )
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    path = os.path.abspath(
        os.path.join(
            _ticket_storage_dir(_clean(ticket_id)),
            _clean(attachment.get("stored_name")),
        )
    )
    storage_root = os.path.abspath(_ticket_storage_dir(_clean(ticket_id)))
    if os.path.commonpath([storage_root, path]) != storage_root or not os.path.isfile(
        path
    ):
        raise HTTPException(status_code=404, detail="Attachment file not found.")
    return FileResponse(
        path,
        media_type=_clean(attachment.get("content_type"))
        or "application/octet-stream",
        filename=_clean(attachment.get("name")) or "attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/helpdesk/admin/tickets")
def admin_list_tickets(
    status: str = Query(default="all"),
    priority: str = Query(default="all"),
    assignment: str = Query(default="all"),
    search: str = Query(default="", max_length=200),
    limit: int = Query(default=200, ge=1, le=MAX_TICKETS_PER_PAGE),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _require_helpdesk_admin(current_user)
    query: dict[str, Any] = {"deleted_at": {"$in": [0, None]}}
    normalized_status = status.strip().lower().replace("-", "_")
    if normalized_status and normalized_status != "all":
        query["status"] = _normalize_choice(
            normalized_status, TICKET_STATUSES, "status"
        )
    normalized_priority = priority.strip().lower().replace("-", "_")
    if normalized_priority and normalized_priority != "all":
        query["priority"] = _normalize_choice(
            normalized_priority, TICKET_PRIORITIES, "priority"
        )
    normalized_assignment = assignment.strip().lower()
    if normalized_assignment == "mine":
        query["assigned_admin_user_id"] = current_user.user_id
    elif normalized_assignment == "unassigned":
        query["assigned_admin_user_id"] = {"$in": ["", None]}
    elif normalized_assignment not in {"", "all"}:
        raise HTTPException(status_code=400, detail="Invalid assignment filter.")
    if search.strip():
        pattern = re.escape(search.strip())
        query["$or"] = [
            {"ticket_number": {"$regex": pattern, "$options": "i"}},
            {"subject": {"$regex": pattern, "$options": "i"}},
            {"owner_name": {"$regex": pattern, "$options": "i"}},
            {"owner_email": {"$regex": pattern, "$options": "i"}},
        ]
    tickets = list(
        raw_helpdesk_tickets_collection.find(query)
        .sort([("priority_rank", -1), ("updated_at", -1)])
        .limit(limit)
    )
    messages = _message_map(tickets, include_internal_notes=True)
    return {
        "tickets": [
            _serialize_ticket(
                ticket, messages=messages.get(_clean(ticket.get("ticket_id")), [])
            )
            for ticket in tickets
        ]
    }


@router.patch("/helpdesk/admin/tickets/{ticket_id}")
def admin_update_ticket(
    ticket_id: str,
    payload: AdminTicketUpdate,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = _find_admin_ticket(ticket_id, current_user)
    now = max(_now_ms(), int(ticket.get("updated_at") or 0) + 2)
    update: dict[str, Any] = {"updated_at": now}
    if payload.status.strip():
        status = _normalize_choice(payload.status, TICKET_STATUSES, "status")
        update["status"] = status
        if status == "resolved":
            update["resolved_at"] = update["updated_at"]
        elif status != "closed":
            update["resolved_at"] = 0
        if status == "closed":
            update["closed_at"] = update["updated_at"]
        else:
            update["closed_at"] = 0
    if payload.priority.strip():
        priority = _normalize_choice(
            payload.priority, TICKET_PRIORITIES, "priority"
        )
        update["priority"] = priority
        update["priority_rank"] = TICKET_PRIORITY_RANKS[priority]

    if payload.assign_to_me:
        update.update(
            {
                "assigned_admin_user_id": current_user.user_id,
                "assigned_admin_email": _normalized_email(current_user.email),
                "assigned_admin_name": _display_name(current_user),
            }
        )
    elif payload.unassign:
        update.update(
            {
                "assigned_admin_user_id": "",
                "assigned_admin_email": "",
                "assigned_admin_name": "",
            }
        )
    elif payload.assigned_admin_user_id.strip():
        admin = raw_admins_collection.find_one(
            {"user_id": payload.assigned_admin_user_id.strip()}
        )
        if not admin or account_role_for_user(admin) not in ADMIN_ACCOUNT_ROLES:
            raise HTTPException(status_code=404, detail="Administrator not found.")
        update.update(
            {
                "assigned_admin_user_id": _clean(admin.get("user_id")),
                "assigned_admin_email": _normalized_email(admin.get("email")),
                "assigned_admin_name": _clean(admin.get("name")),
            }
        )

    activity_messages = _ticket_activity_messages(
        ticket=ticket,
        update=update,
        actor=current_user,
        starting_at=now,
    )
    for activity_message in activity_messages:
        raw_helpdesk_messages_collection.insert_one(activity_message)
    ticket_update: dict[str, Any] = {"$set": update}
    if activity_messages:
        ticket_update["$inc"] = {"message_count": len(activity_messages)}
    raw_helpdesk_tickets_collection.update_one(
        {"ticket_id": _clean(ticket.get("ticket_id"))}, ticket_update
    )
    refreshed = raw_helpdesk_tickets_collection.find_one(
        {"ticket_id": _clean(ticket.get("ticket_id"))}
    ) or {**ticket, **update}
    messages = _message_map([refreshed], include_internal_notes=True)
    return {
        "message": "Support ticket updated.",
        "ticket": _serialize_ticket(
            refreshed,
            messages=messages.get(_clean(refreshed.get("ticket_id")), []),
        ),
    }


@router.post("/helpdesk/admin/tickets/{ticket_id}/replies", status_code=201)
async def admin_reply_to_ticket(
    ticket_id: str,
    body: str = Form(...),
    internal_note: bool = Form(False),
    status: str = Form(""),
    attachments: Optional[list[UploadFile]] = File(default=None),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    ticket = _find_admin_ticket(ticket_id, current_user)
    normalized_body = body.strip()
    if not normalized_body or len(normalized_body) > 10000:
        raise HTTPException(
            status_code=400, detail="Reply must be between 1 and 10,000 characters."
        )
    saved = await _save_attachments(ticket_id=ticket_id, uploads=attachments)
    now = max(_now_ms(), int(ticket.get("updated_at") or 0) + 2)
    message = {
        "message_id": uuid.uuid4().hex,
        "ticket_id": _clean(ticket.get("ticket_id")),
        "author_type": "support",
        "author_user_id": current_user.user_id,
        "author_email": _normalized_email(current_user.email),
        "author_name": _display_name(current_user),
        "body": normalized_body,
        "internal_note": internal_note,
        "automated": False,
        "attachments": saved,
        "created_at": now,
    }
    raw_helpdesk_messages_collection.insert_one(message)
    next_status = _clean(status)
    if next_status:
        next_status = _normalize_choice(next_status, TICKET_STATUSES, "status")
    elif not internal_note and _clean(ticket.get("status")) == "open":
        next_status = "waiting_for_user"
    update: dict[str, Any] = {
        "updated_at": now,
        "last_admin_reply_at": now,
    }
    if not internal_note:
        update.update(
            {
                "assigned_admin_user_id": current_user.user_id,
                "assigned_admin_email": _normalized_email(current_user.email),
                "assigned_admin_name": _display_name(current_user),
            }
        )
    if next_status:
        update["status"] = next_status
        update["resolved_at"] = now if next_status == "resolved" else 0
        update["closed_at"] = now if next_status == "closed" else 0
    activity_messages = _ticket_activity_messages(
        ticket=ticket,
        update=update,
        actor=current_user,
        starting_at=now + 1,
        include_assignment=False,
    )
    for activity_message in activity_messages:
        raw_helpdesk_messages_collection.insert_one(activity_message)
    raw_helpdesk_tickets_collection.update_one(
        {"ticket_id": _clean(ticket.get("ticket_id"))},
        {
            "$set": update,
            "$inc": {
                "message_count": 1 + len(activity_messages),
                "attachment_count": len(saved),
            },
        },
    )
    refreshed = raw_helpdesk_tickets_collection.find_one(
        {"ticket_id": _clean(ticket.get("ticket_id"))}
    ) or {**ticket, **update}
    messages = _message_map([refreshed], include_internal_notes=True)
    return {
        "message": "Internal note added." if internal_note else "Reply sent.",
        "ticket": _serialize_ticket(
            refreshed,
            messages=messages.get(_clean(refreshed.get("ticket_id")), []),
        ),
    }


def _serialize_ai_flag(flag: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _clean(flag.get("flag_id")),
        "reason": _clean(flag.get("reason")),
        "prompt": _clean(flag.get("prompt")),
        "response": _clean(flag.get("response")),
        "feedback": _clean(flag.get("feedback")),
        "project_context": (
            flag.get("project_context")
            if isinstance(flag.get("project_context"), dict)
            else {}
        ),
        "model": _clean(flag.get("model")),
        "model_version": _clean(flag.get("model_version")),
        "app": _clean(flag.get("app")),
        "source_message_id": _clean(flag.get("source_message_id")),
        "status": _clean(flag.get("status")) or "open",
        "resolution_label": _clean(flag.get("resolution_label")),
        "resolution_notes": _clean(flag.get("resolution_notes")),
        "knowledge_base_reference": _clean(
            flag.get("knowledge_base_reference")
        ),
        "model_task_reference": _clean(flag.get("model_task_reference")),
        "linked_ticket_id": _clean(flag.get("linked_ticket_id")),
        "user": {
            "id": _clean(flag.get("owner_user_id")),
            "name": _clean(flag.get("owner_name")),
            "email": _clean(flag.get("owner_email")),
        },
        "created_at": int(flag.get("created_at") or 0),
        "updated_at": int(flag.get("updated_at") or 0),
        "resolved_at": int(flag.get("resolved_at") or 0),
    }


@router.post("/ai-quality/flags", status_code=201)
async def create_ai_quality_flag(
    payload: AIFlagCreate,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    reason = _normalize_choice(payload.reason, AI_FLAG_REASONS, "flag reason")
    app = _normalize_app(payload.app)
    now = _now_ms()
    flag_id = uuid.uuid4().hex
    flag = {
        "flag_id": flag_id,
        "reason": reason,
        "prompt": payload.prompt.strip(),
        "response": payload.response.strip(),
        "feedback": payload.feedback.strip(),
        "project_context": payload.project_context,
        "model": payload.model.strip(),
        "model_version": payload.model_version.strip(),
        "app": app,
        "product": APP_SURFACE,
        "source_message_id": payload.source_message_id.strip(),
        "owner_user_id": current_user.user_id,
        "owner_email": _normalized_email(current_user.email),
        "owner_name": _display_name(current_user),
        "status": "open",
        "resolution_label": "",
        "resolution_notes": "",
        "knowledge_base_reference": "",
        "model_task_reference": "",
        "linked_ticket_id": "",
        "created_at": now,
        "updated_at": now,
        "resolved_at": 0,
    }
    raw_ai_quality_flags_collection.insert_one(flag)
    linked_ticket: Optional[dict[str, Any]] = None
    if payload.create_support_ticket:
        subject = f"Flagged AI response: {reason.replace('_', ' ')}"
        description = payload.feedback.strip() or (
            "I flagged an AI response for quality review. "
            f"Reason: {reason.replace('_', ' ')}."
        )
        linked_ticket = await _create_ticket_record(
            current_user=current_user,
            subject=subject,
            description=description,
            category="ai_response",
            priority="normal",
            app=app,
            linked_ai_flag_id=flag_id,
        )
        raw_ai_quality_flags_collection.update_one(
            {"flag_id": flag_id},
            {
                "$set": {
                    "linked_ticket_id": linked_ticket["id"],
                    "updated_at": _now_ms(),
                }
            },
        )
        flag["linked_ticket_id"] = linked_ticket["id"]
    return {
        "message": "AI response flagged for review.",
        "flag": _serialize_ai_flag(flag),
        "ticket": linked_ticket,
    }


@router.get("/ai-quality/admin/flags")
def admin_list_ai_quality_flags(
    status: str = Query(default="all"),
    reason: str = Query(default="all"),
    limit: int = Query(default=200, ge=1, le=MAX_TICKETS_PER_PAGE),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _require_helpdesk_admin(current_user)
    query: dict[str, Any] = {}
    if status.strip().lower() != "all":
        query["status"] = _normalize_choice(status, AI_FLAG_STATUSES, "status")
    if reason.strip().lower() != "all":
        query["reason"] = _normalize_choice(reason, AI_FLAG_REASONS, "reason")
    flags = list(
        raw_ai_quality_flags_collection.find(query)
        .sort("created_at", -1)
        .limit(limit)
    )
    return {"flags": [_serialize_ai_flag(flag) for flag in flags]}


@router.patch("/ai-quality/admin/flags/{flag_id}")
def admin_resolve_ai_quality_flag(
    flag_id: str,
    payload: AIFlagResolution,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _require_helpdesk_admin(current_user)
    flag = raw_ai_quality_flags_collection.find_one({"flag_id": _clean(flag_id)})
    if not flag:
        raise HTTPException(status_code=404, detail="AI quality flag not found.")
    status = _normalize_choice(payload.status, AI_FLAG_STATUSES, "status")
    label = _normalize_choice(
        payload.resolution_label,
        AI_RESOLUTION_LABELS,
        "resolution label",
    )
    now = _now_ms()
    update = {
        "status": status,
        "resolution_label": label,
        "resolution_notes": payload.resolution_notes.strip(),
        "knowledge_base_reference": payload.knowledge_base_reference.strip(),
        "model_task_reference": payload.model_task_reference.strip(),
        "reviewed_by_user_id": current_user.user_id,
        "reviewed_by_email": _normalized_email(current_user.email),
        "updated_at": now,
        "resolved_at": now if status == "resolved" else 0,
    }
    raw_ai_quality_flags_collection.update_one(
        {"flag_id": _clean(flag_id)}, {"$set": update}
    )
    return {
        "message": "AI quality flag updated.",
        "flag": _serialize_ai_flag({**flag, **update}),
    }
