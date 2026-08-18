from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.auth_context import AuthenticatedUser
from core.database import raw_safety_records_collection
from services.safety.safety_service import DEFAULT_SAFETY_CONFIG, generate_daily_report


logger = logging.getLogger(__name__)
SYSTEM_USER = AuthenticatedUser(
    user_id="conscout-safety-scheduler",
    email="system@conscout.local",
    name="Conscout Safety Scheduler",
    role="admin",
)
_scheduler_started = False
_scheduler_lock = threading.Lock()


def due_report_date(
    config: dict[str, Any], *, now: Optional[datetime] = None
) -> Optional[str]:
    if config.get("auto_daily_reports") is not True:
        return None
    timezone_name = str(config.get("timezone") or "UTC").strip()
    cutoff = str(config.get("daily_report_cutoff") or "18:00").strip()
    try:
        hour, minute = [int(part) for part in cutoff.split(":", 1)]
        local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(timezone_name))
    except (ValueError, ZoneInfoNotFoundError):
        return None
    if (local_now.hour, local_now.minute) < (hour, minute):
        return None
    return local_now.date().isoformat()


def generate_due_daily_reports(*, now: Optional[datetime] = None) -> dict[str, int]:
    generated = 0
    existing = 0
    failed = 0
    configs = raw_safety_records_collection.find(
        {"record_type": "safety_config", "config.auto_daily_reports": True}
    )
    for document in configs:
        config = {**DEFAULT_SAFETY_CONFIG, **(document.get("config") or {})}
        record_date = due_report_date(config, now=now)
        if not record_date:
            continue
        project_id = str(document.get("project_id") or document.get("site_name") or "").strip()
        if not project_id:
            failed += 1
            continue
        if raw_safety_records_collection.find_one(
            {
                "project_id": project_id,
                "record_type": "daily_report",
                "record_date": record_date,
            },
            {"_id": 1},
        ):
            existing += 1
            continue
        try:
            generate_daily_report(
                project_id,
                record_date=record_date,
                user=SYSTEM_USER,
            )
            generated += 1
        except Exception:
            failed += 1
            logger.exception(
                "Automatic daily safety report failed for project %s", project_id
            )
    return {"generated": generated, "existing": existing, "failed": failed}


def run_daily_report_scheduler(poll_seconds: int = 60) -> None:
    while True:
        try:
            result = generate_due_daily_reports()
            if result["generated"] or result["failed"]:
                logger.info("Daily safety report scheduler result: %s", result)
        except Exception:
            logger.exception("Daily safety report scheduler failed")
        time.sleep(max(30, int(poll_seconds)))


def ensure_daily_report_scheduler_started() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        thread = threading.Thread(
            target=run_daily_report_scheduler,
            name="daily-safety-report-scheduler",
            daemon=True,
        )
        thread.start()
        _scheduler_started = True
        logger.info("Daily safety report scheduler started")
