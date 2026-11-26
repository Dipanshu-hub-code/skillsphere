from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import smtplib
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterator
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
    HAS_SLOWAPI = True
except ImportError:
    HAS_SLOWAPI = False

    class RateLimitExceeded(Exception):
        pass

    class Limiter:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def limit(self, *args: Any, **kwargs: Any) -> Any:
            def decorator(func: Any) -> Any:
                return func
            return decorator

    def get_remote_address(request: Request) -> str:  # type: ignore[no-redef]
        return "127.0.0.1"

    def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:  # type: ignore[no-redef]
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
from starlette.concurrency import run_in_threadpool
try:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.utils
    HAS_CLOUDINARY = True
except ImportError:
    from unittest.mock import MagicMock
    cloudinary = MagicMock()
    HAS_CLOUDINARY = False

try:
    import libsql_experimental as libsql
    HAS_TURSO = True
except ImportError:
    HAS_TURSO = False

try:
    from backend.career_chatbot import career_chatbot
except ImportError:
    from career_chatbot import career_chatbot

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = Path(os.environ.get("SKILLSPHERE_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
QUESTION_BANK_UPLOAD_DIR = DATA_DIR / "question_bank_uploads"
RECORDINGS_STORAGE_DIR = DATA_DIR / "recordings"
RECORDINGS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "skillsphere.sqlite3"
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_QUESTION_BANK_PDF_BYTES = 10 * 1024 * 1024
MAX_QUESTION_BANK_PDF_PAGES = 40
IS_PRODUCTION = os.environ.get("SKILLSPHERE_ENV", "development").lower() == "production"
TOKEN_SECRET = os.environ.get("SKILLSPHERE_SECRET_KEY")
if IS_PRODUCTION and not TOKEN_SECRET:
    raise RuntimeError("Set SKILLSPHERE_SECRET_KEY to a unique secret before starting in production")
TOKEN_SECRET = TOKEN_SECRET or "local-development-only-change-before-deploying"
TOKEN_TTL_SECONDS = 60 * 60 * 12
ASSESSMENT_TTL_HOURS = int(os.environ.get("SKILLSPHERE_ASSESSMENT_TTL_HOURS", "48"))
ASSESSMENT_DURATION_MINUTES = int(os.environ.get("SKILLSPHERE_ASSESSMENT_DURATION_MINUTES", "30"))
if ASSESSMENT_TTL_HOURS < 1 or ASSESSMENT_DURATION_MINUTES < 1:
    raise RuntimeError("Assessment TTL and duration settings must be positive integers")
try:
    ROUND1_PASS_PERCENTAGE = float(os.environ.get("SKILLSPHERE_ROUND1_PASS_PERCENTAGE", "75"))
except ValueError as error:
    raise RuntimeError("SKILLSPHERE_ROUND1_PASS_PERCENTAGE must be a number from 0 to 100") from error
if not 0 <= ROUND1_PASS_PERCENTAGE <= 100:
    raise RuntimeError("SKILLSPHERE_ROUND1_PASS_PERCENTAGE must be a number from 0 to 100")
ROUND1_ASSESSMENT_NAME = "Round 1: Technical MCQ Test"
PUBLIC_BASE_URL = os.environ.get("SKILLSPHERE_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def environment_bool(name: str, default: str) -> bool:
    value = os.environ.get(name, default).lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"{name} must be true or false")
    return value == "true"


PROCTORING_REQUIRED = environment_bool("SKILLSPHERE_PROCTORING_REQUIRED", "true")
try:
    PROCTORING_FACE_MISSING_SECONDS = int(
        os.environ.get("SKILLSPHERE_PROCTORING_FACE_MISSING_SECONDS")
        or os.environ.get("SKILLSPHERE_FACE_MISSING_WARNING_SECONDS")
        or "10"
    )
    PROCTORING_MAX_WARNINGS = int(
        os.environ.get("SKILLSPHERE_PROCTORING_MAX_WARNINGS")
        or os.environ.get("SKILLSPHERE_MAX_PROCTORING_WARNINGS")
        or "3"
    )
except ValueError as error:
    raise RuntimeError("Proctoring warning thresholds must be positive integers") from error

_auto_submit_val = (
    os.environ.get("SKILLSPHERE_PROCTORING_AUTO_SUBMIT")
    or os.environ.get("SKILLSPHERE_AUTO_SUBMIT_ON_MAX_WARNINGS")
    or "false"
).lower()
PROCTORING_AUTO_SUBMIT = _auto_submit_val == "true"
if PROCTORING_FACE_MISSING_SECONDS < 1 or PROCTORING_MAX_WARNINGS < 1:
    raise RuntimeError("Proctoring warning thresholds must be positive integers")

# ---------------------------------------------------------------------------
# Cloudinary & Assessment Video Recording configuration
# ---------------------------------------------------------------------------
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
CLOUDINARY_UPLOAD_FOLDER = os.environ.get("CLOUDINARY_UPLOAD_FOLDER", "skillsphere/assessment-recordings").strip()
SKILLSPHERE_RECORDING_REQUIRED = environment_bool("SKILLSPHERE_RECORDING_REQUIRED", "true")
SKILLSPHERE_CLOUDINARY_ENABLED = environment_bool("SKILLSPHERE_CLOUDINARY_ENABLED", "true")

try:
    SKILLSPHERE_RECORDING_MAX_UPLOAD_MB = int(os.environ.get("SKILLSPHERE_RECORDING_MAX_UPLOAD_MB", "100"))
    SKILLSPHERE_RECORDING_RETENTION_DAYS = int(os.environ.get("SKILLSPHERE_RECORDING_RETENTION_DAYS", "30"))
except ValueError:
    SKILLSPHERE_RECORDING_MAX_UPLOAD_MB = 100
    SKILLSPHERE_RECORDING_RETENTION_DAYS = 30


def init_cloudinary() -> bool:
    if not HAS_CLOUDINARY or not SKILLSPHERE_CLOUDINARY_ENABLED:
        return False
    if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
        cloudinary.config(
            cloud_name=CLOUDINARY_CLOUD_NAME,
            api_key=CLOUDINARY_API_KEY,
            api_secret=CLOUDINARY_API_SECRET,
            secure=True
        )
        return True
    return False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed CORS origins — add your production domain here or set via env var
# e.g. SKILLSPHERE_CORS_ORIGINS="https://app.example.com,https://www.example.com"
# ---------------------------------------------------------------------------
_raw_origins = os.environ.get("SKILLSPHERE_CORS_ORIGINS", "")
CORS_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else (["*"] if not IS_PRODUCTION else [PUBLIC_BASE_URL])
)

# ---------------------------------------------------------------------------
# Rate limiter (slowapi) — limits applied per remote IP
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    initialize_database()
    yield

app = FastAPI(title="SkillSphere API", version="1.0.0", lifespan=lifespan)

# CORS — must be added before routes
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(RequestValidationError)
def custom_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    for err in exc.errors():
        msg = err.get("msg", "")
        if "Job description must contain at least 20 characters before you can post this job." in msg:
            return JSONResponse(
                status_code=422,
                content={"detail": "Job description must contain at least 20 characters before you can post this job."},
            )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next: Any) -> Any:
    start_time = time.time()
    try:
        response = await call_next(request)
        duration = time.time() - start_time
        response.headers["X-Process-Time"] = f"{duration:.4f}"
        return response
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred. Please try again later."},
        )

if not IS_PRODUCTION:
    logger.warning(
        "Running in DEVELOPMENT mode. "
        "Set SKILLSPHERE_ENV=production and SKILLSPHERE_SECRET_KEY before deploying."
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


ESSENTIAL_NOTIFICATION_CATEGORIES = {
    "verification",
    "security",
    "application",
    "assessment",
    "interview",
}

NON_ESSENTIAL_PREF_COLUMNS = {
    "messages": "messages_enabled",
    "account_activity": "account_activity_enabled",
    "system_alerts": "system_alerts_enabled",
}

MAX_EMAIL_RETRIES = 3


def is_valid_email_address(email: str | None) -> bool:
    value = (email or "").strip()
    if not value or " " in value or "@" not in value:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def notification_category_for_type(notification_type: str) -> str:
    nt = (notification_type or "").lower()
    if "verif" in nt:
        return "verification"
    if "password" in nt or "security" in nt:
        return "security"
    if "assessment" in nt or "question_bank" in nt or "proctor" in nt:
        return "assessment"
    if "interview" in nt:
        return "interview"
    if "application" in nt or "round" in nt or "status" in nt:
        return "application"
    if "message" in nt:
        return "messages"
    if any(k in nt for k in ("job", "resume", "profile", "ticket", "account")):
        return "account_activity"
    return "system_alerts"


def build_notification_email(
    title: str,
    message: str,
    action_url: str = "",
    category: str = "system_alerts",
) -> tuple[str, str]:
    subject = f"SkillSphere: {title}"
    link = f"{PUBLIC_BASE_URL}{action_url}" if action_url.startswith("/") else (action_url or f"{PUBLIC_BASE_URL}/")
    body = (
        f"{title}\n"
        f"{'-' * min(len(title), 60)}\n\n"
        f"{message}\n\n"
        f"View details in SkillSphere: {link}\n\n"
        f"Category: {category.replace('_', ' ').title()}\n"
        f"You are receiving this email because you have a verified SkillSphere account."
    )
    return subject, body


def get_user_notification_preferences(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT messages_enabled, account_activity_enabled, system_alerts_enabled, updated_at FROM notification_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return {
            "messages_enabled": True,
            "account_activity_enabled": True,
            "system_alerts_enabled": True,
            "updated_at": None,
        }
    return {
        "messages_enabled": bool(row["messages_enabled"]),
        "account_activity_enabled": bool(row["account_activity_enabled"]),
        "system_alerts_enabled": bool(row["system_alerts_enabled"]),
        "updated_at": row["updated_at"],
    }


def _record_email_log(
    db: sqlite3.Connection,
    *,
    notification_id: int | None,
    user_id: int,
    recipient_email: str,
    category: str,
    subject: str,
    body_text: str,
    status: str,
    attempts: int = 1,
    last_error: str = "",
    event_key: str = "",
    next_retry_at: str | None = None,
    sent_at: str | None = None,
) -> int:
    ts = now()
    cursor = db.execute(
        """INSERT INTO email_delivery_logs
        (notification_id, user_id, recipient_email, category, subject, body_text,
         status, attempts, last_error, event_key, next_retry_at, sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            notification_id, user_id, recipient_email, category, subject, body_text,
            status, attempts, last_error, event_key, next_retry_at, sent_at, ts, ts,
        ),
    )
    return cursor.lastrowid


def deliver_notification_email(
    db: sqlite3.Connection,
    *,
    notification_id: int,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    action_url: str = "",
    event_key: str = "",
) -> str:
    category = notification_category_for_type(notification_type)
    subject, body_text = build_notification_email(title, message, action_url, category)
    ts = now()

    user = db.execute(
        "SELECT id, email, active, email_verified FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not user or not user["active"]:
        _record_email_log(
            db, notification_id=notification_id, user_id=user_id, recipient_email="",
            category=category, subject=subject, body_text=body_text,
            status="Failed", attempts=0, last_error="Recipient account not found or inactive",
            event_key=event_key,
        )
        db.execute(
            "UPDATE notifications SET email_delivery_status = 'Failed', email_last_error = ? WHERE id = ?",
            ("Recipient account not found or inactive", notification_id),
        )
        return "Failed"

    recipient_email = (user["email"] or "").strip()
    verified = bool(user["email_verified"]) if "email_verified" in user.keys() else True
    if not verified or not is_valid_email_address(recipient_email):
        reason = "Email address is not verified" if not verified else "Invalid recipient email address"
        _record_email_log(
            db, notification_id=notification_id, user_id=user_id, recipient_email=recipient_email,
            category=category, subject=subject, body_text=body_text,
            status="Skipped_Unverified", attempts=0, last_error=reason, event_key=event_key,
        )
        db.execute(
            "UPDATE notifications SET email_delivery_status = 'Skipped_Unverified', email_last_error = ? WHERE id = ?",
            (reason, notification_id),
        )
        return "Skipped_Unverified"

    if event_key:
        existing_delivery = db.execute(
            """SELECT id FROM email_delivery_logs
            WHERE user_id = ? AND event_key = ? AND status IN ('Sent', 'Development') LIMIT 1""",
            (user_id, event_key),
        ).fetchone()
        if existing_delivery:
            db.execute(
                "UPDATE notifications SET email_delivery_status = 'Duplicate_Skipped' WHERE id = ?",
                (notification_id,),
            )
            return "Duplicate_Skipped"

    if category not in ESSENTIAL_NOTIFICATION_CATEGORIES:
        prefs = get_user_notification_preferences(db, user_id)
        pref_col = NON_ESSENTIAL_PREF_COLUMNS.get(category)
        if pref_col and not prefs.get(pref_col, True):
            _record_email_log(
                db, notification_id=notification_id, user_id=user_id, recipient_email=recipient_email,
                category=category, subject=subject, body_text=body_text,
                status="Skipped_Preference", attempts=0,
                last_error=f"User disabled {category} email notifications", event_key=event_key,
            )
            db.execute(
                "UPDATE notifications SET email_delivery_status = 'Skipped_Preference' WHERE id = ?",
                (notification_id,),
            )
            return "Skipped_Preference"

    try:
        delivery = send_email_message(recipient_email, subject, body_text)
    except Exception as exc:
        logger.exception("Unexpected error delivering notification email")
        delivery = "Failed"
        err_msg = str(exc)[:300]
    else:
        err_msg = "" if delivery != "Failed" else "Email provider delivery failed"

    if delivery in {"Sent", "Development"}:
        _record_email_log(
            db, notification_id=notification_id, user_id=user_id, recipient_email=recipient_email,
            category=category, subject=subject, body_text=body_text,
            status=delivery, attempts=1, event_key=event_key, sent_at=ts,
        )
        db.execute(
            """UPDATE notifications
            SET email_delivery_status = ?, email_attempts = 1, email_last_error = '', email_sent_at = ?
            WHERE id = ?""",
            (delivery, ts, notification_id),
        )
    else:
        next_retry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        _record_email_log(
            db, notification_id=notification_id, user_id=user_id, recipient_email=recipient_email,
            category=category, subject=subject, body_text=body_text,
            status="Failed", attempts=1, last_error=err_msg, event_key=event_key,
            next_retry_at=next_retry,
        )
        db.execute(
            """UPDATE notifications
            SET email_delivery_status = 'Failed', email_attempts = 1, email_last_error = ?
            WHERE id = ?""",
            (err_msg, notification_id),
        )
    return delivery


def retry_failed_email_deliveries(db: sqlite3.Connection, limit: int = 20) -> dict[str, int]:
    rows = db.execute(
        """SELECT d.*, u.email AS current_email, u.active, u.email_verified
        FROM email_delivery_logs d
        JOIN users u ON u.id = d.user_id
        WHERE d.status = 'Failed' AND d.attempts < ?
        ORDER BY d.created_at ASC LIMIT ?""",
        (MAX_EMAIL_RETRIES, limit),
    ).fetchall()
    retried = 0
    succeeded = 0
    failed = 0
    skipped = 0
    for row in rows:
        retried += 1
        ts = now()
        attempts = (row["attempts"] or 0) + 1
        recipient = (row["current_email"] or "").strip()
        if not row["active"] or not row["email_verified"] or not is_valid_email_address(recipient):
            db.execute(
                """UPDATE email_delivery_logs
                SET status = 'Skipped_Unverified', attempts = ?, last_error = 'Unverified or invalid email', updated_at = ?
                WHERE id = ?""",
                (attempts, ts, row["id"]),
            )
            if row["notification_id"]:
                db.execute(
                    "UPDATE notifications SET email_delivery_status = 'Skipped_Unverified', email_attempts = ? WHERE id = ?",
                    (attempts, row["notification_id"]),
                )
            skipped += 1
            continue
        try:
            delivery = send_email_message(recipient, row["subject"], row["body_text"])
        except Exception as exc:
            delivery = "Failed"
            err = str(exc)[:300]
        else:
            err = "" if delivery != "Failed" else "Email provider delivery failed"

        if delivery in {"Sent", "Development"}:
            db.execute(
                """UPDATE email_delivery_logs
                SET status = ?, recipient_email = ?, attempts = ?, last_error = '',
                    next_retry_at = NULL, sent_at = ?, updated_at = ?
                WHERE id = ?""",
                (delivery, recipient, attempts, ts, ts, row["id"]),
            )
            if row["notification_id"]:
                db.execute(
                    """UPDATE notifications
                    SET email_delivery_status = ?, email_attempts = ?, email_last_error = '', email_sent_at = ?
                    WHERE id = ?""",
                    (delivery, attempts, ts, row["notification_id"]),
                )
            succeeded += 1
        else:
            next_retry = (
                (datetime.now(timezone.utc) + timedelta(minutes=5 * attempts)).isoformat()
                if attempts < MAX_EMAIL_RETRIES else None
            )
            db.execute(
                """UPDATE email_delivery_logs
                SET status = 'Failed', attempts = ?, last_error = ?, next_retry_at = ?, updated_at = ?
                WHERE id = ?""",
                (attempts, err, next_retry, ts, row["id"]),
            )
            if row["notification_id"]:
                db.execute(
                    """UPDATE notifications
                    SET email_delivery_status = 'Failed', email_attempts = ?, email_last_error = ?
                    WHERE id = ?""",
                    (attempts, err, row["notification_id"]),
                )
            failed += 1
    return {"retried": retried, "succeeded": succeeded, "failed": failed, "skipped": skipped}


def add_notification(
    db: sqlite3.Connection,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    *,
    application_id: int | None = None,
    assessment_id: int | None = None,
    action_url: str = "",
    email_delivery_status: str = "Not Sent",
    send_email: bool = True,
    event_key: str = "",
) -> int:
    cursor = db.execute(
        """INSERT INTO notifications
        (user_id, type, title, message, related_application_id, related_assessment_id,
         action_url, email_delivery_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, notification_type, title, message, application_id,
            assessment_id, action_url, email_delivery_status, now(),
        ),
    )
    notification_id = cursor.lastrowid
    if email_delivery_status != "Not Sent":
        user_row = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        _record_email_log(
            db, notification_id=notification_id, user_id=user_id,
            recipient_email=user_row["email"] if user_row else "",
            category=notification_category_for_type(notification_type),
            subject=f"SkillSphere: {title}", body_text=message,
            status=email_delivery_status, attempts=1, event_key=event_key,
            sent_at=now() if email_delivery_status in {"Sent", "Development"} else None,
        )
    elif send_email and notification_type != "email_failure":
        deliver_notification_email(
            db,
            notification_id=notification_id,
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            message=message,
            action_url=action_url,
            event_key=event_key,
        )
    return notification_id


def send_email_message(recipient: str, subject: str, body_text: str) -> str:
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("SKILLSPHERE_EMAIL_FROM") or os.environ.get("SKILLSPHERE_SMTP_FROM") or os.environ.get("SKILLSPHERE_SMTP_USERNAME") or "noreply@skillsphere.dev"
    if resend_key:
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                    json={
                        "from": sender,
                        "to": [recipient],
                        "subject": subject,
                        "text": body_text,
                    },
                )
                if res.status_code in (200, 201):
                    return "Sent"
                logger.error("Resend API delivery error: %s %s", res.status_code, res.text)
        except Exception as exc:
            logger.error("Resend API connection error: %s", exc)

    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if sendgrid_key:
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {sendgrid_key}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": recipient}]}],
                        "from": {"email": sender},
                        "subject": subject,
                        "content": [{"type": "text/plain", "value": body_text}],
                    },
                )
                if res.status_code in (200, 202):
                    return "Sent"
                logger.error("SendGrid API delivery error: %s %s", res.status_code, res.text)
        except Exception as exc:
            logger.error("SendGrid API connection error: %s", exc)

    smtp_host = os.environ.get("SKILLSPHERE_SMTP_HOST", "").strip()
    if smtp_host:
        username = os.environ.get("SKILLSPHERE_SMTP_USERNAME", "").strip()
        password = os.environ.get("SKILLSPHERE_SMTP_PASSWORD", "").strip()
        if not sender or (username and not password):
            logger.error("Notification email SMTP configuration is incomplete")
            return "Failed"
        email = EmailMessage()
        email["Subject"] = subject
        email["From"] = sender
        email["To"] = recipient
        email.set_content(body_text)
        try:
            port = int(os.environ.get("SKILLSPHERE_SMTP_PORT", "587"))
            if port == 465:
                with smtplib.SMTP_SSL(smtp_host, port, timeout=20) as server:
                    if username:
                        server.login(username, password)
                    server.send_message(email)
            else:
                with smtplib.SMTP(smtp_host, port, timeout=20) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    if username:
                        server.login(username, password)
                    server.send_message(email)
            return "Sent"
        except (OSError, smtplib.SMTPException):
            logger.exception("Could not send email via SMTP")
            return "Failed"

    if not IS_PRODUCTION:
        logger.info("Development mock email to %s: %s", recipient, subject)
        return "Development"

    logger.error("Notification email delivery is unavailable because no email provider or SMTP is configured")
    return "Failed"


def email_notification(db: sqlite3.Connection, user_id: int, title: str, message: str) -> str:
    user = db.execute("SELECT email, active, email_verified FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not user["active"]:
        return "Failed"
    verified = bool(user["email_verified"]) if "email_verified" in user.keys() else True
    if not verified or not is_valid_email_address(user["email"]):
        return "Skipped_Unverified"
    return send_email_message(user["email"], title, message)


def notify_user(
    db: sqlite3.Connection,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    *,
    application_id: int | None = None,
    assessment_id: int | None = None,
    email: bool = True,
    action_url: str = "",
    event_key: str = "",
) -> int:
    notification_id = add_notification(
        db, user_id, notification_type, title, message,
        application_id=application_id, assessment_id=assessment_id,
        action_url=action_url, send_email=True, event_key=event_key,
    )
    row = db.execute("SELECT email_delivery_status FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    delivery = row["email_delivery_status"] if row else "Not Sent"
    if delivery == "Failed":
        admins = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchall()
        for admin in admins:
            add_notification(
                db, admin["id"], "email_failure", "Notification email failed",
                f"Email notification delivery failed for a {notification_type} update.",
                application_id=application_id, assessment_id=assessment_id,
                send_email=False,
            )
    return notification_id


def record_application_status(
    db: sqlite3.Connection,
    application_id: int,
    status: str,
    actor: dict[str, Any] | None,
    note: str = "",
) -> None:
    db.execute(
        """INSERT INTO application_status_history
        (application_id, status, actor_id, actor_role, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (application_id, status, actor["id"] if actor else None, actor["role"] if actor else "system", note, now()),
    )


class TursoRow(Mapping):
    def __init__(self, description: Any, values: Any) -> None:
        self._columns = [
            col[0] if isinstance(col, (list, tuple)) else str(col)
            for col in description
        ] if description else []
        self._values = list(values) if values is not None else []
        self._mapping = dict(zip(self._columns, self._values))

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self) -> Any:
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self) -> Any:
        return self._columns

    def values(self) -> list[Any]:
        return self._values

    def items(self) -> Any:
        return self._mapping.items()

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)

    def __repr__(self) -> str:
        return f"TursoRow({self._mapping})"


class TursoCursor:
    def __init__(self, raw_cursor: Any) -> None:
        self._cursor = raw_cursor

    def fetchone(self) -> TursoRow | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return TursoRow(self._cursor.description, row)

    def fetchall(self) -> list[TursoRow]:
        desc = self._cursor.description
        rows = self._cursor.fetchall()
        return [TursoRow(desc, r) for r in rows]

    def fetchmany(self, size: int = 1) -> list[TursoRow]:
        desc = self._cursor.description
        rows = self._cursor.fetchmany(size)
        return [TursoRow(desc, r) for r in rows]

    @property
    def lastrowid(self) -> Any:
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self) -> int:
        return getattr(self._cursor, "rowcount", 0)

    @property
    def description(self) -> Any:
        return getattr(self._cursor, "description", None)

    def __iter__(self) -> Any:
        desc = self.description
        if hasattr(self._cursor, "fetchall"):
            rows = self._cursor.fetchall()
            for row in rows:
                yield TursoRow(desc, row)
        else:
            while True:
                row = self._cursor.fetchone()
                if row is None:
                    break
                yield TursoRow(desc, row)

    def execute(self, sql: str, params: Any = None) -> TursoCursor:
        if params is not None:
            raw_cur = self._cursor.execute(sql, _normalize_turso_params(params))
        else:
            raw_cur = self._cursor.execute(sql)
        if raw_cur is not None and raw_cur is not self._cursor:
            self._cursor = raw_cur
        return self

    def executemany(self, sql: str, seq_of_params: Any) -> TursoCursor:
        raw_cur = self._cursor.executemany(sql, _normalize_turso_seq_params(seq_of_params))
        if raw_cur is not None and raw_cur is not self._cursor:
            self._cursor = raw_cur
        return self


def _normalize_turso_params(params: Any) -> Any:
    if isinstance(params, list):
        return tuple(params)
    return params


def _normalize_turso_seq_params(seq_of_params: Any) -> Any:
    if not seq_of_params:
        return seq_of_params
    return [tuple(p) if isinstance(p, list) else p for p in seq_of_params]


def is_db_integrity_or_duplicate_error(e: Exception) -> bool:
    if isinstance(e, sqlite3.IntegrityError):
        return True
    err_str = str(e).lower()
    return "unique" in err_str or "constraint" in err_str or "integrity" in err_str


class TursoConnectionWrapper:
    def __init__(self, raw_connection: Any) -> None:
        self._conn = raw_connection

    def execute(self, sql: str, params: Any = None) -> TursoCursor:
        cleaned_sql = sql.strip().upper()
        if cleaned_sql in ("BEGIN", "BEGIN IMMEDIATE", "BEGIN TRANSACTION"):
            try:
                raw_cur = self._conn.execute(sql)
                return TursoCursor(raw_cur)
            except Exception as e:
                logger.debug("Turso ignoring explicit transaction start: %s", e)
                return TursoCursor(None)
        if params is not None:
            raw_cur = self._conn.execute(sql, _normalize_turso_params(params))
        else:
            raw_cur = self._conn.execute(sql)
        return TursoCursor(raw_cur)

    def executemany(self, sql: str, seq_of_params: Any) -> TursoCursor:
        raw_cur = self._conn.executemany(sql, _normalize_turso_seq_params(seq_of_params))
        return TursoCursor(raw_cur)

    def executescript(self, sql_script: str) -> None:
        if hasattr(self._conn, "executescript"):
            self._conn.executescript(sql_script)
            return
        cur = self._conn.cursor()
        if hasattr(cur, "executescript"):
            cur.executescript(sql_script)
            return
        for stmt in sql_script.split(";"):
            cleaned = stmt.strip()
            if cleaned:
                self._conn.execute(cleaned)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def sync(self) -> None:
        if hasattr(self._conn, "sync"):
            self._conn.sync()

    def cursor(self) -> TursoCursor:
        if hasattr(self._conn, "cursor"):
            return TursoCursor(self._conn.cursor())
        return TursoCursor(self._conn)

    def close(self) -> None:
        self._conn.close()


def create_turso_connection() -> Any:
    try:
        conn = libsql.connect(str(DB_PATH), sync_url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        try:
            conn.sync()
        except Exception as se:
            logger.warning("Turso sync warning on init: %s", se)
        return conn
    except Exception as e:
        logger.warning("Turso embedded replica connect failed (%s), falling back to direct remote connection...", e)
        return libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)


_TURSO_CACHED_CONN: Any = None
_TURSO_LOCK = threading.Lock()


def get_turso_connection() -> Any:
    global _TURSO_CACHED_CONN
    with _TURSO_LOCK:
        if _TURSO_CACHED_CONN is not None:
            return _TURSO_CACHED_CONN
        _TURSO_CACHED_CONN = create_turso_connection()
        return _TURSO_CACHED_CONN


def reset_turso_connection() -> None:
    global _TURSO_CACHED_CONN
    with _TURSO_LOCK:
        if _TURSO_CACHED_CONN is not None:
            try:
                _TURSO_CACHED_CONN.close()
            except Exception:
                pass
            _TURSO_CACHED_CONN = None


@contextmanager
def connect(write: bool = True) -> Iterator[Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TURSO_DATABASE_URL and HAS_TURSO:
        try:
            raw_conn = get_turso_connection()
            wrapped = TursoConnectionWrapper(raw_conn)
        except Exception as e:
            logger.warning("Turso connection error: %s, recreating connection", e)
            reset_turso_connection()
            raw_conn = create_turso_connection()
            wrapped = TursoConnectionWrapper(raw_conn)

        try:
            yield wrapped
            if write:
                wrapped.commit()
                try:
                    wrapped.sync()
                except Exception as se:
                    logger.warning("Turso sync warning on write: %s", se)
        except Exception:
            try:
                wrapped.rollback()
            except Exception:
                pass
            raise
    else:
        connection = sqlite3.connect(DB_PATH, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")       # concurrent reads + writes
        connection.execute("PRAGMA synchronous=NORMAL")     # safe & faster than FULL
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA cache_size=-8000")       # ~8 MB page cache
        connection.execute("PRAGMA temp_store=MEMORY")
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 180_000)
    return f"{salt.hex()}:{result.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = encoded.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 180_000)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def encode_token(user: sqlite3.Row | dict[str, Any]) -> str:
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = {
        "sub": user["id"],
        "role": user["role"],
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(TOKEN_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{header}.{body}.{signed}"


def decode_token(token: str) -> dict[str, Any]:
    import base64

    try:
        header, body, signature = token.split(".")
        expected = hmac.new(TOKEN_SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, actual):
            raise ValueError("Invalid signature")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload.get("exp", 0) <= time.time():
            raise ValueError("Expired token")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token") from error


def public_user(row: sqlite3.Row) -> dict[str, Any]:
    user = dict(row)
    user.pop("password_hash", None)
    try:
        user["profile"] = json.loads(user.pop("profile_json") or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        user["profile"] = {}
    return user


def get_current_user(authorization: str | None = None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue")
    payload = decode_token(authorization[7:])
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (payload.get("sub"),)).fetchone()
    if not row or not row["active"]:
        raise HTTPException(status_code=401, detail="Account no longer exists")
    if "email_verified" in row.keys() and not row["email_verified"]:
        raise HTTPException(status_code=403, detail="Please verify your email address before accessing this feature.")
    return public_user(row)


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return get_current_user(authorization)


def require_role(*roles: str):
    def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="This action is not available for your account")
        return user

    return dependency


class RegisterBody(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: str = "candidate"


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class JobBody(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=10_000)
    location: str = Field(min_length=2, max_length=100)
    experience: str = Field(min_length=2, max_length=80)
    job_type: str = Field(default="Full-time", max_length=30)
    salary: str = Field(default="", max_length=80)
    skills: list[str] = Field(min_length=1, max_length=30)
    assessment_question_count: int = Field(default=20, ge=5, le=50)
    allow_detailed_results: bool = True

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: str) -> str:
        if len((v or "").strip()) < 20:
            raise ValueError("Job description must contain at least 20 characters before you can post this job.")
        return v.strip()


class AssessmentDecisionBody(BaseModel):
    decision: str = Field(pattern="^(Pass|Fail)$")
    notes: str = Field(default="", max_length=1000)


class JobAssessmentSettingsBody(BaseModel):
    assessment_question_count: int = Field(default=20, ge=5, le=50)
    allow_detailed_results: bool = True


class StatusBody(BaseModel):
    status: str


class ProfileBody(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    profile: dict[str, Any] = Field(default_factory=dict)


class UserStatusBody(BaseModel):
    active: bool


class JobStatusBody(BaseModel):
    status: str


class MatchBody(BaseModel):
    job_id: int


class AssessmentTopicBody(BaseModel):
    technology: str = Field(min_length=1, max_length=50)


class AssessmentSubmitBody(BaseModel):
    answers: dict[int, int] = Field(max_length=100)


class AssessmentProctoringStartBody(BaseModel):
    consent_given: bool
    camera_permission_granted: bool
    recording_consent_given: bool = True
    recording_consent_version: str = Field(default="1.0", max_length=20)


class AssessmentRecordingStartBody(BaseModel):
    consent: bool
    consent_version: str = Field(default="1.0", max_length=20)


class AssessmentRecordingCompleteBody(BaseModel):
    duration_seconds: int = Field(default=0, ge=0, le=86400)


class AssessmentProctoringEventBody(BaseModel):
    event_type: str = Field(pattern="^(camera_granted|camera_denied|assessment_started|face_missing|face_returned|multiple_faces|single_face_restored|tab_hidden|window_blur|window_focus|camera_stopped|recording_started|recording_stopped|recording_failed)$")
    duration_seconds: int | None = Field(default=None, ge=0, le=3600)


class QuestionBody(BaseModel):
    technology: str = Field(min_length=1, max_length=50)
    question: str = Field(min_length=10, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    difficulty: str = Field(pattern="^(Easy|Medium|Hard)$")
    active: bool = True
    explanation: str = Field(default="", max_length=2000)
    learning_resource_url: str = Field(default="", max_length=1000)
    subtopic: str = Field(default="", max_length=100)

    @field_validator("explanation", "learning_resource_url", "subtopic", mode="before")
    @classmethod
    def sanitize_question_fields(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)


class QuestionUpdateBody(QuestionBody):
    pass


class AIQuestionGenerateBody(BaseModel):
    technology: str = Field(min_length=1, max_length=50)
    count: int = Field(default=5, ge=1, le=20)
    difficulty: str = Field(default="Mixed", pattern="^(Mixed|Easy|Medium|Hard)$")
    subtopic: str | None = Field(default="", max_length=100)
    custom_prompt: str | None = Field(default="", max_length=500)

    @field_validator("subtopic", "custom_prompt", mode="before")
    @classmethod
    def sanitize_optional_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()


class QuestionBatchBody(BaseModel):
    questions: list[Any] = Field(min_length=1, max_length=50)


def validate_question_data(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Strictly validates a question item for batch insertion.
    Returns (cleaned_dict, None) on success or (None, reason) on failure.
    """
    if isinstance(data, QuestionBody):
        tech = data.technology.strip()
        q_text = data.question.strip()
        opts = [str(o).strip() for o in data.options]
        c_idx = data.correct_index
        diff = data.difficulty.strip().capitalize()
        act = int(data.active)
        expl = (data.explanation or "").strip()[:2000]
        url = (data.learning_resource_url or "").strip()[:1000]
        sub = (data.subtopic or "").strip()[:100]
    elif isinstance(data, dict):
        tech = str(data.get("technology") or "").strip()
        q_text = str(data.get("question") or "").strip()
        raw_opts = data.get("options")
        if not isinstance(raw_opts, (list, tuple)):
            return None, "options must be a list"
        opts = [str(o).strip() for o in raw_opts]
        try:
            c_idx = int(data.get("correct_index", 0))
        except (ValueError, TypeError):
            return None, "correct_index must be an integer"
        diff = str(data.get("difficulty") or "Medium").strip().capitalize()
        act = 1 if data.get("active", True) else 0
        expl = str(data.get("explanation") or "").strip()[:2000]
        url = str(data.get("learning_resource_url") or "").strip()[:1000]
        sub = str(data.get("subtopic") or "").strip()[:100]
    else:
        return None, "invalid question payload format"

    # Strict server-side validation rules:
    # 1. Valid technology: 1-50 chars
    if not (1 <= len(tech) <= 50):
        return None, "technology must be between 1 and 50 characters"
    # 2. Question length: 10-1000 chars
    if not (10 <= len(q_text) <= 1000):
        return None, "question length must be between 10 and 1000 characters"
    # 3. Exactly four distinct non-empty options
    if len(opts) != 4 or any(not o for o in opts) or len({o.casefold() for o in opts}) != 4:
        return None, "must provide exactly four distinct non-empty options"
    # 4. Correct index from 0 to 3
    if not (0 <= c_idx <= 3):
        return None, "correct_index must be between 0 and 3"
    # 5. Valid difficulty
    if diff not in ("Easy", "Medium", "Hard"):
        return None, "difficulty must be Easy, Medium, or Hard"

    return {
        "technology": tech,
        "question": q_text,
        "options": opts,
        "correct_index": c_idx,
        "difficulty": diff,
        "active": act,
        "explanation": expl,
        "learning_resource_url": url,
        "subtopic": sub,
    }, None


class QuestionImportItem(BaseModel):
    source_index: int = Field(ge=0)
    technology: str = Field(min_length=1, max_length=50)
    difficulty: str = Field(pattern="^(Easy|Medium|Hard)$")
    question: str = Field(min_length=10, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    explanation: str = Field(default="", max_length=2000)
    learning_resource_url: str = Field(default="", max_length=1000)
    subtopic: str = Field(default="", max_length=100)
    include: bool = True


class QuestionImportConfirmBody(BaseModel):
    questions: list[QuestionImportItem] = Field(max_length=500)


class ApplicationStatusBody(BaseModel):
    status: str
    note: str = Field(default="", max_length=2000)


class InternalNoteBody(BaseModel):
    note: str = Field(max_length=4000)


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=3000)
    session_id: int | None = None


AIChatMessageBody = ChatMessageRequest


class AIChatSessionCreateBody(BaseModel):
    title: str = Field(default="New Conversation", max_length=120)


class StartMockInterviewBody(BaseModel):
    job_role: str = Field(..., min_length=2, max_length=100)
    difficulty: str = Field(default="Intermediate", pattern=r"^(Beginner|Intermediate|Advanced)$")
    interview_type: str = Field(default="Technical", pattern=r"^(Technical|HR|Resume-Based)$")
    total_questions: int = Field(default=5, ge=3, le=10)


class SubmitMockAnswerBody(BaseModel):
    mock_interview_id: int
    question_index: int = Field(ge=1, le=15)
    answer: str = Field(..., min_length=1, max_length=4000)


class NotificationReadBody(BaseModel):
    is_read: bool = True


class InterviewBody(BaseModel):
    model_config = {"str_strip_whitespace": True}
    application_id: int
    scheduled_at: str = Field(min_length=10, max_length=40)
    teams_url: str = Field(min_length=1, max_length=2048)


class InterviewInvitationBody(BaseModel):
    model_config = {"str_strip_whitespace": True}
    scheduled_at: str = Field(min_length=10, max_length=40)
    timezone: str = Field(min_length=1, max_length=80)
    duration_minutes: int = Field(ge=5, le=480)
    meeting_platform: str = Field(default="SkillSphere Live", pattern="^(SkillSphere Live|Google Meet|Microsoft Teams|Zoom|Other)$")
    meeting_url: str = Field(default="", max_length=2048)
    recruiter_message: str = Field(default="", max_length=2000)
    problem_title: str = Field(default="", max_length=200)
    problem_prompt: str = Field(default="", max_length=8000)
    problem_starter_code: str = Field(default="", max_length=20000)
    early_entry_minutes: int = Field(default=15, ge=0, le=120)


class InterviewUpdateBody(BaseModel):
    model_config = {"str_strip_whitespace": True}
    scheduled_at: str | None = Field(default=None, min_length=10, max_length=40)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    meeting_platform: str | None = Field(default=None, pattern="^(SkillSphere Live|Google Meet|Microsoft Teams|Zoom|Other)$")
    meeting_url: str | None = Field(default=None, max_length=2048)
    recruiter_message: str | None = Field(default=None, max_length=2000)
    problem_title: str | None = Field(default=None, max_length=200)
    problem_prompt: str | None = Field(default=None, max_length=8000)
    problem_starter_code: str | None = Field(default=None, max_length=20000)
    early_entry_minutes: int | None = Field(default=None, ge=0, le=120)


class InterviewWorkspaceBody(BaseModel):
    code: str = Field(max_length=50_000)
    language: str = Field(default="python", pattern="^(python|javascript|typescript|java|cpp|sql)$")


class InterviewCodeRunBody(BaseModel):
    code: str = Field(min_length=1, max_length=50_000)
    language: str = Field(default="python", pattern="^(python|javascript|typescript|java|cpp|sql)$")


class ForgotPasswordBody(BaseModel):
    email: EmailStr


class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=10, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class VerifyEmailBody(BaseModel):
    token: str = Field(default="", max_length=128)
    email: EmailStr | None = None
    otp: str = Field(default="", max_length=12)


class ResendVerificationBody(BaseModel):
    email: EmailStr | None = None


class ChangeVerificationEmailBody(BaseModel):
    current_email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    new_email: EmailStr


class NotificationPreferencesBody(BaseModel):
    messages_enabled: bool = True
    account_activity_enabled: bool = True
    system_alerts_enabled: bool = True


class JobUpdateBody(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=100)
    company: str | None = Field(default=None, min_length=2, max_length=100)
    description: str | None = Field(default=None, min_length=20, max_length=10_000)
    location: str | None = Field(default=None, min_length=2, max_length=100)
    experience: str | None = Field(default=None, min_length=1, max_length=50)
    job_type: str | None = Field(default=None, min_length=1, max_length=50)
    salary: str | None = Field(default=None, max_length=50)
    skills: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(Open|Closed)$")


class CompanyProfileBody(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    website: str | None = Field(default="", max_length=255)
    logo_url: str | None = Field(default="", max_length=500)
    industry: str | None = Field(default="", max_length=100)
    company_size: str | None = Field(default="", max_length=50)
    description: str | None = Field(default="", max_length=2000)


class MessageSendBody(BaseModel):
    recipient_id: int
    content: str = Field(min_length=1, max_length=4000)
    application_id: int | None = None


class QuestionCollectionCreateBody(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default="", max_length=500)
    technology: str | None = Field(default="", max_length=50)


class PracticeTechBody(BaseModel):
    technology: str = Field(min_length=1, max_length=50)
    proficiency: str | None = Field(default="Intermediate", max_length=50)


class PracticeSubmitBody(BaseModel):
    question_id: int
    selected_index: int | None = Field(default=None, ge=0, le=3)
    selected_option_index: int | None = Field(default=None, ge=0, le=3)


class AssessmentScoreAdjustBody(BaseModel):
    new_score: float = Field(ge=0.0, le=100.0)
    new_result: str | None = Field(default=None, pattern="^(Passed|Failed)$")
    reason: str = Field(min_length=5, max_length=1000)


class VideoAssessmentCreateBody(BaseModel):
    job_id: int
    title: str = Field(min_length=2, max_length=100)
    description: str | None = Field(default="", max_length=1000)
    time_limit_seconds: int = Field(default=120, ge=30, le=600)
    questions: list[dict[str, Any]] = Field(min_length=1, max_length=10)


class VideoResponseSubmitBody(BaseModel):
    responses: list[dict[str, Any]] = Field(min_length=1)


class VideoEvaluateBody(BaseModel):
    score: int = Field(ge=0, le=100)
    feedback: str = Field(default="", max_length=2000)


class InterviewScorecardBody(BaseModel):
    technical_rating: int = Field(default=3, ge=1, le=5)
    communication_rating: int = Field(default=3, ge=1, le=5)
    problem_solving_rating: int = Field(default=3, ge=1, le=5)
    culture_fit_rating: int = Field(default=3, ge=1, le=5)
    overall_recommendation: str = Field(default="Neutral")
    strengths: str | None = Field(default="", max_length=2000)
    areas_for_growth: str | None = Field(default="", max_length=2000)
    notes: str | None = Field(default="", max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def populate_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            mapped = dict(data)
            if "technical_score" in mapped and "technical_rating" not in mapped:
                mapped["technical_rating"] = mapped["technical_score"]
            if "communication_score" in mapped and "communication_rating" not in mapped:
                mapped["communication_rating"] = mapped["communication_score"]
            if "problem_solving_score" in mapped and "problem_solving_rating" not in mapped:
                mapped["problem_solving_rating"] = mapped["problem_solving_score"]
            if "culture_fit_score" in mapped and "culture_fit_rating" not in mapped:
                mapped["culture_fit_rating"] = mapped["culture_fit_score"]
            if "recommendation" in mapped and "overall_recommendation" not in mapped:
                mapped["overall_recommendation"] = mapped["recommendation"]
            rec = mapped.get("overall_recommendation")
            if rec in ("Strong Yes", "Strong Hire"):
                mapped["overall_recommendation"] = "Strong Hire"
            elif rec in ("Yes", "Hire"):
                mapped["overall_recommendation"] = "Hire"
            elif rec in ("No", "No Hire"):
                mapped["overall_recommendation"] = "No Hire"
            elif rec in ("Strong No", "Strong No Hire"):
                mapped["overall_recommendation"] = "Strong No Hire"
            else:
                mapped["overall_recommendation"] = "Neutral"
            return mapped
        return data


class TicketCreateBody(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    category: str = Field(pattern="^(Account|Assessment|Interview|Technical|Billing|General)$")
    priority: str = Field(default="Medium", pattern="^(Low|Medium|High|Urgent)$")
    message: str = Field(min_length=5, max_length=5000)


class TicketMessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    is_internal_note: bool = False


class TicketStatusUpdateBody(BaseModel):
    status: str | None = Field(default=None, pattern="^(Open|In Progress|Waiting Customer|Resolved|Closed)$")
    priority: str | None = Field(default=None, pattern="^(Low|Medium|High|Urgent)$")
    assigned_admin_id: int | None = None


class JobModerateBody(BaseModel):
    action: str = Field(pattern="^(flag|unflag|archive|restore)$")
    note: str | None = Field(default="", max_length=500)


class MultilingualChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None
    preferred_language: str | None = None


def log_audit(
    db: sqlite3.Connection,
    user_id: int | None,
    role: str | None,
    action: str,
    target_type: str,
    target_id: int | None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    try:
        db.execute(
            """INSERT INTO audit_logs
            (user_id, role, action, target_type, target_id, details_json, ip_address, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                role or "system",
                action,
                target_type,
                target_id,
                json.dumps(details or {}),
                ip_address or "",
                now(),
            ),
        )
    except Exception as e:
        logger.warning("Audit log recording failed: %s", e)


def calculate_resume_quality(extracted_text: str, profile: dict[str, Any]) -> dict[str, Any]:
    score = 100
    warnings = []
    suggestions = []

    text_len = len(extracted_text.strip())
    if text_len < 100:
        score -= 40
        warnings.append("Resume contains very little text. Ensure this is not a low-contrast image scan.")
        suggestions.append("Upload a standard text-based PDF or DOCX file with readable text content.")
    elif text_len < 300:
        score -= 20
        warnings.append("Resume appears concise. Detailed project descriptions increase recruiter engagement.")
        suggestions.append("Add bullet points explaining your concrete responsibilities and outcomes.")

    name = profile.get("name", "").strip()
    if not name or len(name) < 2:
        score -= 15
        warnings.append("Candidate name could not be automatically verified.")
        suggestions.append("Ensure your full name is prominently displayed at the top of your resume.")

    email = profile.get("email", "").strip()
    if not email or "@" not in email:
        score -= 20
        warnings.append("No standard contact email address detected.")
        suggestions.append("Include an accessible professional email address.")

    phone = profile.get("phone", "").strip()
    if not phone or len(phone) < 7:
        score -= 10
        warnings.append("Phone number is missing or incomplete.")
        suggestions.append("Include your contact phone number with country/area code.")

    skills = profile.get("skills", [])
    if not skills:
        score -= 25
        warnings.append("No recognized industry skills were detected.")
        suggestions.append("Add a dedicated 'Skills' section listing relevant technical languages, frameworks, and tools.")
    elif len(skills) < 5:
        score -= 10
        warnings.append(f"Only {len(skills)} skills detected. Aim for at least 6-10 core competencies.")
        suggestions.append("Expand your skills section with modern tools and methodologies you use.")

    score = max(10, min(100, score))

    level = "Excellent" if score >= 85 else "Good" if score >= 70 else "Needs Improvement" if score >= 50 else "Critical"

    return {
        "score": score,
        "level": level,
        "warnings": warnings,
        "suggestions": suggestions,
        "detected_contact": {
            "has_name": bool(name and len(name) >= 2),
            "has_email": bool(email and "@" in email),
            "has_phone": bool(phone and len(phone) >= 7),
        },
        "skill_count": len(skills),
        "text_length": text_len,
    }


class SupportChatManager:
    def __init__(self):
        # ticket_id -> dict of connection_id -> {ws, user_id, role, name}
        self.rooms: dict[int, dict[str, dict[str, Any]]] = {}

    async def connect(self, ticket_id: int, conn_id: str, websocket: WebSocket, user_id: int, role: str, name: str) -> None:
        await websocket.accept()
        if ticket_id not in self.rooms:
            self.rooms[ticket_id] = {}
        self.rooms[ticket_id][conn_id] = {
            "ws": websocket,
            "user_id": user_id,
            "role": role,
            "name": name,
        }
        await self.broadcast(
            ticket_id,
            {
                "type": "presence",
                "event": "user_joined",
                "user_name": name,
                "role": role,
                "occupancy": len(self.rooms[ticket_id]),
            },
            is_internal=False,
        )

    def disconnect(self, ticket_id: int, conn_id: str) -> None:
        if ticket_id in self.rooms and conn_id in self.rooms[ticket_id]:
            del self.rooms[ticket_id][conn_id]
            if not self.rooms[ticket_id]:
                del self.rooms[ticket_id]

    async def broadcast(self, ticket_id: int, data: dict[str, Any], is_internal: bool = False) -> None:
        room = self.rooms.get(ticket_id, {})
        for conn_id, conn in list(room.items()):
            if is_internal and conn["role"] != "admin":
                continue
            try:
                await conn["ws"].send_json(data)
            except Exception:
                pass


support_chat_manager = SupportChatManager()


ASSESSMENT_QUESTION_BANK: dict[str, list[tuple[str, str, str, str, str, str]]] = {
    "Python": [
        ("Which built-in collection is immutable?", "tuple", "list", "dict", "set", "easy"),
        ("What does a list comprehension create?", "A new list", "A generator only", "A dictionary", "A function", "easy"),
        ("What does dict.get('key', default) return when key is absent?", "The supplied default", "Always None", "A KeyError", "The dictionary itself", "easy"),
        ("Which statement is commonly used to manage a file handle safely?", "with", "global", "assert", "yield", "easy"),
        ("Which block catches an exception?", "except", "ensure", "rescue", "catching", "easy"),
        ("Why should mutable default arguments be avoided?", "They are shared between calls", "They cannot be modified", "They are copied on every call", "They prevent recursion", "medium"),
        ("What does the 'is' operator compare?", "Object identity", "String contents only", "Numeric magnitude", "Types only", "medium"),
        ("What does a function containing yield produce when called?", "A generator", "A list", "A coroutine in every case", "A class", "medium"),
        ("How should a file opened for reading be closed reliably?", "Use a with statement", "Call del on the variable", "Wait for garbage collection", "Call flush only", "easy"),
        ("What is a primary purpose of a virtual environment?", "Isolate project dependencies", "Encrypt source code", "Compile Python to Java", "Replace the interpreter", "easy"),
        ("What does *args collect in a function definition?", "Extra positional arguments", "Extra keyword arguments", "Return values", "Module names", "medium"),
        ("What does **kwargs collect in a function definition?", "Extra keyword arguments", "Extra positional arguments", "Only integer arguments", "The function's annotations", "medium"),
        ("In CPython, what does the GIL generally protect?", "Execution of Python bytecode by one thread at a time", "All files on disk", "Every database transaction", "The network socket layer", "hard"),
        ("What property makes a set useful for membership checks?", "It stores unique hashable elements", "It preserves duplicate values", "It stores only strings", "It sorts values automatically", "easy"),
        ("What is __init__ commonly used for?", "Initialize a new instance", "Destroy a module", "Declare a package", "Handle every exception", "easy"),
        ("What does items[1:4] select?", "Indices 1, 2, and 3", "Indices 1 through 4 inclusive", "Only index 4", "The first four items", "medium"),
        ("What does import package.module do?", "Makes a module available to the current code", "Installs a package from the internet", "Runs its test suite", "Creates a virtual environment", "easy"),
        ("What does an f-string allow?", "Expression interpolation in a string", "Automatic SQL escaping", "File compression", "Type checking at runtime", "easy"),
        ("What is the main purpose of a type hint?", "Communicate expected types to tools and readers", "Convert every value at runtime", "Prevent all exceptions", "Make a function private", "easy"),
        ("When does a finally block normally run?", "After try/except processing, including when an exception propagates", "Only when no exception occurs", "Only after return from the module", "Before the try block", "medium"),
    ],
    "Java": [
        ("What does the Java compiler normally produce for the JVM?", "Bytecode", "Native source code", "An HTML document", "A database schema", "easy"),
        ("Which method signature is the standard Java application entry point?", "public static void main(String[] args)", "public void start(String args)", "static int main()", "private static run()", "easy"),
        ("What is true of Java String objects?", "They are immutable", "They are always mutable", "They can contain only ASCII", "They are primitive types", "easy"),
        ("For objects, what does == compare?", "Reference identity", "All field values recursively", "Only class names", "The hash code strings", "medium"),
        ("Which method is conventionally used to compare String contents?", "equals", "compareReference", "same", "matchAddress", "easy"),
        ("What can an interface primarily define?", "A contract of methods and constants", "A directly instantiated object", "Only private fields", "A database connection", "easy"),
        ("What is method overloading?", "Same method name with different parameter lists", "Replacing a parent method with same signature", "Changing a field's access", "Calling a method recursively", "medium"),
        ("What is method overriding?", "Providing a subclass implementation of an inherited method", "Adding a method with different parameters", "Creating a static import", "Overloading a constructor only", "medium"),
        ("What is a checked exception?", "An exception the compiler requires code to catch or declare", "An exception that cannot be thrown", "A syntax warning", "An unchecked cast", "medium"),
        ("Which collection is a resizable indexed list implementation?", "ArrayList", "HashSet", "TreeMap", "Optional", "easy"),
        ("What is a common property of HashMap keys?", "Keys are unique", "Duplicate keys create multiple entries", "Keys must be integers", "Keys are always sorted", "easy"),
        ("What does final mean on a local variable?", "It can be assigned only once", "It is visible in every package", "It is always null", "It becomes a class", "medium"),
        ("What does static indicate for a method?", "The method belongs to the class, not an instance", "The method cannot be called", "The method is always abstract", "The method runs in a new thread", "easy"),
        ("Which concept hides implementation details behind a public interface?", "Encapsulation", "Serialization", "Compilation", "Iteration", "easy"),
        ("What does class inheritance allow?", "A subclass to derive behavior from a superclass", "A class to extend multiple classes", "An object to change its class at runtime", "An interface to store instance state only", "easy"),
        ("When is a constructor called?", "When an instance is created", "Whenever a field is read", "At the end of every method", "Only when the JVM exits", "easy"),
        ("What does try-with-resources do?", "Closes declared resources automatically", "Retries every failed operation", "Rolls back every database", "Suppresses all exceptions", "medium"),
        ("What do Java generics provide?", "Compile-time type-safe parameterized types", "Automatic network serialization", "Runtime source compilation", "Multiple class inheritance", "medium"),
        ("What does a reference variable contain before initialization?", "null", "An empty object", "A zero-length string", "A default instance", "easy"),
        ("What does a lambda expression represent in Java?", "A concise implementation of a functional interface", "A new package", "A checked exception", "A database table", "medium"),
    ],
    "JavaScript/React": [
        ("Can a variable declared with const be reassigned?", "No", "Yes, at any time", "Only inside a loop", "Only after await", "easy"),
        ("What does === compare?", "Value and type without coercion", "Only string representations", "Object property counts", "Reference names", "easy"),
        ("What is a key property of let?", "It is block-scoped", "It is always global", "It cannot be reassigned", "It declares a class", "easy"),
        ("What does a Promise represent?", "A value that may be available asynchronously", "A synchronous loop", "A DOM element", "A module export only", "easy"),
        ("What does await do inside an async function?", "Waits for a promise to settle and evaluates its result", "Creates a new thread", "Converts a value to JSON", "Stops the browser event loop", "medium"),
        ("What does Array.prototype.map return?", "A new array of transformed elements", "The original array only", "A boolean", "A Set", "easy"),
        ("Why should a React list item have a stable key?", "To help React identify items across renders", "To encrypt its props", "To prevent all rerenders", "To set its CSS class", "medium"),
        ("What does the React useState hook provide?", "A state value and a state update function", "A router and network client", "A DOM node only", "A CSS stylesheet", "easy"),
        ("How should a component treat its props?", "As read-only inputs", "As globally mutable state", "As browser storage", "As database rows", "easy"),
        ("When does useEffect with an empty dependency array normally run?", "After the component's initial render", "Before every keystroke", "Only when the component unmounts", "During module parsing", "medium"),
        ("What is a controlled React input?", "An input whose value is driven by component state", "An input with no event handler", "An input rendered only on the server", "An input that cannot change", "medium"),
        ("How can a React event handler be passed to a button?", "onClick={handleClick}", "click=handleClick()", "event.onclick: handleClick", "onClick='handleClick'", "easy"),
        ("What does an arrow function inherit lexically?", "The surrounding this value", "A new this value per call", "The global document", "The caller's return value", "medium"),
        ("What does JSON.stringify(value) do?", "Serializes a JavaScript value to JSON text", "Parses JSON text into an object", "Clones DOM nodes", "Validates TypeScript types", "easy"),
        ("What does const {name} = person do?", "Destructures the name property", "Deletes the name property", "Renames the object", "Serializes person", "easy"),
        ("What does [...items] create for an array?", "A shallow copy", "A deep copy of every nested object", "A sorted array", "An immutable tuple", "medium"),
        ("What is a closure?", "A function retaining access to its lexical scope", "A closed browser tab", "A class constructor only", "A rejected Promise", "medium"),
        ("What is a common ES module named export syntax?", "export const value = 1", "module value = 1", "include value", "public value = 1", "easy"),
        ("What does the ?? operator do?", "Uses the right side when the left side is null or undefined", "Uses the right side for every falsy value", "Compares object identities", "Creates a Promise", "medium"),
        ("What does React render when a component returns null?", "No UI for that component", "The text 'null'", "An empty div", "A loading spinner", "easy"),
    ],
    "SQL": [
        ("Which clause selects columns and expressions to return?", "SELECT", "WHERE", "GROUP BY", "ORDER BY", "easy"),
        ("Which clause filters rows before grouping?", "WHERE", "HAVING", "LIMIT", "JOIN", "easy"),
        ("What does an INNER JOIN return?", "Rows with matching join keys in both inputs", "Every row from the left input", "Only unmatched rows", "A Cartesian product in all cases", "easy"),
        ("What does a primary key guarantee?", "A unique, non-null row identifier", "Rows are automatically encrypted", "Every column is indexed", "A table has one row", "easy"),
        ("What does a foreign key enforce?", "A relationship to a referenced key", "A specific sort order", "A column's display format", "A unique value in every column", "easy"),
        ("What is GROUP BY used for?", "Grouping rows for aggregate calculations", "Sorting rows alphabetically", "Filtering individual rows", "Renaming a table", "easy"),
        ("Which clause filters groups after aggregation?", "HAVING", "WHERE", "FROM", "OFFSET", "medium"),
        ("What does COUNT(*) count?", "Rows in the selected group or result", "Only non-null values in one named column", "Distinct tables", "Indexes", "easy"),
        ("How should SQL test whether a value is missing?", "IS NULL", "= NULL", "EQUALS NULL", "IN NULL", "easy"),
        ("What is a common purpose of a database index?", "Speed up some lookups at the cost of storage and write work", "Guarantee all queries are faster", "Replace primary keys", "Encrypt table contents", "medium"),
        ("What does a transaction provide?", "A unit of work with defined commit or rollback behavior", "A permanent query cache", "An automatic backup", "A table alias", "easy"),
        ("What does COMMIT do?", "Makes the current transaction's changes durable", "Cancels the current transaction", "Deletes the database", "Creates a savepoint only", "easy"),
        ("What does ROLLBACK do?", "Undoes uncommitted changes in the transaction", "Commits all changes", "Rebuilds indexes", "Exports query results", "easy"),
        ("What does a LEFT JOIN preserve?", "All rows from the left table and matching right rows", "Only rows from the right table", "Only matching rows", "All combinations of both tables", "medium"),
        ("What does a UNIQUE constraint enforce?", "No duplicate values for the constrained key", "No null values in every database", "A fixed row order", "Automatic encryption", "easy"),
        ("What is a goal of normalization?", "Reduce avoidable duplication and update anomalies", "Make every query use a full scan", "Store all data in one column", "Remove all relationships", "medium"),
        ("What is the safest common defense against SQL injection?", "Use parameterized queries", "Concatenate escaped user input manually", "Hide the query text", "Disable database logs", "medium"),
        ("Which clause sorts query results?", "ORDER BY", "GROUP BY", "WHERE", "HAVING", "easy"),
        ("What does UPDATE do?", "Changes values in existing rows", "Adds a new table", "Returns only metadata", "Removes an index", "easy"),
        ("What does DELETE FROM table WHERE condition do?", "Removes rows matching the condition", "Drops the table schema", "Changes matching rows", "Creates a backup", "easy"),
    ],
}

TECHNICAL_SKILL_TOPICS = {
    "python": "Python",
    "fastapi": "Python",
    "java": "Java",
    "javascript": "JavaScript/React",
    "react": "JavaScript/React",
    "typescript": "JavaScript/React",
    "html": "JavaScript/React",
    "css": "JavaScript/React",
    "sql": "SQL",
    "postgresql": "SQL",
    "docker": None,
    "aws": None,
}


def assessment_topic_for_skill(skill: str) -> str | None:
    lowered = skill.lower()
    if lowered in {topic.lower() for topic in ASSESSMENT_QUESTION_BANK}:
        return next(topic for topic in ASSESSMENT_QUESTION_BANK if topic.lower() == lowered)
    return TECHNICAL_SKILL_TOPICS.get(lowered)


def balanced_question_sample(questions: list[sqlite3.Row], count: int) -> list[sqlite3.Row]:
    grouped = {
        level: [question for question in questions if question["difficulty"].lower() == level.lower()]
        for level in ("Easy", "Medium", "Hard")
    }
    rng = random.SystemRandom()
    for level_questions in grouped.values():
        rng.shuffle(level_questions)
    selected: list[sqlite3.Row] = []
    while len(selected) < count:
        progressed = False
        for level in ("Easy", "Medium", "Hard"):
            if grouped[level] and len(selected) < count:
                selected.append(grouped[level].pop())
                progressed = True
        if not progressed:
            break
    return selected


def seed_assessment_questions(db: sqlite3.Connection) -> None:
    for technology, questions in ASSESSMENT_QUESTION_BANK.items():
        for question, correct, *wrong_and_difficulty in questions:
            difficulty = wrong_and_difficulty.pop()
            options = [correct, *wrong_and_difficulty]
            random.SystemRandom().shuffle(options)
            db.execute(
                """INSERT OR IGNORE INTO technical_questions
                (technology, question, options_json, correct_index, difficulty, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (technology, question, json.dumps(options), options.index(correct), difficulty, now()),
            )


def initialize_database() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    QUESTION_BANK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('candidate', 'recruiter', 'admin')),
                profile_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recruiter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                description TEXT NOT NULL,
                location TEXT NOT NULL,
                experience TEXT NOT NULL,
                job_type TEXT NOT NULL,
                salary TEXT NOT NULL DEFAULT '',
                skills_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Open' CHECK(status IN ('Open', 'Closed', 'Archived')),
                assessment_question_count INTEGER NOT NULL DEFAULT 20,
                allow_detailed_results INTEGER NOT NULL DEFAULT 1,
                archived_at TEXT,
                is_archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                parsed_profile_json TEXT NOT NULL DEFAULT '{}',
                processing_status TEXT NOT NULL,
                processing_error TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                resume_id INTEGER REFERENCES resumes(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'Applied',
                match_percentage INTEGER,
                matched_skills_json TEXT NOT NULL DEFAULT '[]',
                missing_skills_json TEXT NOT NULL DEFAULT '[]',
                internal_notes TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL,
                UNIQUE(candidate_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS application_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                actor_role TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                related_application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
                related_assessment_id INTEGER REFERENCES assessments(id) ON DELETE SET NULL,
                action_url TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                email_delivery_status TEXT NOT NULL DEFAULT 'Not Sent',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_bank_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                recruiter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                upload_status TEXT NOT NULL CHECK(upload_status IN ('Preview', 'Confirmed', 'Cancelled', 'Failed')),
                total_extracted INTEGER NOT NULL DEFAULT 0,
                valid_count INTEGER NOT NULL DEFAULT 0,
                invalid_count INTEGER NOT NULL DEFAULT 0,
                preview_json TEXT NOT NULL DEFAULT '[]',
                error_details_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                confirmed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS job_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recruiter_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                technology TEXT NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_index INTEGER NOT NULL CHECK(correct_index BETWEEN 0 AND 3),
                difficulty TEXT NOT NULL CHECK(difficulty IN ('Easy', 'Medium', 'Hard')),
                active INTEGER NOT NULL DEFAULT 1,
                source_type TEXT NOT NULL DEFAULT 'manual' CHECK(source_type IN ('manual', 'pdf_import')),
                source_import_id INTEGER REFERENCES question_bank_imports(id) ON DELETE SET NULL,
                explanation TEXT NOT NULL DEFAULT '',
                learning_resource_url TEXT NOT NULL DEFAULT '',
                subtopic TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, question)
            );
            CREATE TABLE IF NOT EXISTS technical_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                technology TEXT NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_index INTEGER NOT NULL CHECK(correct_index BETWEEN 0 AND 3),
                difficulty TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(technology, question)
            );
            CREATE TABLE IF NOT EXISTS assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                technology TEXT,
                token_hash TEXT UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('Not Sent', 'Sent', 'Started', 'Completed', 'Expired', 'Manual Topic Required', 'Pending Recruiter Decision')),
                match_percentage INTEGER NOT NULL,
                starts_at TEXT,
                expires_at TEXT,
                submitted_at TEXT,
                score INTEGER,
                total_questions INTEGER NOT NULL DEFAULT 20,
                assessment_name TEXT NOT NULL DEFAULT 'Round 1: Technical MCQ Test',
                assessment_round TEXT NOT NULL DEFAULT 'Round 1',
                pass_percentage REAL NOT NULL DEFAULT 75,
                score_percentage REAL,
                correct_answer_count INTEGER,
                incorrect_answer_count INTEGER,
                result TEXT CHECK(result IS NULL OR result IN ('Passed', 'Failed')),
                result_processed_at TEXT,
                time_taken_seconds INTEGER,
                duration_minutes INTEGER NOT NULL,
                email_sent_at TEXT,
                question_topics_json TEXT NOT NULL DEFAULT '[]',
                question_topic_weights_json TEXT NOT NULL DEFAULT '{}',
                selection_reasoning TEXT NOT NULL DEFAULT '',
                proctoring_required INTEGER NOT NULL DEFAULT 1,
                proctoring_consent_at TEXT,
                camera_permission_granted_at TEXT,
                proctoring_started_at TEXT,
                proctoring_warning_count INTEGER NOT NULL DEFAULT 0,
                proctoring_flagged_at TEXT,
                detailed_results_visible INTEGER NOT NULL DEFAULT 1,
                recruiter_decision TEXT CHECK(recruiter_decision IS NULL OR recruiter_decision IN ('Passed', 'Failed')),
                recruiter_decision_at TEXT,
                recruiter_decision_by INTEGER REFERENCES users(id),
                recruiter_decision_notes TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessment_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES technical_questions(id),
                question_order INTEGER NOT NULL,
                question_snapshot TEXT NOT NULL,
                options_snapshot TEXT NOT NULL,
                correct_index INTEGER NOT NULL CHECK(correct_index BETWEEN 0 AND 3),
                selected_answer INTEGER,
                is_correct INTEGER,
                answered_at TEXT,
                technology TEXT,
                difficulty TEXT,
                explanation TEXT NOT NULL DEFAULT '',
                learning_resource_url TEXT NOT NULL DEFAULT '',
                subtopic TEXT NOT NULL DEFAULT '',
                UNIQUE(assessment_id, question_order),
                UNIQUE(assessment_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS assessment_proctoring_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'camera_granted', 'camera_denied', 'assessment_started',
                    'face_missing', 'face_returned', 'multiple_faces',
                    'single_face_restored', 'tab_hidden', 'window_blur',
                    'window_focus', 'camera_stopped'
                )),
                severity TEXT NOT NULL CHECK(severity IN ('info', 'warning')),
                duration_seconds INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                recruiter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                scheduled_at TEXT NOT NULL,
                teams_url TEXT NOT NULL DEFAULT '',
                meeting_platform TEXT NOT NULL DEFAULT 'SkillSphere Live',
                meeting_url TEXT NOT NULL DEFAULT '',
                room_token TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                recruiter_message TEXT NOT NULL DEFAULT '',
                problem_title TEXT NOT NULL DEFAULT '',
                problem_prompt TEXT NOT NULL DEFAULT '',
                problem_starter_code TEXT NOT NULL DEFAULT '',
                early_entry_minutes INTEGER NOT NULL DEFAULT 15,
                status TEXT NOT NULL DEFAULT 'Scheduled'
                    CHECK(status IN ('Scheduled', 'Rescheduled', 'Cancelled', 'Completed')),
                invitation_sent_at TEXT,
                candidate_viewed_at TEXT,
                code TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'python',
                code_submitted INTEGER NOT NULL DEFAULT 0,
                submitted_code TEXT NOT NULL DEFAULT '',
                submitted_language TEXT NOT NULL DEFAULT '',
                code_submitted_at TEXT,
                last_execution_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interview_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                left_at TEXT,
                status TEXT NOT NULL DEFAULT 'Joined'
            );
            CREATE TABLE IF NOT EXISTS interview_code_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                language TEXT NOT NULL,
                code TEXT NOT NULL,
                stdout TEXT NOT NULL DEFAULT '',
                stderr TEXT NOT NULL DEFAULT '',
                exit_code INTEGER NOT NULL DEFAULT 0,
                timed_out INTEGER NOT NULL DEFAULT 0,
                is_submission INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_ai_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
                recommendation TEXT NOT NULL CHECK(recommendation IN ('Strong Hire', 'Recommended', 'Borderline', 'Not Recommended')),
                summary TEXT NOT NULL,
                strengths_json TEXT NOT NULL DEFAULT '[]',
                risks_json TEXT NOT NULL DEFAULT '[]',
                interview_questions_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessment_recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                cloudinary_public_id TEXT,
                cloudinary_asset_id TEXT,
                cloudinary_resource_type TEXT DEFAULT 'video',
                secure_url TEXT,
                duration_seconds INTEGER DEFAULT 0,
                file_size_bytes INTEGER DEFAULT 0,
                format TEXT DEFAULT 'webm',
                recording_status TEXT NOT NULL DEFAULT 'Pending' CHECK(recording_status IN ('Pending', 'Uploading', 'Uploaded', 'Failed', 'Partial')),
                upload_error TEXT,
                started_at TEXT,
                ended_at TEXT,
                uploaded_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recording_access_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recording_id INTEGER NOT NULL REFERENCES assessment_recordings(id) ON DELETE CASCADE,
                admin_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                accessed_at TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_assessment_recordings_assessment ON assessment_recordings(assessment_id);
            CREATE INDEX IF NOT EXISTS idx_assessment_recordings_candidate ON assessment_recordings(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_assessment_recordings_app ON assessment_recordings(application_id);
            CREATE INDEX IF NOT EXISTS idx_recording_access_logs_recording ON recording_access_logs(recording_id);
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_title TEXT NOT NULL DEFAULT 'New Conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_intent TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
                sender TEXT NOT NULL CHECK(sender IN ('candidate', 'assistant')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1.0
            );
            CREATE TABLE IF NOT EXISTS mock_interviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                interview_type TEXT NOT NULL,
                job_role TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                total_questions INTEGER NOT NULL DEFAULT 5,
                current_question_index INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'In Progress' CHECK(status IN ('In Progress', 'Completed', 'Cancelled')),
                final_feedback_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mock_interview_exchanges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mock_interview_id INTEGER NOT NULL REFERENCES mock_interviews(id) ON DELETE CASCADE,
                question_index INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                feedback TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_sessions_candidate ON chat_sessions(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_mock_interviews_candidate ON mock_interviews(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_mock_interview_exchanges_interview ON mock_interview_exchanges(mock_interview_id);
            CREATE INDEX IF NOT EXISTS idx_applications_candidate ON applications(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
            CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
            CREATE INDEX IF NOT EXISTS idx_app_history_app ON application_status_history(application_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, is_read);
            CREATE INDEX IF NOT EXISTS idx_job_questions_job ON job_questions(job_id, active);
            CREATE INDEX IF NOT EXISTS idx_technical_questions_tech ON technical_questions(technology, active);
            CREATE INDEX IF NOT EXISTS idx_assessments_candidate ON assessments(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_assessments_job ON assessments(job_id);
            CREATE INDEX IF NOT EXISTS idx_assessments_status ON assessments(status);
            CREATE INDEX IF NOT EXISTS idx_assessments_token_hash ON assessments(token_hash);
            CREATE INDEX IF NOT EXISTS idx_assessment_questions_assessment ON assessment_questions(assessment_id);
            CREATE INDEX IF NOT EXISTS idx_assessment_events_assessment ON assessment_proctoring_events(assessment_id);
            CREATE INDEX IF NOT EXISTS idx_interviews_application ON interviews(application_id);
            CREATE INDEX IF NOT EXISTS idx_interviews_recruiter ON interviews(recruiter_id);
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                otp_hash TEXT NOT NULL DEFAULT '',
                used INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                email_sent_to TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                messages_enabled INTEGER NOT NULL DEFAULT 1,
                account_activity_enabled INTEGER NOT NULL DEFAULT 1,
                system_alerts_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS email_delivery_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER REFERENCES notifications(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_email TEXT NOT NULL,
                category TEXT NOT NULL,
                subject TEXT NOT NULL,
                body_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                last_error TEXT NOT NULL DEFAULT '',
                event_key TEXT NOT NULL DEFAULT '',
                next_retry_at TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_email_delivery_logs_status ON email_delivery_logs(status, next_retry_at);
            CREATE INDEX IF NOT EXISTS idx_email_delivery_logs_event ON email_delivery_logs(user_id, event_key);
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS resume_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                content_type TEXT NOT NULL,
                extracted_text TEXT,
                parsed_profile_json TEXT,
                version_number INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
                job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id),
                recipient_id INTEGER NOT NULL REFERENCES users(id),
                content TEXT NOT NULL,
                read_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recruiter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                technology TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_collection_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_id INTEGER NOT NULL REFERENCES question_collections(id) ON DELETE CASCADE,
                technology TEXT NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_index INTEGER NOT NULL,
                difficulty TEXT NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                learning_resource_url TEXT NOT NULL DEFAULT '',
                subtopic TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_technologies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                technology TEXT NOT NULL,
                proficiency TEXT NOT NULL DEFAULT 'Intermediate',
                created_at TEXT NOT NULL,
                UNIQUE(candidate_id, technology)
            );
            CREATE TABLE IF NOT EXISTS practice_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                technology TEXT NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_index INTEGER NOT NULL,
                difficulty TEXT NOT NULL,
                explanation TEXT NOT NULL DEFAULT '',
                subtopic TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS practice_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES practice_questions(id) ON DELETE CASCADE,
                selected_index INTEGER NOT NULL,
                is_correct INTEGER NOT NULL,
                attempted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessment_score_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                recruiter_id INTEGER NOT NULL REFERENCES users(id),
                original_score REAL,
                new_score REAL NOT NULL,
                original_result TEXT,
                new_result TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS video_assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                recruiter_id INTEGER NOT NULL REFERENCES users(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                time_limit_seconds INTEGER NOT NULL DEFAULT 120,
                status TEXT NOT NULL DEFAULT 'Draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS video_assessment_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_assessment_id INTEGER NOT NULL REFERENCES video_assessments(id) ON DELETE CASCADE,
                question_order INTEGER NOT NULL DEFAULT 1,
                question_text TEXT NOT NULL,
                max_duration_seconds INTEGER NOT NULL DEFAULT 120,
                prep_time_seconds INTEGER NOT NULL DEFAULT 30,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS video_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_assessment_id INTEGER NOT NULL REFERENCES video_assessments(id) ON DELETE CASCADE,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                candidate_id INTEGER NOT NULL REFERENCES users(id),
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'Invited',
                deadline_at TEXT,
                invited_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS video_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL REFERENCES video_assignments(id) ON DELETE CASCADE,
                question_id INTEGER NOT NULL REFERENCES video_assessment_questions(id) ON DELETE CASCADE,
                video_url TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                score INTEGER,
                recruiter_feedback TEXT NOT NULL DEFAULT '',
                evaluated_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS interview_scorecards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                recruiter_id INTEGER NOT NULL REFERENCES users(id),
                technical_rating INTEGER NOT NULL DEFAULT 0,
                communication_rating INTEGER NOT NULL DEFAULT 0,
                problem_solving_rating INTEGER NOT NULL DEFAULT 0,
                culture_fit_rating INTEGER NOT NULL DEFAULT 0,
                overall_recommendation TEXT NOT NULL DEFAULT 'Neutral',
                strengths TEXT NOT NULL DEFAULT '',
                areas_for_growth TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(interview_id)
            );
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                subject TEXT NOT NULL,
                category TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'Medium',
                status TEXT NOT NULL DEFAULT 'Open',
                assigned_admin_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id),
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_internal_note INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ticket_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
                admin_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_availability (
                admin_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                is_online INTEGER NOT NULL DEFAULT 0,
                active_chats_count INTEGER NOT NULL DEFAULT 0,
                max_concurrent_chats INTEGER NOT NULL DEFAULT 5,
                last_heartbeat_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS websocket_auth_tickets (
                ticket_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                support_ticket_id INTEGER,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                role TEXT,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id INTEGER,
                details_json TEXT NOT NULL DEFAULT '{}',
                ip_address TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_saved_jobs_cand ON saved_jobs(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_messages_app ON messages(application_id);
            CREATE INDEX IF NOT EXISTS idx_messages_recip ON messages(recipient_id, read_at);
            CREATE INDEX IF NOT EXISTS idx_practice_tech ON practice_questions(technology);
            CREATE INDEX IF NOT EXISTS idx_practice_att_cand ON practice_attempts(candidate_id);
            CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status);
            CREATE INDEX IF NOT EXISTS idx_ticket_msgs_ticket ON ticket_messages(ticket_id);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action, created_at);
            """
        )
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "active" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "email_verified" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
        chat_msg_columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_messages)")}
        if "intent" not in chat_msg_columns:
            db.execute("ALTER TABLE chat_messages ADD COLUMN intent TEXT NOT NULL DEFAULT ''")
        if "confidence" not in chat_msg_columns:
            db.execute("ALTER TABLE chat_messages ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0")
        chat_session_columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_sessions)")}
        if "last_intent" not in chat_session_columns:
            db.execute("ALTER TABLE chat_sessions ADD COLUMN last_intent TEXT NOT NULL DEFAULT ''")
        job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        for column, definition in (
            ("assessment_question_count", "INTEGER NOT NULL DEFAULT 20"),
            ("allow_detailed_results", "INTEGER NOT NULL DEFAULT 1"),
            ("archived_at", "TEXT"),
            ("is_archived", "INTEGER NOT NULL DEFAULT 0"),
            ("flagged", "INTEGER NOT NULL DEFAULT 0"),
            ("moderation_note", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in job_columns:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
        jobs_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        jobs_sql = (jobs_row[0] if jobs_row else "") or ""
        if "CHECK(status IN ('Open', 'Closed'))" in jobs_sql and "Archived" not in jobs_sql:
            cols = [r[1] for r in db.execute("PRAGMA table_info(jobs)").fetchall()]
            cols_joined = ", ".join(f'"{c}"' for c in cols)
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("DROP TABLE IF EXISTS jobs_new")
            db.execute("""
                CREATE TABLE jobs_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recruiter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    description TEXT NOT NULL,
                    location TEXT NOT NULL,
                    experience TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    salary TEXT NOT NULL DEFAULT '',
                    skills_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Open' CHECK(status IN ('Open', 'Closed', 'Archived')),
                    assessment_question_count INTEGER NOT NULL DEFAULT 20,
                    allow_detailed_results INTEGER NOT NULL DEFAULT 1,
                    archived_at TEXT,
                    is_archived INTEGER NOT NULL DEFAULT 0,
                    flagged INTEGER NOT NULL DEFAULT 0,
                    moderation_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            db.execute(f"INSERT INTO jobs_new ({cols_joined}) SELECT {cols_joined} FROM jobs")
            db.execute("DROP TABLE jobs")
            db.execute("ALTER TABLE jobs_new RENAME TO jobs")
            db.execute("PRAGMA foreign_keys = ON")
        application_columns = {row["name"] for row in db.execute("PRAGMA table_info(applications)")}
        if "match_percentage" not in application_columns:
            db.execute("ALTER TABLE applications ADD COLUMN match_percentage INTEGER")
        for column, definition in (
            ("matched_skills_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("missing_skills_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("internal_notes", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in application_columns:
                db.execute(f"ALTER TABLE applications ADD COLUMN {column} {definition}")
        job_question_columns = {row["name"] for row in db.execute("PRAGMA table_info(job_questions)")}
        for column, definition in (
            ("recruiter_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE"),
            ("source_type", "TEXT NOT NULL DEFAULT 'manual'"),
            ("source_import_id", "INTEGER REFERENCES question_bank_imports(id) ON DELETE SET NULL"),
            ("explanation", "TEXT NOT NULL DEFAULT ''"),
            ("learning_resource_url", "TEXT NOT NULL DEFAULT ''"),
            ("subtopic", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in job_question_columns:
                db.execute(f"ALTER TABLE job_questions ADD COLUMN {column} {definition}")
        db.execute(
            """UPDATE job_questions SET recruiter_id = owner_id
            WHERE recruiter_id IS NULL"""
        )
        assessments_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='assessments'").fetchone()
        if assessments_row and "Pending Recruiter Decision" not in assessments_row[0]:
            cols = [r[1] for r in db.execute("PRAGMA table_info(assessments)").fetchall()]
            cols_joined = ", ".join(f'"{c}"' for c in cols)
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("""
                CREATE TABLE assessments_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
                    candidate_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    technology TEXT,
                    token_hash TEXT UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('Not Sent', 'Sent', 'Started', 'Completed', 'Expired', 'Manual Topic Required', 'Pending Recruiter Decision')),
                    match_percentage INTEGER NOT NULL,
                    starts_at TEXT,
                    expires_at TEXT,
                    submitted_at TEXT,
                    score INTEGER,
                    total_questions INTEGER NOT NULL DEFAULT 20,
                    assessment_name TEXT NOT NULL DEFAULT 'Round 1: Technical MCQ Test',
                    assessment_round TEXT NOT NULL DEFAULT 'Round 1',
                    pass_percentage REAL NOT NULL DEFAULT 75,
                    score_percentage REAL,
                    correct_answer_count INTEGER,
                    incorrect_answer_count INTEGER,
                    result TEXT,
                    result_processed_at TEXT,
                    time_taken_seconds INTEGER,
                    duration_minutes INTEGER NOT NULL,
                    email_sent_at TEXT,
                    question_topics_json TEXT NOT NULL DEFAULT '[]',
                    question_topic_weights_json TEXT NOT NULL DEFAULT '{}',
                    selection_reasoning TEXT NOT NULL DEFAULT '',
                    proctoring_required INTEGER NOT NULL DEFAULT 1,
                    proctoring_consent_at TEXT,
                    camera_permission_granted_at TEXT,
                    proctoring_started_at TEXT,
                    proctoring_warning_count INTEGER NOT NULL DEFAULT 0,
                    proctoring_flagged_at TEXT,
                    detailed_results_visible INTEGER NOT NULL DEFAULT 1,
                    recording_required INTEGER NOT NULL DEFAULT 1,
                    recording_consent_given_at TEXT,
                    recording_consent_version TEXT,
                    recording_status TEXT DEFAULT 'Pending',
                    recording_failure_reason TEXT,
                    recording_available_at TEXT,
                    submitted_token_hash TEXT,
                    recruiter_decision TEXT CHECK(recruiter_decision IS NULL OR recruiter_decision IN ('Passed', 'Failed')),
                    recruiter_decision_at TEXT,
                    recruiter_decision_by INTEGER REFERENCES users(id),
                    recruiter_decision_notes TEXT,
                    created_at TEXT NOT NULL DEFAULT ''
                )
            """)
            db.execute(f"INSERT INTO assessments_new ({cols_joined}) SELECT {cols_joined} FROM assessments")
            db.execute("DROP TABLE assessments")
            db.execute("ALTER TABLE assessments_new RENAME TO assessments")
            db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_candidate_job ON assessments(candidate_id, job_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_token_hash ON assessments(token_hash)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_proctoring_flagged ON assessments(proctoring_flagged_at, starts_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_status ON assessments(status, submitted_at)")
            db.execute("PRAGMA foreign_keys = ON")
        assessment_columns = {row["name"] for row in db.execute("PRAGMA table_info(assessments)")}
        for column, definition in (
            ("question_topics_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("question_topic_weights_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("selection_reasoning", "TEXT NOT NULL DEFAULT ''"),
            ("assessment_name", "TEXT NOT NULL DEFAULT 'Round 1: Technical MCQ Test'"),
            ("assessment_round", "TEXT NOT NULL DEFAULT 'Round 1'"),
            ("pass_percentage", "REAL NOT NULL DEFAULT 75"),
            ("score_percentage", "REAL"),
            ("correct_answer_count", "INTEGER"),
            ("incorrect_answer_count", "INTEGER"),
            ("result", "TEXT CHECK(result IS NULL OR result IN ('Passed', 'Failed'))"),
            ("result_processed_at", "TEXT"),
            ("time_taken_seconds", "INTEGER"),
            ("proctoring_required", "INTEGER NOT NULL DEFAULT 1"),
            ("proctoring_consent_at", "TEXT"),
            ("camera_permission_granted_at", "TEXT"),
            ("proctoring_started_at", "TEXT"),
            ("proctoring_warning_count", "INTEGER NOT NULL DEFAULT 0"),
            ("proctoring_flagged_at", "TEXT"),
            ("detailed_results_visible", "INTEGER NOT NULL DEFAULT 1"),
            ("recording_required", "INTEGER NOT NULL DEFAULT 1"),
            ("recording_consent_given_at", "TEXT"),
            ("recording_consent_version", "TEXT"),
            ("recording_status", "TEXT DEFAULT 'Pending'"),
            ("recording_failure_reason", "TEXT"),
            ("recording_available_at", "TEXT"),
            ("submitted_token_hash", "TEXT"),
            ("recruiter_decision", "TEXT CHECK(recruiter_decision IS NULL OR recruiter_decision IN ('Passed', 'Failed'))"),
            ("recruiter_decision_at", "TEXT"),
            ("recruiter_decision_by", "INTEGER REFERENCES users(id)"),
            ("recruiter_decision_notes", "TEXT"),
        ):
            if column not in assessment_columns:
                db.execute(f"ALTER TABLE assessments ADD COLUMN {column} {definition}")
        db.execute(
            """UPDATE assessments SET recruiter_decision = result
            WHERE status = 'Completed' AND recruiter_decision IS NULL AND result IS NOT NULL"""
        )
        db.execute(
            """UPDATE assessments SET proctoring_required = 0
            WHERE status = 'Started' AND starts_at IS NOT NULL
            AND proctoring_consent_at IS NULL AND proctoring_started_at IS NULL"""
        )
        assessment_question_columns = {row["name"] for row in db.execute("PRAGMA table_info(assessment_questions)")}
        for column, definition in (
            ("technology", "TEXT"),
            ("difficulty", "TEXT"),
            ("explanation", "TEXT NOT NULL DEFAULT ''"),
            ("learning_resource_url", "TEXT NOT NULL DEFAULT ''"),
            ("subtopic", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in assessment_question_columns:
                db.execute(f"ALTER TABLE assessment_questions ADD COLUMN {column} {definition}")
        interview_columns = {row["name"] for row in db.execute("PRAGMA table_info(interviews)")}
        for column, definition in (
            ("meeting_platform", "TEXT NOT NULL DEFAULT 'Microsoft Teams'"),
            ("meeting_url", "TEXT NOT NULL DEFAULT ''"),
            ("room_token", "TEXT NOT NULL DEFAULT ''"),
            ("duration_minutes", "INTEGER NOT NULL DEFAULT 60"),
            ("timezone", "TEXT NOT NULL DEFAULT 'UTC'"),
            ("recruiter_message", "TEXT NOT NULL DEFAULT ''"),
            ("problem_title", "TEXT NOT NULL DEFAULT ''"),
            ("problem_prompt", "TEXT NOT NULL DEFAULT ''"),
            ("problem_starter_code", "TEXT NOT NULL DEFAULT ''"),
            ("early_entry_minutes", "INTEGER NOT NULL DEFAULT 15"),
            ("status", "TEXT NOT NULL DEFAULT 'Scheduled' CHECK(status IN ('Scheduled', 'Rescheduled', 'Cancelled', 'Completed'))"),
            ("invitation_sent_at", "TEXT"),
            ("candidate_viewed_at", "TEXT"),
            ("code_submitted", "INTEGER NOT NULL DEFAULT 0"),
            ("submitted_code", "TEXT NOT NULL DEFAULT ''"),
            ("submitted_language", "TEXT NOT NULL DEFAULT ''"),
            ("code_submitted_at", "TEXT"),
            ("last_execution_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if column not in interview_columns:
                db.execute(f"ALTER TABLE interviews ADD COLUMN {column} {definition}")
        missing_room_tokens = db.execute("SELECT id FROM interviews WHERE room_token = '' OR room_token IS NULL").fetchall()
        for r in missing_room_tokens:
            db.execute("UPDATE interviews SET room_token = ? WHERE id = ?", (secrets.token_urlsafe(24), r["id"]))
        db.execute(
            """UPDATE interviews SET meeting_url = teams_url
            WHERE meeting_url = '' AND teams_url != ''"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_interviews_application_schedule ON interviews(application_id, scheduled_at)"
        )
        evt_columns = {row["name"] for row in db.execute("PRAGMA table_info(email_verification_tokens)")}
        for column, definition in (
            ("otp_hash", "TEXT NOT NULL DEFAULT ''"),
            ("used", "INTEGER NOT NULL DEFAULT 0"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("email_sent_to", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in evt_columns:
                db.execute(f"ALTER TABLE email_verification_tokens ADD COLUMN {column} {definition}")
        notif_columns = {row["name"] for row in db.execute("PRAGMA table_info(notifications)")}
        for column, definition in (
            ("email_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("email_last_error", "TEXT NOT NULL DEFAULT ''"),
            ("email_sent_at", "TEXT"),
        ):
            if column not in notif_columns:
                db.execute(f"ALTER TABLE notifications ADD COLUMN {column} {definition}")
        db.execute(
            """INSERT INTO application_status_history
            (application_id, status, actor_role, created_at)
            SELECT a.id, a.status, 'system', a.applied_at FROM applications a
            WHERE NOT EXISTS (
                SELECT 1 FROM application_status_history h WHERE h.application_id = a.id
            )"""
        )
        db.execute(
            """UPDATE assessments SET
            assessment_name = ?, assessment_round = 'Round 1',
            score_percentage = ROUND(score * 100.0 / NULLIF(total_questions, 0), 2),
            correct_answer_count = score,
            incorrect_answer_count = total_questions - score,
            result = CASE
                WHEN score * 100.0 / NULLIF(total_questions, 0) >= pass_percentage THEN 'Passed'
                ELSE 'Failed'
            END,
            result_processed_at = COALESCE(result_processed_at, submitted_at),
            time_taken_seconds = CASE
                WHEN starts_at IS NOT NULL AND submitted_at IS NOT NULL
                THEN MAX(0, CAST((julianday(submitted_at) - julianday(starts_at)) * 86400 AS INTEGER))
                ELSE time_taken_seconds
            END
            WHERE status = 'Completed' AND score IS NOT NULL AND result IS NULL""",
            (ROUND1_ASSESSMENT_NAME,),
        )
        migrated_results = db.execute(
            """SELECT x.application_id, a.status, x.recruiter_decision AS result, x.score_percentage,
            x.correct_answer_count, x.total_questions, x.pass_percentage
            FROM assessments x JOIN applications a ON a.id = x.application_id
            WHERE x.status = 'Completed' AND x.recruiter_decision IS NOT NULL
            AND a.status = 'Assessment Completed'"""
        ).fetchall()
        for result_row in migrated_results:
            db.execute(
                "UPDATE applications SET status = ? WHERE id = ?",
                (f"Round 1 {result_row['result']}", result_row["application_id"]),
            )
            latest = db.execute(
                """SELECT status FROM application_status_history
                WHERE application_id = ? ORDER BY created_at DESC, id DESC LIMIT 1""",
                (result_row["application_id"],),
            ).fetchone()
            round_status = f"Round 1 {result_row['result']}"
            if not latest or latest["status"] != round_status:
                record_application_status(
                    db, result_row["application_id"], round_status,
                    {"id": None, "role": "system"},
                    f"Round 1 score: {result_row['score_percentage']:g}% "
                    f"({result_row['correct_answer_count']}/{result_row['total_questions']} correct); "
                    f"passing score: {result_row['pass_percentage']:g}%.",
                )
        db.execute("CREATE INDEX IF NOT EXISTS idx_application_history_application ON application_status_history(application_id, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, is_read, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_job_questions_job_topic_active ON job_questions(job_id, technology, active)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_question_imports_job_created ON question_bank_imports(job_id, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_candidate_job ON assessments(candidate_id, job_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_token_hash ON assessments(token_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_assessment_questions_assessment ON assessment_questions(assessment_id, question_order)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_proctoring_events_assessment_time ON assessment_proctoring_events(assessment_id, created_at, id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_assessments_proctoring_flagged ON assessments(proctoring_flagged_at, starts_at)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_candidate_ai_summaries_app ON candidate_ai_summaries(application_id)")
        # Additional performance indexes
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_candidate ON applications(candidate_id, applied_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_recruiter ON jobs(recruiter_id, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_assessments_status ON assessments(status, submitted_at)")
        db.execute(
            """INSERT INTO application_status_history
            (application_id, status, actor_role, created_at)
            SELECT a.id, a.status, 'system', a.applied_at FROM applications a
            WHERE NOT EXISTS (
                SELECT 1 FROM application_status_history h WHERE h.application_id = a.id
            )"""
        )
        enable_demo = os.environ.get("SKILLSPHERE_ENABLE_DEMO", "true").lower() not in {"false", "0", "no"}

        # Seed configured production admin if specified
        admin_email = os.environ.get("SKILLSPHERE_ADMIN_EMAIL")
        admin_password = os.environ.get("SKILLSPHERE_ADMIN_PASSWORD")
        if admin_email and admin_password and not db.execute(
            "SELECT 1 FROM users WHERE email = ?", (admin_email,)
        ).fetchone():
            db.execute(
                "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                ("SkillSphere Admin", admin_email, password_hash(admin_password), now()),
            )

        if not enable_demo:
            return

        # Seed demo users if they do not exist
        if not db.execute("SELECT 1 FROM users WHERE email = ?", ("candidate@skillsphere.dev",)).fetchone():
            users = [
                ("Alex Morgan", "candidate@skillsphere.dev", password_hash("Demo123!"), "candidate", '{"location":"Brooklyn, NY"}', now()),
                ("Jamie Rivera", "recruiter@skillsphere.dev", password_hash("Demo123!"), "recruiter", '{"company":"Northstar"}', now()),
                ("Avery Chen", "admin@skillsphere.dev", password_hash("Demo123!"), "admin", "{}", now()),
            ]
            db.executemany(
                "INSERT OR IGNORE INTO users (name, email, password_hash, role, profile_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                users,
            )

        # Seed demo jobs if none exist
        if not db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
            recruiter_row = db.execute("SELECT id FROM users WHERE email = ?", ("recruiter@skillsphere.dev",)).fetchone()
            if recruiter_row:
                recruiter_id = recruiter_row["id"]
                jobs = [
                    ("Product Designer", "Northstar", "Shape clear, human-centered product experiences from early discovery through thoughtful delivery. Partner with product and engineering to make complex workflows feel effortless.", "New York, NY", "2–4 years", "Full-time", "$95k – $125k", ["Figma", "UX research", "Prototyping", "Design systems"]),
                    ("Frontend Engineer", "Layer", "Help us make useful tools feel delightful. Build accessible, polished interfaces with a product-minded team that cares about performance, collaboration, and the details.", "Remote · US", "3–5 years", "Remote", "$110k – $145k", ["React", "TypeScript", "CSS", "Accessibility"]),
                    ("Data Analyst", "Goodkind", "Turn questions into insights that help our teams and customers make better decisions. Work across product analytics, experimentation, and clear data storytelling.", "Austin, TX", "1–3 years", "Full-time", "$82k – $105k", ["SQL", "Python", "Tableau", "Analytics"]),
                    ("People Operations Partner", "Fieldwork", "Create warm, thoughtful experiences across the employee journey. Help build scalable people programs in a growing, remote-friendly company.", "Remote · US", "3–6 years", "Remote", "$88k – $118k", ["People ops", "HR", "Communication", "Operations"]),
                    ("Marketing Associate", "Sunday Studio", "Bring a curious, creative perspective to campaigns that help more people discover our work. You'll collaborate across content, community, and growth.", "Brooklyn, NY", "1–2 years", "Full-time", "$65k – $82k", ["Content", "Social media", "Writing", "Marketing"]),
                    ("Backend Engineer", "Northstar", "Build dependable APIs and thoughtfully designed services for a growing product. We value ownership, clear communication, and pragmatic engineering.", "New York, NY", "4–6 years", "Contract", "$125k – $160k", ["Python", "FastAPI", "PostgreSQL", "APIs"]),
                ]
                seeded_at = now()
                db.executemany(
                    """INSERT INTO jobs
                    (recruiter_id, title, company, description, location, experience, job_type, salary, skills_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(recruiter_id, *job[:7], json.dumps(job[7]), seeded_at) for job in jobs],
                )

        # Seed practice questions if table is empty
        if not db.execute("SELECT COUNT(*) FROM practice_questions").fetchone()[0]:
            practice_seed = [
                ("Python", "What is the primary difference between a list and a tuple in Python?", json.dumps(["Lists are mutable, tuples are immutable", "Lists cannot store strings, tuples can", "Tuples are always faster than lists for indexing", "Lists have fixed length, tuples do not"]), 0, "Easy", "Lists can be modified in-place whereas tuples cannot be altered after creation.", "Data Structures"),
                ("Python", "Which built-in function returns both the index and value during loop iteration?", json.dumps(["range()", "enumerate()", "zip()", "map()"]), 1, "Easy", "enumerate() generates tuples containing (index, element).", "Built-ins"),
                ("Python", "What does the '@' symbol signify when placed above a function definition?", json.dumps(["Decorator", "Async generator", "Global variable", "Macro"]), 0, "Medium", "The @ syntax in Python denotes a decorator that wraps function behavior.", "Functions"),
                ("Python", "How does a Python generator function yield values lazily?", json.dumps(["Using the 'return' statement", "Using the 'yield' statement", "Using a while loop only", "Using an inline lambda"]), 1, "Medium", "The 'yield' expression pauses function execution and returns an intermediate value.", "Generators"),
                ("JavaScript", "Which keyword defines a block-scoped variable that cannot be reassigned?", json.dumps(["var", "let", "const", "static"]), 2, "Easy", "const binds a block-scoped identifier that cannot be reassigned.", "Variables"),
                ("JavaScript", "What is the return type of the 'typeof null' operator in JavaScript?", json.dumps(["'null'", "'undefined'", "'object'", "'boolean'"]), 2, "Medium", "In JavaScript, typeof null returns 'object' due to a historical design decision.", "Types"),
                ("JavaScript", "What does Promise.all() do when one of the passed promises rejects?", json.dumps(["Waits for all others to settle", "Immediately rejects with that reason", "Ignores the error and returns partial results", "Retries the rejected promise"]), 1, "Medium", "Promise.all() rejects immediately upon the first rejected promise (fail-fast).", "Async"),
                ("React", "Which hook should be used to perform side effects such as data fetching in functional components?", json.dumps(["useState", "useEffect", "useMemo", "useRef"]), 1, "Easy", "useEffect runs side effects after component render cycles.", "Hooks"),
                ("React", "What is the primary benefit of the 'key' prop when rendering lists in React?", json.dumps(["Styles the list element", "Helps React identify which items have changed, been added, or removed", "Enforces unique CSS classes", "Enables Redux state tracking"]), 1, "Easy", "Keys provide a stable identity across renders for efficient DOM reconciliation.", "Reconciliation"),
                ("SQL", "Which clause is used to filter records based on aggregated group conditions?", json.dumps(["WHERE", "GROUP BY", "HAVING", "ORDER BY"]), 2, "Medium", "HAVING filters groups created by GROUP BY, unlike WHERE which filters individual rows.", "Queries"),
                ("SQL", "What is the effect of an INNER JOIN between Table A and Table B?", json.dumps(["Returns all rows from both tables", "Returns rows that have matching values in both tables", "Returns all rows from Table A and only matching from Table B", "Returns Cartesian product"]), 1, "Easy", "INNER JOIN selects records that have matching values in both datasets.", "Joins"),
                ("HTML/CSS", "What does CSS Flexbox 'justify-content: space-between' do?", json.dumps(["Centers all flex items", "Evenly distributes items with first item at start and last at end", "Stacks items vertically", "Adds margin around the container"]), 1, "Easy", "Items are evenly distributed along the main axis; first item touches start, last touches end.", "Flexbox"),
            ]
            now_iso = now()
            db.executemany(
                """INSERT INTO practice_questions
                (technology, question, options_json, correct_index, difficulty, explanation, subtopic, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(*item, now_iso) for item in practice_seed],
            )


def public_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    try:
        job["skills"] = json.loads(job.pop("skills_json", "[]") or "[]")
    except (json.JSONDecodeError, KeyError, TypeError):
        job["skills"] = []
    return job


def public_resume(row: sqlite3.Row) -> dict[str, Any]:
    resume = dict(row)
    resume.pop("storage_path", None)
    resume.pop("extracted_text", None)
    try:
        resume["parsed_profile"] = json.loads(resume.pop("parsed_profile_json", "{}") or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        resume["parsed_profile"] = {}
    return resume


def public_application(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def detect_profile(text: str) -> dict[str, Any]:
    # 1. Attempt AI-assisted parsing if Gemini is configured and functional
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key and len(text) >= 20:
        prompt = (
            "Analyze this resume text and extract candidate profile information. "
            "Respond ONLY with a JSON object in this exact structure:\n"
            "{\n"
            '  "name": "Full Name",\n'
            '  "email": "email@example.com",\n'
            '  "phone": "phone number",\n'
            '  "location": "City, Country",\n'
            '  "skills": ["Skill 1", "Skill 2"],\n'
            '  "summary": "1-2 sentence professional summary"\n'
            "}\n\n"
            f"Resume Text:\n{text[:4000]}"
        )
        try:
            llm_result = call_llm_json(prompt, "You are a professional resume parser.")
            if isinstance(llm_result, dict) and llm_result.get("skills"):
                return {
                    "name": str(llm_result.get("name") or "").strip(),
                    "email": str(llm_result.get("email") or "").strip(),
                    "phone": str(llm_result.get("phone") or "").strip(),
                    "location": str(llm_result.get("location") or "").strip(),
                    "skills": [str(s).strip() for s in llm_result.get("skills", []) if s][:30],
                    "summary": str(llm_result.get("summary") or "").strip(),
                }
        except Exception as e:
            logger.warning("Gemini resume profile parsing failed, using standalone local engine: %s", e)

    # 2. Standalone Intelligent Local Parser (100% offline, zero external dependencies)
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text)
    email = email_match.group(0).lower() if email_match else ""

    phone_match = re.search(
        r"(?:(?:\+|00)\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}\b",
        text
    )
    phone = phone_match.group(0).strip() if phone_match else ""

    # Name Detection: scan candidate lines with header noise filters + email fallback
    name = ""
    noise_header = re.compile(
        r"\b(resume|curriculum\s+vitae|cv|page|profile|summary|contact|portfolio|phone|email|"
        r"education|experience|projects|skills|technical|github|linkedin|address|personal|details|"
        r"developer|engineer|analyst|intern|senior|junior|lead|manager|consultant)\b",
        re.I
    )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:8]:
        if 2 <= len(line) <= 50 and not re.search(r"[@\d:/\\|]", line):
            words = line.split()
            if 1 <= len(words) <= 4 and not noise_header.search(line):
                # Verify words look like proper names
                if all(w[0].isalpha() for w in words):
                    name = " ".join(w.capitalize() for w in words)
                    break

    # If header name was not found, deduce candidate name from email username
    if not name and email:
        username = email.split("@")[0]
        cleaned_parts = [p.capitalize() for p in re.split(r"[._\-+]+", username) if len(p) >= 2 and not p.isdigit()]
        if cleaned_parts:
            name = " ".join(cleaned_parts)

    # Location Detection: scan for common location patterns
    location = ""
    loc_explicit_match = re.search(
        r"(?:location|address|city|residing\s+in)[\s:\-]+([A-Z][a-zA-Z\s]{2,25},\s*(?:[A-Z]{2}|[A-Z][a-zA-Z]{2,20}))\b",
        text,
        re.I
    )
    if loc_explicit_match:
        cand_loc = loc_explicit_match.group(1).strip()
        if not noise_header.search(cand_loc) and len(cand_loc.split()) <= 4:
            location = cand_loc
    if not location:
        generic_loc_match = re.search(
            r"\b([A-Z][a-zA-Z\s]{2,20},\s*(?:[A-Z]{2}|[A-Z][a-zA-Z]{2,15}))\b",
            text
        )
        if generic_loc_match:
            cand_loc = generic_loc_match.group(1).strip()
            cand_lower = cand_loc.lower()
            # Ensure it is not matching tech skills like "Python, React" or section headers
            tech_noise = ("python", "react", "sql", "java", "fastapi", "docker", "developer", "engineer", "analyst")
            if not noise_header.search(cand_loc) and not any(k in cand_lower for k in tech_noise) and len(cand_loc.split()) <= 4:
                location = cand_loc

    # Summary Detection: scan text for introductory overview
    summary = ""
    sum_match = re.search(
        r"(?:summary|professional\s+summary|profile|about\s+me|objective)[\s:\-\n]+(.*?)(?=\n\s*(?:experience|skills|education|projects|technical|\Z))",
        text,
        re.I | re.S
    )
    if sum_match:
        extracted_summary = " ".join(sum_match.group(1).split()).strip()
        if 20 <= len(extracted_summary) <= 300:
            summary = extracted_summary

    # Comprehensive Skills Detection (Section Parsing + Keyword Dictionary)
    skill_display_map = {
        "sql": "SQL", "aws": "AWS", "gcp": "GCP", "html": "HTML", "html5": "HTML5",
        "css": "CSS", "css3": "CSS3", "api": "API", "rest": "REST API", "graphql": "GraphQL",
        "ci/cd": "CI/CD", "llm": "LLM", "nlp": "NLP", "c++": "C++", "c#": "C#", "php": "PHP",
        "javascript": "JavaScript", "typescript": "TypeScript", "nodejs": "Node.js",
        "node.js": "Node.js", "react": "React", "react.js": "React", "nextjs": "Next.js",
        "next.js": "Next.js", "vue": "Vue.js", "vue.js": "Vue.js", "fastapi": "FastAPI",
        "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mongodb": "MongoDB",
        "mysql": "MySQL", "docker": "Docker", "kubernetes": "Kubernetes", "k8s": "Kubernetes",
        "python": "Python", "java": "Java", "figma": "Figma", "git": "Git", "github": "GitHub",
        "gitlab": "GitLab", "linux": "Linux", "tailwind": "Tailwind", "tailwindcss": "Tailwind CSS",
        "redux": "Redux", "express": "Express", "expressjs": "Express", "django": "Django",
        "flask": "Flask", "spring": "Spring Boot", "spring boot": "Spring Boot", "jira": "Jira"
    }

    known_skills = (
        # Languages
        "python", "javascript", "typescript", "java", "kotlin", "swift",
        "go", "golang", "rust", "c++", "c#", "php", "ruby", "scala", "dart",
        "c", "r", "matlab",
        # Frontend
        "react", "react.js", "next.js", "nextjs", "vue", "vue.js", "nuxt", "angular",
        "svelte", "html", "html5", "css", "css3", "sass", "tailwind", "tailwindcss",
        "bootstrap", "webpack", "vite", "redux",
        # Backend / APIs
        "fastapi", "django", "flask", "node.js", "nodejs", "express", "expressjs",
        "spring boot", "spring", "rails", "graphql", "rest", "grpc", "api",
        # Databases
        "sql", "postgresql", "postgres", "mysql", "mongodb", "redis",
        "sqlite", "elasticsearch", "dynamodb", "firebase", "oracle",
        # Cloud / DevOps
        "aws", "azure", "google cloud", "gcp", "docker", "kubernetes", "k8s",
        "terraform", "ansible", "ci/cd", "github actions", "jenkins",
        "git", "github", "gitlab", "linux", "bash", "shell scripting",
        # Data / ML / AI
        "machine learning", "deep learning", "pytorch", "tensorflow", "keras",
        "scikit-learn", "sklearn", "pandas", "numpy", "data analysis", "data science",
        "llm", "nlp", "computer vision", "opencv", "tableau", "power bi", "excel",
        "spark", "airflow", "dbt",
        # Design / Product
        "figma", "sketch", "ux research", "product design", "prototyping",
        "design systems", "user research", "wireframing", "adobe xd",
        # Soft / management
        "project management", "agile", "scrum", "communication",
        "leadership", "product management", "jira",
    )

    found_skills: list[str] = []
    seen: set[str] = set()

    # Pass A: Extract skills directly from explicit "Skills:" sections in resume
    sec_match = re.search(
        r"(?:technical\s+skills|skills\s*&?\s*tools|skills|technologies|core\s+competencies|languages\s*&?\s*frameworks)[\s:\-\n]+(.*?)(?=\n\s*(?:experience|employment|work|education|projects|certifications|\Z))",
        text,
        re.I | re.S
    )
    if sec_match:
        tokens = re.split(r"[,•|\n\t;*\/·]+", sec_match.group(1))
        for tok in tokens:
            cleaned = re.sub(r"^[a-zA-Z\s]+:\s*", "", tok).strip()
            if 2 <= len(cleaned) <= 25 and not re.search(r"[@\d]{3,}", cleaned):
                if cleaned.lower() not in {"and", "etc", "various", "tools", "skills", "experience", "basic"}:
                    display = skill_display_map.get(cleaned.lower(), cleaned.title())
                    if display.lower() not in seen:
                        seen.add(display.lower())
                        found_skills.append(display)

    # Pass B: Scan full text against the curated tech dictionary
    for skill in known_skills:
        if re.search(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])", text, re.I):
            display = skill_display_map.get(skill.lower(), skill.title())
            if display.lower() not in seen:
                seen.add(display.lower())
                found_skills.append(display)

    return {
        "name": name or "",
        "email": email,
        "phone": phone,
        "location": location,
        "skills": found_skills[:30],
        "summary": summary,
    }


def calculate_match(
    job: sqlite3.Row | dict[str, Any],
    resume: sqlite3.Row | dict[str, Any],
    saved_profile_json: str,
) -> dict[str, Any]:
    try:
        required = json.loads(job["skills_json"]) if job["skills_json"] else []
    except (json.JSONDecodeError, TypeError):
        required = []
    try:
        resume_profile = json.loads(resume["parsed_profile_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        resume_profile = {}
    try:
        saved_profile = json.loads(saved_profile_json or "{}")
    except (json.JSONDecodeError, TypeError):
        saved_profile = {}
    extracted_skills = resume_profile.get("skills", [])
    saved_skills = saved_profile.get("skills", [])
    if not isinstance(extracted_skills, list) or not all(isinstance(skill, str) for skill in extracted_skills):
        raise HTTPException(status_code=500, detail="The saved resume profile is invalid")
    if not isinstance(saved_skills, list) or not all(isinstance(skill, str) for skill in saved_skills):
        raise HTTPException(status_code=422, detail="Profile skills must be a list of text values")
    profile_skills = " ".join([*extracted_skills, *saved_skills])
    text = f"{resume['extracted_text']} {profile_skills}"
    matched = [
        skill for skill in required
        if re.search(rf"(?<![a-z0-9]){re.escape(skill.strip())}(?![a-z0-9])", text, re.I)
    ]
    missing = [skill for skill in required if skill not in matched]
    score = round(len(matched) / len(required) * 100) if required else 0
    if not matched:
        recommendation = "Add relevant experience or skills to your profile to strengthen your application."
    elif missing:
        recommendation = f"Highlight your experience with {', '.join(missing)} if it reflects your background."
    else:
        recommendation = "Your resume mentions every listed skill. Make sure your experience shows how you use them."
    return {
        "job_id": job["id"],
        "match_percentage": score,
        "matched_skills": matched,
        "missing_skills": missing,
        "recommendation": recommendation,
    }


def primary_assessment_topic(
    job: sqlite3.Row | dict[str, Any],
    resume: sqlite3.Row | dict[str, Any],
    saved_profile_json: str,
) -> str | None:
    resume_profile = json.loads(resume["parsed_profile_json"] or "{}")
    saved_profile = json.loads(saved_profile_json or "{}")
    profile_skills = [
        *resume_profile.get("skills", []),
        *saved_profile.get("skills", []),
        *detect_profile(resume["extracted_text"])["skills"],
    ]
    evidence = f"{resume['extracted_text']} {' '.join(profile_skills)}".lower()
    required = {skill.lower() for skill in json.loads(job["skills_json"])}
    detected: list[tuple[str, bool, int]] = []
    for skill, topic in TECHNICAL_SKILL_TOPICS.items():
        pattern = rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])"
        count = len(re.findall(pattern, evidence))
        if not count:
            continue
        relevant = skill in required or (topic is not None and any(
            assessment_topic_for_skill(required_skill) == topic for required_skill in required
        ))
        detected.append((skill, relevant, count))
    if not detected:
        return None
    detected.sort(key=lambda item: (item[1], item[2]), reverse=True)
    if len(detected) > 1 and detected[0][1:] == detected[1][1:]:
        if TECHNICAL_SKILL_TOPICS[detected[0][0]] != TECHNICAL_SKILL_TOPICS[detected[1][0]]:
            return None
    return TECHNICAL_SKILL_TOPICS[detected[0][0]]


def canonical_question_topic(skill: str) -> str:
    normalized = skill.strip().casefold()
    return (assessment_topic_for_skill(normalized) or skill.strip()).casefold()


def relevant_question_topic_weights(
    job: sqlite3.Row,
    resume: sqlite3.Row,
    saved_profile_json: str,
    available_topics: dict[str, int],
) -> tuple[dict[str, int], str]:
    resume_profile = json.loads(resume["parsed_profile_json"] or "{}")
    saved_profile = json.loads(saved_profile_json or "{}")
    candidate_skills = [
        *resume_profile.get("skills", []),
        *saved_profile.get("skills", []),
        *detect_profile(resume["extracted_text"])["skills"],
    ]
    required_skills = json.loads(job["skills_json"])
    candidate_text = f"{resume['extracted_text']} {' '.join(candidate_skills)}".casefold()
    candidate_topics = {canonical_question_topic(skill) for skill in candidate_skills if skill.strip()}
    for topic in available_topics:
        if re.search(rf"(?<![a-z0-9]){re.escape(topic.casefold())}(?![a-z0-9])", candidate_text):
            candidate_topics.add(topic.casefold())
    required_by_topic: dict[str, list[str]] = {}
    for skill in required_skills:
        required_by_topic.setdefault(canonical_question_topic(skill), []).append(skill)

    weights: dict[str, int] = {}
    for topic in available_topics:
        normalized_topic = topic.casefold()
        if normalized_topic not in candidate_topics or normalized_topic not in required_by_topic:
            continue
        matching_skills = required_by_topic[normalized_topic]
        strength = 0
        for skill in matching_skills:
            mentions = len(re.findall(
                rf"(?<![a-z0-9]){re.escape(skill.strip().casefold())}(?![a-z0-9])",
                candidate_text,
            ))
            strength += max(1, mentions)
        if strength:
            weights[topic] = strength
    if not weights:
        return {}, "No job-question topic was found in both candidate skills and job requirements."
    reason = (
        "Topics were selected from active questions whose technologies match both the candidate's "
        "resume/profile and the job's required skills; topic proportions are weighted by matching skill evidence."
    )
    return weights, reason


def _is_valid_active_question_row(question: sqlite3.Row) -> bool:
    q_text = str(question["question"] or "").strip()
    if len(q_text) < 10:
        return False
    try:
        opts = json.loads(question["options_json"])
    except Exception:
        return False
    if not isinstance(opts, list) or len(opts) != 4:
        return False
    cleaned_opts = [str(o).strip() for o in opts]
    if any(not o for o in cleaned_opts) or len({o.casefold() for o in cleaned_opts}) != 4:
        return False
    c_idx = question["correct_index"]
    return isinstance(c_idx, int) and 0 <= c_idx <= 3


def relevant_balanced_question_sample(
    questions: list[sqlite3.Row],
    topic_weights: dict[str, int],
    count: int,
) -> list[sqlite3.Row]:
    seen_ids: set[int] = set()
    seen_texts: set[tuple[str, str]] = set()
    unique_valid: list[sqlite3.Row] = []
    for question in questions:
        qid = question["id"] if "id" in question.keys() else None
        if qid is not None and qid in seen_ids:
            continue
        if not _is_valid_active_question_row(question):
            continue
        t_key = (str(question["technology"] or "").strip().casefold(), str(question["question"] or "").strip().casefold())
        if t_key in seen_texts:
            continue
        if qid is not None:
            seen_ids.add(qid)
        seen_texts.add(t_key)
        unique_valid.append(question)

    groups = {
        topic: [question for question in unique_valid if question["technology"].casefold() == topic.casefold()]
        for topic in topic_weights
    }
    active_topics = [topic for topic, group in groups.items() if group]
    if sum(len(group) for group in groups.values()) < count or not active_topics:
        return []
    total_weight = sum(max(1, topic_weights[topic]) for topic in active_topics)
    desired = {topic: count * max(1, topic_weights[topic]) / total_weight for topic in active_topics}
    allocation = {topic: min(len(groups[topic]), int(desired[topic])) for topic in active_topics}
    remaining = count - sum(allocation.values())
    while remaining:
        candidates = [topic for topic in active_topics if allocation[topic] < len(groups[topic])]
        if not candidates:
            return []
        candidates.sort(
            key=lambda topic: (desired[topic] - allocation[topic], len(groups[topic]) - allocation[topic], topic_weights[topic]),
            reverse=True,
        )
        allocation[candidates[0]] += 1
        remaining -= 1
    selected: list[sqlite3.Row] = []
    for topic in active_topics:
        selected.extend(balanced_question_sample(groups[topic], allocation[topic]))
    random.SystemRandom().shuffle(selected)
    return selected


def assessment_for_match(
    db: sqlite3.Connection,
    application_id: int,
    candidate_id: int,
    job: sqlite3.Row,
    resume: sqlite3.Row,
    saved_profile_json: str,
    match_percentage: int,
) -> None:
    questions = db.execute(
        """SELECT id, technology, question, options_json, correct_index, difficulty
        FROM job_questions WHERE job_id = ? AND active = 1 ORDER BY id""",
        (job["id"],),
    ).fetchall()
    valid_questions = [q for q in questions if _is_valid_active_question_row(q)]
    available_topics: dict[str, int] = {}
    for question in valid_questions:
        available_topics[question["technology"]] = available_topics.get(question["technology"], 0) + 1
    topic_weights, reasoning = relevant_question_topic_weights(
        job, resume, saved_profile_json, available_topics
    )
    relevant_questions = [
        question for question in valid_questions
        if question["technology"].casefold() in {topic.casefold() for topic in topic_weights}
    ]
    target_count = (
        job["assessment_question_count"]
        if ("assessment_question_count" in job.keys() and job["assessment_question_count"])
        else 20
    )
    total_available = len(valid_questions)
    manual_topics: list[str] = []
    if not topic_weights:
        manual_topics = [topic for topic, count in available_topics.items() if count >= target_count]
    if len(relevant_questions) < target_count and not manual_topics:
        if total_available >= target_count and available_topics:
            # Combined pool across all active topics meets target_count (e.g. 8 + 7 + 5 = 20)
            topic_weights = {topic: 1 for topic in available_topics}
            reasoning = (
                "Questions will be balanced across all available active topics in the job question bank."
            )
        else:
            owner = db.execute("SELECT recruiter_id FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if owner:
                already_notified = db.execute(
                    """SELECT 1 FROM notifications WHERE user_id = ? AND type = 'question_bank_incomplete'
                    AND related_application_id = ? LIMIT 1""",
                    (owner["recruiter_id"], application_id),
                ).fetchone()
                if not already_notified:
                    notify_user(
                        db, owner["recruiter_id"], "question_bank_incomplete",
                        "Candidate is waiting for an assessment",
                        f"At least {target_count} questions are required across the selected topics. Currently available: {total_available}.",
                        application_id=application_id, email=True,
                    )
                candidate_already_notified = db.execute(
                    """SELECT 1 FROM notifications WHERE user_id = ? AND type = 'assessment_pending'
                    AND related_application_id = ? LIMIT 1""",
                    (candidate_id, application_id),
                ).fetchone()
                if not candidate_already_notified:
                    notify_user(
                        db, candidate_id, "assessment_pending", "Your assessment is being prepared",
                        f"The hiring team is preparing the next step for your {job['title']} application. "
                        "You’ll receive a notification when it’s ready.",
                        application_id=application_id,
                    )
            return
    status = "Manual Topic Required" if manual_topics else "Not Sent"
    topics = manual_topics if manual_topics else list(topic_weights)
    technology = None if manual_topics else ", ".join(topics)
    if manual_topics:
        reasoning = "No active question topic matched both candidate skills and job requirements; recruiter selection is required."
    existing = db.execute(
        "SELECT id, status FROM assessments WHERE application_id = ?", (application_id,)
    ).fetchone()
    if existing and existing["status"] == "Completed":
        return
    if existing:
        db.execute("DELETE FROM assessment_questions WHERE assessment_id = ?", (existing["id"],))
        db.execute(
            """UPDATE assessments SET candidate_id = ?, job_id = ?, technology = ?,
            token_hash = NULL, status = ?, match_percentage = ?, starts_at = NULL,
            expires_at = NULL, submitted_at = NULL, score = NULL, total_questions = 20,
            assessment_name = ?, assessment_round = 'Round 1', pass_percentage = ?,
            score_percentage = NULL, correct_answer_count = NULL, incorrect_answer_count = NULL,
            result = NULL, result_processed_at = NULL, time_taken_seconds = NULL,
            duration_minutes = ?, email_sent_at = NULL, question_topics_json = ?,
            question_topic_weights_json = ?, selection_reasoning = ?,
            proctoring_required = ?, proctoring_consent_at = NULL,
            camera_permission_granted_at = NULL, proctoring_started_at = NULL,
            proctoring_warning_count = 0, proctoring_flagged_at = NULL WHERE id = ?""",
            (
                candidate_id, job["id"], technology, status, match_percentage,
                ROUND1_ASSESSMENT_NAME, ROUND1_PASS_PERCENTAGE,
                ASSESSMENT_DURATION_MINUTES, json.dumps(topics),
                json.dumps(topic_weights if not manual_topics else {}), reasoning,
                int(PROCTORING_REQUIRED), existing["id"],
            ),
        )
        db.execute(
            "DELETE FROM assessment_proctoring_events WHERE assessment_id = ?",
            (existing["id"],),
        )
    else:
        db.execute(
            """INSERT INTO assessments
            (application_id, candidate_id, job_id, technology, status, match_percentage,
             total_questions, assessment_name, assessment_round, pass_percentage,
             duration_minutes, question_topics_json, question_topic_weights_json,
             selection_reasoning, proctoring_required, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 20, ?, 'Round 1', ?, ?, ?, ?, ?, ?, ?)""",
            (
                application_id, candidate_id, job["id"], technology, status, match_percentage,
                ROUND1_ASSESSMENT_NAME, ROUND1_PASS_PERCENTAGE,
                ASSESSMENT_DURATION_MINUTES, json.dumps(topics),
                json.dumps(topic_weights if not manual_topics else {}), reasoning,
                int(PROCTORING_REQUIRED), now(),
            ),
        )


def extract_docx_text(content: bytes) -> str:
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as docx:
            xml_content = docx.read("word/document.xml")
            root = ET.fromstring(xml_content)
            namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            paragraphs = []
            for p in root.findall(".//w:p", namespaces):
                p_text = "".join(node.text for node in p.findall(".//w:t", namespaces) if node.text)
                if p_text.strip():
                    paragraphs.append(p_text.strip())
            if paragraphs:
                return "\n".join(paragraphs).strip()
            all_text = " ".join(node.text for node in root.findall(".//w:t", namespaces) if node.text)
            return all_text.strip()
    except Exception as e:
        logger.warning("DOCX text extraction failed: %s", e)
        return ""


def extract_pdf_stream_text(content: bytes) -> str:
    import zlib
    import re
    extracted_parts: list[str] = []
    # Find all stream ... endstream blocks in PDF
    for m in re.finditer(b"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
        raw_stream = m.group(1)
        decompressed = None
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, 32 + zlib.MAX_WBITS):
            try:
                decompressed = zlib.decompress(raw_stream, wbits)
                break
            except Exception:
                continue
        if decompressed is None:
            decompressed = raw_stream
        if decompressed:
            # Look for Tj PDF text operator: (Text) Tj
            for tj in re.finditer(rb"\(([^)]+)\)\s*Tj", decompressed):
                try:
                    s = tj.group(1).decode("latin-1", errors="ignore")
                    if s.strip():
                        extracted_parts.append(s.strip())
                except Exception:
                    pass
            # Look for TJ PDF text operator: [(Text) 120 (More)] TJ
            for tj_arr in re.finditer(rb"\[([^\]]+)\]\s*TJ", decompressed):
                for sub in re.finditer(rb"\(([^)]+)\)", tj_arr.group(1)):
                    try:
                        s = sub.group(1).decode("latin-1", errors="ignore")
                        if s.strip():
                            extracted_parts.append(s.strip())
                    except Exception:
                        pass
    raw_text = " ".join(extracted_parts).strip()
    if len(raw_text) >= 25 and re.search(r"[a-zA-Z]{3,}", raw_text):
        return raw_text
    return ""


def extract_text_via_gemini_vision(content: bytes, mime_type: str) -> str:
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        return ""
    import base64
    import io

    images_to_send: list[tuple[str, str]] = []
    if mime_type == "application/pdf":
        try:
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(content)
            # Render first 1-3 pages to JPEG for optimal multimodal OCR
            for i in range(min(len(doc), 3)):
                pil_img = doc[i].render(scale=1.5).to_pil()
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=85)
                images_to_send.append(("image/jpeg", base64.b64encode(buf.getvalue()).decode("utf-8")))
        except Exception as e:
            logger.warning("Rendering PDF pages to JPEG for Gemini Vision failed: %s", e)
            images_to_send.append(("application/pdf", base64.b64encode(content).decode("utf-8")))
    elif mime_type in {"image/jpeg", "image/png"}:
        images_to_send.append((mime_type, base64.b64encode(content).decode("utf-8")))
    else:
        return ""

    for model in ["gemini-2.0-flash", "gemini-1.5-flash"]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            parts: list[dict[str, Any]] = [
                {"text": "Transcribe and extract all resume text from this document accurately, including contact information, education, work experience, and technical/professional skills. Return ONLY the extracted plain text without commentary."}
            ]
            for img_mime, img_b64 in images_to_send:
                parts.append({
                    "inlineData": {
                        "mimeType": img_mime,
                        "data": img_b64
                    }
                })
            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.1}
            }
            with httpx.Client(timeout=30.0) as client:
                res = client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    parts_out = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
                    raw_text = parts_out[0].get("text", "").strip()
                    if raw_text and len(raw_text) >= 10:
                        return raw_text
                else:
                    logger.warning("Gemini Vision HTTP %s: %s", res.status_code, res.text[:200])
        except Exception as e:
            logger.warning("Gemini Vision resume extraction failed with %s: %s", model, e)
    return ""


def extract_resume_text(content: bytes, content_type: str) -> str:
    if content_type == "text/plain":
        try:
            return content.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        docx_text = extract_docx_text(content)
        if docx_text and len(docx_text) >= 10:
            return docx_text
        return ""

    if content_type == "application/pdf":
        from pypdf import PdfReader
        import io

        # Stage 1: Try pypdf text extraction (100% offline)
        pypdf_text = ""
        try:
            reader = PdfReader(io.BytesIO(content))
            if len(reader.pages) > 40:
                raise HTTPException(status_code=422, detail="This PDF has too many pages. Please upload a document with 40 pages or fewer.")

            # Try layout mode first for multi-column resumes, then standard
            pages_text = []
            for page in reader.pages:
                try:
                    pt = page.extract_text(extraction_mode="layout") or page.extract_text() or ""
                except Exception:
                    pt = page.extract_text() or ""
                if pt.strip():
                    pages_text.append(pt.strip())

            # Also extract interactive form fields if present
            if hasattr(reader, "get_form_text_fields"):
                try:
                    form_fields = reader.get_form_text_fields()
                    if form_fields:
                        for k, v in form_fields.items():
                            if v and str(v).strip():
                                pages_text.append(f"{k}: {v}")
                except Exception:
                    pass
            pypdf_text = "\n\n".join(pages_text).strip()
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=422, detail="This PDF is unreadable or corrupted. Please check the file and try again.") from error

        # Stage 2: Try pypdfium2 native textpage extraction (100% offline Chromium PDF engine)
        pdfium_text = ""
        try:
            import pypdfium2 as pdfium
            document = pdfium.PdfDocument(content)
            pdfium_pages = []
            for index in range(len(document)):
                try:
                    page = document[index]
                    textpage = page.get_textpage()
                    # count=-1 extracts all characters on the page
                    pt = textpage.get_text_range(0, -1) or textpage.get_text_range()
                    if pt and pt.strip():
                        pdfium_pages.append(pt.strip())
                except Exception:
                    continue
            pdfium_text = "\n\n".join(pdfium_pages).strip()
        except Exception as pdfium_err:
            logger.warning("pypdfium2 native textpage extraction failed: %s", pdfium_err)

        # Pick whichever offline engine extracted more content
        best_offline_text = pdfium_text if len(pdfium_text) > len(pypdf_text) else pypdf_text
        if best_offline_text and len(best_offline_text) >= 15:
            return best_offline_text

        # Stage 3: Low-level PDF stream decompression (100% offline pure Python)
        stream_text = extract_pdf_stream_text(content)
        if stream_text and len(stream_text) >= 25:
            return stream_text

        # Stage 4: Try Gemini Vision OCR (multimodal AI cloud OCR)
        gemini_text = extract_text_via_gemini_vision(content, "application/pdf")
        if gemini_text and len(gemini_text) >= 15:
            return gemini_text

        # Stage 5: Try local Tesseract OCR (if installed)
        try:
            import pypdfium2 as pdfium
            import pytesseract

            document = pdfium.PdfDocument(content)
            pages = []
            for index in range(min(len(document), 5)):
                image = document[index].render(scale=1.5).to_pil()
                page_ocr = pytesseract.image_to_string(image).strip()
                if page_ocr:
                    pages.append(page_ocr)
            ocr_text = "\n\n".join(pages).strip()
            if ocr_text and len(ocr_text) >= 15:
                return ocr_text
        except Exception as ocr_error:
            logger.warning("Scanned PDF local OCR could not be performed: %s", ocr_error)

        return ""

    if content_type in {"image/jpeg", "image/png"}:
        from PIL import Image
        import io

        expected_format = "JPEG" if content_type == "image/jpeg" else "PNG"
        try:
            image = Image.open(io.BytesIO(content), formats=[expected_format])
            image.verify()
        except Exception as error:
            raise HTTPException(status_code=422, detail="This image is unreadable or corrupted. Please check the file and try again.") from error

        # Stage 1: Try Gemini Vision OCR (cloud multimodal AI)
        gemini_text = extract_text_via_gemini_vision(content, content_type)
        if gemini_text and len(gemini_text) >= 15:
            return gemini_text

        # Stage 2: Try local Tesseract OCR (if installed)
        try:
            import pytesseract
            image = Image.open(io.BytesIO(content), formats=[expected_format]).convert("RGB")
            ocr_res = pytesseract.image_to_string(image).strip()
            if ocr_res and len(ocr_res) >= 15:
                return ocr_res
        except Exception as ocr_error:
            logger.warning("Image local OCR could not be performed: %s", ocr_error)

        return ""

    return ""


def startup() -> None:
    initialize_database()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, Any]:
    """Health-check endpoint for load balancers and uptime monitors."""
    db_ok = False
    try:
        with connect() as db:
            db.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        logger.exception("Health check: database is unavailable")
    status = "ok" if db_ok else "degraded"
    return {"status": status, "database": "ok" if db_ok else "unavailable", "version": "1.0.0"}


def issue_verification_challenge(
    db: sqlite3.Connection,
    user_id: int,
    name: str,
    email: str,
) -> tuple[str, str]:
    db.execute(
        "UPDATE email_verification_tokens SET used = 1 WHERE user_id = ? AND used = 0",
        (user_id,),
    )
    raw_token = secrets.token_urlsafe(32)
    otp = f"{secrets.randbelow(900000) + 100000}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    ts = now()
    db.execute(
        """INSERT INTO email_verification_tokens
        (user_id, token_hash, otp_hash, used, attempts, email_sent_to, expires_at, created_at)
        VALUES (?, ?, ?, 0, 0, ?, ?, ?)""",
        (user_id, token_hash, otp_hash, email.lower(), expires_at, ts),
    )
    verify_link = f"{PUBLIC_BASE_URL}/?{urlencode({'verify_email': raw_token})}"
    subject = "SkillSphere: Verify your email address"
    content = (
        f"Hello {name},\n\n"
        f"Welcome to SkillSphere! Please verify your email address to activate your account.\n\n"
        f"Verification code (OTP): {otp}\n"
        f"Or verify using this secure link: {verify_link}\n\n"
        f"This link and code are single-use and expire in 24 hours."
    )
    try:
        delivery = send_email_message(email.lower(), subject, content)
    except Exception as exc:
        logger.warning("Could not send verification email: %s", exc)
        delivery = "Failed"
    try:
        _record_email_log(
            db,
            notification_id=None,
            user_id=user_id,
            recipient_email=email.lower(),
            category="verification",
            subject=subject,
            body_text=content,
            status=delivery,
            attempts=1,
            sent_at=ts if delivery in {"Sent", "Development"} else None,
            last_error="" if delivery != "Failed" else "Failed to send verification email",
        )
    except Exception as log_exc:
        logger.warning("Could not record verification email log: %s", log_exc)
    return raw_token, otp


@app.post("/api/auth/register", status_code=201)
@limiter.limit("10/minute")
def register(request: Request, body: RegisterBody) -> dict[str, Any]:
    role = body.role.lower()
    if role not in {"candidate", "recruiter"}:
        raise HTTPException(status_code=422, detail="Choose a job seeker or recruiter account")
    email = str(body.email).strip().lower()
    if not is_valid_email_address(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    try:
        with connect() as db:
            existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                raise HTTPException(status_code=409, detail="An account with this email already exists")
            cursor = db.execute(
                "INSERT INTO users (name, email, password_hash, role, email_verified, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (body.name.strip(), email, password_hash(body.password), role, now()),
            )
            user_id = cursor.lastrowid
            row = (
                db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
                if user_id
                else db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            )
            if not row:
                raise HTTPException(status_code=500, detail="Could not create account")
            user_id = row["id"]
            raw_token, otp = issue_verification_challenge(db, user_id, body.name.strip(), email)
            log_audit(db, user_id, role, "ACCOUNT_REGISTERED", "user", user_id)
    except HTTPException:
        raise
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="An account with this email already exists") from error
    except Exception as error:
        if "unique" in str(error).lower() or "constraint" in str(error).lower():
            raise HTTPException(status_code=409, detail="An account with this email already exists") from error
        logger.exception("Registration failed for %s", email)
        raise HTTPException(status_code=500, detail="Registration failed. Please try again.") from error
    user = public_user(row)
    resp: dict[str, Any] = {
        "verification_required": True,
        "access_token": None,
        "token_type": "bearer",
        "user": user,
        "message": "Account created. Please check your email for your verification link or 6-digit code.",
    }
    if not IS_PRODUCTION and not (os.environ.get("RESEND_API_KEY", "").strip() or os.environ.get("SENDGRID_API_KEY", "").strip() or os.environ.get("SKILLSPHERE_SMTP_HOST", "").strip()):
        resp["dev_token"] = raw_token
        resp["dev_otp"] = otp
    return resp


@app.post("/api/auth/login")
@limiter.limit("20/minute")
def login(request: Request, body: LoginBody) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (body.email.lower(),)).fetchone()
    if not row or not row["active"] or not check_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    if "email_verified" in row.keys() and not row["email_verified"]:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email address before signing in.",
        )
    return {"access_token": encode_token(row), "token_type": "bearer", "user": public_user(row)}


@app.get("/api/me")
def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return user


@app.get("/api/jobs")
def list_jobs(
    q: str = "",
    location: str = "",
    job_type: str = "",
    experience: str = "",
    skill: str = "",
    mine: bool = False,
    sort_by: str = "newest",
    page: int = 1,
    page_size: int = 50,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="Page must be positive and page size must be between 1 and 100")
    clauses = ["j.status = 'Open'"]
    values: list[Any] = []
    if mine:
        if user["role"] != "recruiter":
            raise HTTPException(status_code=403, detail="Only recruiters can view their own job listings")
        clauses = ["j.recruiter_id = ?", "j.status != 'Archived'"]
        values = [user["id"]]
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ? OR j.skills_json LIKE ?)")
        values.extend([like, like, like, like])
    if location.strip():
        clauses.append("j.location LIKE ?")
        values.append(f"%{location.strip()}%")
    if job_type.strip():
        clauses.append("j.job_type = ?")
        values.append(job_type.strip())
    if experience.strip():
        clauses.append("j.experience LIKE ?")
        values.append(f"%{experience.strip()}%")
    if skill.strip():
        clauses.append("j.skills_json LIKE ?")
        values.append(f"%{skill.strip()}%")
    sort_options = {
        "newest": "j.created_at DESC",
        "company": "j.company COLLATE NOCASE ASC",
        "title": "j.title COLLATE NOCASE ASC",
    }
    if sort_by not in sort_options:
        raise HTTPException(status_code=422, detail="Choose a valid job sort order")
    where = " AND ".join(clauses)
    with connect() as db:
        total = db.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", values).fetchone()[0]
        rows = db.execute(
            f"SELECT j.* FROM jobs j WHERE {where} ORDER BY {sort_options[sort_by]}, j.id DESC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
    return {"items": [public_job(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@app.get("/api/jobs/saved")
def list_saved_jobs(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT j.*, s.created_at AS saved_at
            FROM saved_jobs s JOIN jobs j ON j.id = s.job_id
            WHERE s.candidate_id = ? AND j.status = 'Open'
            ORDER BY s.created_at DESC""",
            (user["id"],),
        ).fetchall()
    return {"items": [public_job(r) for r in rows], "total": len(rows)}


@app.get("/api/jobs/recommended")
def recommended_jobs(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        resume_row = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
        jobs_rows = db.execute("SELECT * FROM jobs WHERE status = 'Open' ORDER BY created_at DESC LIMIT 50").fetchall()
        user_profile = json.dumps(user.get("profile") or {})
        recommendations = []
        for j_row in jobs_rows:
            job = public_job(j_row)
            if resume_row:
                match = calculate_match(job, resume_row, user_profile)
                score = match.get("match_percentage", 0)
                matched_skills = match.get("matched_skills", [])
                missing_skills = match.get("missing_skills", [])
            else:
                score = 50
                matched_skills = []
                missing_skills = job.get("skills", [])
            recommendations.append({
                **job,
                "match_percentage": score,
                "matched_skills": matched_skills,
                "missing_skills": missing_skills,
            })
        recommendations.sort(key=lambda x: x["match_percentage"], reverse=True)
    return {"items": recommendations[:10], "total": len(recommendations[:10])}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    del user
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ? AND status = 'Open'", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="This role is no longer available")
    return public_job(row)


@app.delete("/api/jobs/{job_id}")
def delete_job(
    job_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = owned_job(db, job_id, user)
        now_iso = now()
        db.execute(
            "UPDATE jobs SET status = 'Archived', is_archived = 1, archived_at = ? WHERE id = ?",
            (now_iso, job_id),
        )
    logger.info("Job %s soft-archived by %s %s", job_id, user.get("role"), user.get("id"))
    return {"deleted": True, "archived": True, "job_id": job_id, "detail": "Job posting removed and archived successfully"}


@app.post("/api/jobs", status_code=201)
def create_job(body: JobBody, user: dict[str, Any] = Depends(require_role("recruiter", "admin"))) -> dict[str, Any]:
    if len((body.description or "").strip()) < 20:
        raise HTTPException(
            status_code=422,
            detail="Job description must contain at least 20 characters before you can post this job.",
        )
    skills = list(dict.fromkeys(skill.strip() for skill in body.skills if skill.strip()))
    if not skills:
        raise HTTPException(status_code=422, detail="Add at least one required skill")
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO jobs (recruiter_id, title, company, description, location, experience, job_type, salary, skills_json, assessment_question_count, allow_detailed_results, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], body.title.strip(), body.company.strip(), body.description.strip(), body.location.strip(),
             body.experience.strip(), body.job_type.strip(), body.salary.strip(), json.dumps(skills),
             body.assessment_question_count, 1 if body.allow_detailed_results else 0, now()),
        )
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        admins = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchall()
        for admin in admins:
            add_notification(
                db, admin["id"], "job_posted", "New job posted",
                f"{body.title} was posted by {body.company}.",
            )
    return public_job(row)


@app.patch("/api/jobs/{job_id}/assessment-settings")
def update_job_assessment_settings(
    job_id: int,
    body: JobAssessmentSettingsBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        db.execute(
            """UPDATE jobs SET assessment_question_count = ?, allow_detailed_results = ?
            WHERE id = ?""",
            (body.assessment_question_count, 1 if body.allow_detailed_results else 0, job_id),
        )
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return public_job(row)


def owned_job(db: sqlite3.Connection, job_id: int, user: dict[str, Any]) -> sqlite3.Row:
    job = db.execute(
        "SELECT * FROM jobs WHERE id = ? AND (? = 'admin' OR recruiter_id = ?)",
        (job_id, user["role"], user["id"]),
    ).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job listing not found for your account")
    return job


def validated_question(body: QuestionBody) -> tuple[str, str, list[str], int, str, int, str, str, str]:
    technology = body.technology.strip()
    question = body.question.strip()
    if not technology or not question:
        raise HTTPException(status_code=422, detail="Technology and question text are required")
    options = [option.strip() for option in body.options]
    if any(not option for option in options) or len({option.casefold() for option in options}) != 4:
        raise HTTPException(status_code=422, detail="Provide four distinct, non-empty answer options")
    return (
        technology, question, options,
        body.correct_index, body.difficulty, int(body.active),
        body.explanation.strip(), body.learning_resource_url.strip(), body.subtopic.strip(),
    )


def job_question_summary(db: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    job = db.execute("SELECT assessment_question_count FROM jobs WHERE id = ?", (job_id,)).fetchone()
    target_count = (job["assessment_question_count"] if job and job["assessment_question_count"] else 20)
    difficulty_rows = db.execute(
        """SELECT technology, difficulty, COUNT(*) AS active_count
        FROM job_questions WHERE job_id = ? AND active = 1
        GROUP BY technology, difficulty ORDER BY technology, difficulty""",
        (job_id,),
    ).fetchall()
    by_technology: dict[str, dict[str, int]] = {}
    for row in difficulty_rows:
        by_technology.setdefault(row["technology"], {})[row["difficulty"]] = row["active_count"]
    technology_rows = db.execute(
        """SELECT technology, COUNT(*) AS active_count FROM job_questions
        WHERE job_id = ? AND active = 1 GROUP BY technology ORDER BY technology""",
        (job_id,),
    ).fetchall()
    topics = [
        {"technology": row["technology"], "active_count": row["active_count"],
         "difficulty_counts": by_technology.get(row["technology"], {}),
         "ready": row["active_count"] >= target_count, "additional_needed": max(0, target_count - row["active_count"])}
        for row in technology_rows
    ]
    active_total = sum(topic["active_count"] for topic in topics)
    needed = max(0, target_count - active_total)
    total_by_difficulty = {
        row["difficulty"]: row["active_count"]
        for row in db.execute(
            """SELECT difficulty, COUNT(*) AS active_count FROM job_questions
            WHERE job_id = ? AND active = 1 GROUP BY difficulty""",
            (job_id,),
        )
    }
    return {
        "job_id": job_id,
        "configured_question_count": target_count,
        "active_count": active_total,
        "difficulty_counts": total_by_difficulty,
        "ready": active_total >= target_count,
        "additional_needed": needed,
        "topics": topics,
    }


def parse_question_bank_text(text: str) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    current_technology = ""
    current_difficulty = "Medium"
    current_difficulty_valid = True
    block: dict[str, Any] | None = None

    def finish_block() -> None:
        nonlocal block
        if block is None:
            return
        source_index = len(questions)
        question_text = " ".join(line.strip() for line in block["question_lines"] if line.strip()).strip()
        options = [block["options"].get(label, "").strip() for label in "ABCD"]
        errors: list[str] = []
        if not block["technology"]:
            errors.append(f"Question {source_index + 1} is missing Technology.")
        if not question_text:
            errors.append(f"Question {source_index + 1} is missing question text.")
        for label, option in zip("ABCD", options):
            if not option:
                errors.append(f"Question {source_index + 1} is missing option {label}.")
        for label in sorted(block["duplicate_options"]):
            errors.append(f"Question {source_index + 1} has duplicate option {label}.")
        if block["duplicate_answer"]:
            errors.append(f"Question {source_index + 1} has multiple Answer fields.")
        if not block["difficulty_valid"]:
            errors.append(f"Question {source_index + 1} has an invalid difficulty; use Easy, Medium, or Hard.")
        answer = block["answer"]
        if answer not in {"A", "B", "C", "D"}:
            errors.append(f"Question {source_index + 1} has no valid Answer field.")
        duplicate = any(
            item["question"].casefold() == question_text.casefold() for item in questions
            if item["question"]
        )
        if duplicate:
            errors.append(f"Question {source_index + 1} duplicates an earlier question in this PDF.")
        questions.append({
            "source_index": source_index,
            "technology": block["technology"] or "",
            "difficulty": block["difficulty"] or "Medium",
            "question": question_text,
            "options": options,
            "correct_index": "ABCD".find(answer) if answer in {"A", "B", "C", "D"} else -1,
            "valid": not errors,
            "errors": errors,
            "include": not errors,
        })
        block = None

    for raw_line in text.replace("\r", "\n").splitlines():
        line = raw_line.strip()
        technology_match = re.match(r"^Technology\s*:\s*(.*)$", line, re.I)
        difficulty_match = re.match(r"^Difficulty\s*:\s*(.*)$", line, re.I)
        question_match = re.match(r"^Question\s*:\s*(.*)$", line, re.I)
        if question_match:
            finish_block()
            block = {
                "technology": current_technology,
                "difficulty": current_difficulty,
                "difficulty_valid": current_difficulty_valid,
                "question_lines": [question_match.group(1).strip()],
                "options": {},
                "duplicate_options": set(),
                "duplicate_answer": False,
                "current_option": None,
                "answer": "",
            }
            continue
        if technology_match:
            finish_block()
            current_technology = technology_match.group(1).strip()
            continue
        if difficulty_match:
            finish_block()
            candidate_difficulty = difficulty_match.group(1).strip().capitalize()
            current_difficulty_valid = candidate_difficulty in {"Easy", "Medium", "Hard"}
            current_difficulty = candidate_difficulty if current_difficulty_valid else "Medium"
            continue
        if block is None or not line:
            continue
        option_match = re.match(r"^([A-D])\s*[.)]\s*(.*)$", line, re.I)
        if option_match:
            label = option_match.group(1).upper()
            if label in block["options"]:
                block["duplicate_options"].add(label)
            else:
                block["options"][label] = option_match.group(2).strip()
            block["current_option"] = label
            continue
        answer_match = re.match(r"^Answer\s*:\s*([A-D])\s*$", line, re.I)
        if answer_match:
            if block["answer"]:
                block["duplicate_answer"] = True
            block["answer"] = answer_match.group(1).upper()
            block["current_option"] = None
            continue
        if re.match(r"^Answer\s*:", line, re.I):
            if block["answer"]:
                block["duplicate_answer"] = True
            block["answer"] = ""
            block["current_option"] = None
            continue
        if block["current_option"]:
            label = block["current_option"]
            block["options"][label] = f"{block['options'].get(label, '')} {line}".strip()
        elif not re.match(r"^[A-D]\s*[.)]", line, re.I):
            block["question_lines"].append(line)
    finish_block()
    return questions


def extract_question_bank_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail="This PDF is unreadable or corrupted. Please check the file and try again.",
        ) from error
    if not reader.pages:
        raise HTTPException(status_code=422, detail="This PDF does not contain any pages.")
    if len(reader.pages) > MAX_QUESTION_BANK_PDF_PAGES:
        raise HTTPException(
            status_code=422,
            detail=f"This PDF has too many pages. Upload a document with {MAX_QUESTION_BANK_PDF_PAGES} pages or fewer.",
        )
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if not text:
        try:
            text = extract_resume_text(content, "application/pdf")
        except HTTPException as error:
            raise HTTPException(status_code=422, detail=error.detail) from error
        except Exception as error:
            logger.exception("OCR failed while reading a question-bank PDF")
            raise HTTPException(
                status_code=422,
                detail="No readable text was found in this PDF. Install Tesseract OCR or upload a text-based PDF.",
            ) from error
    if not text:
        raise HTTPException(
            status_code=422,
            detail="No readable text was found in this PDF. Install Tesseract OCR or upload a text-based PDF.",
        )
    return text


def call_llm_json(prompt: str, system: str = "") -> dict[str, Any] | None:
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        for model in ["gemini-2.0-flash", "gemini-1.5-flash"]:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
                payload = {
                    "contents": [
                        {"role": "user", "parts": [{"text": f"{system}\n\n{prompt}"}]}
                    ],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "temperature": 0.3,
                    }
                }
                with httpx.Client(timeout=15.0) as client:
                    res = client.post(url, json=payload)
                    if res.status_code == 200:
                        data = res.json()
                        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
                        raw_text = parts[0].get("text", "")
                        if raw_text:
                            return json.loads(raw_text)
            except Exception as e:
                logger.warning("Gemini %s call failed: %s", model, e)

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.3
            }
            with httpx.Client(timeout=15.0) as client:
                res = client.post(url, headers=headers, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    raw_text = data["choices"][0]["message"]["content"]
                    return json.loads(raw_text)
        except Exception as e:
            logger.warning("OpenAI API call failed, falling back to local engine: %s", e)

    return None


def generate_mock_interview_question(
    job_role: str,
    interview_type: str,
    difficulty: str,
    question_index: int,
    total_questions: int,
    resume_text: str = "",
    previous_exchanges: list[dict[str, Any]] | None = None,
) -> str:
    """
    Deterministic, rule-based mock interview question generator.
    Selects role-tailored, difficulty-calibrated questions without external LLM dependencies.
    """
    idx = max(0, question_index - 1)

    if interview_type == "HR":
        hr_questions = [
            "Tell me about yourself, your background, and what motivates you to pursue this role.",
            "Can you describe a challenging technical conflict or setback in a previous team project, and how you worked through it?",
            "What do you consider your greatest technical strength, and what is one technical area you are actively working to improve?",
            "How do you prioritize multiple competing deliverables when working under tight project deadlines?",
            "Where do you see yourself professionally over the next 2 to 3 years, and what skills or domains are you focusing on?",
            "Give an example of a mistake or bug you introduced in a project. How did you identify it, resolve it, and what did you learn?",
            "Why are you interested in this role and what unique contributions do you expect to bring to our engineering team?",
        ]
        return hr_questions[idx % len(hr_questions)]

    if interview_type == "Resume-Based" and resume_text and len(resume_text.strip()) > 30:
        resume_questions = [
            "Based on the projects and experience in your resume, walk me through the architecture and design decisions of your most significant project.",
            "Looking at your resume's technical background, what was the most difficult technical challenge you encountered and how did you overcome it?",
            "How did you approach testing, error handling, and performance optimization in the software solutions mentioned on your resume?",
            "If you had the opportunity to rebuild one of the core systems described in your resume today, what technologies or architectural patterns would you change and why?",
            "Can you explain the collaboration, version control, and deployment workflows you followed during the projects listed on your resume?",
        ]
        return resume_questions[idx % len(resume_questions)]

    role_lower = (job_role or "").lower()
    if "python" in role_lower:
        tech_questions = [
            "Explain Python's memory management, reference counting, and how the garbage collector handles cyclic references.",
            "What is the Global Interpreter Lock (GIL) in CPython, and how does it impact multi-threaded CPU-bound versus I/O-bound tasks?",
            "How do Python generators work under the hood, and what are the performance advantages of using `yield` and `yield from`?",
            "Explain how decorators and closures work in Python. How would you write a decorator to measure function execution time?",
            "Compare `asyncio` coroutines with multi-threading and multi-processing in Python. When would you choose each?",
        ]
    elif "react" in role_lower or "frontend" in role_lower or "javascript" in role_lower:
        tech_questions = [
            "Explain the Virtual DOM and React reconciliation algorithm (Fiber). How does React optimize rendering updates?",
            "What is the difference between `useEffect`, `useLayoutEffect`, and `useMemo` in React, and when should each be used?",
            "How does JavaScript's event loop handle the call stack, microtasks (Promises), and macrotasks (`setTimeout`)?",
            "How would you optimize the performance of a React web application suffering from unnecessary re-renders and slow initial load?",
            "What state management strategy would you choose for a large-scale frontend app, and why?",
        ]
    elif "data" in role_lower or "sql" in role_lower or "database" in role_lower:
        tech_questions = [
            "Explain the ACID properties of relational databases and compare different transaction isolation levels.",
            "How do B-Tree and Hash indexes work in relational databases, and what are best practices for indexing composite keys?",
            "Explain database normalization from 1NF through BCNF. In what scenarios is denormalization intentionally justified?",
            "How would you diagnose and optimize a slow SQL query executing across tables with millions of rows?",
            "Compare relational SQL databases with NoSQL databases (Document, Key-Value) in terms of scalability and CAP theorem trade-offs.",
        ]
    else:
        tech_questions = [
            f"How would you explain the core architecture, key components, and design patterns when designing a scalable {job_role} application?",
            f"What specific methodology do you use to diagnose, trace, and resolve critical bugs or performance bottlenecks in {job_role} projects?",
            f"Can you explain your approach to writing unit, integration, and end-to-end tests for a {job_role} codebase?",
            f"How do you design RESTful or asynchronous APIs to be backward compatible, secure, and resilient under high traffic in {job_role}?",
            f"Describe a complex architectural trade-off you had to evaluate in {job_role} and why you chose your specific approach.",
        ]

    return tech_questions[idx % len(tech_questions)]


def evaluate_mock_interview_answer(
    job_role: str,
    interview_type: str,
    difficulty: str,
    question: str,
    answer: str,
) -> dict[str, Any]:
    prompt = f"""Evaluate this candidate's interview answer.
Role: {job_role}
Type: {interview_type}
Difficulty: {difficulty}
Question: {question}
Candidate's Answer: {answer}

Provide constructive feedback and return ONLY a valid JSON object matching this schema:
{{
    "score": <integer from 1 to 10>,
    "feedback": "<2-3 sentence constructive feedback on clarity, technical accuracy, and completeness>",
    "strengths": ["<strength 1>", "<strength 2>"],
    "improvements": ["<improvement tip 1>", "<improvement tip 2>"]
}}"""

    system = "You are an insightful, encouraging interview evaluator. Return only valid JSON."
    res = call_llm_json(prompt, system=system)
    if isinstance(res, dict) and "feedback" in res:
        score = res.get("score")
        if not isinstance(score, (int, float)) or score < 1 or score > 10:
            score = 7
        return {
            "score": int(score),
            "feedback": str(res.get("feedback", "")),
            "strengths": [str(s) for s in res.get("strengths", []) if s][:3],
            "improvements": [str(i) for i in res.get("improvements", []) if i][:3],
        }

    return {
        "score": 7,
        "feedback": "Good attempt. Your answer addressed the question, but could be enhanced by providing concrete metrics, specific examples, or structuring your response using the STAR method.",
        "strengths": ["Clear communication", "Addressed the core question"],
        "improvements": ["Include more concrete technical details or quantifiable results"],
    }


def generate_mock_interview_final_report(
    job_role: str,
    interview_type: str,
    difficulty: str,
    exchanges: list[dict[str, Any]],
) -> dict[str, Any]:
    transcript_lines = []
    total_score = 0
    valid_scores = 0
    for e in exchanges:
        q = e.get("question", "")
        a = e.get("answer", "")
        fb_obj = e.get("feedback_obj", {})
        fb = fb_obj.get("feedback", "") if isinstance(fb_obj, dict) else str(e.get("feedback", ""))
        sc = e.get("score", 7)
        if isinstance(sc, (int, float)):
            total_score += sc
            valid_scores += 1
        transcript_lines.append(f"Q{e.get('question_index', 1)}: {q}\nCandidate Answer: {a}\nFeedback: {fb}\nScore: {sc}/10\n")

    transcript_text = "\n".join(transcript_lines)
    avg_score_10 = (total_score / valid_scores) if valid_scores else 7.0
    calc_overall_100 = int(round(avg_score_10 * 10))

    prompt = f"""Review this completed mock interview transcript and generate an extensive final performance report.
Role: {job_role}
Type: {interview_type}
Difficulty: {difficulty}

Transcript:
{transcript_text}

Respond ONLY with a valid JSON object matching this exact structure:
{{
    "overall_score": <integer 0-100>,
    "performance_level": "<one of: 'Excellent', 'Proficient', 'Developing', 'Needs Preparation'>",
    "technical_evaluation": "<detailed evaluation of technical accuracy and depth>",
    "communication_evaluation": "<detailed evaluation of answer structure, clarity, and articulation>",
    "strengths": ["<specific strength 1>", "<specific strength 2>", "<specific strength 3>"],
    "areas_for_improvement": ["<actionable improvement 1>", "<actionable improvement 2>", "<actionable improvement 3>"],
    "recommended_study_topics": ["<topic 1>", "<topic 2>", "<topic 3>", "<topic 4>"],
    "suggested_practice_questions": ["<practice question 1>", "<practice question 2>", "<practice question 3>"],
    "summary": "<2-3 sentence concluding encouraging summary>"
}}"""

    system = "You are an expert career counselor and interview coach. Return only valid JSON."
    res = call_llm_json(prompt, system=system)
    disclaimer = "Mock interview scores are practice feedback and do not affect actual job applications, recruiter decisions or technical assessment results."

    if isinstance(res, dict) and "overall_score" in res:
        overall = res.get("overall_score")
        if not isinstance(overall, (int, float)) or overall < 0 or overall > 100:
            overall = calc_overall_100
        level = str(res.get("performance_level") or "Proficient")
        return {
            "overall_score": int(overall),
            "performance_level": level,
            "questions_answered": len(exchanges),
            "total_questions": len(exchanges),
            "technical_evaluation": str(res.get("technical_evaluation") or "Demonstrated solid foundational understanding."),
            "communication_evaluation": str(res.get("communication_evaluation") or "Answers were clear and logically sequenced."),
            "strengths": [str(s) for s in res.get("strengths", []) if s][:5],
            "areas_for_improvement": [str(i) for i in res.get("areas_for_improvement", []) if i][:5],
            "recommended_study_topics": [str(t) for t in res.get("recommended_study_topics", []) if t][:6],
            "suggested_practice_questions": [str(q) for q in res.get("suggested_practice_questions", []) if q][:5],
            "summary": str(res.get("summary") or "Overall a strong practice session with clear potential."),
            "disclaimer": disclaimer,
        }

    perf_level = "Excellent" if calc_overall_100 >= 85 else "Proficient" if calc_overall_100 >= 70 else "Developing" if calc_overall_100 >= 55 else "Needs Preparation"
    return {
        "overall_score": calc_overall_100,
        "performance_level": perf_level,
        "questions_answered": len(exchanges),
        "total_questions": len(exchanges),
        "technical_evaluation": f"Demonstrated good domain knowledge suitable for {difficulty} level in {job_role}.",
        "communication_evaluation": "Responses were direct and communicated key ideas well.",
        "strengths": [
            "Good understanding of fundamental concepts",
            "Clear and professional tone",
            "Willingness to tackle multi-part questions",
        ],
        "areas_for_improvement": [
            "Deepen familiarity with edge cases and scalability considerations",
            "Incorporate more quantifiable metrics when discussing previous project impact",
        ],
        "recommended_study_topics": [
            f"{job_role} Best Practices & Architecture",
            "Data Structures & System Efficiency",
            "STAR Interview Framing Method",
        ],
        "suggested_practice_questions": [
            f"How do you design an end-to-end resilient workflow in {job_role}?",
            "Can you walk through how you would optimize database queries under high concurrency?",
        ],
        "summary": f"Great job completing your {job_role} mock interview! Continued practice with targeted project examples will make your real interviews even stronger.",
        "disclaimer": disclaimer,
    }


LOCAL_TECH_QUESTION_BANKS: dict[str, list[dict[str, Any]]] = {
    "Python": [
        {
            "subtopic": "List Comprehensions & Lambdas",
            "difficulty": "Easy",
            "question": "What is the output of [x**2 for x in range(5) if x % 2 == 1] in Python 3?",
            "options": ["[1, 9]", "[0, 4, 16]", "[1, 3, 5]", "[1, 4, 9, 16]"],
            "correct_index": 0,
            "explanation": "range(5) evaluates to 0, 1, 2, 3, 4. The odd numbers are 1 and 3. Squaring them gives 1 and 9, producing [1, 9].",
            "learning_resource_url": "https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions"
        },
        {
            "subtopic": "Memory & Mutability",
            "difficulty": "Medium",
            "question": "Why is using a mutable default argument like `def add_item(item, target=[])` discouraged in Python?",
            "options": [
                "The default list is created once at function definition and shared across all calls.",
                "Python throws a TypeError when compiling mutable default values.",
                "Mutable default parameters are automatically converted into tuples at runtime.",
                "It causes a recursion depth limit error upon successive invocations."
            ],
            "correct_index": 0,
            "explanation": "In Python, default parameter expressions are evaluated once when the function is defined, so subsequent calls without arguments mutate the same list object.",
            "learning_resource_url": "https://docs.python.org/3/reference/compound_stmts.html#function-definitions"
        },
        {
            "subtopic": "Decorators & Closures",
            "difficulty": "Medium",
            "question": "Which standard library decorator preserves the original function's docstring and name when creating a custom decorator?",
            "options": ["functools.wraps", "functools.lru_cache", "inspect.signature", "contextlib.contextmanager"],
            "correct_index": 0,
            "explanation": "@functools.wraps copies the __name__, __doc__, and other attributes from the decorated function onto the inner wrapper.",
            "learning_resource_url": "https://docs.python.org/3/library/functools.html#functools.wraps"
        },
        {
            "subtopic": "Generators & Iteration",
            "difficulty": "Hard",
            "question": "What is the primary architectural difference between `yield` and `yield from` in a Python generator?",
            "options": [
                "`yield from` delegates iteration to a subgenerator and bi-directionally forwards values, exceptions, and returns.",
                "`yield from` converts any generator into an async coroutine automatically.",
                "`yield from` executes the generator synchronously on a background thread pool.",
                "`yield from` is deprecated in Python 3.8+ in favor of async generator loops."
            ],
            "correct_index": 0,
            "explanation": "`yield from <subgen>` transparently connects the caller and subgenerator, passing .send() inputs and .throw() exceptions directly.",
            "learning_resource_url": "https://peps.python.org/pep-0380/"
        },
        {
            "subtopic": "GIL & Concurrency",
            "difficulty": "Hard",
            "question": "In standard CPython, how does the Global Interpreter Lock (GIL) affect multi-threaded CPU-bound programs?",
            "options": [
                "It allows only one thread to execute Python bytecode at a time, preventing true multi-core CPU speedup.",
                "It automatically distributes threads across all available CPU cores using kernel interrupts.",
                "It terminates the process if more than four concurrent threads are spawned.",
                "It isolates thread memory heaps by duplicating the global namespace."
            ],
            "correct_index": 0,
            "explanation": "CPython's GIL ensures thread safety for memory management, meaning CPU-bound pure Python threads run cooperatively on a single core.",
            "learning_resource_url": "https://docs.python.org/3/glossary.html#term-global-interpreter-lock"
        }
    ],
    "SQL": [
        {
            "subtopic": "Aggregation & Filtering",
            "difficulty": "Easy",
            "question": "Which SQL clause is used to filter query results after an aggregate GROUP BY operation?",
            "options": ["HAVING", "WHERE", "ORDER BY", "QUALIFY"],
            "correct_index": 0,
            "explanation": "WHERE filters raw rows before aggregation, whereas HAVING filters grouped rows after aggregation.",
            "learning_resource_url": "https://en.wikipedia.org/wiki/Having_(SQL)"
        },
        {
            "subtopic": "Window Functions",
            "difficulty": "Medium",
            "question": "What is the distinction between `RANK()` and `DENSE_RANK()` window functions when duplicate values occur?",
            "options": [
                "RANK() leaves gaps in the rank numbering after ties, whereas DENSE_RANK() never skips ranking numbers.",
                "DENSE_RANK() skips numbers after ties, while RANK() maintains contiguous sequences.",
                "RANK() operates only on numeric columns, while DENSE_RANK() operates on text.",
                "DENSE_RANK() resets the rank back to 1 on every partition without ORDER BY."
            ],
            "correct_index": 0,
            "explanation": "If two rows tie for 1st place, RANK() assigns 1, 1, 3, while DENSE_RANK() assigns 1, 1, 2 without gaps.",
            "learning_resource_url": "https://www.postgresql.org/docs/current/functions-window.html"
        },
        {
            "subtopic": "Indexing & Execution Plans",
            "difficulty": "Medium",
            "question": "What is a 'covering index' in relational databases (RDBMS)?",
            "options": [
                "An index containing all columns referenced in the query, allowing index-only scan without table lookups.",
                "An index that automatically encrypts table data on physical storage disks.",
                "A clustered index applied to all foreign keys simultaneously.",
                "An index that automatically partitions tables across database shards."
            ],
            "correct_index": 0,
            "explanation": "A covering index contains all required columns (keys and included columns), so the database engine satisfies the query entirely from the B-tree index.",
            "learning_resource_url": "https://use-the-index-luke.com/"
        },
        {
            "subtopic": "Transactions & ACID",
            "difficulty": "Hard",
            "question": "Which transaction isolation level prevents dirty reads, non-repeatable reads, and phantom reads in ANSI SQL?",
            "options": ["SERIALIZABLE", "REPEATABLE READ", "READ COMMITTED", "READ UNCOMMITTED"],
            "correct_index": 0,
            "explanation": "SERIALIZABLE is the highest isolation level and prevents dirty reads, non-repeatable reads, and phantom reads by simulating completely sequential execution.",
            "learning_resource_url": "https://en.wikipedia.org/wiki/Isolation_(database_systems)"
        },
        {
            "subtopic": "CTE & Recursive Queries",
            "difficulty": "Hard",
            "question": "In a Common Table Expression (WITH RECURSIVE), what two query components are separated by the UNION ALL operator?",
            "options": [
                "The base anchor member query and the recursive member query.",
                "The primary key constraint and the foreign key index.",
                "The aggregation subquery and the partition filter clause.",
                "The transaction lock statement and the commit rollback hook."
            ],
            "correct_index": 0,
            "explanation": "A recursive CTE consists of an anchor member (which establishes the initial result set) and a recursive member joined via UNION or UNION ALL.",
            "learning_resource_url": "https://www.postgresql.org/docs/current/queries-with.html"
        }
    ],
    "JavaScript": [
        {
            "subtopic": "Event Loop & Concurrency",
            "difficulty": "Medium",
            "question": "In what order does JavaScript execute microtasks (e.g. Promise callbacks) vs macrotasks (e.g. setTimeout)?",
            "options": [
                "All queued microtasks run immediately after current synchronous code, before the next macrotask.",
                "Macrotasks execute first, followed by microtasks only when the browser window blurs.",
                "Microtasks and macrotasks are interleaved alternately in a round-robin schedule.",
                "Microtasks run only in Web Worker threads and do not block the main event loop."
            ],
            "correct_index": 0,
            "explanation": "The JavaScript event loop drains the entire microtask queue (Promise.then, queueMicrotask) before executing the next macrotask (setTimeout, setInterval).",
            "learning_resource_url": "https://developer.mozilla.org/en-US/docs/Web/API/HTML_DOM_API/Microtask_guide"
        },
        {
            "subtopic": "Scopes & Closures",
            "difficulty": "Easy",
            "question": "What is the scoping difference between `var` and `let` in JavaScript?",
            "options": [
                "`var` is function-scoped and hoisted with undefined; `let` is block-scoped with a temporal dead zone.",
                "`let` variables are shared globally across window objects, while `var` is strictly local.",
                "`var` creates immutable bindings, while `let` allows variable reassignment.",
                "There is no difference in modern ES6+ ECMAScript engines."
            ],
            "correct_index": 0,
            "explanation": "`var` attaches to the nearest function scope, while `let` adheres strictly to curly-bracket blocks `{}` and cannot be accessed before declaration.",
            "learning_resource_url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Statements/let"
        },
        {
            "subtopic": "Prototypes & Inheritance",
            "difficulty": "Hard",
            "question": "What does `Object.create(proto)` do under the hood in JavaScript?",
            "options": [
                "Creates a new object whose internal prototype ([[Prototype]]) is explicitly set to `proto`.",
                "Clones all enumerable properties of `proto` using structured deep-copy cloning.",
                "Freezes the `proto` object and converts its methods into static class attributes.",
                "Instantiates a singleton proxy that intercepts getter and setter operations."
            ],
            "correct_index": 0,
            "explanation": "Object.create creates a new empty object that inherits directly from the passed prototype object in the prototype chain.",
            "learning_resource_url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Object/create"
        }
    ],
    "React": [
        {
            "subtopic": "Hooks & State",
            "difficulty": "Easy",
            "question": "Why must React Hooks (like `useState` and `useEffect`) always be called at the top level of a component?",
            "options": [
                "React relies on the call order of hooks across renders to preserve state across invocations.",
                "Calling hooks in conditionals triggers a memory leak in the browser DOM engine.",
                "Hooks compile to global variables that collide when called inside loop blocks.",
                "Virtual DOM nodes cannot re-render if hooks are nested inside callbacks."
            ],
            "correct_index": 0,
            "explanation": "React stores hook states in an internal array/linked list mapped to the component instance; consistent execution order is required to map state slots accurately.",
            "learning_resource_url": "https://react.dev/warnings/invalid-hook-call-warning"
        },
        {
            "subtopic": "Performance & Memoization",
            "difficulty": "Medium",
            "question": "When should you use `useCallback` instead of an inline function in React?",
            "options": [
                "When passing the callback to a memoized child component (`React.memo`) to avoid unnecessary re-renders.",
                "To automatically run the function asynchronously on a web worker thread.",
                "Whenever a function performs an HTTP network request via fetch.",
                "To prevent the component from mounting multiple times during initial page load."
            ],
            "correct_index": 0,
            "explanation": "useCallback caches a function definition between renders, which prevents breaking shallow reference checks on memoized child components.",
            "learning_resource_url": "https://react.dev/reference/react/useCallback"
        }
    ]
}


def generate_questions_local(
    technology: str,
    count: int = 5,
    difficulty: str = "Mixed",
    subtopic: str = "",
    custom_prompt: str = "",
    exclude_questions: set[str] | None = None,
) -> list[dict[str, Any]]:
    seen_q = set(exclude_questions or set())
    matched_key = next((k for k in LOCAL_TECH_QUESTION_BANKS if k.lower() == technology.lower()), None)
    bank = list(LOCAL_TECH_QUESTION_BANKS.get(matched_key, [])) if matched_key else []

    results: list[dict[str, Any]] = []

    if bank:
        if difficulty in {"Easy", "Medium", "Hard"}:
            bank_diff = [q for q in bank if q["difficulty"] == difficulty]
            if bank_diff:
                bank = bank_diff
        random.shuffle(bank)
        for item in bank:
            if len(results) >= count:
                break
            q_key = item["question"].strip().casefold()
            if q_key in seen_q:
                continue
            seen_q.add(q_key)
            results.append({
                "technology": technology,
                "subtopic": item.get("subtopic") or (subtopic or f"{technology} Core"),
                "difficulty": item.get("difficulty") or (difficulty if difficulty != "Mixed" else "Medium"),
                "question": item["question"],
                "options": list(item["options"]),
                "correct_index": item["correct_index"],
                "explanation": item["explanation"],
                "learning_resource_url": item.get("learning_resource_url", ""),
                "active": True,
            })

    procedural_topics = [
        ("Architecture & Design", "Medium", "Which design principle is considered best practice when structuring modular components in {tech}?"),
        ("Error Handling & Diagnostics", "Easy", "What is the recommended approach for intercepting and handling unexpected runtime exceptions in {tech}?"),
        ("Performance & Scaling", "Hard", "Which strategy provides the most effective throughput optimization when processing high concurrency in {tech}?"),
        ("Security & Validation", "Medium", "What primary security mechanism protects against injection and untrusted input vulnerabilities in {tech}?"),
        ("Testing & Quality Assurance", "Easy", "When implementing unit and integration tests in {tech}, what is the primary benefit of test isolation?"),
        ("Memory & Resource Management", "Hard", "How does the runtime engine of {tech} handle resource cleanup and prevent memory leaks over time?"),
        ("Data Transformation & Flow", "Medium", "Which pattern ensures predictable data mutations and immutable state transitions in {tech}?"),
        ("Dependency Management & Isolation", "Easy", "Why is sandboxed dependency isolation recommended when configuring deployment pipelines for {tech}?"),
        ("API Contracts & Versioning", "Medium", "How should backward-compatible interface changes be introduced in production {tech} services?"),
        ("Observability & Logging", "Easy", "Which structured logging practice makes diagnosing production issues in {tech} fastest and safest?"),
        ("Caching & Latency", "Hard", "What cache invalidation strategy avoids stale reads while keeping {tech} response latency predictable?"),
        ("Concurrency & Async Execution", "Hard", "Which concurrency pattern prevents race conditions and deadlocks in multi-worker {tech} workloads?"),
        ("Schema & Input Validation", "Easy", "Why should strict schema validation be enforced at service boundaries in {tech}?"),
        ("Configuration & Secrets", "Medium", "What is the safest way to manage environment-specific credentials in {tech} deployments?"),
        ("Automated Regression Testing", "Medium", "Which testing practice best catches integration regressions before deploying {tech} updates?"),
        ("Rate Limiting & Backpressure", "Hard", "How does applying backpressure and rate limiting protect downstream {tech} dependencies during traffic spikes?"),
        ("Code Maintainability", "Easy", "Which refactoring practice keeps {tech} modules readable and straightforward to test?"),
        ("Fault Tolerance & Retries", "Hard", "When retrying transient failures in {tech}, why is exponential backoff with jitter recommended?"),
        ("Data Consistency", "Medium", "How do atomic transactions prevent partial state corruption in {tech} workflows?"),
        ("Deployment & Rollbacks", "Easy", "What deployment check ensures a new {tech} release can be rolled back safely without data loss?"),
    ]
    scenario_contexts = [
        "in a high-throughput production service",
        "during a live code review",
        "for a distributed microservice architecture",
        "under strict latency and reliability SLAs",
        "when onboarding a multi-team codebase",
    ]

    attempt_idx = 0
    max_local_attempts = max(count * 4, 60)
    while len(results) < count and attempt_idx < max_local_attempts:
        topic_name, diff, q_tmpl = procedural_topics[attempt_idx % len(procedural_topics)]
        cycle = attempt_idx // len(procedural_topics)
        attempt_idx += 1
        final_diff = difficulty if difficulty in {"Easy", "Medium", "Hard"} else diff
        target_subtopic = subtopic or f"{technology} {topic_name}"

        q_text = q_tmpl.format(tech=technology)
        if cycle > 0:
            ctx = scenario_contexts[(cycle - 1) % len(scenario_contexts)]
            q_text = f"{q_text.rstrip('?')} ({ctx})?"
        if custom_prompt:
            q_text += f" [Context: {custom_prompt[:50]}]"

        q_key = q_text.strip().casefold()
        if q_key in seen_q:
            continue
        seen_q.add(q_key)

        correct_ans = f"Applying modular boundaries, explicit validation, and standard {technology} {topic_name.lower()} practices."
        opt_b = "Bypassing validation layers to maximize raw I/O throughput in high-load clusters."
        opt_c = "Storing mutable state globally across all shared threads without locking."
        opt_d = "Disabling strict type and boundary checking to suppress runtime warnings."

        options = [correct_ans, opt_b, opt_c, opt_d]
        random.shuffle(options)
        c_idx = options.index(correct_ans)

        results.append({
            "technology": technology,
            "subtopic": target_subtopic,
            "difficulty": final_diff,
            "question": q_text,
            "options": options,
            "correct_index": c_idx,
            "explanation": f"In {technology}, robust implementations prioritize loose coupling, standardized lifecycle management, and verified boundary validation.",
            "learning_resource_url": "",
            "active": True,
        })

    return results[:count]


def generate_ai_questions(
    technology: str,
    count: int = 5,
    difficulty: str = "Mixed",
    subtopic: str = "",
    custom_prompt: str = "",
    existing_questions: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    sys_prompt = (
        "You are an expert technical interviewer and assessment designer. "
        "Generate realistic, challenging multiple-choice technical questions for candidate assessments. "
        "Respond ONLY with a JSON object in this exact schema:\n"
        "{\n"
        "  \"questions\": [\n"
        "    {\n"
        "      \"technology\": \"string\",\n"
        "      \"subtopic\": \"string\",\n"
        "      \"difficulty\": \"Easy\" | \"Medium\" | \"Hard\",\n"
        "      \"question\": \"string (at least 15 characters, clear and unambiguous)\",\n"
        "      \"options\": [\"opt 1\", \"opt 2\", \"opt 3\", \"opt 4\"],\n"
        "      \"correct_index\": 0,\n"
        "      \"explanation\": \"string (concise technical reason why this option is correct)\",\n"
        "      \"learning_resource_url\": \"string\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "Ensure each question has exactly 4 distinct non-empty options with only ONE objectively correct answer."
    )

    seen_keys: set[str] = set(existing_questions or set())
    collected: list[dict[str, Any]] = []
    raw_generated = 0
    rejected_or_dup = 0
    retry_count = 0
    max_retries = 3

    for attempt in range(max_retries):
        needed = count - len(collected)
        if needed <= 0:
            break
        if attempt > 0:
            retry_count += 1
        user_prompt = f"Generate {needed} unique {difficulty} questions for technology '{technology}'."
        if subtopic:
            user_prompt += f" Focus on subtopic: '{subtopic}'."
        if custom_prompt:
            user_prompt += f" Specific instructions: '{custom_prompt}'."
        if seen_keys:
            user_prompt += f" Do not repeat any of the {len(seen_keys)} previously generated questions."

        try:
            llm_res = call_llm_json(user_prompt, sys_prompt)
        except Exception as exc:
            logger.warning("AI question generation attempt %d failed: %s", attempt + 1, exc)
            llm_res = None

        if not (llm_res and isinstance(llm_res, dict) and isinstance(llm_res.get("questions"), list)):
            continue

        for q in llm_res["questions"]:
            raw_generated += 1
            if not isinstance(q, dict):
                rejected_or_dup += 1
                continue
            candidate_item = {
                "technology": str(q.get("technology") or technology).strip() or technology,
                "subtopic": str(q.get("subtopic") or subtopic).strip(),
                "difficulty": str(q.get("difficulty") or "Medium").strip().capitalize(),
                "question": str(q.get("question") or "").strip(),
                "options": q.get("options") if isinstance(q.get("options"), list) else [],
                "correct_index": q.get("correct_index", 0),
                "explanation": str(q.get("explanation") or "").strip(),
                "learning_resource_url": str(q.get("learning_resource_url") or "").strip(),
                "active": True,
            }
            validated, _ = validate_question_data(candidate_item)
            if not validated:
                rejected_or_dup += 1
                continue
            q_key = validated["question"].strip().casefold()
            if q_key in seen_keys:
                rejected_or_dup += 1
                continue
            seen_keys.add(q_key)
            collected.append(validated)
            if len(collected) >= count:
                break

    if len(collected) < count:
        needed = count - len(collected)
        fallback = generate_questions_local(
            technology,
            needed,
            difficulty,
            subtopic,
            custom_prompt,
            exclude_questions=seen_keys,
        )
        for item in fallback:
            validated, _ = validate_question_data(item)
            if not validated:
                continue
            q_key = validated["question"].strip().casefold()
            if q_key in seen_keys:
                continue
            seen_keys.add(q_key)
            collected.append(validated)
            raw_generated += 1
            if len(collected) >= count:
                break

    stats = {
        "requested_count": count,
        "generated_count": raw_generated,
        "rejected_or_duplicate_count": rejected_or_dup,
        "retry_count": retry_count,
        "final_count": len(collected),
    }
    logger.info(
        "AI question generation complete: tech=%s requested=%d generated=%d rejected_or_dup=%d retries=%d final=%d",
        technology, count, raw_generated, rejected_or_dup, retry_count, len(collected),
    )
    return collected[:count], stats


def synthesize_candidate_ai_summary(
    db: sqlite3.Connection,
    application_id: int,
    force_refresh: bool = False
) -> dict[str, Any]:
    # Gather application profile
    app_row = db.execute(
        """SELECT a.id, a.candidate_id, a.job_id, a.status, a.match_percentage,
        a.matched_skills_json, a.missing_skills_json, a.applied_at,
        u.name AS candidate_name, u.email AS candidate_email,
        j.title AS job_title, j.company AS job_company, j.skills_json AS job_skills_json,
        j.experience AS job_experience,
        r.extracted_text AS resume_text
        FROM applications a
        JOIN users u ON u.id = a.candidate_id
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN resumes r ON r.candidate_id = a.candidate_id
        WHERE a.id = ?""",
        (application_id,)
    ).fetchone()
    if not app_row:
        raise HTTPException(status_code=404, detail="Application not found")

    matched_skills = json.loads(app_row["matched_skills_json"] or "[]")
    missing_skills = json.loads(app_row["missing_skills_json"] or "[]")
    job_skills = json.loads(app_row["job_skills_json"] or "[]")
    match_pct = app_row["match_percentage"] or 0

    # Gather assessment data if available
    assess_row = db.execute(
        """SELECT x.id, x.status, x.technology, x.total_questions, x.pass_percentage,
        x.score_percentage, x.correct_answer_count, x.incorrect_answer_count,
        x.result, x.time_taken_seconds, x.proctoring_warning_count, x.proctoring_flagged_at
        FROM assessments x WHERE x.application_id = ?""",
        (application_id,)
    ).fetchone()

    test_taken = assess_row and assess_row["status"] in {"Completed", "Pending Recruiter Decision"}
    test_score = assess_row["score_percentage"] if test_taken else None
    pass_pct = assess_row["pass_percentage"] if assess_row else 75
    test_result = assess_row["result"] if test_taken else None
    proctor_warnings = assess_row["proctoring_warning_count"] if assess_row else 0
    proctor_flagged = bool(assess_row and assess_row["proctoring_flagged_at"])

    if not force_refresh:
        cached = db.execute(
            """SELECT id, application_id, recommendation, summary, strengths_json,
            risks_json, interview_questions_json, created_at, updated_at
            FROM candidate_ai_summaries WHERE application_id = ?""",
            (application_id,)
        ).fetchone()
        if cached:
            raw_questions = json.loads(cached["interview_questions_json"] or "[]")
            norm_q = []
            for item in raw_questions:
                if isinstance(item, dict) and "question" in item:
                    norm_q.append(item)
                else:
                    norm_q.append({
                        "question": str(item),
                        "focus_area": "Technical Competency",
                        "what_to_look_for": "Problem-solving methodology, practical depth, and clear communication."
                    })
            return {
                "id": cached["id"],
                "application_id": cached["application_id"],
                "candidate_name": app_row["candidate_name"],
                "candidate_email": app_row["candidate_email"],
                "job_title": app_row["job_title"],
                "company": app_row["job_company"],
                "resume_match_percentage": match_pct,
                "assessment_score_percentage": test_score,
                "assessment_result": test_result,
                "assessment_status": assess_row["status"] if assess_row else (app_row["status"] or "Not Issued"),
                "proctoring_flagged": proctor_flagged,
                "proctoring_warning_count": proctor_warnings,
                "recommendation": cached["recommendation"],
                "summary": cached["summary"],
                "strengths": json.loads(cached["strengths_json"] or "[]"),
                "risks": json.loads(cached["risks_json"] or "[]"),
                "interview_questions": norm_q,
                "created_at": cached["created_at"],
                "updated_at": cached["updated_at"],
            }

    # Try calling LLM for rich executive summary
    sys_prompt = (
        "You are an expert talent acquisition director and technical screener. "
        "Review the candidate's application details, resume alignment, technical assessment scores, and proctoring integrity. "
        "Return a strictly valid JSON response with the following schema:\n"
        "{\n"
        "  \"recommendation\": \"Strong Hire\" | \"Recommended\" | \"Borderline\" | \"Not Recommended\",\n"
        "  \"summary\": \"A concise, balanced 2-3 sentence executive screening summary covering qualifications, test outcome, and candidate standing.\",\n"
        "  \"strengths\": [\"string\", \"string\", \"string\"],\n"
        "  \"risks\": [\"string\", \"string\"],\n"
        "  \"interview_questions\": [\"string (targeted technical question for Round 2)\", \"string\", \"string\"]\n"
        "}"
    )
    user_prompt = f"""
Candidate: {app_row['candidate_name']} ({app_row['candidate_email']})
Job Opening: {app_row['job_title']} at {app_row['job_company']}
Required Experience: {app_row['job_experience']}
Required Skills: {', '.join(job_skills)}
Resume Match: {match_pct}% (Matched: {', '.join(matched_skills) or 'None'}; Missing: {', '.join(missing_skills) or 'None'})
Assessment Status: {assess_row['status'] if assess_row else 'Not Issued'}
Assessment Outcome: {f"Score: {test_score}% ({test_result}); Pass Required: {pass_pct}%" if test_taken else "Assessment not yet completed"}
Proctoring Signals: {proctor_warnings} warnings logged; Flagged: {'Yes' if proctor_flagged else 'No'}
"""
    llm_summary = call_llm_json(user_prompt, sys_prompt)

    recommendation = "Recommended"
    summary_text = ""
    strengths: list[str] = []
    risks: list[str] = []
    interview_questions: list[str] = []

    if llm_summary and isinstance(llm_summary, dict) and "recommendation" in llm_summary:
        rec_cand = str(llm_summary.get("recommendation", "")).strip()
        if rec_cand in {"Strong Hire", "Recommended", "Borderline", "Not Recommended"}:
            recommendation = rec_cand
        summary_text = str(llm_summary.get("summary", "")).strip()
        strengths = [str(s).strip() for s in llm_summary.get("strengths", []) if s]
        risks = [str(r).strip() for r in llm_summary.get("risks", []) if r]
        interview_questions = [str(q).strip() for q in llm_summary.get("interview_questions", []) if q]

    if not summary_text:
        # Deterministic local synthesis
        if test_taken:
            if test_result == "Passed" and (test_score or 0) >= 85 and proctor_warnings == 0 and match_pct >= 80:
                recommendation = "Strong Hire"
                summary_text = (
                    f"{app_row['candidate_name']} demonstrated outstanding technical aptitude with an {test_score}% score "
                    f"on Round 1 ({assess_row['technology']}) and an {match_pct}% resume alignment with the {app_row['job_title']} position. "
                    f"Proctoring monitoring was clean with zero suspicious events recorded."
                )
            elif test_result == "Passed":
                if proctor_warnings >= 3 or proctor_flagged:
                    recommendation = "Borderline"
                    summary_text = (
                        f"{app_row['candidate_name']} attained a passing score of {test_score}% on the technical evaluation, "
                        f"but proctoring recorded {proctor_warnings} warning events (flagged for review). "
                        f"Recommend verifying technical depth and problem-solving process during the live interview."
                    )
                else:
                    recommendation = "Recommended"
                    summary_text = (
                        f"{app_row['candidate_name']} cleared Round 1 with a solid {test_score}% score in {assess_row['technology']}, "
                        f"exceeding the {pass_pct}% passing benchmark. Resume matches key core competencies including {', '.join(matched_skills[:3])}."
                    )
            else:
                recommendation = "Not Recommended"
                summary_text = (
                    f"{app_row['candidate_name']} did not meet the required passing score on the Round 1 technical test "
                    f"({test_score}% achieved vs {pass_pct}% required in {assess_row['technology']}). "
                    f"Resume matches {match_pct}% of the job requirements."
                )
        else:
            if match_pct >= 75:
                recommendation = "Recommended"
                summary_text = (
                    f"{app_row['candidate_name']} presents a strong background with {match_pct}% skill alignment for {app_row['job_title']}. "
                    f"Primary verified skills include {', '.join(matched_skills[:4]) or 'profile match'}. "
                    f"Awaiting Round 1 technical MCQ assessment completion."
                )
            else:
                recommendation = "Borderline"
                summary_text = (
                    f"{app_row['candidate_name']} demonstrates {match_pct}% skill match for {app_row['job_title']}. "
                    f"Key missing skills include {', '.join(missing_skills[:3]) or 'specified requirements'}. "
                    f"Technical evaluation is advised before scheduling an interview."
                )

        # Generate strengths list
        strengths = [f"Strong background in {skill}" for skill in matched_skills[:4]]
        if test_taken and test_result == "Passed":
            strengths.append(f"Scored {test_score}% on {assess_row['technology']} technical MCQ assessment")
        if test_taken and proctor_warnings == 0:
            strengths.append("High test integrity: completed proctored assessment with 0 integrity warnings")
        if not strengths:
            strengths = ["Resume profile submitted for evaluation", "Relevant industry background"]

        # Generate risks list
        if missing_skills:
            risks.append(f"Gaps identified in required skills: {', '.join(missing_skills[:3])}")
        if test_taken and test_result == "Failed":
            risks.append(f"Failed Round 1 technical evaluation ({test_score}% achieved vs {pass_pct}% pass mark)")
        if proctor_warnings > 0:
            risks.append(f"Proctoring logged {proctor_warnings} warning event(s) during assessment")
        if not risks:
            risks = ["Verify hands-on system design and architecture experience during Round 2 interview"]

        # Generate targeted interview questions
        target_topic = missing_skills[0] if missing_skills else (assess_row['technology'] if assess_row and assess_row['technology'] else 'core technologies')
        interview_questions = [
            {
                "question": f"Can you explain a production challenge you encountered with {target_topic} and how you architected the solution?",
                "focus_area": f"{target_topic} Architecture",
                "what_to_look_for": f"Depth of practical experience in {target_topic}, trade-off analysis, concurrency/async handling, and error recovery."
            },
            {
                "question": f"How do you approach automated testing, performance profiling, and reliability in a high-scale {app_row['job_title']} environment?",
                "focus_area": "Code Quality & Testing",
                "what_to_look_for": "Structured unit and integration testing habits, CI/CD familiarity, and proactive telemetry monitoring."
            },
            {
                "question": "Describe a scenario where you had to balance rapid delivery against accumulating technical debt when launching a critical feature.",
                "focus_area": "Engineering Pragmatism",
                "what_to_look_for": "Pragmatic decision-making, stakeholder communication, and clean refactoring strategies."
            }
        ]

    now_iso = now()
    db.execute(
        """INSERT INTO candidate_ai_summaries
        (application_id, recommendation, summary, strengths_json, risks_json,
         interview_questions_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(application_id) DO UPDATE SET
        recommendation = excluded.recommendation,
        summary = excluded.summary,
        strengths_json = excluded.strengths_json,
        risks_json = excluded.risks_json,
        interview_questions_json = excluded.interview_questions_json,
        updated_at = excluded.updated_at""",
        (
            application_id,
            recommendation,
            summary_text,
            json.dumps(strengths),
            json.dumps(risks),
            json.dumps(interview_questions),
            now_iso,
            now_iso,
        )
    )

    return {
        "application_id": application_id,
        "candidate_name": app_row["candidate_name"],
        "candidate_email": app_row["candidate_email"],
        "job_title": app_row["job_title"],
        "company": app_row["job_company"],
        "resume_match_percentage": match_pct,
        "assessment_score_percentage": test_score,
        "assessment_result": test_result,
        "assessment_status": assess_row["status"] if assess_row else (app_row["status"] or "Not Issued"),
        "proctoring_flagged": proctor_flagged,
        "proctoring_warning_count": proctor_warnings,
        "recommendation": recommendation,
        "summary": summary_text,
        "strengths": strengths,
        "risks": risks,
        "interview_questions": interview_questions,
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def question_import_preview(row: sqlite3.Row) -> dict[str, Any]:
    questions = json.loads(row["preview_json"] or "[]")
    technology_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    for item in questions:
        if item["technology"]:
            technology_counts[item["technology"]] = technology_counts.get(item["technology"], 0) + 1
        if item["difficulty"]:
            difficulty_counts[item["difficulty"]] = difficulty_counts.get(item["difficulty"], 0) + 1
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "filename": row["filename"],
        "status": row["upload_status"],
        "total_extracted": row["total_extracted"],
        "valid_count": row["valid_count"],
        "invalid_count": row["invalid_count"],
        "technology_counts": technology_counts,
        "difficulty_counts": difficulty_counts,
        "errors": json.loads(row["error_details_json"] or "[]"),
        "questions": questions,
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "preview": True,
    }


@app.get("/api/jobs/{job_id}/questions")
def list_job_questions(
    job_id: int,
    technology: str | None = None,
    difficulty: str | None = None,
    active: bool | None = None,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        conditions = ["job_id = ?"]
        params: list[Any] = [job_id]
        if technology:
            conditions.append("lower(technology) = lower(?)")
            params.append(technology)
        if difficulty:
            if difficulty not in {"Easy", "Medium", "Hard"}:
                raise HTTPException(status_code=422, detail="Choose Easy, Medium, or Hard")
            conditions.append("difficulty = ?")
            params.append(difficulty)
        if active is not None:
            conditions.append("active = ?")
            params.append(int(active))
        questions = db.execute(
            f"""SELECT id, job_id, technology, question, options_json, correct_index,
            difficulty, active, source_type, source_import_id, created_at, updated_at FROM job_questions
            WHERE {' AND '.join(conditions)} ORDER BY technology, difficulty, id""",
            tuple(params),
        ).fetchall()
        return {
            "items": [
                {**dict(row), "options": json.loads(row["options_json"]), "options_json": None}
                for row in questions
            ],
            **job_question_summary(db, job_id),
        }


@app.post("/api/jobs/{job_id}/questions", status_code=201)
def create_job_question(
    job_id: int,
    body: QuestionBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    technology, question, options, correct_index, difficulty, active, explanation, learning_resource_url, subtopic = validated_question(body)
    with connect() as db:
        job = owned_job(db, job_id, user)
        rec_id = job["recruiter_id"] if "recruiter_id" in job.keys() and job["recruiter_id"] else user["id"]
        try:
            cursor = db.execute(
                """INSERT INTO job_questions
                (job_id, owner_id, recruiter_id, technology, question, options_json, correct_index,
                 difficulty, active, source_type, explanation, learning_resource_url, subtopic, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?)""",
                (
                    job_id, rec_id, rec_id, technology, question, json.dumps(options),
                    correct_index, difficulty, active, explanation, learning_resource_url, subtopic, now(), now(),
                ),
            )
        except Exception as error:
            if is_db_integrity_or_duplicate_error(error):
                raise HTTPException(status_code=409, detail="That question already exists for this job") from error
            raise
        row = db.execute("SELECT * FROM job_questions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        summary = job_question_summary(db, job_id)
    if summary["ready"]:
        try:
            auto_send_ready_assessments(job_id)
        except Exception as e:
            logger.error("Auto-send ready assessments failed for job %s: %s", job_id, e)
    return {**dict(row), "options": options, "options_json": None, "summary": summary}


@app.patch("/api/jobs/{job_id}/questions/{question_id}")
def update_job_question(
    job_id: int,
    question_id: int,
    body: QuestionUpdateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    technology, question, options, correct_index, difficulty, active, explanation, learning_resource_url, subtopic = validated_question(body)
    with connect() as db:
        owned_job(db, job_id, user)
        try:
            result = db.execute(
                """UPDATE job_questions SET technology = ?, question = ?, options_json = ?,
                correct_index = ?, difficulty = ?, active = ?, explanation = ?,
                learning_resource_url = ?, subtopic = ?, updated_at = ?
                WHERE id = ? AND job_id = ?""",
                (technology, question, json.dumps(options), correct_index, difficulty, active,
                 explanation, learning_resource_url, subtopic, now(), question_id, job_id),
            )
        except Exception as error:
            if is_db_integrity_or_duplicate_error(error):
                raise HTTPException(status_code=409, detail="That question already exists for this job") from error
            raise
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="Question not found for this job")
        row = db.execute("SELECT * FROM job_questions WHERE id = ?", (question_id,)).fetchone()
        summary = job_question_summary(db, job_id)
    if summary["ready"]:
        try:
            auto_send_ready_assessments(job_id)
        except Exception as e:
            logger.error("Auto-send ready assessments failed for job %s: %s", job_id, e)
    return {**dict(row), "options": options, "options_json": None}


@app.delete("/api/jobs/{job_id}/questions/{question_id}")
def delete_job_question(
    job_id: int,
    question_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, bool]:
    with connect() as db:
        owned_job(db, job_id, user)
        result = db.execute(
            "DELETE FROM job_questions WHERE id = ? AND job_id = ?", (question_id, job_id)
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="Question not found for this job")
    return {"deleted": True}


@app.post("/api/jobs/{job_id}/questions/ai-generate")
def ai_generate_questions(
    job_id: int,
    body: AIQuestionGenerateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        existing_rows = db.execute(
            "SELECT lower(trim(question)) AS q FROM job_questions WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        existing_questions = {r["q"] for r in existing_rows if r["q"]}
    questions, stats = generate_ai_questions(
        technology=body.technology,
        count=body.count,
        difficulty=body.difficulty,
        subtopic=body.subtopic,
        custom_prompt=body.custom_prompt,
        existing_questions=existing_questions,
    )
    return {
        "job_id": job_id,
        "technology": body.technology,
        "requested_count": body.count,
        "count": len(questions),
        "stats": stats,
        "questions": questions,
    }


@app.post("/api/jobs/{job_id}/questions/batch", status_code=201)
def batch_create_job_questions(
    job_id: int,
    body: QuestionBatchBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = owned_job(db, job_id, user)
        rec_id = job["recruiter_id"] if "recruiter_id" in job.keys() and job["recruiter_id"] else user["id"]

        existing_rows = db.execute(
            "SELECT lower(trim(question)) as q, lower(trim(technology)) as t FROM job_questions WHERE job_id = ?",
            (job_id,),
        ).fetchall()
        existing_set = {(r["t"], r["q"]) for r in existing_rows}

        valid_items: list[dict[str, Any]] = []
        invalid_count = 0
        skipped_count = 0

        for q in body.questions:
            item, err = validate_question_data(q)
            if not item:
                invalid_count += 1
                logger.info(
                    "Job %s question batch invalid question: reason=%s user=%s",
                    job_id, err, user.get("id"),
                )
                continue
            key = (item["technology"].casefold(), item["question"].casefold())
            if key in existing_set:
                skipped_count += 1
                continue
            existing_set.add(key)
            valid_items.append(item)

        created_count = 0
        if valid_items:
            now_iso = now()
            try:
                for item in valid_items:
                    db.execute(
                        """INSERT INTO job_questions
                        (job_id, owner_id, recruiter_id, technology, question, options_json, correct_index,
                         difficulty, active, source_type, explanation, learning_resource_url, subtopic, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?, ?, ?)""",
                        (
                            job_id, rec_id, rec_id, item["technology"], item["question"],
                            json.dumps(item["options"]), item["correct_index"], item["difficulty"],
                            item["active"], item["explanation"], item["learning_resource_url"],
                            item["subtopic"], now_iso, now_iso,
                        ),
                    )
                    created_count += 1
            except Exception as e:
                logger.error(
                    "Transaction error saving batch questions for job %s, user %s: %s",
                    job_id, user.get("id"), e, exc_info=True,
                )
                raise HTTPException(status_code=500, detail="Failed to save questions batch due to a database error")

        summary = job_question_summary(db, job_id)

    if summary.get("ready"):
        try:
            auto_send_ready_assessments(job_id)
        except Exception as e:
            logger.error("Auto-send ready assessments failed for job %s: %s", job_id, e)

    logger.info(
        "Batch question save complete for job %s by user %s: created=%d, skipped=%d, invalid=%d",
        job_id, user.get("id"), created_count, skipped_count, invalid_count,
    )
    return {
        "created": created_count,
        "count": created_count,
        "skipped": skipped_count,
        "duplicates": skipped_count,
        "invalid": invalid_count,
        "total": len(body.questions),
        "summary": summary,
    }


@app.get("/api/jobs/{job_id}/question-bank-summary")
def get_job_question_summary(
    job_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        return job_question_summary(db, job_id)


@app.post("/api/jobs/{job_id}/question-bank/imports", status_code=201)
async def preview_question_bank_import(
    job_id: int,
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
    filename = Path((file.filename or "").replace("\\", "/")).name
    if Path(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="Upload a PDF question bank.")
    content = await file.read(MAX_QUESTION_BANK_PDF_BYTES + 1)
    if len(content) > MAX_QUESTION_BANK_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Question-bank PDFs must be {MAX_QUESTION_BANK_PDF_BYTES // (1024 * 1024)} MB or smaller.",
        )
    if not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="The uploaded file does not have a valid PDF signature.")

    text = extract_question_bank_pdf_text(content)
    questions = parse_question_bank_text(text)
    if not questions:
        raise HTTPException(
            status_code=422,
            detail="No supported MCQ blocks were found. Please use the provided question-bank template.",
        )
    clean_filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)[:180] or "question-bank.pdf"
    QUESTION_BANK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    storage_path = QUESTION_BANK_UPLOAD_DIR / f"{uuid.uuid4().hex}.pdf"
    storage_path.write_bytes(content)
    errors = [message for item in questions for message in item["errors"]]
    with connect() as db:
        job = owned_job(db, job_id, user)
        cursor = db.execute(
            """INSERT INTO question_bank_imports
            (job_id, recruiter_id, filename, storage_path, upload_status, total_extracted,
             valid_count, invalid_count, preview_json, error_details_json, created_at)
            VALUES (?, ?, ?, ?, 'Preview', ?, ?, ?, ?, ?, ?)""",
            (
                job_id, job["recruiter_id"], clean_filename, str(storage_path),
                len(questions), sum(bool(item["valid"]) for item in questions),
                sum(not item["valid"] for item in questions), json.dumps(questions),
                json.dumps(errors), now(),
            ),
        )
        import_row = db.execute(
            "SELECT * FROM question_bank_imports WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        notify_user(
            db, job["recruiter_id"], "question_bank_import_ready",
            "Question-bank PDF ready for review",
            f"{clean_filename} contains {sum(bool(item['valid']) for item in questions)} valid and "
            f"{sum(not item['valid'] for item in questions)} invalid question blocks for {job['title']}. "
            "Review the preview before importing.",
            email=True,
        )
        if errors:
            notify_user(
                db, job["recruiter_id"], "question_bank_import_invalid",
                "Some question blocks need attention",
                f"{len(errors)} issue(s) were found in {clean_filename}. Review the preview before confirming.",
                email=True,
            )
    return question_import_preview(import_row)


@app.post("/api/jobs/{job_id}/question-bank/imports/{import_id}/confirm")
def confirm_question_bank_import(
    job_id: int,
    import_id: int,
    body: QuestionImportConfirmBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = owned_job(db, job_id, user)
        import_row = db.execute(
            """SELECT * FROM question_bank_imports
            WHERE id = ? AND job_id = ? AND recruiter_id = ?""",
            (import_id, job_id, job["recruiter_id"]),
        ).fetchone()
        if not import_row:
            raise HTTPException(status_code=404, detail="Question-bank import not found for this job")
        if import_row["upload_status"] != "Preview":
            raise HTTPException(status_code=409, detail="This question-bank import is no longer awaiting review")
        preview_questions = json.loads(import_row["preview_json"] or "[]")
        preview_by_index = {item["source_index"]: item for item in preview_questions}
        seen_sources: set[int] = set()
        imported_sources: set[int] = set()
        skipped_sources: set[int] = set()
        inserted = 0
        skipped: list[str] = []
        imported_question_texts: set[str] = set()
        for approved in body.questions:
            if not approved.include:
                continue
            if approved.source_index not in preview_by_index or approved.source_index in seen_sources:
                raise HTTPException(status_code=422, detail="An import preview question was missing or duplicated")
            seen_sources.add(approved.source_index)
            technology = approved.technology.strip()
            question = approved.question.strip()
            options = [option.strip() for option in approved.options]
            if not technology or not question or len({value.casefold() for value in options}) != 4 or any(not value for value in options):
                raise HTTPException(
                    status_code=422,
                    detail=f"Question {approved.source_index + 1} needs a technology, question, and four distinct options.",
                )
            normalized_question = question.casefold()
            exists = db.execute(
                "SELECT 1 FROM job_questions WHERE job_id = ? AND lower(question) = lower(?)",
                (job_id, question),
            ).fetchone()
            if exists or normalized_question in imported_question_texts:
                skipped.append(f"Question {approved.source_index + 1} duplicates an existing question.")
                skipped_sources.add(approved.source_index)
                continue
            imported_question_texts.add(normalized_question)
            db.execute(
                """INSERT INTO job_questions
                (job_id, owner_id, recruiter_id, technology, question, options_json, correct_index,
                 difficulty, active, source_type, source_import_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'pdf_import', ?, ?, ?)""",
                (
                    job_id, job["recruiter_id"], job["recruiter_id"], technology, question,
                    json.dumps(options), approved.correct_index, approved.difficulty,
                    import_id, now(), now(),
                ),
            )
            imported_sources.add(approved.source_index)
            inserted += 1
        if not inserted:
            raise HTTPException(status_code=422, detail="Select at least one valid, non-duplicate question to import.")
        final_errors = json.loads(import_row["error_details_json"] or "[]") + skipped
        db.execute(
            """UPDATE question_bank_imports SET upload_status = 'Confirmed',
            valid_count = ?, invalid_count = ?, error_details_json = ?, confirmed_at = ?
            WHERE id = ?""",
            (
                inserted,
                len(
                    {
                        item["source_index"]
                        for item in preview_questions
                        if not item["valid"] and item["source_index"] not in imported_sources
                    }
                    | skipped_sources
                ),
                json.dumps(final_errors),
                now(),
                import_id,
            ),
        )
        summary = job_question_summary(db, job_id)
        notify_user(
            db, job["recruiter_id"], "question_bank_import_completed",
            "Question-bank import completed",
            f"Imported {inserted} questions from {import_row['filename']} for {job['title']}.",
            email=True,
        )
        if not summary["ready"]:
            notify_user(
                db, job["recruiter_id"], "question_bank_incomplete",
                "Question bank needs more questions",
                f"The question bank for {job['title']} has {summary['active_count']} active questions; "
                f"add {summary['additional_needed']} more to reach 20.",
                email=True,
            )
    auto_send_ready_assessments(job_id)
    return {
        "import_id": import_id,
        "status": "Confirmed",
        "imported_count": inserted,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "summary": summary,
    }


@app.get("/api/jobs/{job_id}/question-bank/imports")
def list_question_bank_imports(
    job_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = owned_job(db, job_id, user)
        rows = db.execute(
            """SELECT id, job_id, recruiter_id, filename, upload_status, total_extracted,
            valid_count, invalid_count, error_details_json, created_at, confirmed_at
            FROM question_bank_imports WHERE job_id = ? AND recruiter_id = ?
            ORDER BY created_at DESC, id DESC""",
            (job_id, job["recruiter_id"]),
        ).fetchall()
    return {
        "items": [
            {
                **{key: row[key] for key in row.keys() if key != "error_details_json"},
                "errors": json.loads(row["error_details_json"] or "[]"),
            }
            for row in rows
        ]
    }


@app.get("/api/jobs/{job_id}/question-bank/imports/{import_id}")
def get_question_bank_import(
    job_id: int,
    import_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = owned_job(db, job_id, user)
        row = db.execute(
            """SELECT * FROM question_bank_imports
            WHERE id = ? AND job_id = ? AND recruiter_id = ?""",
            (import_id, job_id, job["recruiter_id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Question-bank import not found for this job")
        if row["upload_status"] != "Preview":
            raise HTTPException(status_code=409, detail="Only a pending preview can be reviewed")
        return question_import_preview(row)


@app.delete("/api/jobs/{job_id}/question-bank/imports/{import_id}")
def cancel_question_bank_import(
    job_id: int,
    import_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = owned_job(db, job_id, user)
        row = db.execute(
            """SELECT id, storage_path, upload_status FROM question_bank_imports
            WHERE id = ? AND job_id = ? AND recruiter_id = ?""",
            (import_id, job_id, job["recruiter_id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Question-bank import not found for this job")
        if row["upload_status"] != "Preview":
            raise HTTPException(status_code=409, detail="Only a pending preview can be cancelled")
        db.execute(
            "UPDATE question_bank_imports SET upload_status = 'Cancelled' WHERE id = ?",
            (import_id,),
        )
        try:
            Path(row["storage_path"]).unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove cancelled question-bank PDF import %s", import_id)
    return {"import_id": import_id, "status": "Cancelled"}


@app.post("/api/jobs/{job_id}/apply", status_code=201)
def apply(job_id: int, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id = ? AND status = 'Open'", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="This role is no longer available")
        resume = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
        if not resume:
            raise HTTPException(status_code=409, detail="Upload a resume before applying")
        match = calculate_match(job, resume, json.dumps(user["profile"]))
        application_status = "Shortlisted" if match["match_percentage"] >= 70 else "Applied"
        existing = db.execute(
            "SELECT id, status FROM applications WHERE candidate_id = ? AND job_id = ?",
            (user["id"], job_id),
        ).fetchone()
        if existing and existing["status"] != "Withdrawn":
            raise HTTPException(status_code=409, detail="You have already applied for this role")
        if existing:
            db.execute(
                """UPDATE applications SET resume_id = ?, status = ?, match_percentage = ?,
                matched_skills_json = ?, missing_skills_json = ?, applied_at = ?
                WHERE id = ?""",
                (
                    resume["id"], application_status, match["match_percentage"],
                    json.dumps(match["matched_skills"]), json.dumps(match["missing_skills"]),
                    now(), existing["id"],
                ),
            )
            application_id = existing["id"]
            record_application_status(db, application_id, application_status, user, "Reapplied after withdrawal")
        else:
            cursor = db.execute(
                """INSERT INTO applications
                (candidate_id, job_id, resume_id, status, match_percentage, matched_skills_json, missing_skills_json, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user["id"], job_id, resume["id"], application_status, match["match_percentage"],
                    json.dumps(match["matched_skills"]), json.dumps(match["missing_skills"]), now(),
                ),
            )
            application_id = cursor.lastrowid
            record_application_status(db, application_id, application_status, user)
        notify_user(
            db, user["id"], "application_submitted", "Application submitted",
            f"Your application for {job['title']} at {job['company']} was received.",
            application_id=application_id, email=True,
        )
        notify_user(
            db, job["recruiter_id"], "new_application", "New candidate application",
            f"A candidate applied for {job['title']} at {job['company']}.",
            application_id=application_id,
        )
        admins = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchall()
        for admin in admins:
            add_notification(
                db, admin["id"], "new_application", "New candidate application",
                f"A new application was received for {job['title']}.", application_id=application_id,
            )
        assessment_for_match(
            db, application_id, user["id"], job, resume, json.dumps(user["profile"]), match["match_percentage"]
        )
        row = db.execute(
            """SELECT a.id, a.candidate_id, a.job_id, a.status, a.match_percentage, a.applied_at, j.title, j.company
            FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?""",
            (application_id,),
        ).fetchone()
        if application_status == "Shortlisted":
            notify_user(
                db, user["id"], "shortlisted", "You have been shortlisted",
                f"Your application for {job['title']} matched {match['match_percentage']}% of the required skills.",
                application_id=application_id, email=True,
            )
            notify_user(
                db, job["recruiter_id"], "candidate_shortlisted", "A candidate was shortlisted",
                f"A candidate for {job['title']} matched {match['match_percentage']}% of the required skills.",
                application_id=application_id,
            )
    if application_status == "Shortlisted":
        auto_send_assessment_link(application_id)
        with connect() as db:
            row = db.execute(
                """SELECT a.id, a.candidate_id, a.job_id, a.status, a.match_percentage, a.applied_at,
                j.title, j.company FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?""",
                (application_id,),
            ).fetchone()
    return public_application(row)


@app.get("/api/applications")
def list_applications(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        if user["role"] in {"recruiter", "admin"}:
            ownership = "AND j.recruiter_id = ?" if user["role"] == "recruiter" else ""
            params: tuple[int, ...] = (user["id"],) if user["role"] == "recruiter" else ()
            legacy_ids = db.execute(
                f"""SELECT a.id FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE a.match_percentage IS NULL {ownership}""",
                params,
            ).fetchall()
            for legacy in legacy_ids:
                application = db.execute(
                    """SELECT a.id AS application_id, a.candidate_id, a.status, j.*, r.extracted_text,
                    r.parsed_profile_json, u.profile_json
                    FROM applications a JOIN jobs j ON j.id = a.job_id
                    JOIN users u ON u.id = a.candidate_id
                    LEFT JOIN resumes r ON r.candidate_id = a.candidate_id
                    WHERE a.id = ?""",
                    (legacy["id"],),
                ).fetchone()
                if application is None or application["parsed_profile_json"] is None:
                    continue
                resume = {
                    "extracted_text": application["extracted_text"] or "",
                    "parsed_profile_json": application["parsed_profile_json"] or "{}",
                }
                match = calculate_match(application, resume, application["profile_json"])
                status = "Shortlisted" if match["match_percentage"] >= 70 and application["status"] == "Applied" else application["status"]
                db.execute(
                    "UPDATE applications SET match_percentage = ?, status = ? WHERE id = ?",
                    (match["match_percentage"], status, application["id"]),
                )
                assessment_for_match(
                    db,
                    application["application_id"],
                    application["candidate_id"],
                    application,
                    resume,
                    application["profile_json"],
                    match["match_percentage"],
                )
            expire_assessment_records(db)
        if user["role"] == "candidate":
            rows = db.execute(
                """SELECT a.id, a.candidate_id, a.job_id, a.status, a.match_percentage,
                a.matched_skills_json, a.missing_skills_json, a.applied_at, j.title, j.company,
                x.id AS assessment_id,
                CASE WHEN x.id IS NULL AND a.status = 'Shortlisted' AND a.match_percentage >= 70
                THEN 'Pending Question Bank' ELSE x.status END AS assessment_status,
                x.technology AS assessment_topic, x.assessment_name, x.assessment_round,
                x.total_questions AS assessment_total_questions,
                x.duration_minutes AS assessment_duration_minutes,
                x.pass_percentage AS assessment_pass_percentage,
                x.email_sent_at AS assessment_email_sent_at, x.starts_at AS assessment_started_at,
                x.submitted_at AS assessment_submitted_at, x.score AS assessment_score,
                x.score_percentage AS assessment_score_percentage, x.result AS assessment_result,
                x.correct_answer_count AS assessment_correct_count,
                x.incorrect_answer_count AS assessment_incorrect_count,
                (SELECT n.action_url FROM notifications n
                 WHERE n.related_assessment_id = x.id AND n.user_id = a.candidate_id
                 AND n.action_url IS NOT NULL AND n.action_url != ''
                 ORDER BY n.id DESC LIMIT 1) AS assessment_action_url
                FROM applications a JOIN jobs j ON j.id = a.job_id
                LEFT JOIN assessments x ON x.application_id = a.id
                WHERE a.candidate_id = ? ORDER BY a.applied_at DESC""",
                (user["id"],),
            ).fetchall()
        elif user["role"] == "recruiter":
            rows = db.execute(
                """SELECT a.id, a.candidate_id, a.job_id, a.status, a.match_percentage,
                a.matched_skills_json, a.missing_skills_json, a.internal_notes, a.applied_at,
                j.title, j.company, u.name AS candidate_name, u.email AS candidate_email,
                x.id AS assessment_id,
                x.status AS assessment_status, x.technology AS assessment_topic,
                x.question_topic_weights_json,
                x.assessment_name, x.assessment_round, x.score_percentage AS assessment_score_percentage,
                x.result AS assessment_result, x.correct_answer_count AS assessment_correct_count,
                x.incorrect_answer_count AS assessment_incorrect_count,
                x.email_sent_at AS assessment_email_sent_at, x.score AS assessment_score,
                x.starts_at AS assessment_started_at, x.submitted_at AS assessment_submitted_at,
                (SELECT COUNT(*) FROM job_questions q WHERE q.job_id = j.id AND q.active = 1) AS active_question_count
                FROM applications a JOIN jobs j ON j.id = a.job_id JOIN users u ON u.id = a.candidate_id
                LEFT JOIN assessments x ON x.application_id = a.id
                WHERE j.recruiter_id = ? ORDER BY a.applied_at DESC""",
                (user["id"],),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT a.id, a.candidate_id, a.job_id, a.status, a.match_percentage,
                a.matched_skills_json, a.missing_skills_json, a.applied_at,
                j.title, j.company, u.name AS candidate_name, u.email AS candidate_email,
                x.id AS assessment_id,
                x.status AS assessment_status, x.technology AS assessment_topic,
                x.question_topic_weights_json,
                x.assessment_name, x.assessment_round, x.score_percentage AS assessment_score_percentage,
                x.result AS assessment_result, x.correct_answer_count AS assessment_correct_count,
                x.incorrect_answer_count AS assessment_incorrect_count,
                x.email_sent_at AS assessment_email_sent_at, x.score AS assessment_score,
                x.submitted_at AS assessment_submitted_at
                FROM applications a JOIN jobs j ON j.id = a.job_id JOIN users u ON u.id = a.candidate_id
                LEFT JOIN assessments x ON x.application_id = a.id
                ORDER BY a.applied_at DESC LIMIT 100"""
            ).fetchall()
        application_ids = [row["id"] for row in rows]
        interviews_by_application: dict[int, dict[str, Any]] = {}
        if application_ids:
            placeholders = ",".join("?" for _ in application_ids)
            interview_rows = db.execute(
                f"""SELECT i.id, i.application_id, i.scheduled_at, i.meeting_platform,
                i.meeting_url, i.duration_minutes, i.timezone, i.recruiter_message, i.status,
                i.invitation_sent_at, i.candidate_viewed_at, i.updated_at
                FROM interviews i
                WHERE i.id IN (
                    SELECT MAX(i2.id) FROM interviews i2
                    WHERE i2.application_id IN ({placeholders}) GROUP BY i2.application_id
                )""",
                application_ids,
            ).fetchall()
            interviews_by_application = {
                row["application_id"]: dict(row) for row in interview_rows
            }
        dist_by_assessment: dict[int, dict[str, int]] = {}
        if user["role"] in {"recruiter", "admin"}:
            assessment_ids = [row["assessment_id"] for row in rows if row["assessment_id"]]
            if assessment_ids:
                aplaceholders = ",".join("?" for _ in assessment_ids)
                dist_rows = db.execute(
                    f"""SELECT aq.assessment_id, t.technology, COUNT(*) AS cnt
                    FROM assessment_questions aq
                    JOIN technical_questions t ON t.id = aq.question_id
                    WHERE aq.assessment_id IN ({aplaceholders})
                    GROUP BY aq.assessment_id, t.technology""",
                    assessment_ids,
                ).fetchall()
                for dr in dist_rows:
                    dist_by_assessment.setdefault(dr["assessment_id"], {})[dr["technology"]] = dr["cnt"]
    items = []
    for row in rows:
        item = dict(row)
        item["matched_skills"] = json.loads(item.pop("matched_skills_json") or "[]")
        item["missing_skills"] = json.loads(item.pop("missing_skills_json") or "[]")
        item.pop("question_topic_weights_json", None)
        if user["role"] in {"recruiter", "admin"} and item.get("assessment_id") and item.get("assessment_status") not in {None, "Not Sent", "Manual Topic Required"}:
            item["topic_distribution"] = dist_by_assessment.get(item["assessment_id"], {})
        else:
            item["topic_distribution"] = {}
        interview = interviews_by_application.get(item["id"])
        if interview and user["role"] == "candidate" and interview["status"] in {"Cancelled", "Completed"}:
            interview["meeting_url"] = ""
        item["interview"] = interview
        if user["role"] == "candidate":
            item["assessment_score"] = None
            item["assessment_score_percentage"] = None
            item["assessment_correct_count"] = None
            item["assessment_incorrect_count"] = None
            item["assessment_result"] = None
            if item.get("assessment_status") in {"Pending Recruiter Decision", "Completed"}:
                item["assessment_status"] = "Submitted"
        items.append(item)
    return {"items": items}


@app.get("/api/applications/{application_id}/history")
def application_history(
    application_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with connect() as db:
        scope = """a.id = ? AND (
            ? = 'admin' OR a.candidate_id = ? OR
            EXISTS (SELECT 1 FROM jobs j WHERE j.id = a.job_id AND j.recruiter_id = ?)
        )"""
        exists = db.execute(
            f"SELECT 1 FROM applications a WHERE {scope}",
            (application_id, user["role"], user["id"], user["id"]),
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Application not found")
        rows = db.execute(
            """SELECT h.status, h.actor_role, h.note, h.created_at
            FROM application_status_history h WHERE h.application_id = ?
            ORDER BY h.created_at ASC, h.id ASC""",
            (application_id,),
        ).fetchall()
    items = [dict(row) for row in rows]
    if user["role"] == "candidate":
        for item in items:
            item["note"] = ""
    return {"items": items}


@app.patch("/api/applications/{application_id}/notes")
def update_application_note(
    application_id: int,
    body: InternalNoteBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, str]:
    with connect() as db:
        owned = db.execute(
            """SELECT a.id FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
            (application_id, user["role"], user["id"]),
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=404, detail="Application not found for your job listings")
        db.execute("UPDATE applications SET internal_notes = ? WHERE id = ?", (body.note, application_id))
    return {"note": body.note}


@app.get("/api/applications/{application_id}/ai-summary")
def get_candidate_ai_summary(
    application_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        managed_assessment(db, application_id, user)
        summary = synthesize_candidate_ai_summary(db, application_id, force_refresh=False)
    return summary


@app.post("/api/applications/{application_id}/ai-summary")
def generate_candidate_ai_summary_endpoint(
    application_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        managed_assessment(db, application_id, user)
        summary = synthesize_candidate_ai_summary(db, application_id, force_refresh=True)
    return summary


@app.get("/api/notifications")
def list_notifications(
    unread: bool = False,
    limit: int = 50,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 100")
    with connect() as db:
        where = "user_id = ?" + (" AND is_read = 0" if unread else "")
        rows = db.execute(
            f"""SELECT id, type, title, message, related_application_id, related_assessment_id,
            action_url, is_read, email_delivery_status, created_at
            FROM notifications WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?""",
            (user["id"], limit),
        ).fetchall()
        unread_count = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
            (user["id"],),
        ).fetchone()[0]
    return {"items": [dict(row) for row in rows], "unread_count": unread_count}


@app.get("/api/notifications/unread-count")
def notification_unread_count(user: dict[str, Any] = Depends(current_user)) -> dict[str, int]:
    with connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
            (user["id"],),
        ).fetchone()[0]
    return {"unread_count": count}


@app.patch("/api/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    body: NotificationReadBody,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with connect() as db:
        result = db.execute(
            "UPDATE notifications SET is_read = ? WHERE id = ? AND user_id = ?",
            (int(body.is_read), notification_id, user["id"]),
        )
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="Notification not found")
    return {"id": notification_id, "is_read": body.is_read}


@app.patch("/api/notifications/read-all")
def mark_all_notifications_read(user: dict[str, Any] = Depends(current_user)) -> dict[str, int]:
    with connect() as db:
        updated = db.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
            (user["id"],),
        ).rowcount
    return {"updated": updated}


def assessment_delivery(
    recipient: str,
    job_title: str,
    company: str,
    technology: str,
    expires_at: str,
    assessment_url: str,
) -> str:
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    smtp_host = os.environ.get("SKILLSPHERE_SMTP_HOST", "").strip()

    if not (resend_key or sendgrid_key or smtp_host):
        if IS_PRODUCTION:
            raise HTTPException(status_code=503, detail="Assessment email is not configured on this server")
        logger.warning("Development assessment email for %s: %s", recipient, assessment_url)
        return "development"

    if IS_PRODUCTION and not os.environ.get("SKILLSPHERE_PUBLIC_BASE_URL"):
        raise HTTPException(status_code=503, detail="Set SKILLSPHERE_PUBLIC_BASE_URL before sending assessment links")

    subject = f"Technical assessment: {job_title}"
    expiry_label = datetime.fromisoformat(expires_at).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body_text = (
        f"You have been shortlisted for {job_title} at {company}.\n\n"
        f"Please complete this 20-question {technology} assessment. You will have\n"
        f"{ASSESSMENT_DURATION_MINUTES} minutes after starting, and this link expires\n"
        f"on {expiry_label}.\n\n"
        f"Start your assessment: {assessment_url}\n\n"
        f"The assessment is individual and can only be submitted once.\n"
    )

    result = send_email_message(recipient, subject, body_text)
    if result in ("Sent", "Development"):
        return "email"
    raise HTTPException(status_code=502, detail="Could not send the assessment email")


def managed_assessment(
    db: sqlite3.Connection,
    application_id: int,
    user: dict[str, Any],
) -> sqlite3.Row:
    row = db.execute(
        """SELECT a.id AS application_id, a.candidate_id, a.job_id, a.status AS application_status,
        a.match_percentage, u.email AS candidate_email, j.title, j.company,
        j.recruiter_id AS owner_id, j.assessment_question_count, j.allow_detailed_results,
        x.id AS assessment_id, x.technology, x.question_topics_json,
        x.question_topic_weights_json,
        x.status AS assessment_status,
        x.email_sent_at, x.score, x.submitted_at
        FROM applications a JOIN users u ON u.id = a.candidate_id
        JOIN jobs j ON j.id = a.job_id
        LEFT JOIN assessments x ON x.application_id = a.id
        WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
        (application_id, user["role"], user["id"]),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Application not found for your job listings")
    return row


def create_assessment_if_ready(db: sqlite3.Connection, application_id: int) -> None:
    application = db.execute(
        """SELECT a.id AS application_id, a.candidate_id, a.status, a.match_percentage, a.job_id,
        j.*, r.extracted_text, r.parsed_profile_json, u.profile_json
        FROM applications a JOIN jobs j ON j.id = a.job_id
        JOIN users u ON u.id = a.candidate_id
        LEFT JOIN resumes r ON r.candidate_id = a.candidate_id
        WHERE a.id = ?""",
        (application_id,),
    ).fetchone()
    if not application or application["match_percentage"] is None or application["parsed_profile_json"] is None:
        return
    resume = {
        "extracted_text": application["extracted_text"] or "",
        "parsed_profile_json": application["parsed_profile_json"] or "{}",
    }
    assessment_for_match(
        db, application["application_id"], application["candidate_id"],
        application, resume, application["profile_json"], application["match_percentage"],
    )


def notify_assessment_expired(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    recruiter = db.execute("SELECT recruiter_id FROM jobs WHERE id = ?", (row["job_id"],)).fetchone()
    for recipient in (row["candidate_id"], recruiter["recruiter_id"] if recruiter else None):
        if recipient:
            add_notification(
                db, recipient, "assessment_expired", "Assessment link expired",
                f"The technical assessment for {row['title']} expired.",
                application_id=row["application_id"], assessment_id=row["id"],
            )


def expire_assessment_records(db: sqlite3.Connection) -> None:
    expiring = db.execute(
        """SELECT x.id, x.candidate_id, x.application_id, x.job_id, x.status,
        x.expires_at, j.title, j.recruiter_id
        FROM assessments x JOIN jobs j ON j.id = x.job_id
        WHERE x.status IN ('Sent', 'Started') AND x.expires_at <= ?""",
        (now(),),
    ).fetchall()
    for assessment in expiring:
        updated = db.execute(
            """UPDATE assessments SET status = 'Expired', token_hash = NULL
            WHERE id = ? AND status IN ('Sent', 'Started')""",
            (assessment["id"],),
        ).rowcount
        if not updated:
            continue
        application = db.execute(
            "SELECT status FROM applications WHERE id = ?", (assessment["application_id"],)
        ).fetchone()
        if application and application["status"] in {"Assessment Sent", "Assessment Started"}:
            db.execute(
                "UPDATE applications SET status = 'Assessment Expired' WHERE id = ?",
                (assessment["application_id"],),
            )
            record_application_status(
                db, assessment["application_id"], "Assessment Expired",
                {"id": None, "role": "system"},
            )
        notify_assessment_expired(db, assessment)


def issue_assessment_link(
    db: sqlite3.Connection,
    managed: sqlite3.Row,
    *,
    reissue: bool,
) -> dict[str, Any]:
    if managed["application_status"] in {"Withdrawn", "Rejected", "Accepted", "Selected"}:
        raise HTTPException(status_code=409, detail="This application is no longer eligible for an assessment")
    if not managed["assessment_id"]:
        raise HTTPException(status_code=409, detail="Assessment data is unavailable for this application")
    if not managed["technology"]:
        raise HTTPException(status_code=409, detail="Select an assessment topic before sending the assessment")
    allowed = {"Sent", "Expired"} if reissue else {"Not Sent"}
    if managed["assessment_status"] not in allowed:
        if reissue:
            raise HTTPException(status_code=409, detail="Only sent or expired assessments can be reissued")
        raise HTTPException(status_code=409, detail="Use reissue for an assessment that has already been sent")

    target_count = (
        managed["assessment_question_count"]
        if ("assessment_question_count" in managed.keys() and managed["assessment_question_count"])
        else 20
    )
    allow_detailed = (
        managed["allow_detailed_results"]
        if ("allow_detailed_results" in managed.keys() and managed["allow_detailed_results"] is not None)
        else 1
    )
    topics = json.loads(managed["question_topics_json"] or "[]")
    if not topics:
        topics = [managed["technology"]]
    normalized_topics = {topic.casefold(): topic for topic in topics}
    stored_weights = json.loads(managed["question_topic_weights_json"] or "{}")
    topic_weights = {
        topic: max(1, int(stored_weights.get(topic, 1)))
        for topic in normalized_topics.values()
    }
    placeholders = ",".join("?" for _ in topics)
    questions = db.execute(
        f"""SELECT id, technology, question, options_json, correct_index, difficulty,
        explanation, learning_resource_url, subtopic FROM job_questions
        WHERE job_id = ? AND lower(technology) IN ({placeholders}) AND active = 1""",
        (managed["job_id"], *(topic.casefold() for topic in topics)),
    ).fetchall()
    selected = relevant_balanced_question_sample(questions, topic_weights, target_count)
    if len(selected) != target_count:
        # Fallback: check if the combined active pool across all topics for this job meets target_count
        all_job_questions = db.execute(
            """SELECT id, technology, question, options_json, correct_index, difficulty,
            explanation, learning_resource_url, subtopic FROM job_questions
            WHERE job_id = ? AND active = 1""",
            (managed["job_id"],),
        ).fetchall()
        all_valid = [q for q in all_job_questions if _is_valid_active_question_row(q)]
        if len(all_valid) >= target_count:
            all_topics = sorted({q["technology"] for q in all_valid})
            all_weights = {t: topic_weights.get(t, 1) for t in all_topics}
            selected = relevant_balanced_question_sample(all_valid, all_weights, target_count)
            topics = all_topics
        if len(selected) != target_count:
            avail_count = len([q for q in questions if _is_valid_active_question_row(q)])
            raise HTTPException(
                status_code=409,
                detail=f"At least {target_count} questions are required across the selected topics. Currently available: {avail_count}. Add {max(0, target_count - avail_count)} more active questions.",
            )
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    created = datetime.now(timezone.utc)
    expires_at = (created.timestamp() + ASSESSMENT_TTL_HOURS * 3600)
    expiry = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    assessment_url = f"{PUBLIC_BASE_URL}/?{urlencode({'assessment': token})}"
    candidate_email = (managed["candidate_email"] or "").strip() if "candidate_email" in managed.keys() else ""
    if not candidate_email or "@" not in candidate_email or "." not in candidate_email.split("@")[-1]:
        raise HTTPException(
            status_code=400,
            detail="Cannot send assessment: candidate does not have a valid registered email address.",
        )
    delivery = assessment_delivery(
        candidate_email, managed["title"], managed["company"], managed["technology"], expiry, assessment_url
    )

    db.execute("DELETE FROM assessment_questions WHERE assessment_id = ?", (managed["assessment_id"],))
    db.execute(
        "DELETE FROM assessment_proctoring_events WHERE assessment_id = ?",
        (managed["assessment_id"],),
    )
    for order, question in enumerate(selected, start=1):
        options = json.loads(question["options_json"])
        correct_option = options[question["correct_index"]]
        random.SystemRandom().shuffle(options)
        db.execute(
            """INSERT OR IGNORE INTO technical_questions
            (technology, question, options_json, correct_index, difficulty, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                question["technology"], question["question"], question["options_json"],
                question["correct_index"], question["difficulty"].lower(), now(),
            ),
        )
        application = db.execute(
            "SELECT status FROM applications WHERE id = ?", (managed["application_id"],)
        ).fetchone()
        if application and application["status"] != "Assessment Sent":
            db.execute(
                "UPDATE applications SET status = 'Assessment Sent' WHERE id = ?",
                (managed["application_id"],),
            )
            record_application_status(
                db, managed["application_id"], "Assessment Sent",
                {"id": managed["owner_id"], "role": "recruiter"},
            )
        legacy_question = db.execute(
            "SELECT id FROM technical_questions WHERE technology = ? AND question = ?",
            (question["technology"], question["question"]),
        ).fetchone()
        db.execute(
            """INSERT INTO assessment_questions
            (assessment_id, question_id, question_order, question_snapshot, options_snapshot, correct_index, technology, difficulty, explanation, learning_resource_url, subtopic)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                managed["assessment_id"], legacy_question["id"], order, question["question"],
                json.dumps(options), options.index(correct_option),
                question["technology"], question["difficulty"],
                question["explanation"] if "explanation" in question.keys() else "",
                question["learning_resource_url"] if "learning_resource_url" in question.keys() else "",
                question["subtopic"] if "subtopic" in question.keys() else "",
            ),
        )
    raw_dist: dict[str, int] = {}
    for q in selected:
        t_name = q["technology"]
        raw_dist[t_name] = raw_dist.get(t_name, 0) + 1
    topic_distribution = {t: raw_dist[t] for t in topics if t in raw_dist}
    for t, c in raw_dist.items():
        if t not in topic_distribution:
            topic_distribution[t] = c
    combined_tech_label = ", ".join(topic_distribution.keys()) if topic_distribution else managed["technology"]
    db.execute(
        """UPDATE assessments SET token_hash = ?, status = 'Sent', starts_at = NULL,
        expires_at = ?, submitted_at = NULL, score = NULL, total_questions = ?,
        technology = ?, question_topics_json = ?,
        assessment_name = ?, assessment_round = 'Round 1', pass_percentage = ?,
        score_percentage = NULL, correct_answer_count = NULL, incorrect_answer_count = NULL,
        result = NULL, result_processed_at = NULL, time_taken_seconds = NULL,
        duration_minutes = ?, email_sent_at = ?, proctoring_required = ?,
        proctoring_consent_at = NULL, camera_permission_granted_at = NULL,
        proctoring_started_at = NULL, proctoring_warning_count = 0,
        proctoring_flagged_at = NULL, detailed_results_visible = ?
        WHERE id = ?""",
        (
            token_hash, expiry, target_count,
            combined_tech_label,
            json.dumps(list(topic_distribution.keys())),
            ROUND1_ASSESSMENT_NAME, ROUND1_PASS_PERCENTAGE,
            ASSESSMENT_DURATION_MINUTES,
            now() if delivery == "email" else None,
            int(PROCTORING_REQUIRED),
            int(bool(allow_detailed)),
            managed["assessment_id"],
        ),
    )
    add_notification(
        db,
        managed["candidate_id"],
        "assessment_sent",
        "Your assessment is ready",
        f"Your {combined_tech_label} assessment for {managed['title']} is available. The secure link was sent to your registered email.",
        application_id=managed["application_id"],
        assessment_id=managed["assessment_id"],
        action_url=assessment_url,
        email_delivery_status="Sent" if delivery == "email" else "Development",
    )
    add_notification(
        db, managed["owner_id"], "assessment_sent", "Assessment link sent",
        f"A {combined_tech_label} assessment link was sent to {managed['candidate_email']} for {managed['title']}.",
        application_id=managed["application_id"], assessment_id=managed["assessment_id"],
        email_delivery_status="Sent" if delivery == "email" else "Development",
    )
    return {
        "status": "Sent",
        "delivery": delivery,
        "topic_distribution": topic_distribution,
        "selected_question_ids": [q["id"] for q in selected],
    }


def auto_send_assessment_link(application_id: int) -> None:
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            create_assessment_if_ready(db, application_id)
            recruiter = db.execute(
                """SELECT j.recruiter_id FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE a.id = ?""",
                (application_id,),
            ).fetchone()
            if not recruiter:
                return
            owner = {"id": recruiter["recruiter_id"], "role": "recruiter"}
            managed = managed_assessment(db, application_id, owner)
            if not managed["assessment_id"] or not managed["technology"] or managed["assessment_status"] != "Not Sent":
                return
            issue_assessment_link(db, managed, reissue=False)
    except HTTPException as error:
        logger.error("Could not automatically deliver assessment for application %s: %s", application_id, error.detail)
        with connect() as db:
            application = db.execute(
                """SELECT a.candidate_id, j.recruiter_id, j.title FROM applications a
                JOIN jobs j ON j.id = a.job_id WHERE a.id = ?""",
                (application_id,),
            ).fetchone()
            if application:
                for recipient, role in (
                    (application["candidate_id"], "candidate"),
                    (application["recruiter_id"], "recruiter"),
                ):
                    add_notification(
                        db, recipient, "assessment_delivery_failed", "Assessment link could not be sent",
                        f"The assessment for {application['title']} could not be emailed. Ask an administrator to check email settings.",
                        application_id=application_id,
                    )


def auto_send_ready_assessments(job_id: int) -> None:
    with connect() as db:
        applications = db.execute(
            """SELECT id FROM applications WHERE job_id = ?
            AND status = 'Shortlisted' AND match_percentage >= 70 ORDER BY applied_at, id""",
            (job_id,),
        ).fetchall()
    for application in applications:
        auto_send_assessment_link(application["id"])


@app.post("/api/applications/{application_id}/assessment/send")
def send_assessment(
    application_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        managed_assessment(db, application_id, user)
        create_assessment_if_ready(db, application_id)
        managed = managed_assessment(db, application_id, user)
        if not managed["assessment_id"]:
            summary = job_question_summary(db, managed["job_id"])
            target_q = summary.get("configured_question_count", 20)
            total_avail = summary.get("active_count", 0)
            if not summary["ready"] or total_avail < target_q:
                raise HTTPException(
                    status_code=409,
                    detail=f"At least {target_q} questions are required across the selected topics. Currently available: {total_avail}. Add {max(0, target_q - total_avail)} more active questions.",
                )
            topics_info = summary.get("topics", [])
            selected_topics = [t["technology"] for t in topics_info if t.get("active_count", 0) > 0]
            if not selected_topics:
                raise HTTPException(
                    status_code=409,
                    detail="No assessment topic available. Add questions to the question bank first.",
                )
            best_topic = ", ".join(selected_topics)
            equal_weights = {t: 1 for t in selected_topics}
            db.execute(
                """INSERT INTO assessments
                (application_id, candidate_id, job_id, technology, status, match_percentage,
                 total_questions, assessment_name, assessment_round, pass_percentage,
                 duration_minutes, question_topics_json, question_topic_weights_json,
                 selection_reasoning, proctoring_required, created_at)
                VALUES (?, ?, ?, ?, 'Not Sent', ?, ?, ?, 'Round 1', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    application_id, managed["candidate_id"], managed["job_id"], best_topic,
                    managed["match_percentage"] or 0, target_q,
                    ROUND1_ASSESSMENT_NAME, ROUND1_PASS_PERCENTAGE,
                    ASSESSMENT_DURATION_MINUTES,
                    json.dumps(selected_topics), json.dumps(equal_weights),
                    "Manual: recruiter-initiated across all active question bank topics",
                    int(PROCTORING_REQUIRED), now(),
                ),
            )
            managed = managed_assessment(db, application_id, user)
        result = issue_assessment_link(db, managed, reissue=False)
    return result


@app.post("/api/applications/{application_id}/assessment/reissue")
def reissue_assessment(
    application_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        managed = managed_assessment(db, application_id, user)
        result = issue_assessment_link(db, managed, reissue=True)
    return result


@app.get("/api/assessments")
def assessment_dashboard(
    status: str | None = None,
    technology: str | None = None,
    job_id: int | None = None,
    candidate: str | None = None,
    result: str | None = None,
    min_score: float | None = None,
    max_score: float | None = None,
    completed_from: date | None = None,
    completed_to: date | None = None,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    conditions = []
    params: list[Any] = []
    if user["role"] == "candidate":
        conditions.append("x.candidate_id = ?")
        params.append(user["id"])
    elif user["role"] == "recruiter":
        conditions.append("j.recruiter_id = ?")
        params.append(user["id"])
    if status:
        conditions.append("x.status = ?")
        params.append(status)
    if technology:
        conditions.append("x.technology = ?")
        params.append(technology)
    if job_id:
        conditions.append("x.job_id = ?")
        params.append(job_id)
    if candidate and user["role"] in {"recruiter", "admin"}:
        conditions.append("(u.name LIKE ? OR u.email LIKE ?)")
        params.extend([f"%{candidate}%", f"%{candidate}%"])
    if result:
        if result not in {"Passed", "Failed"}:
            raise HTTPException(status_code=422, detail="Choose Passed or Failed for result")
        conditions.append("x.result = ?")
        params.append(result)
    if min_score is not None:
        if not 0 <= min_score <= 100:
            raise HTTPException(status_code=422, detail="Minimum score must be between 0 and 100")
        conditions.append("x.score_percentage >= ?")
        params.append(min_score)
    if max_score is not None:
        if not 0 <= max_score <= 100:
            raise HTTPException(status_code=422, detail="Maximum score must be between 0 and 100")
        conditions.append("x.score_percentage <= ?")
        params.append(max_score)
    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(status_code=422, detail="Minimum score cannot exceed maximum score")
    if completed_from:
        conditions.append("x.submitted_at >= ?")
        params.append(datetime.combine(completed_from, datetime.min.time(), timezone.utc).isoformat())
    if completed_to:
        conditions.append("x.submitted_at < ?")
        params.append(datetime.combine(completed_to + timedelta(days=1), datetime.min.time(), timezone.utc).isoformat())
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with connect() as db:
        expire_assessment_records(db)
        rows = db.execute(
            f"""SELECT x.id, x.application_id, x.candidate_id, x.job_id, x.technology,
            x.assessment_name, x.assessment_round, x.pass_percentage, x.score_percentage,
            x.correct_answer_count, x.incorrect_answer_count, x.result, x.result_processed_at,
            x.time_taken_seconds,
            x.question_topics_json, x.selection_reasoning,
            x.status AS assessment_status, x.match_percentage, x.starts_at, x.expires_at,
            x.submitted_at, x.score, x.total_questions, x.duration_minutes,
            x.email_sent_at, j.title, j.company, a.status AS application_status,
            u.name AS candidate_name, u.email AS candidate_email,
            (SELECT COUNT(*) FROM assessment_questions q
             WHERE q.assessment_id = x.id AND q.selected_answer IS NOT NULL) AS answered_count,
            (SELECT n.action_url FROM notifications n
             WHERE n.related_assessment_id = x.id AND n.user_id = x.candidate_id
             AND n.action_url IS NOT NULL AND n.action_url != ''
             ORDER BY n.id DESC LIMIT 1) AS action_url
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            JOIN applications a ON a.id = x.application_id
            JOIN users u ON u.id = x.candidate_id {where}
            ORDER BY x.created_at DESC, x.id DESC""",
            tuple(params),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        if user["role"] == "candidate":
            item.pop("question_topics_json", None)
            item.pop("selection_reasoning", None)
            item["score"] = None
            item["score_percentage"] = None
            item["correct_answer_count"] = None
            item["incorrect_answer_count"] = None
            item["result"] = None
            item["pass_percentage"] = None
            if item.get("assessment_status") in {"Pending Recruiter Decision", "Completed"}:
                item["assessment_status"] = "Submitted"
        else:
            item.pop("action_url", None)
        item["score_percentage"] = (
            item["score_percentage"]
            if item["score_percentage"] is not None
            else round(item["score"] / item["total_questions"] * 100, 2)
            if item["score"] is not None and item["total_questions"] else None
        )
        if item["time_taken_seconds"] is None and item["starts_at"] and item["submitted_at"]:
            item["time_taken_seconds"] = max(
                0,
                int((datetime.fromisoformat(item["submitted_at"]) - datetime.fromisoformat(item["starts_at"])).total_seconds()),
            )
        items.append(item)
    return {"items": items}


@app.get("/api/assessment-reviews/{assessment_id}")
def assessment_review(
    assessment_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if user["role"] == "candidate":
        raise HTTPException(
            status_code=403,
            detail="Assessment results are confidential and under recruiter review",
        )
    with connect() as db:
        if user["role"] == "recruiter":
            conditions = "x.id = ? AND j.recruiter_id = ?"
            params: tuple[Any, ...] = (assessment_id, user["id"])
        else:
            conditions = "x.id = ?"
            params = (assessment_id,)
        row = db.execute(
            f"""SELECT x.id, x.application_id, x.candidate_id, x.technology, x.status, x.match_percentage,
            x.assessment_name, x.assessment_round, x.pass_percentage,
            x.starts_at, x.expires_at, x.submitted_at, x.score, x.score_percentage,
            x.total_questions, x.correct_answer_count, x.incorrect_answer_count, x.result,
            x.result_processed_at, x.time_taken_seconds, a.status AS application_status,
            x.duration_minutes, x.detailed_results_visible,
            x.recruiter_decision, x.recruiter_decision_at, x.recruiter_decision_by, x.recruiter_decision_notes,
            j.title, j.company, j.allow_detailed_results,
            u.name AS candidate_name, u.email AS candidate_email
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            JOIN users u ON u.id = x.candidate_id
            JOIN applications a ON a.id = x.application_id WHERE {conditions}""",
            params,
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found or access denied")
        if row["status"] not in {"Completed", "Pending Recruiter Decision"} or not row["submitted_at"]:
            raise HTTPException(status_code=409, detail="Question-by-question review is available after submission")
        questions = db.execute(
            """SELECT q.question_order, q.question_snapshot, q.options_snapshot,
            q.selected_answer, q.correct_index, q.is_correct,
            q.technology, q.difficulty, q.explanation, q.learning_resource_url, q.subtopic
            FROM assessment_questions q WHERE q.assessment_id = ? ORDER BY q.question_order""",
            (assessment_id,),
        ).fetchall()
        rec_row = db.execute(
            """SELECT recording_status, duration_seconds, format, uploaded_at
            FROM assessment_recordings WHERE assessment_id = ? ORDER BY id DESC LIMIT 1""",
            (assessment_id,),
        ).fetchone()

    can_view_detailed = (
        user["role"] in {"recruiter", "admin"}
        or bool(
            row["detailed_results_visible"]
            if row["detailed_results_visible"] is not None
            else row["allow_detailed_results"]
        )
    )

    topic_stats: dict[str, dict[str, int]] = {}
    diff_stats: dict[str, dict[str, int]] = {
        "Easy": {"total": 0, "correct": 0},
        "Medium": {"total": 0, "correct": 0},
        "Hard": {"total": 0, "correct": 0},
    }
    for q in questions:
        tech = q["technology"] or row["technology"] or "General"
        diff = q["difficulty"] or "Medium"
        if diff not in diff_stats:
            diff_stats[diff] = {"total": 0, "correct": 0}
        corr = int(bool(q["is_correct"]))
        topic_stats.setdefault(tech, {"total": 0, "correct": 0})
        topic_stats[tech]["total"] += 1
        topic_stats[tech]["correct"] += corr
        diff_stats[diff]["total"] += 1
        diff_stats[diff]["correct"] += corr

    topic_breakdown = [
        {
            "technology": tech,
            "total": stats["total"],
            "correct": stats["correct"],
            "percentage": round(stats["correct"] * 100.0 / stats["total"], 1) if stats["total"] else 0.0,
        }
        for tech, stats in topic_stats.items()
    ]
    difficulty_breakdown = {
        diff: {
            "total": stats["total"],
            "correct": stats["correct"],
            "percentage": round(stats["correct"] * 100.0 / stats["total"], 1) if stats["total"] else 0.0,
        }
        for diff, stats in diff_stats.items()
    }

    strengths = [item["technology"] for item in topic_breakdown if item["percentage"] >= 70.0]
    areas_to_improve = [item["technology"] for item in topic_breakdown if item["percentage"] < 70.0]

    recommendations: list[str] = []
    for weak in areas_to_improve:
        w_low = weak.lower()
        if "sql" in w_low:
            recommendations.append("Practice SQL queries, joins, aggregations, window functions, and GROUP BY / HAVING clauses.")
        elif "python" in w_low:
            recommendations.append("Review Python data cleaning, pandas manipulation, list/dict comprehensions, and OOP principles.")
        elif "react" in w_low or "javascript" in w_low:
            recommendations.append("Strengthen JavaScript ES6+ features, asynchronous async/await patterns, and React state/lifecycle management.")
        elif "java" in w_low:
            recommendations.append("Review Java OOP concepts, collections framework, exception handling, and multithreading basics.")
        elif "tableau" in w_low or "bi" in w_low:
            recommendations.append("Practice data visualization principles, calculated fields, dashboard actions, and level-of-detail (LOD) expressions.")
        else:
            recommendations.append(f"Focus on core concepts, problem-solving, and hands-on exercises in {weak}.")

    if not recommendations:
        recommendations.append("Outstanding performance across all evaluated topics! Continue building advanced projects to maintain your edge.")

    details = []
    for question in questions:
        options = json.loads(question["options_snapshot"])
        is_corr = bool(question["is_correct"]) if question["is_correct"] is not None else None
        hide_details = (user["role"] == "candidate" and not can_view_detailed)
        details.append({
            "order": question["question_order"],
            "question": question["question_snapshot"],
            "options": options,
            "selected_answer": question["selected_answer"],
            "correct_answer": None if hide_details else question["correct_index"],
            "is_correct": is_corr,
            "technology": question["technology"] or row["technology"] or "General",
            "difficulty": question["difficulty"] or "Medium",
            "explanation": "" if hide_details else (question["explanation"] or ""),
            "learning_resource_url": "" if hide_details else (question["learning_resource_url"] or ""),
            "subtopic": question["subtopic"] or "",
        })

    return {
        **dict(row),
        "can_view_detailed_answers": can_view_detailed,
        "recording_status": rec_row["recording_status"] if rec_row else (row["recording_status"] if "recording_status" in row.keys() else "Pending"),
        "recording_captured": bool(rec_row and rec_row["recording_status"] == "Uploaded"),
        "recording_duration_seconds": rec_row["duration_seconds"] if rec_row else None,
        "strengths": strengths,
        "areas_to_improve": areas_to_improve,
        "topic_breakdown": topic_breakdown,
        "difficulty_breakdown": difficulty_breakdown,
        "recommendations": recommendations,
        "questions": details,
    }


@app.post("/api/assessments/{assessment_id}/decision")
def make_assessment_decision(
    assessment_id: int,
    body: AssessmentDecisionBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        if user["role"] == "recruiter":
            row = db.execute(
                """SELECT x.id, x.application_id, x.candidate_id, x.job_id, x.status,
                x.submitted_at, x.result, x.score_percentage, x.correct_answer_count,
                x.total_questions, x.pass_percentage, j.title, j.company,
                u.name AS candidate_name
                FROM assessments x
                JOIN jobs j ON j.id = x.job_id
                JOIN users u ON u.id = x.candidate_id
                WHERE x.id = ? AND j.recruiter_id = ?""",
                (assessment_id, user["id"]),
            ).fetchone()
        else:
            row = db.execute(
                """SELECT x.id, x.application_id, x.candidate_id, x.job_id, x.status,
                x.submitted_at, x.result, x.score_percentage, x.correct_answer_count,
                x.total_questions, x.pass_percentage, j.title, j.company,
                u.name AS candidate_name
                FROM assessments x
                JOIN jobs j ON j.id = x.job_id
                JOIN users u ON u.id = x.candidate_id
                WHERE x.id = ?""",
                (assessment_id,),
            ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found or access denied")
        if not row["submitted_at"] and row["status"] not in {"Pending Recruiter Decision", "Completed"}:
            raise HTTPException(status_code=400, detail="Cannot make decision on unsubmitted assessment")

        decision_val = "Passed" if body.decision == "Pass" else "Failed"
        decided_at = now()
        new_app_status = f"Round 1 {decision_val}"

        db.execute(
            """UPDATE assessments SET recruiter_decision = ?, recruiter_decision_at = ?,
            recruiter_decision_by = ?, recruiter_decision_notes = ?, status = 'Completed'
            WHERE id = ?""",
            (decision_val, decided_at, user["id"], body.notes.strip(), assessment_id),
        )
        db.execute(
            "UPDATE applications SET status = ? WHERE id = ?",
            (new_app_status, row["application_id"]),
        )
        history_note = f"Recruiter manual decision: {decision_val}"
        if body.notes.strip():
            history_note += f" - {body.notes.strip()}"
        record_application_status(
            db, row["application_id"], new_app_status, user, history_note,
        )
        notify_user(
            db, row["candidate_id"], "application_status_updated",
            f"Application status updated: {row['title']}",
            f"Your application status for {row['title']} has been updated to {new_app_status}.",
            application_id=row["application_id"], assessment_id=row["id"], email=True,
        )

    return {
        "assessment_id": assessment_id,
        "application_id": row["application_id"],
        "recruiter_decision": decision_val,
        "recruiter_decision_at": decided_at,
        "recruiter_decision_by": user["id"],
        "application_status": new_app_status,
        "message": f"Candidate successfully marked as {decision_val}.",
    }


@app.get("/api/candidate/analytics")
def candidate_analytics(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        applications = db.execute(
            """SELECT a.id, a.status, a.match_percentage, a.applied_at, j.title, j.company
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.candidate_id = ? ORDER BY a.applied_at DESC""",
            (user["id"],),
        ).fetchall()

        status_counts: dict[str, int] = {}
        for app_row in applications:
            status_counts[app_row["status"]] = status_counts.get(app_row["status"], 0) + 1

        interviews_count = db.execute(
            """SELECT COUNT(*) FROM interviews i
            JOIN applications a ON a.id = i.application_id
            WHERE a.candidate_id = ?""",
            (user["id"],),
        ).fetchone()[0]

        completed_assessments = db.execute(
            """SELECT x.id, x.score, x.total_questions, x.score_percentage, x.result,
            x.technology, x.submitted_at, j.title
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            WHERE x.candidate_id = ? AND x.status IN ('Completed', 'Pending Recruiter Decision')
            ORDER BY x.submitted_at DESC""",
            (user["id"],),
        ).fetchall()

        return {
            "total_applications": len(applications),
            "active_applications": sum(1 for a in applications if a["status"] not in ("Rejected", "Withdrawn", "Accepted", "Selected")),
            "interviews_scheduled": interviews_count,
            "assessments_completed": len(completed_assessments),
            "average_score_percentage": None,
            "applications_by_status": status_counts,
            "strengths": [],
            "areas_to_improve": [],
            "recent_assessments": [
                {
                    "id": a["id"],
                    "technology": a["technology"],
                    "submitted_at": a["submitted_at"],
                    "title": a["title"],
                }
                for a in completed_assessments[:5]
            ],
        }


@app.get("/api/recruiter/analytics")
def recruiter_analytics(
    report_range: str = "this_week",
    start_date: str | None = None,
    end_date: str | None = None,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    if report_range == "this_week":
        start, end = monday, monday + timedelta(days=7)
    elif report_range == "last_week":
        start, end = monday - timedelta(days=7), monday
    elif report_range == "this_month":
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        end = next_month
    elif report_range == "last_month":
        this_month_start = today.replace(day=1)
        end = this_month_start
        start = (this_month_start - timedelta(days=1)).replace(day=1)
    elif report_range == "all":
        start = date(2020, 1, 1)
        end = today + timedelta(days=1)
    elif report_range == "custom":
        try:
            start = date.fromisoformat(start_date or "")
            end = date.fromisoformat(end_date or "") + timedelta(days=1)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Custom reports need valid start_date and end_date values") from error
        if end <= start:
            raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    else:
        raise HTTPException(status_code=422, detail="Choose this_week, last_week, this_month, last_month, all, or custom")

    range_start = datetime.combine(start, datetime.min.time(), timezone.utc).isoformat()
    range_end = datetime.combine(end, datetime.min.time(), timezone.utc).isoformat()

    with connect() as db:
        job_condition = "1=1" if user["role"] == "admin" else "j.recruiter_id = ?"
        job_params = () if user["role"] == "admin" else (user["id"],)

        jobs = db.execute(
            f"SELECT id, title, company, status, assessment_question_count, allow_detailed_results, skills_json FROM jobs j WHERE {job_condition}",
            job_params,
        ).fetchall()
        job_ids = [j["id"] for j in jobs]

        if not job_ids:
            return {
                "range": report_range,
                "start_date": start.isoformat(),
                "end_date": (end - timedelta(days=1)).isoformat(),
                "summary": {"total_jobs": 0, "total_applications": 0, "assessments_completed": 0, "interviews_scheduled": 0, "accepted": 0},
                "conversion_funnel": [],
                "jobs_breakdown": [],
                "top_skills": [],
                "question_bank_readiness": {"ready_jobs": 0, "incomplete_jobs": 0},
            }

        placeholders = ",".join("?" for _ in job_ids)

        apps = db.execute(
            f"""SELECT a.id, a.job_id, a.status, a.match_percentage, a.applied_at
            FROM applications a WHERE a.job_id IN ({placeholders})
            AND a.applied_at >= ? AND a.applied_at < ?""",
            (*job_ids, range_start, range_end),
        ).fetchall()

        all_apps = db.execute(
            f"""SELECT a.id, a.job_id, a.status, a.match_percentage
            FROM applications a WHERE a.job_id IN ({placeholders})""",
            tuple(job_ids),
        ).fetchall()

        funnel_stages = [
            ("Applied", lambda s: True),
            ("Under Review", lambda s: s not in ("Applied",)),
            ("Shortlisted", lambda s: s in ("Shortlisted", "Assessment Sent", "Assessment Started", "Assessment Completed", "Round 1 Passed", "Round 1 Failed", "Interview", "Accepted", "Selected")),
            ("Assessment Completed", lambda s: s in ("Assessment Completed", "Round 1 Passed", "Round 1 Failed", "Interview", "Accepted", "Selected")),
            ("Round 1 Passed", lambda s: s in ("Round 1 Passed", "Interview", "Accepted", "Selected")),
            ("Interview", lambda s: s in ("Interview", "Accepted", "Selected")),
            ("Accepted", lambda s: s in ("Accepted", "Selected")),
        ]
        total_applied = len(all_apps)
        funnel = []
        for stage_name, matcher in funnel_stages:
            count = sum(1 for a in all_apps if matcher(a["status"]))
            pct = round((count / total_applied * 100), 1) if total_applied > 0 else 0.0
            funnel.append({"stage": stage_name, "count": count, "conversion_rate": pct})

        assessments = db.execute(
            f"""SELECT x.id, x.job_id, x.status, x.result, x.score_percentage
            FROM assessments x WHERE x.job_id IN ({placeholders})""",
            tuple(job_ids),
        ).fetchall()

        interviews_count = db.execute(
            f"""SELECT COUNT(*) FROM interviews i
            JOIN applications a ON a.id = i.application_id
            WHERE a.job_id IN ({placeholders})""",
            tuple(job_ids),
        ).fetchone()[0]

        ready_count = 0
        incomplete_count = 0
        jobs_breakdown = []
        for j in jobs:
            s = job_question_summary(db, j["id"])
            if s["ready"]:
                ready_count += 1
            else:
                incomplete_count += 1
            job_apps = [a for a in all_apps if a["job_id"] == j["id"]]
            job_ass = [x for x in assessments if x["job_id"] == j["id"] and x["status"] == "Completed"]
            job_passed = sum(1 for x in job_ass if x["result"] == "Passed")
            avg_s = round(sum(x["score_percentage"] or 0.0 for x in job_ass) / len(job_ass), 1) if job_ass else None
            jobs_breakdown.append({
                "job_id": j["id"],
                "title": j["title"],
                "company": j["company"],
                "status": j["status"],
                "assessment_question_count": j["assessment_question_count"],
                "allow_detailed_results": bool(j["allow_detailed_results"]),
                "applicant_count": len(job_apps),
                "assessment_completed_count": len(job_ass),
                "assessment_pass_rate": round(job_passed / len(job_ass) * 100, 1) if job_ass else None,
                "average_score": avg_s,
                "question_bank_ready": s["ready"],
                "additional_questions_needed": s["additional_needed"],
            })

        all_skills: dict[str, int] = {}
        for j in jobs:
            try:
                for sk in json.loads(j["skills_json"] or "[]"):
                    all_skills[sk] = all_skills.get(sk, 0) + 1
            except Exception:
                pass
        top_skills = [{"skill": k, "count": v} for k, v in sorted(all_skills.items(), key=lambda x: x[1], reverse=True)[:8]]

        return {
            "range": report_range,
            "start_date": start.isoformat(),
            "end_date": (end - timedelta(days=1)).isoformat(),
            "summary": {
                "total_jobs": len(jobs),
                "total_applications": len(all_apps),
                "applications_in_period": len(apps),
                "assessments_completed": sum(1 for x in assessments if x["status"] == "Completed"),
                "interviews_scheduled": interviews_count,
                "accepted": sum(1 for a in all_apps if a["status"] in ("Accepted", "Selected")),
            },
            "conversion_funnel": funnel,
            "jobs_breakdown": jobs_breakdown,
            "top_skills": top_skills,
            "question_bank_readiness": {
                "ready_jobs": ready_count,
                "incomplete_jobs": incomplete_count,
            },
        }


@app.get("/api/assessment-reviews/{assessment_id}/proctoring")
def assessment_proctoring_review(
    assessment_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """SELECT x.id, x.proctoring_required, x.proctoring_consent_at,
            x.camera_permission_granted_at, x.proctoring_started_at,
            x.proctoring_warning_count, x.proctoring_flagged_at,
            x.starts_at, x.status, j.title, j.company,
            u.name AS candidate_name, u.email AS candidate_email
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            JOIN users u ON u.id = x.candidate_id
            WHERE x.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
            (assessment_id, user["role"], user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found for your job listings")
        events = db.execute(
            """SELECT event_type, severity, duration_seconds, created_at
            FROM assessment_proctoring_events WHERE assessment_id = ?
            ORDER BY created_at, id""",
            (assessment_id,),
        ).fetchall()
    return {
        **dict(row),
        "flagged": row["proctoring_flagged_at"] is not None,
        "events": [dict(event) for event in events],
    }


@app.get("/api/admin/proctoring")
def admin_proctoring(
    candidate: str | None = None,
    job_id: int | None = None,
    recruiter_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    flagged_only: bool | None = None,
    limit: int = 100,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    del user
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 200")
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    conditions = ["(x.proctoring_required = 1 OR EXISTS (SELECT 1 FROM assessment_proctoring_events e WHERE e.assessment_id = x.id))"]
    params: list[Any] = []
    if candidate:
        conditions.append("(candidate.name LIKE ? OR candidate.email LIKE ?)")
        params.extend([f"%{candidate}%", f"%{candidate}%"])
    if job_id is not None:
        conditions.append("x.job_id = ?")
        params.append(job_id)
    if recruiter_id is not None:
        conditions.append("j.recruiter_id = ?")
        params.append(recruiter_id)
    if start_date:
        conditions.append("COALESCE(x.proctoring_started_at, x.created_at) >= ?")
        params.append(datetime.combine(start_date, datetime.min.time(), timezone.utc).isoformat())
    if end_date:
        conditions.append("COALESCE(x.proctoring_started_at, x.created_at) < ?")
        params.append(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), timezone.utc).isoformat())
    if flagged_only:
        conditions.append("x.proctoring_flagged_at IS NOT NULL")
    elif flagged_only is False:
        conditions.append("x.proctoring_flagged_at IS NULL")
    with connect() as db:
        rows = db.execute(
            f"""SELECT x.id, x.application_id, x.status, x.proctoring_required,
            x.proctoring_started_at, x.proctoring_warning_count, x.proctoring_flagged_at,
            j.id AS job_id, j.title, j.company, j.recruiter_id,
            recruiter.name AS recruiter_name, candidate.id AS candidate_id,
            candidate.name AS candidate_name, candidate.email AS candidate_email,
            r.id AS recording_id, COALESCE(r.recording_status, x.recording_status) AS recording_status,
            r.duration_seconds AS recording_duration, r.upload_error AS recording_error
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            JOIN users candidate ON candidate.id = x.candidate_id
            JOIN users recruiter ON recruiter.id = j.recruiter_id
            LEFT JOIN assessment_recordings r ON r.assessment_id = x.id
            WHERE {' AND '.join(conditions)}
            ORDER BY COALESCE(x.proctoring_flagged_at, x.proctoring_started_at, x.created_at) DESC,
            x.id DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
        assessment_ids = [row["id"] for row in rows]
        events_by_assessment: dict[int, list[dict[str, Any]]] = {assessment_id: [] for assessment_id in assessment_ids}
        if assessment_ids:
            placeholders = ",".join("?" for _ in assessment_ids)
            events = db.execute(
                f"""SELECT assessment_id, event_type, severity, duration_seconds, created_at
                FROM assessment_proctoring_events
                WHERE assessment_id IN ({placeholders}) ORDER BY created_at, id""",
                assessment_ids,
            ).fetchall()
            for event in events:
                events_by_assessment[event["assessment_id"]].append({
                    "event_type": event["event_type"],
                    "severity": event["severity"],
                    "duration_seconds": event["duration_seconds"],
                    "created_at": event["created_at"],
                })
    return {
        "items": [
            {
                **dict(row),
                "flagged": row["proctoring_flagged_at"] is not None,
                "events": events_by_assessment[row["id"]],
            }
            for row in rows
        ]
    }


@app.patch("/api/applications/{application_id}/assessment/topic")
def select_assessment_topic(
    application_id: int,
    body: AssessmentTopicBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        managed = managed_assessment(db, application_id, user)
        if managed["application_status"] in {"Withdrawn", "Rejected", "Accepted", "Selected"}:
            raise HTTPException(status_code=409, detail="This application is no longer eligible for an assessment")
        if managed["assessment_status"] != "Manual Topic Required":
            raise HTTPException(status_code=409, detail="A manual topic can only be set when automatic selection is unavailable")
            # Manual selections must still have a complete, relevant job-specific bank.
        selected_topics = [t.strip() for t in body.technology.split(",") if t.strip()]
        if not selected_topics:
            raise HTTPException(status_code=422, detail="Select at least one valid topic")
        job_row = db.execute("SELECT assessment_question_count FROM jobs WHERE id = ?", (managed["job_id"],)).fetchone()
        target_q = (job_row["assessment_question_count"] if job_row and job_row["assessment_question_count"] else 20)
        placeholders = ",".join("?" for _ in selected_topics)
        bank_count = db.execute(
            f"""SELECT COUNT(*) FROM job_questions
            WHERE job_id = ? AND lower(technology) IN ({placeholders}) AND active = 1""",
            (managed["job_id"], *(t.casefold() for t in selected_topics)),
        ).fetchone()[0]
        if bank_count < target_q:
            raise HTTPException(
                status_code=409,
                detail=f"At least {target_q} questions are required across the selected topics. Currently available: {bank_count}.",
            )
        combined_label = ", ".join(selected_topics)
        db.execute(
            """UPDATE assessments SET technology = ?, status = 'Not Sent',
            question_topics_json = ?, question_topic_weights_json = ?, selection_reasoning = ?
            WHERE id = ?""",
            (
                combined_label, json.dumps(selected_topics),
                json.dumps({t: 1 for t in selected_topics}),
                "The recruiter selected these topics from the job question bank.",
                managed["assessment_id"],
            ),
        )
    return {"technology": combined_label, "status": "Not Sent"}


def public_assessment_for_token(
    db: sqlite3.Connection,
    token: str,
    *,
    check_deadline: bool = True,
    allow_submitted: bool = False,
) -> tuple[sqlite3.Row | None, str | None]:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    where = "WHERE (x.token_hash = ?" + (" OR x.submitted_token_hash = ?" if allow_submitted else "") + ")"
    params: tuple[Any, ...] = (token_hash, token_hash) if allow_submitted else (token_hash,)
    row = db.execute(
        f"""SELECT x.*, j.title, j.company, a.status AS application_status
        FROM assessments x JOIN jobs j ON j.id = x.job_id
        JOIN applications a ON a.id = x.application_id
        {where}""",
        params,
    ).fetchone()
    if not row:
        return None, "This assessment link is invalid or has already been used"
    if row["application_status"] in {"Withdrawn", "Rejected", "Accepted", "Selected"}:
        db.execute(
            "UPDATE assessments SET token_hash = NULL, status = 'Expired' WHERE id = ? AND status NOT IN ('Completed', 'Pending Recruiter Decision')",
            (row["id"],),
        )
        return row, "This assessment is no longer available"
    valid_statuses = {"Sent", "Started", "Completed", "Pending Recruiter Decision"} if allow_submitted else {"Sent", "Started"}
    if row["status"] not in valid_statuses:
        return row, "This assessment link is invalid or has already been used"
    if check_deadline and row["expires_at"]:
        if row["status"] not in {"Completed", "Pending Recruiter Decision"} and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            changed = db.execute(
                "UPDATE assessments SET token_hash = NULL, status = 'Expired' WHERE id = ?",
                (row["id"],),
            ).rowcount
            if changed:
                application = db.execute(
                    "SELECT status FROM applications WHERE id = ?", (row["application_id"],)
                ).fetchone()
                if application and application["status"] in {"Assessment Sent", "Assessment Started"}:
                    db.execute(
                        "UPDATE applications SET status = 'Assessment Expired' WHERE id = ?",
                        (row["application_id"],),
                    )
                    record_application_status(
                        db, row["application_id"], "Assessment Expired",
                        {"id": None, "role": "system"},
                    )
                notify_assessment_expired(db, row)
            return row, "This assessment has expired"
    return row, None


def assessment_payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    questions = []
    if row["status"] == "Started":
        questions = db.execute(
            """SELECT id, question_order, question_snapshot, options_snapshot
            FROM assessment_questions WHERE assessment_id = ? ORDER BY question_order""",
            (row["id"],),
        ).fetchall()
    return {
        "title": row["assessment_name"],
        "assessment_name": row["assessment_name"],
        "assessment_round": row["assessment_round"],
        "job_title": row["title"],
        "company": row["company"],
        "technology": row["technology"],
        "status": row["status"],
        "total_questions": row["total_questions"],
        "duration_minutes": row["duration_minutes"],
        "pass_percentage": row["pass_percentage"],
        "starts_at": row["starts_at"],
        "expires_at": row["expires_at"],
        "proctoring": {
            "required": bool(row["proctoring_required"]),
            "consent_given": row["proctoring_consent_at"] is not None,
            "camera_permission_granted": row["camera_permission_granted_at"] is not None,
            "warning_count": row["proctoring_warning_count"],
            "max_warnings": PROCTORING_MAX_WARNINGS,
            "face_missing_warning_seconds": PROCTORING_FACE_MISSING_SECONDS,
            "flagged": row["proctoring_flagged_at"] is not None,
            "auto_submit": PROCTORING_AUTO_SUBMIT,
            "recording_required": bool(row["recording_required"] if "recording_required" in row.keys() else 1),
            "recording_consent_given": (row["recording_consent_given_at"] is not None) if "recording_consent_given_at" in row.keys() else False,
            "recording_consent_version": row["recording_consent_version"] if "recording_consent_version" in row.keys() else "1.0",
            "recording_status": row["recording_status"] if "recording_status" in row.keys() else "Pending",
        },
        "questions": [
            {
                "id": question["id"],
                "order": question["question_order"],
                "question": question["question_snapshot"],
                "options": json.loads(question["options_snapshot"]),
            }
            for question in questions
        ],
    }


def insert_proctoring_event(
    db: sqlite3.Connection,
    assessment_id: int,
    event_type: str,
    severity: str,
    duration_seconds: int | None = None,
) -> None:
    db.execute(
        """INSERT INTO assessment_proctoring_events
        (assessment_id, event_type, severity, duration_seconds, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (assessment_id, event_type, severity, duration_seconds, now()),
    )


@app.get("/api/assessments/{token}")
def get_public_assessment(token: str) -> dict[str, Any]:
    with connect() as db:
        row, error = public_assessment_for_token(db, token)
        payload = assessment_payload(db, row) if row and not error else None
    if error:
        raise HTTPException(status_code=410 if row else 404, detail=error)
    return payload


@app.post("/api/assessments/{token}/proctoring/start")
def start_assessment_proctoring(
    token: str,
    body: AssessmentProctoringStartBody,
) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row, error = public_assessment_for_token(db, token)
        if error:
            raise HTTPException(status_code=410 if row else 404, detail=error)
        if row["status"] not in {"Sent", "Started"}:
            raise HTTPException(status_code=409, detail="This assessment is not available to start")
        if not body.consent_given or not body.camera_permission_granted or not getattr(body, "recording_consent_given", True):
            insert_proctoring_event(db, row["id"], "camera_denied", "info")
            return {"started": False, "required": bool(row["proctoring_required"])}
        consented_at = now()
        rec_version = getattr(body, "recording_consent_version", "1.0") or "1.0"
        db.execute(
            """UPDATE assessments SET proctoring_consent_at = COALESCE(proctoring_consent_at, ?),
            camera_permission_granted_at = COALESCE(camera_permission_granted_at, ?),
            recording_consent_given_at = COALESCE(recording_consent_given_at, ?),
            recording_consent_version = COALESCE(recording_consent_version, ?)
            WHERE id = ?""",
            (consented_at, consented_at, consented_at, rec_version, row["id"]),
        )
        insert_proctoring_event(db, row["id"], "camera_granted", "info")
    return {"started": True, "required": bool(row["proctoring_required"])}


@app.post("/api/assessments/{token}/proctoring/events")
def record_assessment_proctoring_event(
    token: str,
    body: AssessmentProctoringEventBody,
) -> dict[str, Any]:
    warning_events = {
        "camera_denied", "face_missing", "multiple_faces", "tab_hidden",
        "window_blur", "camera_stopped",
    }
    if body.event_type == "face_missing" and (
        body.duration_seconds is None
        or body.duration_seconds < PROCTORING_FACE_MISSING_SECONDS
    ):
        raise HTTPException(
            status_code=422,
            detail=f"A face-missing warning requires at least {PROCTORING_FACE_MISSING_SECONDS} seconds.",
        )
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row, error = public_assessment_for_token(db, token)
        if error:
            raise HTTPException(status_code=410 if row else 404, detail=error)
        if body.event_type == "camera_denied" and row["status"] == "Sent":
            severity = "info"
        elif row["status"] == "Started":
            severity = "warning" if body.event_type in warning_events else "info"
        else:
            raise HTTPException(status_code=409, detail="Proctoring events require an active assessment")
        recent_events = db.execute(
            """SELECT COUNT(*) FROM assessment_proctoring_events
            WHERE assessment_id = ? AND created_at >= ?""",
            (row["id"], (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()),
        ).fetchone()[0]
        if recent_events >= 60:
            raise HTTPException(status_code=429, detail="Too many proctoring events; try again shortly")
        insert_proctoring_event(db, row["id"], body.event_type, severity, body.duration_seconds)
        if severity == "warning":
            db.execute(
                """UPDATE assessments SET proctoring_warning_count = proctoring_warning_count + 1,
                proctoring_flagged_at = CASE
                    WHEN proctoring_warning_count + 1 >= ? THEN COALESCE(proctoring_flagged_at, ?)
                    ELSE proctoring_flagged_at END
                WHERE id = ?""",
                (PROCTORING_MAX_WARNINGS, now(), row["id"]),
            )
            updated = db.execute(
                """SELECT proctoring_warning_count, proctoring_flagged_at
                FROM assessments WHERE id = ?""",
                (row["id"],),
            ).fetchone()
            if updated["proctoring_flagged_at"] and not row["proctoring_flagged_at"]:
                admins = db.execute(
                    "SELECT id FROM users WHERE role = 'admin' AND active = 1"
                ).fetchall()
                for admin in admins:
                    add_notification(
                        db, admin["id"], "assessment_proctoring_flagged",
                        "Assessment flagged for integrity review",
                        f"An assessment for {row['title']} reached the proctoring warning threshold.",
                        application_id=row["application_id"], assessment_id=row["id"],
                    )
        else:
            updated = db.execute(
                """SELECT proctoring_warning_count, proctoring_flagged_at
                FROM assessments WHERE id = ?""",
                (row["id"],),
            ).fetchone()
    return {
        "warning_count": updated["proctoring_warning_count"],
        "max_warnings": PROCTORING_MAX_WARNINGS,
        "flagged": updated["proctoring_flagged_at"] is not None,
        "auto_submit": PROCTORING_AUTO_SUBMIT,
        "should_auto_submit": PROCTORING_AUTO_SUBMIT
        and updated["proctoring_flagged_at"] is not None
        and row["proctoring_flagged_at"] is None,
    }


@app.get("/api/assessments/{token}/proctoring/status")
def get_assessment_proctoring_status(token: str) -> dict[str, Any]:
    with connect() as db:
        row, error = public_assessment_for_token(db, token)
        if error:
            raise HTTPException(status_code=410 if row else 404, detail=error)
    return {
        "required": bool(row["proctoring_required"]),
        "warning_count": row["proctoring_warning_count"],
        "max_warnings": PROCTORING_MAX_WARNINGS,
        "face_missing_warning_seconds": PROCTORING_FACE_MISSING_SECONDS,
        "flagged": row["proctoring_flagged_at"] is not None,
        "auto_submit": PROCTORING_AUTO_SUBMIT,
    }


@app.post("/api/assessments/{token}/start")
def start_public_assessment(token: str) -> dict[str, Any]:
    error = None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row, error = public_assessment_for_token(db, token)
        rec_req = bool(row["recording_required"]) if "recording_required" in row.keys() else False
        rec_consented = (row["recording_consent_given_at"] is not None) if "recording_consent_given_at" in row.keys() else False
        if row and not error and row["proctoring_required"] and (
            row["proctoring_consent_at"] is None
            or row["camera_permission_granted_at"] is None
            or (rec_req and not rec_consented)
        ):
            error = "Provide consent and enable camera access before starting this assessment"
        if row and not error and row["status"] == "Sent":
            started_at = datetime.now(timezone.utc)
            deadline = started_at.timestamp() + row["duration_minutes"] * 60
            db.execute(
                """UPDATE assessments SET status = 'Started', starts_at = ?, expires_at = ?
                , proctoring_started_at = ?
                WHERE id = ? AND status = 'Sent'""",
                (
                    started_at.isoformat(),
                    datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
                    started_at.isoformat(),
                    row["id"],
                ),
            )
            insert_proctoring_event(db, row["id"], "assessment_started", "info")
            row = db.execute(
                """SELECT x.*, j.title, j.company, a.status AS application_status
                FROM assessments x JOIN jobs j ON j.id = x.job_id
                JOIN applications a ON a.id = x.application_id WHERE x.id = ?""",
                (row["id"],),
            ).fetchone()
            application = db.execute(
                """SELECT a.id, a.candidate_id, a.status, j.recruiter_id, j.title
                FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?""",
                (row["application_id"],),
            ).fetchone()
            if application and application["status"] == "Assessment Sent":
                db.execute(
                    "UPDATE applications SET status = 'Assessment Started' WHERE id = ?",
                    (application["id"],),
                )
                record_application_status(
                    db, application["id"], "Assessment Started", {"id": None, "role": "candidate"}
                )
                notify_user(
                    db, application["recruiter_id"], "assessment_started", "Candidate started an assessment",
                    f"A candidate started the assessment for {application['title']}.",
                    application_id=application["id"], assessment_id=row["id"],
                )
                notify_user(
                    db, application["candidate_id"], "assessment_started",
                    "Assessment started", f"You started the assessment for {application['title']}.",
                    application_id=application["id"], assessment_id=row["id"],
                )
        payload = assessment_payload(db, row) if row and not error else None
    if error:
        status_code = 409 if "consent" in error.lower() or "camera" in error.lower() else 410 if row else 404
        raise HTTPException(status_code=status_code, detail=error)
    return payload


@app.post("/api/assessments/{token}/submit")
def submit_public_assessment(token: str, body: AssessmentSubmitBody) -> dict[str, Any]:
    error = None
    response = None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row, error = public_assessment_for_token(db, token, check_deadline=False)
        if row and not error and row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            if row["status"] == "Sent":
                db.execute(
                    "UPDATE assessments SET status = 'Expired', token_hash = NULL WHERE id = ?",
                    (row["id"],),
                )
                error = "This assessment has expired"
        if row and not error and row["status"] == "Sent":
            error = "Start the assessment before submitting answers"
        if row and not error:
            if row["starts_at"] is None:
                error = "Start the assessment before submitting answers"
            else:
                questions = db.execute(
                    "SELECT id, correct_index FROM assessment_questions WHERE assessment_id = ?",
                    (row["id"],),
                ).fetchall()
                known_ids = {question["id"] for question in questions}
                if any(question_id not in known_ids or answer not in range(4) for question_id, answer in body.answers.items()):
                    error = "Answers contain an invalid question or option"
                else:
                    score = 0
                    submitted_at = now()
                    for question in questions:
                        selected = body.answers.get(question["id"])
                        correct = selected == question["correct_index"]
                        score += int(correct)
                        db.execute(
                            """UPDATE assessment_questions SET selected_answer = ?, is_correct = ?,
                            answered_at = ? WHERE id = ?""",
                            (selected, int(correct), submitted_at if selected is not None else None, question["id"]),
                        )
                    score_percentage = score * 100.0 / len(questions) if questions else 0.0
                    result = "Passed" if score_percentage >= row["pass_percentage"] else "Failed"
                    score_percentage = round(score_percentage, 2)
                    starts_at = datetime.fromisoformat(row["starts_at"])
                    submitted_datetime = datetime.fromisoformat(submitted_at)
                    time_taken_seconds = max(0, int((submitted_datetime - starts_at).total_seconds()))
                    status = "Pending Recruiter Decision"
                    changed = db.execute(
                        """UPDATE assessments SET status = ?, submitted_at = ?, score = ?,
                        score_percentage = ?, correct_answer_count = ?, incorrect_answer_count = ?,
                        result = ?, result_processed_at = ?, time_taken_seconds = ?,
                        submitted_token_hash = token_hash, token_hash = NULL
                        WHERE id = ? AND status = 'Started' AND result_processed_at IS NULL""",
                        (
                            status, submitted_at, score, score_percentage, score,
                            len(questions) - score, result, submitted_at, time_taken_seconds, row["id"],
                        ),
                    ).rowcount
                    if changed != 1:
                        error = "This assessment has already been submitted"
                    else:
                        application = db.execute(
                            """SELECT a.id, a.candidate_id, a.status, j.recruiter_id, j.title,
                            u.name AS candidate_name
                            FROM applications a JOIN jobs j ON j.id = a.job_id
                            JOIN users u ON u.id = a.candidate_id
                            WHERE a.id = ?""",
                            (row["application_id"],),
                        ).fetchone()
                        if application:
                            if application["status"] in {
                                "Assessment Sent", "Assessment Started", "Assessment Completed",
                            }:
                                if application["status"] != "Assessment Completed":
                                    db.execute(
                                        "UPDATE applications SET status = 'Assessment Completed' WHERE id = ?",
                                        (application["id"],),
                                    )
                                    record_application_status(
                                        db, application["id"], "Assessment Completed",
                                        {"id": None, "role": "candidate"},
                                    )
                            candidate_title = "Assessment submitted"
                            candidate_message = (
                                f"Your assessment for {row['title']} has been submitted successfully. "
                                f"The recruiter will review your results and update your application status."
                            )
                            notify_user(
                                db, application["candidate_id"], "assessment_submitted",
                                candidate_title, candidate_message,
                                application_id=application["id"], assessment_id=row["id"], email=True,
                            )
                            notify_user(
                                db, application["recruiter_id"], "round1_assessment_completed",
                                "Round 1 assessment completed",
                                f"{application['candidate_name']} completed the assessment for "
                                f"{row['title']}. Review results to make a pass/fail decision.",
                                application_id=application["id"], assessment_id=row["id"], email=True,
                            )
                            admins = db.execute(
                                "SELECT id FROM users WHERE role = 'admin' AND active = 1"
                            ).fetchall()
                            for admin in admins:
                                add_notification(
                                    db, admin["id"], "round1_assessment_completed",
                                    "Round 1 assessment completed",
                                    f"{application['candidate_name']} completed Round 1 for {row['title']}.",
                                    application_id=application["id"], assessment_id=row["id"],
                                )
                        response = {
                            "status": status,
                            "assessment_name": row["assessment_name"],
                            "assessment_round": row["assessment_round"],
                            "job_title": row["title"],
                            "submitted_at": submitted_at,
                            "time_taken_seconds": time_taken_seconds,
                            "message": "Your assessment has been submitted successfully. The recruiter will review your results and update your application status.",
                        }
    if error:
        if "expired" in error.lower() or "available" in error.lower():
            status_code = 410
        elif "invalid question" in error.lower():
            status_code = 422
        else:
            status_code = 409
        raise HTTPException(status_code=status_code, detail=error)
    return response


# ---------------------------------------------------------------------------
# Proctoring Webcam Video Recording & Cloudinary Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/assessments/{token}/recording/start")
def start_assessment_recording(
    token: str,
    body: AssessmentRecordingStartBody,
) -> dict[str, Any]:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row, error = public_assessment_for_token(db, token, allow_submitted=False)
        if error:
            raise HTTPException(status_code=410 if row else 404, detail=error)
        if row["status"] not in {"Sent", "Started"}:
            raise HTTPException(status_code=409, detail="Assessment is not active")
        if not body.consent:
            raise HTTPException(status_code=400, detail="Webcam recording consent is required")

        now_iso = now()
        db.execute(
            """UPDATE assessments SET recording_consent_given_at = ?,
            recording_consent_version = ?, recording_status = 'Pending' WHERE id = ?""",
            (now_iso, body.consent_version, row["id"]),
        )
        existing = db.execute(
            "SELECT id FROM assessment_recordings WHERE assessment_id = ?",
            (row["id"],),
        ).fetchone()
        if existing:
            rec_id = existing["id"]
            db.execute(
                """UPDATE assessment_recordings SET recording_status = 'Pending',
                started_at = ?, updated_at = ? WHERE id = ?""",
                (now_iso, now_iso, rec_id),
            )
        else:
            cur = db.execute(
                """INSERT INTO assessment_recordings
                (assessment_id, application_id, candidate_id, recording_status, started_at, created_at, updated_at)
                VALUES (?, ?, ?, 'Pending', ?, ?, ?)""",
                (row["id"], row["application_id"], row["candidate_id"], now_iso, now_iso, now_iso),
            )
            rec_id = cur.lastrowid

    return {"recording_id": rec_id, "status": "Pending", "started_at": now_iso}


@app.post("/api/assessments/{token}/recording/chunk")
async def upload_assessment_recording_chunk(
    token: str,
    chunk_index: int = 0,
    chunk: UploadFile = File(...),
) -> dict[str, Any]:
    with connect() as db:
        row, error = public_assessment_for_token(db, token, allow_submitted=True)
        if error:
            raise HTTPException(status_code=410 if row else 404, detail=error)
        if row["status"] not in {"Started", "Completed"}:
            raise HTTPException(status_code=409, detail="Assessment is not in progress")

    chunk_data = await chunk.read()
    max_chunk_bytes = 15 * 1024 * 1024
    if len(chunk_data) > max_chunk_bytes:
        raise HTTPException(status_code=413, detail="Chunk exceeds maximum allowed size")

    chunk_dir = RECORDINGS_STORAGE_DIR / f"temp_{row['id']}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_file = chunk_dir / f"chunk_{chunk_index:05d}.part"
    chunk_file.write_bytes(chunk_data)

    with connect() as db:
        db.execute("UPDATE assessments SET recording_status = 'Uploading' WHERE id = ?", (row["id"],))
        db.execute(
            """UPDATE assessment_recordings SET recording_status = 'Uploading', updated_at = ?
            WHERE assessment_id = ?""",
            (now(), row["id"]),
        )

    return {"chunk_index": chunk_index, "bytes_received": len(chunk_data), "status": "Uploading"}


@app.post("/api/assessments/{token}/recording/complete")
async def complete_assessment_recording(
    token: str,
    duration_seconds: int = 0,
    video: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    with connect() as db:
        row, error = public_assessment_for_token(db, token, allow_submitted=True)
        if error:
            raise HTTPException(status_code=410 if row else 404, detail=error)

    file_bytes: bytes = b""
    if video is not None:
        file_bytes = await video.read()
    else:
        chunk_dir = RECORDINGS_STORAGE_DIR / f"temp_{row['id']}"
        if chunk_dir.exists():
            chunks = sorted(chunk_dir.glob("chunk_*.part"))
            if chunks:
                assembled_parts = [ch.read_bytes() for ch in chunks]
                file_bytes = b"".join(assembled_parts)
                for ch in chunks:
                    try:
                        ch.unlink()
                    except Exception:
                        pass
                try:
                    chunk_dir.rmdir()
                except Exception:
                    pass

    max_bytes = SKILLSPHERE_RECORDING_MAX_UPLOAD_MB * 1024 * 1024
    if len(file_bytes) > max_bytes:
        with connect() as db:
            db.execute(
                """UPDATE assessment_recordings SET recording_status = 'Failed',
                upload_error = 'Video file exceeded size limit', updated_at = ? WHERE assessment_id = ?""",
                (now(), row["id"]),
            )
            db.execute("UPDATE assessments SET recording_status = 'Failed' WHERE id = ?", (row["id"],))
        raise HTTPException(status_code=413, detail="Recording exceeded maximum allowed file size")

    if not file_bytes:
        with connect() as db:
            db.execute(
                """UPDATE assessment_recordings SET recording_status = 'Partial',
                upload_error = 'No video data was recorded or received', updated_at = ? WHERE assessment_id = ?""",
                (now(), row["id"]),
            )
            db.execute("UPDATE assessments SET recording_status = 'Partial' WHERE id = ?", (row["id"],))
        return {"recording_status": "Partial", "message": "No video data received"}

    now_iso = now()
    ended_at = now_iso
    timestamp_tag = int(time.time())
    public_id = f"recording_{row['id']}_{row['candidate_id']}_{timestamp_tag}"

    with connect() as db:
        rec_row = db.execute("SELECT id FROM assessment_recordings WHERE assessment_id = ?", (row["id"],)).fetchone()
        if not rec_row:
            db.execute(
                """INSERT INTO assessment_recordings
                (assessment_id, application_id, candidate_id, recording_status, started_at, created_at, updated_at)
                VALUES (?, ?, ?, 'Uploading', ?, ?, ?)""",
                (row["id"], row["application_id"], row["candidate_id"], now_iso, now_iso, now_iso),
            )

    cloudinary_ready = init_cloudinary()
    if cloudinary_ready:
        try:
            upload_result = cloudinary.uploader.upload(
                file_bytes,
                resource_type="video",
                folder=CLOUDINARY_UPLOAD_FOLDER,
                public_id=public_id,
                tags=[
                    f"assessment_{row['id']}",
                    f"application_{row['application_id']}",
                    f"candidate_{row['candidate_id']}",
                    f"job_{row['job_id']}",
                    "proctoring",
                ],
                context={
                    "assessment_id": str(row["id"]),
                    "candidate_id": str(row["candidate_id"]),
                    "application_id": str(row["application_id"]),
                    "type": "proctoring_recording",
                },
            )
            c_public_id = upload_result.get("public_id", public_id)
            c_asset_id = upload_result.get("asset_id", "")
            c_secure_url = upload_result.get("secure_url", "")
            c_duration = int(upload_result.get("duration") or duration_seconds)
            c_bytes = int(upload_result.get("bytes") or len(file_bytes))
            c_format = upload_result.get("format", "webm")

            with connect() as db:
                db.execute(
                    """UPDATE assessment_recordings SET
                    cloudinary_public_id = ?,
                    cloudinary_asset_id = ?,
                    cloudinary_resource_type = 'video',
                    secure_url = ?,
                    duration_seconds = ?,
                    file_size_bytes = ?,
                    format = ?,
                    recording_status = 'Uploaded',
                    upload_error = NULL,
                    ended_at = ?,
                    uploaded_at = ?,
                    updated_at = ?
                    WHERE assessment_id = ?""",
                    (
                        c_public_id, c_asset_id, c_secure_url, c_duration,
                        c_bytes, c_format, ended_at, now_iso, now_iso, row["id"],
                    ),
                )
                db.execute(
                    """UPDATE assessments SET recording_status = 'Uploaded',
                    recording_available_at = ? WHERE id = ?""",
                    (now_iso, row["id"]),
                )
            return {
                "recording_status": "Uploaded",
                "duration_seconds": c_duration,
                "file_size_bytes": c_bytes,
                "format": c_format,
            }
        except Exception as exc:
            logger.error("Cloudinary video upload failed: %s", exc)
            sanitized_err = "Cloudinary upload service failed"
            with connect() as db:
                db.execute(
                    """UPDATE assessment_recordings SET
                    recording_status = 'Failed',
                    upload_error = ?,
                    ended_at = ?,
                    updated_at = ?
                    WHERE assessment_id = ?""",
                    (sanitized_err, ended_at, now_iso, row["id"]),
                )
                db.execute(
                    """UPDATE assessments SET recording_status = 'Failed',
                    recording_failure_reason = ? WHERE id = ?""",
                    (sanitized_err, row["id"]),
                )
                admins = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchall()
                for adm in admins:
                    add_notification(
                        db, adm["id"], "recording_failed",
                        "Assessment recording upload failed",
                        f"Video recording for assessment #{row['id']} failed to upload to Cloudinary.",
                        assessment_id=row["id"],
                    )
            return {"recording_status": "Failed", "error": sanitized_err}
    else:
        # Development / Local fallback mode
        local_path = RECORDINGS_STORAGE_DIR / f"{public_id}.webm"
        local_path.write_bytes(file_bytes)
        file_size = len(file_bytes)
        local_ref = f"/api/admin/recordings/{row['id']}/stream"

        with connect() as db:
            db.execute(
                """UPDATE assessment_recordings SET
                cloudinary_public_id = ?,
                cloudinary_asset_id = ?,
                cloudinary_resource_type = 'video',
                secure_url = ?,
                duration_seconds = ?,
                file_size_bytes = ?,
                format = 'webm',
                recording_status = 'Uploaded',
                upload_error = NULL,
                ended_at = ?,
                uploaded_at = ?,
                updated_at = ?
                WHERE assessment_id = ?""",
                (
                    public_id, "local_asset", local_ref, duration_seconds,
                    file_size, ended_at, now_iso, now_iso, row["id"],
                ),
            )
            db.execute(
                """UPDATE assessments SET recording_status = 'Uploaded',
                recording_available_at = ? WHERE id = ?""",
                (now_iso, row["id"]),
            )
        return {
            "recording_status": "Uploaded",
            "duration_seconds": duration_seconds,
            "file_size_bytes": file_size,
            "format": "webm",
        }


@app.get("/api/admin/assessments/{assessment_id}/recordings")
def get_assessment_recordings(
    assessment_id: int,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, assessment_id, application_id, candidate_id,
            cloudinary_public_id, cloudinary_resource_type, duration_seconds,
            file_size_bytes, format, recording_status, upload_error, started_at,
            ended_at, uploaded_at, created_at, updated_at
            FROM assessment_recordings WHERE assessment_id = ?""",
            (assessment_id,),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/admin/recordings/{recording_id}/playback")
def get_recording_playback(
    recording_id: int,
    request: Request,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        rec = db.execute(
            """SELECT r.*, a.status AS assessment_status, u.name AS candidate_name
            FROM assessment_recordings r
            JOIN assessments a ON a.id = r.assessment_id
            JOIN users u ON u.id = r.candidate_id
            WHERE r.id = ?""",
            (recording_id,),
        ).fetchone()
        if not rec:
            raise HTTPException(status_code=404, detail="Recording not found")

        if rec["recording_status"] != "Uploaded":
            raise HTTPException(
                status_code=409,
                detail=f"Recording is not ready for playback (status: {rec['recording_status']})",
            )

        ip_addr = request.client.host if request.client else "unknown"
        ua = request.headers.get("user-agent", "")[:250]
        db.execute(
            """INSERT INTO recording_access_logs
            (recording_id, admin_id, accessed_at, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?)""",
            (recording_id, user["id"], now(), ip_addr, ua),
        )

        cloudinary_ready = init_cloudinary()
        expires_seconds = 3600
        expires_at = int(time.time()) + expires_seconds

        if cloudinary_ready and rec["cloudinary_asset_id"] != "local_asset" and rec["cloudinary_public_id"]:
            playback_url = cloudinary.utils.cloudinary_url(
                rec["cloudinary_public_id"],
                resource_type="video",
                sign_url=True,
                secure=True,
                expires_at=expires_at,
            )[0]
        else:
            playback_url = f"/api/admin/recordings/{recording_id}/stream?token={encode_token(user)}"

        events = db.execute(
            """SELECT event_type, severity, duration_seconds, created_at
            FROM assessment_proctoring_events WHERE assessment_id = ? ORDER BY created_at ASC""",
            (rec["assessment_id"],),
        ).fetchall()

    return {
        "recording_id": rec["id"],
        "assessment_id": rec["assessment_id"],
        "candidate_name": rec["candidate_name"],
        "playback_url": playback_url,
        "expires_in_seconds": expires_seconds,
        "duration_seconds": rec["duration_seconds"],
        "format": rec["format"],
        "recording_status": rec["recording_status"],
        "events": [dict(e) for e in events],
    }


@app.get("/api/admin/recordings/{recording_id}/stream")
def stream_recording_video(
    recording_id: int,
    token: str = "",
    authorization: str | None = Header(default=None),
) -> FileResponse:
    auth_token = token or (authorization.replace("Bearer ", "") if authorization else "")
    user = decode_token(auth_token)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin authorization required")

    with connect() as db:
        rec = db.execute(
            "SELECT cloudinary_public_id, assessment_id FROM assessment_recordings WHERE id = ?",
            (recording_id,),
        ).fetchone()
        if not rec:
            raise HTTPException(status_code=404, detail="Recording not found")

    public_id = rec["cloudinary_public_id"]
    candidates = list(RECORDINGS_STORAGE_DIR.glob(f"*{public_id}*.webm"))
    if not candidates:
        candidates = list(RECORDINGS_STORAGE_DIR.glob(f"*recording_{rec['assessment_id']}*.webm"))
    if not candidates:
        raise HTTPException(status_code=404, detail="Local video file not found")

    return FileResponse(candidates[0], media_type="video/webm")


@app.get("/api/admin/recordings")
def list_admin_recordings(
    candidate: str = "",
    job_id: str = "",
    recruiter_id: str = "",
    start_date: str = "",
    end_date: str = "",
    status: str = "",
    flagged_only: str = "",
    page: int = 1,
    page_size: int = 50,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        where_clauses = ["1=1"]
        params: list[Any] = []
        if candidate:
            where_clauses.append("(u.name LIKE ? OR u.email LIKE ?)")
            params.extend([f"%{candidate}%", f"%{candidate}%"])
        if job_id:
            where_clauses.append("j.id = ?")
            params.append(job_id)
        if recruiter_id:
            where_clauses.append("j.recruiter_id = ?")
            params.append(recruiter_id)
        if start_date:
            where_clauses.append("r.created_at >= ?")
            params.append(start_date)
        if end_date:
            where_clauses.append("r.created_at <= ?")
            params.append(end_date + "T23:59:59")
        if status:
            where_clauses.append("r.recording_status = ?")
            params.append(status)
        if flagged_only == "true":
            where_clauses.append("a.proctoring_flagged_at IS NOT NULL")
        elif flagged_only == "false":
            where_clauses.append("a.proctoring_flagged_at IS NULL")

        where_sql = " AND ".join(where_clauses)
        rows = db.execute(
            f"""SELECT r.id, r.assessment_id, r.application_id, r.candidate_id,
            r.recording_status, r.duration_seconds, r.file_size_bytes, r.format,
            r.upload_error, r.started_at, r.ended_at, r.uploaded_at, r.created_at,
            u.name AS candidate_name, u.email AS candidate_email,
            j.title AS job_title, j.company AS job_company,
            rec.name AS recruiter_name,
            a.assessment_round, a.assessment_name, a.status AS assessment_status,
            a.proctoring_warning_count, a.proctoring_flagged_at,
            (SELECT COUNT(*) FROM assessment_proctoring_events WHERE assessment_id = a.id AND event_type = 'tab_hidden') AS tab_switch_count,
            (SELECT COUNT(*) FROM assessment_proctoring_events WHERE assessment_id = a.id AND event_type = 'face_missing') AS face_missing_count
            FROM assessment_recordings r
            JOIN assessments a ON a.id = r.assessment_id
            JOIN users u ON u.id = r.candidate_id
            JOIN jobs j ON j.id = a.job_id
            JOIN users rec ON rec.id = j.recruiter_id
            WHERE {where_sql}
            ORDER BY r.created_at DESC LIMIT ? OFFSET ?""",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()

        total_recordings = db.execute("SELECT COUNT(*) FROM assessment_recordings").fetchone()[0]
        uploads_pending = db.execute("SELECT COUNT(*) FROM assessment_recordings WHERE recording_status IN ('Pending', 'Uploading')").fetchone()[0]
        uploads_failed = db.execute("SELECT COUNT(*) FROM assessment_recordings WHERE recording_status = 'Failed'").fetchone()[0]
        flagged_with_rec = db.execute(
            """SELECT COUNT(DISTINCT r.id) FROM assessment_recordings r
            JOIN assessments a ON a.id = r.assessment_id
            WHERE a.proctoring_flagged_at IS NOT NULL""",
        ).fetchone()[0]
        total_proctored = db.execute("SELECT COUNT(*) FROM assessments WHERE proctoring_required = 1").fetchone()[0]

    return {
        "items": [dict(r) for r in rows],
        "metrics": {
            "total_recordings": total_recordings,
            "uploads_pending": uploads_pending,
            "uploads_failed": uploads_failed,
            "flagged_with_recordings": flagged_with_rec,
            "total_proctored_assessments": total_proctored,
            "recent_recordings": len(rows),
        },
    }


@app.post("/api/admin/recordings/retention-cleanup")
def cleanup_retention_recordings(
    include_flagged: bool = False,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=SKILLSPHERE_RECORDING_RETENTION_DAYS)).isoformat()
    with connect() as db:
        flagged_clause = "" if include_flagged else " AND a.proctoring_flagged_at IS NULL"
        expired_recs = db.execute(
            f"""SELECT r.id, r.cloudinary_public_id FROM assessment_recordings r
            JOIN assessments a ON a.id = r.assessment_id
            WHERE r.created_at < ?{flagged_clause}""",
            (cutoff_date,),
        ).fetchall()

        deleted_count = 0
        cloudinary_ready = init_cloudinary()
        for rec in expired_recs:
            pub_id = rec["cloudinary_public_id"]
            if pub_id:
                if cloudinary_ready and not pub_id.startswith("local_"):
                    try:
                        cloudinary.uploader.destroy(pub_id, resource_type="video")
                    except Exception as e:
                        logger.warning("Failed to destroy Cloudinary video %s: %s", pub_id, e)
                local_file = RECORDINGS_STORAGE_DIR / f"{pub_id}.webm"
                if local_file.exists():
                    try:
                        local_file.unlink()
                    except Exception:
                        pass
            db.execute("DELETE FROM assessment_recordings WHERE id = ?", (rec["id"],))
            deleted_count += 1

    return {
        "deleted_count": deleted_count,
        "cleaned_count": deleted_count,
        "cutoff_date": cutoff_date,
        "retention_days": SKILLSPHERE_RECORDING_RETENTION_DAYS,
    }


class LiveMonitoringManager:
    def __init__(self) -> None:
        self.candidate_sockets: dict[int, WebSocket] = {}
        self.candidate_camera_active: dict[int, bool] = {}
        self.recruiter_sockets: dict[int, set[WebSocket]] = {}
        self.active_viewers: dict[int, set[str]] = {}

    async def register_candidate(self, assessment_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        self.candidate_sockets[assessment_id] = websocket
        self.candidate_camera_active[assessment_id] = True
        await self.broadcast_to_recruiters(
            assessment_id,
            {"type": "candidate_status", "connected": True, "camera_connected": True, "status": "Assessment In Progress"},
        )

    async def unregister_candidate(self, assessment_id: int, websocket: WebSocket) -> None:
        if self.candidate_sockets.get(assessment_id) == websocket:
            del self.candidate_sockets[assessment_id]
        self.candidate_camera_active[assessment_id] = False
        await self.broadcast_to_recruiters(
            assessment_id,
            {"type": "candidate_status", "connected": False, "camera_connected": False, "status": "Camera Disconnected"},
        )

    def set_candidate_camera(self, assessment_id: int, active: bool) -> None:
        self.candidate_camera_active[assessment_id] = active

    async def register_recruiter(self, assessment_id: int, websocket: WebSocket, viewer_name: str) -> None:
        await websocket.accept()
        if assessment_id not in self.recruiter_sockets:
            self.recruiter_sockets[assessment_id] = set()
        self.recruiter_sockets[assessment_id].add(websocket)
        if assessment_id not in self.active_viewers:
            self.active_viewers[assessment_id] = set()
        self.active_viewers[assessment_id].add(viewer_name)

        has_cand = assessment_id in self.candidate_sockets
        cam_active = bool(self.candidate_camera_active.get(assessment_id, False))

        await websocket.send_json({
            "type": "candidate_status",
            "connected": has_cand,
            "camera_connected": has_cand and cam_active,
            "status": "Assessment In Progress" if has_cand else "Monitoring Unavailable",
        })

        cand_ws = self.candidate_sockets.get(assessment_id)
        if cand_ws:
            try:
                await cand_ws.send_json({
                    "type": "viewer_joined",
                    "viewer_name": viewer_name,
                    "viewers_count": len(self.active_viewers[assessment_id]),
                })
            except Exception:
                pass

    async def unregister_recruiter(self, assessment_id: int, websocket: WebSocket, viewer_name: str) -> None:
        if assessment_id in self.recruiter_sockets:
            self.recruiter_sockets[assessment_id].discard(websocket)
            if not self.recruiter_sockets[assessment_id]:
                del self.recruiter_sockets[assessment_id]
        if assessment_id in self.active_viewers:
            self.active_viewers[assessment_id].discard(viewer_name)
            if not self.active_viewers[assessment_id]:
                del self.active_viewers[assessment_id]

        cand_ws = self.candidate_sockets.get(assessment_id)
        if cand_ws:
            try:
                viewers_count = len(self.active_viewers.get(assessment_id, set()))
                await cand_ws.send_json({
                    "type": "viewer_left",
                    "viewer_name": viewer_name,
                    "viewers_count": viewers_count,
                })
            except Exception:
                pass

    async def broadcast_to_recruiters(
        self,
        assessment_id: int,
        message: dict[str, Any],
        sender: WebSocket | None = None,
    ) -> None:
        for ws in list(self.recruiter_sockets.get(assessment_id, set())):
            if ws != sender:
                try:
                    await ws.send_json(message)
                except Exception:
                    pass

    async def send_to_candidate(self, assessment_id: int, message: dict[str, Any]) -> None:
        ws = self.candidate_sockets.get(assessment_id)
        if ws:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    def is_candidate_connected(self, assessment_id: int) -> bool:
        return assessment_id in self.candidate_sockets

    def is_camera_active(self, assessment_id: int) -> bool:
        return self.is_candidate_connected(assessment_id) and bool(self.candidate_camera_active.get(assessment_id, False))


live_monitoring_manager = LiveMonitoringManager()


@app.get("/api/recruiter/live-assessments")
def get_recruiter_live_assessments(
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    where = "WHERE x.status IN ('Started', 'Sent', 'Completed', 'Pending Recruiter Decision')"
    params: list[Any] = []
    if user["role"] == "recruiter":
        where += " AND j.recruiter_id = ?"
        params.append(user["id"])

    with connect() as db:
        rows = db.execute(
            f"""SELECT x.id AS assessment_id, x.application_id, x.candidate_id, x.job_id,
            x.status AS assessment_status, x.starts_at, x.expires_at, x.submitted_at,
            x.assessment_name, x.assessment_round, x.technology,
            j.title AS job_title, j.company, j.recruiter_id,
            u.name AS candidate_name, u.email AS candidate_email
            FROM assessments x
            JOIN jobs j ON j.id = x.job_id
            JOIN users u ON u.id = x.candidate_id
            {where}
            ORDER BY CASE WHEN x.status = 'Started' THEN 0 ELSE 1 END,
                     x.starts_at DESC, x.submitted_at DESC
            LIMIT 50""",
            tuple(params),
        ).fetchall()

    items = []
    for row in rows:
        assessment_id = row["assessment_id"]
        is_cand_online = live_monitoring_manager.is_candidate_connected(assessment_id)
        is_cam_active = live_monitoring_manager.is_camera_active(assessment_id)

        if row["assessment_status"] in ("Completed", "Pending Recruiter Decision"):
            assessment_display = "Assessment Submitted"
            camera_display = "Camera Disconnected"
            monitoring_available = False
        elif row["assessment_status"] == "Started":
            assessment_display = "Assessment In Progress"
            if is_cand_online and is_cam_active:
                camera_display = "Camera Connected"
                monitoring_available = True
            elif is_cand_online and not is_cam_active:
                camera_display = "Camera Disconnected"
                monitoring_available = False
            else:
                camera_display = "Monitoring Unavailable"
                monitoring_available = False
        else:
            assessment_display = row["assessment_status"]
            camera_display = "Monitoring Unavailable"
            monitoring_available = False

        items.append({
            "assessment_id": assessment_id,
            "application_id": row["application_id"],
            "candidate_id": row["candidate_id"],
            "candidate_name": row["candidate_name"],
            "candidate_email": row["candidate_email"],
            "job_id": row["job_id"],
            "job_title": row["job_title"],
            "company": row["company"],
            "assessment_name": row["assessment_name"] or "Round 1: Technical MCQ Test",
            "assessment_round": row["assessment_round"] or "Round 1",
            "technology": row["technology"],
            "starts_at": row["starts_at"],
            "expires_at": row["expires_at"],
            "submitted_at": row["submitted_at"],
            "assessment_status": row["assessment_status"],
            "status": assessment_display,
            "camera_status": camera_display,
            "camera_connected": is_cam_active,
            "monitoring_available": monitoring_available,
        })

    return {"items": items}


@app.get("/api/assessments/{assessment_id}/live-status")
def get_assessment_live_status(
    assessment_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            """SELECT x.id, x.status, j.recruiter_id, j.title, u.name AS candidate_name
            FROM assessments x
            JOIN jobs j ON j.id = x.job_id
            JOIN users u ON u.id = x.candidate_id
            WHERE x.id = ?""",
            (assessment_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if user["role"] == "recruiter" and row["recruiter_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Unauthorized to monitor this assessment")

    is_online = live_monitoring_manager.is_candidate_connected(assessment_id)
    cam_active = live_monitoring_manager.is_camera_active(assessment_id)

    if row["status"] in ("Completed", "Pending Recruiter Decision"):
        status_text = "Assessment Submitted"
        cam_text = "Camera Disconnected"
    elif row["status"] == "Started":
        status_text = "Assessment In Progress"
        cam_text = "Camera Connected" if (is_online and cam_active) else ("Camera Disconnected" if is_online else "Monitoring Unavailable")
    else:
        status_text = row["status"]
        cam_text = "Monitoring Unavailable"

    return {
        "assessment_id": assessment_id,
        "candidate_connected": is_online,
        "camera_connected": cam_active,
        "status": status_text,
        "camera_status": cam_text,
        "monitoring_available": (row["status"] == "Started" and is_online and cam_active),
    }


@app.websocket("/ws/live-monitoring/candidate/{token}")
async def candidate_live_monitoring_websocket(websocket: WebSocket, token: str) -> None:
    try:
        with connect() as db:
            row, error = public_assessment_for_token(db, token, allow_submitted=False)
            if error or not row or row["status"] not in {"Started", "Sent"}:
                await websocket.close(code=1008)
                return
            assessment_id = row["id"]
    except Exception:
        await websocket.close(code=1008)
        return

    await live_monitoring_manager.register_candidate(assessment_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "camera_status":
                is_active = bool(data.get("connected", True))
                live_monitoring_manager.set_candidate_camera(assessment_id, is_active)
                await live_monitoring_manager.broadcast_to_recruiters(
                    assessment_id,
                    {
                        "type": "candidate_status",
                        "connected": True,
                        "camera_connected": is_active,
                        "status": "Assessment In Progress" if is_active else "Camera Disconnected",
                    },
                )
            elif msg_type == "webrtc_offer":
                await live_monitoring_manager.broadcast_to_recruiters(
                    assessment_id,
                    {
                        "type": "webrtc_offer",
                        "sdp": data.get("sdp"),
                    },
                )
            elif msg_type == "ice_candidate":
                await live_monitoring_manager.broadcast_to_recruiters(
                    assessment_id,
                    {
                        "type": "ice_candidate",
                        "candidate": data.get("candidate"),
                    },
                )
            elif msg_type in {"assessment_submitted", "assessment_ended"}:
                await live_monitoring_manager.broadcast_to_recruiters(
                    assessment_id,
                    {
                        "type": "candidate_status",
                        "connected": False,
                        "camera_connected": False,
                        "status": "Assessment Submitted",
                    },
                )
                break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await live_monitoring_manager.unregister_candidate(assessment_id, websocket)


@app.websocket("/ws/live-monitoring/recruiter/{assessment_id}")
async def recruiter_live_monitoring_websocket(
    websocket: WebSocket,
    assessment_id: int,
    token: str = "",
) -> None:
    if not token:
        await websocket.close(code=1008)
        return
    try:
        payload = decode_token(token)
        with connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (payload.get("sub"),)).fetchone()
            if not row or not row["active"]:
                await websocket.close(code=1008)
                return
            user = public_user(row)
            if user["role"] not in {"recruiter", "admin"}:
                await websocket.close(code=1008)
                return

            assessment = db.execute(
                """SELECT x.id, x.status, j.recruiter_id, j.title, u.name as candidate_name
                FROM assessments x
                JOIN jobs j ON j.id = x.job_id
                JOIN users u ON u.id = x.candidate_id
                WHERE x.id = ?""",
                (assessment_id,),
            ).fetchone()
            if not assessment:
                await websocket.close(code=1008)
                return
            if user["role"] == "recruiter" and assessment["recruiter_id"] != user["id"]:
                await websocket.close(code=1008)
                return
    except Exception:
        await websocket.close(code=1008)
        return

    viewer_name = user["name"]
    await live_monitoring_manager.register_recruiter(assessment_id, websocket, viewer_name)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "request_stream":
                await live_monitoring_manager.send_to_candidate(
                    assessment_id,
                    {"type": "request_offer", "requester": viewer_name},
                )
            elif msg_type == "webrtc_answer":
                await live_monitoring_manager.send_to_candidate(
                    assessment_id,
                    {"type": "webrtc_answer", "sdp": data.get("sdp")},
                )
            elif msg_type == "ice_candidate":
                await live_monitoring_manager.send_to_candidate(
                    assessment_id,
                    {"type": "ice_candidate", "candidate": data.get("candidate")},
                )
            elif msg_type == "stop_viewing":
                break
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await live_monitoring_manager.unregister_recruiter(assessment_id, websocket, viewer_name)


def interview_access_clause(user: dict[str, Any]) -> tuple[str, tuple[int, ...]]:
    if user["role"] == "admin":
        return "1 = 1", ()
    if user["role"] == "candidate":
        return "a.candidate_id = ?", (user["id"],)
    return "j.recruiter_id = ?", (user["id"],)


def validate_teams_url(teams_url: str) -> str:
    try:
        parsed_url = urlparse(teams_url)
        valid = (
            parsed_url.scheme == "https"
            and parsed_url.hostname in {
                "teams.microsoft.com", "teams.microsoft.us", "teams.live.com", "teams.cloud.microsoft",
            }
            and parsed_url.username is None
            and parsed_url.password is None
            and parsed_url.port in {None, 443}
            and bool(parsed_url.path)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(status_code=422, detail="Provide a valid HTTPS Microsoft Teams meeting link")
    return teams_url


def validate_meeting_url(meeting_url: str) -> str:
    try:
        parsed_url = urlparse(meeting_url)
        port = parsed_url.port
        valid = (
            parsed_url.scheme == "https"
            and bool(parsed_url.hostname)
            and not any(character.isspace() for character in parsed_url.hostname)
            and parsed_url.username is None
            and parsed_url.password is None
            and (port is None or port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(status_code=422, detail="Provide a valid HTTPS meeting URL")
    return meeting_url


def parse_interview_schedule(scheduled_at: str, timezone_name: str, *, require_future: bool = True) -> str:
    try:
        zone = ZoneInfo(timezone_name)
        scheduled = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=zone)
        scheduled = scheduled.astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise HTTPException(status_code=422, detail="Provide a valid interview date, time, and time zone") from error
    if require_future and scheduled <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Interview date and time must be in the future")
    return scheduled.isoformat()


def interview_notification_message(interview: dict[str, Any], action: str) -> str:
    scheduled = datetime.fromisoformat(interview["scheduled_at"].replace("Z", "+00:00"))
    scheduled = scheduled.astimezone(ZoneInfo(interview["timezone"]))
    schedule_label = scheduled.strftime("%d %B %Y, %I:%M %p")
    if action == "scheduled":
        status_message = f"is scheduled for {schedule_label}"
    elif action == "rescheduled":
        status_message = f"has been rescheduled to {schedule_label}"
    elif action == "cancelled":
        status_message = f"scheduled for {schedule_label} has been cancelled"
    elif action == "completed":
        status_message = f"scheduled for {schedule_label} has been marked completed"
    else:
        status_message = f"details were updated; the interview is scheduled for {schedule_label}"
    message = (
        f"Your interview for {interview['title']} at {interview['company']} {status_message} "
        f"({interview['timezone']}).\n"
        f"Duration: {interview['duration_minutes']} minutes.\n"
        f"Meeting platform: {interview['meeting_platform']}."
    )
    if interview["recruiter_message"]:
        message += f"\nRecruiter instructions: {interview['recruiter_message']}"
    if interview["status"] in {"Scheduled", "Rescheduled"} and interview["meeting_url"]:
        message += f"\nSecure meeting link: {interview['meeting_url']}"
    return message


def notify_interview_candidate(
    db: sqlite3.Connection,
    interview: dict[str, Any],
    notification_type: str,
    title: str,
    action: str,
) -> None:
    notify_user(
        db,
        interview["candidate_id"],
        notification_type,
        title,
        interview_notification_message(interview, action),
        application_id=interview["application_id"],
        email=True,
        action_url="/",
    )


def interview_ice_servers() -> list[dict[str, Any]]:
    servers: list[dict[str, Any]] = [
        {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}
    ]
    turn_url = os.environ.get("SKILLSPHERE_TURN_URL", "").strip()
    if turn_url:
        turn_entry: dict[str, Any] = {"urls": [turn_url]}
        turn_user = os.environ.get("SKILLSPHERE_TURN_USERNAME", "").strip()
        turn_cred = os.environ.get("SKILLSPHERE_TURN_CREDENTIAL", "").strip()
        if turn_user:
            turn_entry["username"] = turn_user
        if turn_cred:
            turn_entry["credential"] = turn_cred
        servers.append(turn_entry)
    return servers


def compute_waiting_room_state(interview: Mapping[str, Any], user_role: str) -> tuple[bool, int]:
    if user_role in {"recruiter", "admin"}:
        return True, 0
    early_mins = int(interview.get("early_entry_minutes") if interview.get("early_entry_minutes") is not None else 15)
    try:
        sched_dt = datetime.fromisoformat(str(interview["scheduled_at"]))
        if sched_dt.tzinfo is None:
            sched_dt = sched_dt.replace(tzinfo=timezone.utc)
        open_dt = sched_dt - timedelta(minutes=early_mins)
        now_dt = datetime.now(timezone.utc)
        if now_dt >= open_dt:
            return True, 0
        mins_left = max(1, int((open_dt - now_dt).total_seconds() // 60) + 1)
        return False, mins_left
    except Exception:
        return True, 0


def public_interview(
    interview: sqlite3.Row | dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    result = dict(interview)
    if user["role"] == "candidate" and result["status"] in {"Cancelled", "Completed"}:
        result["meeting_url"] = ""
        result["teams_url"] = ""
    try:
        result["last_execution"] = json.loads(result.pop("last_execution_json", "{}") or "{}")
    except Exception:
        result["last_execution"] = {}
    can_enter, mins_until = compute_waiting_room_state(result, user["role"])
    result["can_enter_room"] = can_enter
    result["minutes_until_open"] = mins_until
    result["ice_servers"] = interview_ice_servers()
    return result


def interview_details(db: sqlite3.Connection, interview_id: int, user: dict[str, Any]) -> sqlite3.Row:
    access_clause, access_values = interview_access_clause(user)
    row = db.execute(
        f"""SELECT i.id, i.application_id, i.recruiter_id, i.scheduled_at, i.teams_url,
            COALESCE(NULLIF(i.meeting_url, ''), i.teams_url) AS meeting_url,
            i.room_token, i.meeting_platform, i.duration_minutes, i.timezone, i.recruiter_message,
            i.problem_title, i.problem_prompt, i.problem_starter_code, i.early_entry_minutes,
            i.status, i.invitation_sent_at, i.candidate_viewed_at,
            i.code, i.language, i.code_submitted, i.submitted_code, i.submitted_language,
            i.code_submitted_at, i.last_execution_json,
            i.created_at, i.updated_at, a.candidate_id, a.job_id,
            candidate.name AS candidate_name, j.title, j.company,
            recruiter.name AS recruiter_name
            FROM interviews i
            JOIN applications a ON a.id = i.application_id
            JOIN users candidate ON candidate.id = a.candidate_id
            JOIN users recruiter ON recruiter.id = i.recruiter_id
            JOIN jobs j ON j.id = a.job_id
            WHERE i.id = ? AND {access_clause}""",
        (interview_id, *access_values),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Interview not found")
    return row


@app.get("/api/interviews")
def list_interviews(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    access_clause, access_values = interview_access_clause(user)
    with connect() as db:
        rows = db.execute(
            f"""SELECT i.id, i.application_id, i.recruiter_id, i.scheduled_at, i.teams_url,
                COALESCE(NULLIF(i.meeting_url, ''), i.teams_url) AS meeting_url,
                i.room_token, i.meeting_platform, i.duration_minutes, i.timezone, i.recruiter_message,
                i.problem_title, i.problem_prompt, i.early_entry_minutes,
                i.status, i.invitation_sent_at, i.candidate_viewed_at,
                i.language, i.code_submitted, i.code_submitted_at,
                i.created_at, i.updated_at, a.candidate_id, a.job_id,
                candidate.name AS candidate_name, j.title, j.company,
                recruiter.name AS recruiter_name
                FROM interviews i
                JOIN applications a ON a.id = i.application_id
                JOIN users candidate ON candidate.id = a.candidate_id
                JOIN users recruiter ON recruiter.id = i.recruiter_id
                JOIN jobs j ON j.id = a.job_id
                WHERE {access_clause}
                ORDER BY i.scheduled_at ASC, i.id ASC""",
            access_values,
        ).fetchall()
    return {"items": [public_interview(row, user) for row in rows]}


def create_interview_invitation(
    db: sqlite3.Connection,
    application_id: int,
    body: InterviewInvitationBody,
    user: dict[str, Any],
) -> dict[str, Any]:
    scheduled_at = parse_interview_schedule(body.scheduled_at, body.timezone)
    room_token = secrets.token_urlsafe(24)
    if body.meeting_platform == "SkillSphere Live":
        meeting_url = f"{PUBLIC_BASE_URL}/?interview_room={room_token}"
    else:
        if not body.meeting_url.strip():
            raise HTTPException(status_code=422, detail="Enter a secure HTTPS meeting link")
        meeting_url = validate_meeting_url(body.meeting_url)
    application = db.execute(
        """SELECT a.id, a.status, a.candidate_id, j.id AS job_id, j.title, j.company,
        j.recruiter_id FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
        (application_id, user["role"], user["id"]),
    ).fetchone()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found for your job listings")
    if application["status"] in {"Rejected", "Withdrawn", "Accepted", "Selected"}:
        raise HTTPException(status_code=409, detail="This application cannot be scheduled for an interview")
    timestamp = now()
    starter_code = body.problem_starter_code or ""
    cursor = db.execute(
        """INSERT INTO interviews
        (application_id, recruiter_id, scheduled_at, teams_url, meeting_platform, meeting_url,
         room_token, duration_minutes, timezone, recruiter_message,
         problem_title, problem_prompt, problem_starter_code, early_entry_minutes, code,
         status, invitation_sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Scheduled', ?, ?, ?)""",
        (
            application["id"], application["recruiter_id"], scheduled_at,
            meeting_url if body.meeting_platform == "Microsoft Teams" else "",
            body.meeting_platform, meeting_url, room_token, body.duration_minutes, body.timezone,
            body.recruiter_message, body.problem_title, body.problem_prompt, starter_code,
            body.early_entry_minutes, starter_code, timestamp, timestamp, timestamp,
        ),
    )
    interview_id = cursor.lastrowid
    if application["status"] != "Interview":
        db.execute("UPDATE applications SET status = 'Interview' WHERE id = ?", (application["id"],))
    record_application_status(
        db, application["id"], "Interview", user,
        f"Interview scheduled for {scheduled_at}.",
    )
    log_audit(db, user["id"], user["role"], "INTERVIEW_CREATED", "interview", interview_id)
    interview = dict(interview_details(db, interview_id, user))
    notify_interview_candidate(
        db, interview, "interview_scheduled", "Interview scheduled", "scheduled"
    )
    return interview


@app.post("/api/applications/{application_id}/interviews", status_code=201)
def schedule_application_interview(
    application_id: int,
    body: InterviewInvitationBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        interview = create_interview_invitation(db, application_id, body, user)
    return public_interview(interview, user)


@app.patch("/api/applications/{application_id}/interviews/{interview_id}")
def update_application_interview(
    application_id: int,
    interview_id: int,
    body: InterviewUpdateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Provide at least one interview detail to update")
    if updates.get("meeting_platform") == "SkillSphere Live":
        updates.pop("meeting_url", None)
    elif "meeting_url" in updates and updates["meeting_url"]:
        updates["meeting_url"] = validate_meeting_url(updates["meeting_url"])
    elif "meeting_url" in updates and not updates["meeting_url"]:
        updates.pop("meeting_url")
    try:
        with connect() as db:
            interview_row = interview_details(db, interview_id, user)
            if interview_row["application_id"] != application_id:
                raise HTTPException(status_code=404, detail="Interview not found for this application")
            if interview_row["status"] in {"Cancelled", "Completed"}:
                raise HTTPException(status_code=409, detail="This interview can no longer be changed")
            if "scheduled_at" in updates:
                timezone_name = updates.get("timezone", interview_row["timezone"])
                updates["scheduled_at"] = parse_interview_schedule(updates["scheduled_at"], timezone_name)
                updates["timezone"] = timezone_name
            elif "timezone" in updates:
                try:
                    ZoneInfo(updates["timezone"])
                except ZoneInfoNotFoundError as error:
                    raise HTTPException(status_code=422, detail="Provide a valid time zone") from error
            rescheduled = (
                "scheduled_at" in updates
                and updates["scheduled_at"] != interview_row["scheduled_at"]
            )
            link_changed = (
                "meeting_url" in updates
                and updates["meeting_url"] != interview_row["meeting_url"]
            )
            if "meeting_url" in updates:
                updates["teams_url"] = (
                    updates["meeting_url"]
                    if updates.get("meeting_platform", interview_row["meeting_platform"]) == "Microsoft Teams"
                    else ""
                )
            elif "meeting_platform" in updates:
                updates["teams_url"] = (
                    interview_row["meeting_url"] if updates["meeting_platform"] == "Microsoft Teams" else ""
                )
            if rescheduled:
                updates["status"] = "Rescheduled"
            updates["invitation_sent_at"] = now()
            updates["updated_at"] = now()
            assignments = ", ".join(f"{column} = ?" for column in updates)
            db.execute(
                f"UPDATE interviews SET {assignments} WHERE id = ?",
                (*updates.values(), interview_id),
            )
            updated = dict(interview_details(db, interview_id, user))
            record_application_status(
                db, application_id, "Interview", user,
                "Interview rescheduled." if rescheduled else "Interview details updated.",
            )
            notification_type = (
                "interview_rescheduled" if rescheduled
                else "interview_link_changed" if link_changed
                else "interview_updated"
            )
            notification_title = (
                "Interview rescheduled" if rescheduled
                else "Interview link changed" if link_changed
                else "Interview details updated"
            )
            notify_interview_candidate(
                db,
                updated,
                notification_type,
                notification_title,
                "rescheduled" if rescheduled else "updated",
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="Interview details could not be saved") from error
    return public_interview(updated, user)


def change_interview_status(
    application_id: int,
    interview_id: int,
    status: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    with connect() as db:
        interview_row = interview_details(db, interview_id, user)
        if interview_row["application_id"] != application_id:
            raise HTTPException(status_code=404, detail="Interview not found for this application")
        if interview_row["status"] in {"Cancelled", "Completed"}:
            raise HTTPException(status_code=409, detail="This interview is already closed")
        db.execute(
            "UPDATE interviews SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), interview_id),
        )
        updated = dict(interview_details(db, interview_id, user))
        record_application_status(
            db, application_id, "Interview", user,
            f"Interview {status.lower()}.",
        )
        notify_interview_candidate(
            db,
            updated,
            f"interview_{status.lower()}",
            f"Interview {status.lower()}",
            status.lower(),
        )
    return public_interview(updated, user)


@app.post("/api/applications/{application_id}/interviews/{interview_id}/cancel")
def cancel_application_interview(
    application_id: int,
    interview_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    return change_interview_status(application_id, interview_id, "Cancelled", user)


@app.post("/api/applications/{application_id}/interviews/{interview_id}/complete")
def complete_application_interview(
    application_id: int,
    interview_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    return change_interview_status(application_id, interview_id, "Completed", user)


@app.get("/api/interviews/upcoming")
def upcoming_interviews(
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT i.id, i.application_id, i.scheduled_at, i.meeting_platform,
            COALESCE(NULLIF(i.meeting_url, ''), i.teams_url) AS meeting_url,
            i.duration_minutes, i.timezone, i.recruiter_message, i.status,
            j.title, j.company
            FROM interviews i JOIN applications a ON a.id = i.application_id
            JOIN jobs j ON j.id = a.job_id
            WHERE a.candidate_id = ? AND i.status IN ('Scheduled', 'Rescheduled')
            AND i.scheduled_at > ? ORDER BY i.scheduled_at ASC, i.id ASC""",
            (user["id"], now()),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/interviews/{interview_id}/viewed")
def mark_interview_viewed(
    interview_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, str]:
    with connect() as db:
        interview_details(db, interview_id, user)
        viewed_at = now()
        db.execute(
            "UPDATE interviews SET candidate_viewed_at = COALESCE(candidate_viewed_at, ?) WHERE id = ?",
            (viewed_at, interview_id),
        )
        result = db.execute(
            "SELECT candidate_viewed_at FROM interviews WHERE id = ?", (interview_id,)
        ).fetchone()
    return {"candidate_viewed_at": result["candidate_viewed_at"]}


@app.post("/api/interviews", status_code=201)
def create_interview(
    body: InterviewBody,
    user: dict[str, Any] = Depends(require_role("recruiter")),
) -> dict[str, Any]:
    try:
        scheduled = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            raise ValueError("Timezone is required")
        scheduled_at = scheduled.astimezone(timezone.utc).isoformat()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Provide an interview date and time with a timezone") from error
    teams_url = validate_teams_url(body.teams_url)
    with connect() as db:
        application = db.execute(
            """SELECT a.id, a.status, a.candidate_id, j.title, j.company, j.recruiter_id
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ? AND j.recruiter_id = ?""",
            (body.application_id, user["id"]),
        ).fetchone()
        if not application:
            raise HTTPException(status_code=404, detail="Application not found for your job listings")
        if application["status"] in {"Rejected", "Withdrawn", "Accepted", "Selected"}:
            raise HTTPException(status_code=409, detail="This application cannot be scheduled for an interview")
        timestamp = now()
        cursor = db.execute(
            """INSERT INTO interviews
            (application_id, recruiter_id, scheduled_at, teams_url, meeting_platform, meeting_url,
             duration_minutes, timezone, status, invitation_sent_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'Microsoft Teams', ?, 60, 'UTC', 'Scheduled', ?, ?, ?)""",
            (application["id"], user["id"], scheduled_at, teams_url, teams_url, timestamp, timestamp, timestamp),
        )
        db.execute("UPDATE applications SET status = 'Interview' WHERE id = ?", (application["id"],))
        record_application_status(
            db, application["id"], "Interview", user, f"Interview scheduled for {scheduled_at}."
        )
        interview = dict(interview_details(db, cursor.lastrowid, user))
        notify_interview_candidate(
            db, interview, "interview_scheduled", "Interview scheduled", "scheduled"
        )
    return interview


@app.get("/api/interviews/{interview_id}")
def get_interview(
    interview_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with connect() as db:
        interview = interview_details(db, interview_id, user)
    return public_interview(interview, user)


class InterviewConnectionManager:
    def __init__(self) -> None:
        self.active_rooms: dict[int, set[WebSocket]] = {}
        self.room_users: dict[int, dict[WebSocket, dict[str, Any]]] = {}

    async def connect(self, interview_id: int, websocket: WebSocket, user: dict[str, Any]) -> None:
        await websocket.accept()
        self.active_rooms.setdefault(interview_id, set()).add(websocket)
        self.room_users.setdefault(interview_id, {})[websocket] = {
            "user_id": user["id"],
            "name": user["name"],
            "role": user["role"],
        }

    def disconnect(self, interview_id: int, websocket: WebSocket) -> dict[str, Any] | None:
        if interview_id in self.active_rooms:
            self.active_rooms[interview_id].discard(websocket)
            if not self.active_rooms[interview_id]:
                del self.active_rooms[interview_id]
        user_info = None
        if interview_id in self.room_users:
            user_info = self.room_users[interview_id].pop(websocket, None)
            if not self.room_users[interview_id]:
                del self.room_users[interview_id]
        return user_info

    def participants(self, interview_id: int) -> list[dict[str, Any]]:
        return list(self.room_users.get(interview_id, {}).values())

    async def broadcast(self, interview_id: int, message: dict[str, Any], sender: WebSocket | None = None) -> None:
        for connection in list(self.active_rooms.get(interview_id, set())):
            if connection != sender:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass


interview_manager = InterviewConnectionManager()


def execute_interview_code(language: str, code: str) -> dict[str, Any]:
    import shutil
    import subprocess
    import sys
    import tempfile
    import time

    start_ts = time.monotonic()
    max_output = 16_384
    timeout_sec = 5

    if language == "sql":
        try:
            conn = sqlite3.connect(":memory:")
            conn.set_authorizer(
                lambda action, *args: sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
                else sqlite3.SQLITE_OK
            )
            conn.executescript(
                """
                CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, department TEXT, salary INTEGER);
                INSERT INTO employees VALUES
                    (1, 'Alice', 'Engineering', 125000),
                    (2, 'Bob', 'Engineering', 115000),
                    (3, 'Carol', 'Product', 105000),
                    (4, 'Dave', 'Sales', 95000);
                """
            )
            cur = conn.cursor()
            statements = [s.strip() for s in code.split(";") if s.strip()]
            out_lines: list[str] = []
            for stmt in statements:
                cur.execute(stmt)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    out_lines.append(" | ".join(cols))
                    out_lines.append("-" * len(out_lines[-1]))
                    for r in cur.fetchmany(100):
                        out_lines.append(" | ".join(str(v) for v in r))
                else:
                    out_lines.append(f"OK ({cur.rowcount} row(s) affected)")
            conn.close()
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            return {
                "stdout": "\n".join(out_lines)[:max_output] or "Query executed successfully (0 rows).",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            return {
                "stdout": "",
                "stderr": str(exc)[:max_output],
                "exit_code": 1,
                "timed_out": False,
                "duration_ms": int((time.monotonic() - start_ts) * 1000),
            }

    with tempfile.TemporaryDirectory(prefix="skillsphere_exec_") as tmpdir:
        safe_env = {"PATH": os.environ.get("PATH", ""), "HOME": tmpdir, "TMP": tmpdir, "TEMP": tmpdir}
        cmd: list[str] = []
        if language == "python":
            prelude = (
                "import builtins\n"
                "_blocked = {'socket', 'urllib', 'http', 'ftplib', 'smtplib', 'subprocess', 'shutil', 'ctypes'}\n"
                "_orig_import = builtins.__import__\n"
                "def _safe_import(name, *args, **kwargs):\n"
                "    if name.split('.')[0] in _blocked:\n"
                "        raise ImportError(f'Module {name} is disabled in sandbox')\n"
                "    return _orig_import(name, *args, **kwargs)\n"
                "builtins.__import__ = _safe_import\n"
            )
            script_path = Path(tmpdir) / "solution.py"
            script_path.write_text(prelude + "\n" + code, encoding="utf-8")
            cmd = [sys.executable, "-I", str(script_path)]
        elif language in {"javascript", "typescript"}:
            node_bin = shutil.which("node")
            if not node_bin:
                return {"stdout": "", "stderr": "Node.js runtime is not installed on this server.", "exit_code": 127, "timed_out": False, "duration_ms": 0}
            script_path = Path(tmpdir) / "solution.js"
            script_path.write_text(code, encoding="utf-8")
            cmd = [node_bin, str(script_path)]
        elif language == "java":
            javac_bin = shutil.which("javac")
            java_bin = shutil.which("java")
            if not javac_bin or not java_bin:
                return {"stdout": "", "stderr": "Java runtime (javac/java) is not installed on this server.", "exit_code": 127, "timed_out": False, "duration_ms": 0}
            src_path = Path(tmpdir) / "Main.java"
            src_path.write_text(code, encoding="utf-8")
            try:
                comp = subprocess.run([javac_bin, str(src_path)], cwd=tmpdir, env=safe_env, capture_output=True, text=True, timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                return {"stdout": "", "stderr": "Compilation timed out after 5s.", "exit_code": 124, "timed_out": True, "duration_ms": 5000}
            if comp.returncode != 0:
                return {"stdout": comp.stdout[:max_output], "stderr": comp.stderr[:max_output], "exit_code": comp.returncode, "timed_out": False, "duration_ms": int((time.monotonic() - start_ts) * 1000)}
            cmd = [java_bin, "-Xmx128m", "-cp", tmpdir, "Main"]
        elif language == "cpp":
            gpp_bin = shutil.which("g++")
            if not gpp_bin:
                return {"stdout": "", "stderr": "C++ compiler (g++) is not installed on this server.", "exit_code": 127, "timed_out": False, "duration_ms": 0}
            src_path = Path(tmpdir) / "solution.cpp"
            bin_path = Path(tmpdir) / ("solution.exe" if os.name == "nt" else "solution")
            src_path.write_text(code, encoding="utf-8")
            try:
                comp = subprocess.run([gpp_bin, "-std=c++17", str(src_path), "-o", str(bin_path)], cwd=tmpdir, env=safe_env, capture_output=True, text=True, timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                return {"stdout": "", "stderr": "Compilation timed out after 5s.", "exit_code": 124, "timed_out": True, "duration_ms": 5000}
            if comp.returncode != 0:
                return {"stdout": comp.stdout[:max_output], "stderr": comp.stderr[:max_output], "exit_code": comp.returncode, "timed_out": False, "duration_ms": int((time.monotonic() - start_ts) * 1000)}
            cmd = [str(bin_path)]

        try:
            proc = subprocess.run(cmd, cwd=tmpdir, env=safe_env, capture_output=True, text=True, timeout=timeout_sec)
            return {
                "stdout": proc.stdout[:max_output],
                "stderr": proc.stderr[:max_output],
                "exit_code": proc.returncode,
                "timed_out": False,
                "duration_ms": int((time.monotonic() - start_ts) * 1000),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "stdout": (exc.stdout or "")[:max_output] if isinstance(exc.stdout, str) else "",
                "stderr": f"Execution timed out after {timeout_sec}s limit.",
                "exit_code": 124,
                "timed_out": True,
                "duration_ms": int((time.monotonic() - start_ts) * 1000),
            }


@app.get("/api/interviews/room/{room_token}")
def get_interview_by_room_token(
    room_token: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT id FROM interviews WHERE room_token = ?", (room_token.strip(),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Interview room not found")
        interview = interview_details(db, row["id"], user)
        log_audit(db, user["id"], user["role"], "INTERVIEW_ROOM_ACCESSED", "interview", row["id"])
    data = public_interview(interview, user)
    data["active_participants"] = interview_manager.participants(row["id"])
    return data


@app.put("/api/interviews/{interview_id}/workspace")
async def update_interview_workspace(
    interview_id: int,
    body: InterviewWorkspaceBody,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if user["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only the candidate can edit code in the interview workspace")
    with connect() as db:
        interview_details(db, interview_id, user)
        updated_at = now()
        db.execute(
            "UPDATE interviews SET code = ?, language = ?, updated_at = ? WHERE id = ?",
            (body.code, body.language, updated_at, interview_id),
        )
        interview = interview_details(db, interview_id, user)
    await interview_manager.broadcast(
        interview_id,
        {"type": "code_change", "code": body.code, "language": body.language, "sender": user["name"]},
    )
    return public_interview(interview, user)


@app.post("/api/interviews/{interview_id}/code/run")
async def run_interview_code(
    interview_id: int,
    body: InterviewCodeRunBody,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if user["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only the candidate can run code in the interview room")
    with connect() as db:
        interview_details(db, interview_id, user)
        result = execute_interview_code(body.language, body.code)
        ts = now()
        db.execute(
            """INSERT INTO interview_code_runs
            (interview_id, user_id, language, code, stdout, stderr, exit_code, timed_out, is_submission, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                interview_id, user["id"], body.language, body.code,
                result["stdout"], result["stderr"], result["exit_code"],
                int(result["timed_out"]), result["duration_ms"], ts,
            ),
        )
        db.execute(
            "UPDATE interviews SET code = ?, language = ?, last_execution_json = ?, updated_at = ? WHERE id = ?",
            (body.code, body.language, json.dumps(result), ts, interview_id),
        )
        log_audit(db, user["id"], user["role"], "INTERVIEW_CODE_RUN", "interview", interview_id)
    await interview_manager.broadcast(
        interview_id,
        {"type": "code_execution", "execution": result, "language": body.language, "sender": user["name"]},
    )
    return result


@app.post("/api/interviews/{interview_id}/code/submit")
async def submit_interview_code(
    interview_id: int,
    body: InterviewCodeRunBody,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if user["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only the candidate can submit code in the interview room")
    with connect() as db:
        interview_details(db, interview_id, user)
        result = execute_interview_code(body.language, body.code)
        ts = now()
        db.execute(
            """INSERT INTO interview_code_runs
            (interview_id, user_id, language, code, stdout, stderr, exit_code, timed_out, is_submission, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                interview_id, user["id"], body.language, body.code,
                result["stdout"], result["stderr"], result["exit_code"],
                int(result["timed_out"]), result["duration_ms"], ts,
            ),
        )
        db.execute(
            """UPDATE interviews
            SET code = ?, language = ?, code_submitted = 1, submitted_code = ?,
                submitted_language = ?, code_submitted_at = ?, last_execution_json = ?, updated_at = ?
            WHERE id = ?""",
            (body.code, body.language, body.code, body.language, ts, json.dumps(result), ts, interview_id),
        )
        log_audit(db, user["id"], user["role"], "INTERVIEW_CODE_SUBMITTED", "interview", interview_id)
    await interview_manager.broadcast(
        interview_id,
        {
            "type": "code_submitted",
            "code": body.code,
            "language": body.language,
            "execution": result,
            "submitted_at": ts,
            "sender": user["name"],
        },
    )
    return {"ok": True, "submitted_at": ts, "execution": result}


@app.websocket("/ws/interviews/{interview_id}")
async def interview_websocket(websocket: WebSocket, interview_id: int, token: str = "") -> None:
    try:
        if not token:
            await websocket.close(code=1008)
            return
        payload = decode_token(token)
        with connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (payload.get("sub"),)).fetchone()
            if not row or not row["active"] or ("email_verified" in row.keys() and not row["email_verified"]):
                await websocket.close(code=1008)
                return
            user = public_user(row)
            interview = interview_details(db, interview_id, user)
            can_enter, mins_until = compute_waiting_room_state(dict(interview), user["role"])
            ts = now()
            db.execute(
                "INSERT INTO interview_participants (interview_id, user_id, role, joined_at, status) VALUES (?, ?, ?, ?, 'Joined')",
                (interview_id, user["id"], user["role"], ts),
            )
            log_audit(db, user["id"], user["role"], "INTERVIEW_JOINED", "interview", interview_id)
    except Exception:
        await websocket.close(code=1008)
        return

    await interview_manager.connect(interview_id, websocket, user)
    try:
        try:
            last_exec = json.loads(interview["last_execution_json"] or "{}")
        except Exception:
            last_exec = {}
        await websocket.send_json({
            "type": "sync",
            "code": interview["code"] or "",
            "language": interview["language"] or "python",
            "code_submitted": bool(interview["code_submitted"]) if "code_submitted" in interview.keys() else False,
            "last_execution": last_exec,
            "waiting_room": not can_enter,
            "minutes_until_open": mins_until,
            "participants": interview_manager.participants(interview_id),
        })
        await interview_manager.broadcast(
            interview_id,
            {
                "type": "participant_joined",
                "user": {"user_id": user["id"], "name": user["name"], "role": user["role"]},
                "participants": interview_manager.participants(interview_id),
            },
            sender=websocket,
        )
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "code_change":
                if user["role"] != "candidate":
                    continue
                new_code = str(data.get("code", ""))[:50_000]
                new_lang = str(data.get("language", "python"))
                if new_lang not in {"python", "javascript", "typescript", "java", "cpp", "sql"}:
                    new_lang = "python"
                with connect() as db:
                    db.execute(
                        "UPDATE interviews SET code = ?, language = ?, updated_at = ? WHERE id = ?",
                        (new_code, new_lang, now(), interview_id),
                    )
                await interview_manager.broadcast(
                    interview_id,
                    {"type": "code_change", "code": new_code, "language": new_lang, "sender": user["name"]},
                    sender=websocket,
                )
            elif msg_type in {"typing", "media_state", "webrtc_offer", "webrtc_answer", "webrtc_ice"}:
                relay = dict(data)
                relay["sender_id"] = user["id"]
                relay["sender_name"] = user["name"]
                relay["sender_role"] = user["role"]
                await interview_manager.broadcast(interview_id, relay, sender=websocket)
    except (WebSocketDisconnect, Exception):
        left_user = interview_manager.disconnect(interview_id, websocket)
        if left_user:
            try:
                with connect() as db:
                    db.execute(
                        """UPDATE interview_participants SET left_at = ?, status = 'Left'
                        WHERE id = (SELECT MAX(id) FROM interview_participants WHERE interview_id = ? AND user_id = ? AND left_at IS NULL)""",
                        (now(), interview_id, left_user["user_id"]),
                    )
                    log_audit(db, left_user["user_id"], left_user["role"], "INTERVIEW_LEFT", "interview", interview_id)
            except Exception:
                pass
            await interview_manager.broadcast(
                interview_id,
                {
                    "type": "participant_left",
                    "user": left_user,
                    "participants": interview_manager.participants(interview_id),
                },
            )


@app.patch("/api/applications/{application_id}/withdraw")
def withdraw_application(
    application_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    withdrawable = {"Applied", "Under Review", "Shortlisted", "Assessment Sent", "Assessment Started"}
    with connect() as db:
        row = db.execute(
            "SELECT id, status FROM applications WHERE id = ? AND candidate_id = ?",
            (application_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Application not found")
        if row["status"] not in withdrawable:
            raise HTTPException(status_code=409, detail="This application can no longer be withdrawn")
        db.execute("UPDATE applications SET status = 'Withdrawn' WHERE id = ?", (application_id,))
        record_application_status(db, application_id, "Withdrawn", user)
        db.execute(
            """UPDATE assessments SET token_hash = NULL,
            status = CASE WHEN status = 'Completed' THEN status ELSE 'Expired' END
            WHERE application_id = ?""",
            (application_id,),
        )
        job = db.execute(
            """SELECT j.title, j.recruiter_id FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?""",
            (application_id,),
        ).fetchone()
        if job:
            notify_user(
                db, job["recruiter_id"], "application_withdrawn", "Candidate withdrew",
                f"A candidate withdrew from {job['title']}.", application_id=application_id,
            )
    return {"id": application_id, "status": "Withdrawn"}


@app.put("/api/applications/{application_id}/status")
@app.patch("/api/applications/{application_id}/status", include_in_schema=False)
def update_application_status(
    application_id: int,
    body: ApplicationStatusBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    allowed = {
        "Applied", "Under Review", "Shortlisted", "Assessment Sent", "Assessment Started",
        "Assessment Completed", "Assessment Expired", "Round 1 Passed", "Round 1 Failed",
        "Interview", "Accepted", "Selected", "Rejected",
    }
    if body.status not in allowed:
        raise HTTPException(status_code=422, detail="Choose a valid application status")
    new_status = "Accepted" if body.status == "Selected" else body.status
    transitions = {
        "Applied": {"Under Review", "Shortlisted", "Interview", "Rejected"},
        "Under Review": {"Shortlisted", "Interview", "Rejected"},
        "Shortlisted": {"Assessment Sent", "Interview", "Accepted", "Rejected"},
        "Assessment Sent": {"Assessment Started", "Assessment Expired", "Interview", "Accepted", "Rejected"},
        "Assessment Started": {"Assessment Completed", "Assessment Expired", "Interview", "Rejected"},
        "Assessment Completed": {"Round 1 Passed", "Round 1 Failed", "Interview", "Accepted", "Rejected"},
        "Assessment Expired": {"Assessment Sent", "Interview", "Accepted", "Rejected"},
        "Round 1 Passed": {"Interview", "Accepted", "Rejected"},
        "Round 1 Failed": {"Accepted", "Rejected"},
        "Interview": {"Accepted", "Rejected"},
        "Accepted": set(),
        "Rejected": set(),
        "Withdrawn": set(),
        "Selected": {"Accepted", "Rejected"},
    }
    with connect() as db:
        row = db.execute(
            """SELECT a.id, a.status, a.candidate_id, u.email AS candidate_email,
            j.title, j.recruiter_id FROM applications a JOIN jobs j ON j.id = a.job_id
            JOIN users u ON u.id = a.candidate_id
            WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
            (application_id, user["role"], user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Application not found for your job listings")
        if new_status != row["status"] and new_status not in transitions.get(row["status"], set()):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot move an application from {row['status']} to {new_status}",
            )
        assessment_stage = {
            "Assessment Sent": "Sent",
            "Assessment Started": "Started",
            "Assessment Completed": "Completed",
            "Assessment Expired": "Expired",
        }
        if new_status != row["status"] and new_status in assessment_stage:
            assessment = db.execute(
                "SELECT status FROM assessments WHERE application_id = ?", (application_id,)
            ).fetchone()
            if not assessment or assessment["status"] != assessment_stage[new_status]:
                raise HTTPException(
                    status_code=409,
                    detail="Assessment pipeline stages are updated automatically by assessment activity",
                )
        if new_status in {"Round 1 Passed", "Round 1 Failed"} and new_status != row["status"]:
            assessment = db.execute(
                "SELECT id, recruiter_decision FROM assessments WHERE application_id = ?", (application_id,)
            ).fetchone()
            decision_val = "Passed" if new_status == "Round 1 Passed" else "Failed"
            if assessment:
                db.execute(
                    """UPDATE assessments SET recruiter_decision = ?, recruiter_decision_at = ?,
                    recruiter_decision_by = ?, status = 'Completed' WHERE id = ?""",
                    (decision_val, now(), user["id"], assessment["id"]),
                )
        db.execute("UPDATE applications SET status = ? WHERE id = ?", (new_status, application_id))
        if new_status != row["status"]:
            record_application_status(db, application_id, new_status, user, body.note)
            notify_user(
                db, row["candidate_id"], "application_status", f"Application status: {new_status}",
                f"Your application for {row['title']} is now {new_status}.",
                application_id=application_id, email=True,
            )
        if new_status in {"Rejected", "Accepted"}:
            db.execute(
                """UPDATE assessments SET token_hash = NULL,
                status = CASE WHEN status IN ('Completed', 'Pending Recruiter Decision') THEN status ELSE 'Expired' END
                WHERE application_id = ?""",
                (application_id,),
            )
    return {"id": row["id"], "status": new_status}


@app.get("/api/applications/{application_id}/resume")
def download_application_resume(
    application_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> FileResponse:
    with connect() as db:
        # Try resume linked directly to this application first
        row = db.execute(
            """SELECT r.storage_path, r.filename, r.content_type FROM applications a
            JOIN jobs j ON j.id = a.job_id JOIN resumes r ON r.id = a.resume_id
            WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
            (application_id, user["role"], user["id"]),
        ).fetchone()
        if not row:
            # Fallback: look up latest resume for the candidate who made the application
            row = db.execute(
                """SELECT r.storage_path, r.filename, r.content_type
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                JOIN resumes r ON r.candidate_id = a.candidate_id
                WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)
                ORDER BY r.uploaded_at DESC LIMIT 1""",
                (application_id, user["role"], user["id"]),
            ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Resume not found for this application")
    path = Path(row["storage_path"]).resolve()
    if not path.is_relative_to(UPLOAD_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Resume file is currently unavailable on the server")
    # Serve inline for PDF so browsers can open it in a new tab; download for DOCX/images
    content_type = row["content_type"] or "application/octet-stream"
    disposition = "inline" if content_type == "application/pdf" else "attachment"
    return FileResponse(
        path,
        filename=row["filename"],
        media_type=content_type,
        headers={"Content-Disposition": f'{disposition}; filename="{row["filename"]}"'},
    )


@app.post("/api/resume/upload", status_code=201)
async def upload_resume(
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    content = await file.read(MAX_RESUME_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="This file is empty")
    if len(content) > MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="Resume must be 10 MB or smaller")
    name = Path(file.filename or "resume").name
    suffix = Path(name).suffix.lower()
    mime_by_suffix = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }
    expected_type = mime_by_suffix.get(suffix)
    if not expected_type:
        raise HTTPException(status_code=415, detail="Use a PDF, DOCX, JPG, JPEG, or PNG resume")
    if expected_type == "application/pdf" and b"%PDF-" not in content[:1024]:
        raise HTTPException(status_code=415, detail="This file does not contain a valid PDF")
    if expected_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" and not content.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=415, detail="This file does not contain a valid DOCX document")
    if expected_type == "image/jpeg" and not content.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=415, detail="This file does not contain a valid JPEG image")
    if expected_type == "image/png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=415, detail="This file does not contain a valid PNG image")
    storage_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    status = "completed"
    processing_error = ""
    try:
        text = await run_in_threadpool(extract_resume_text, content, expected_type)
        if not text:
            status = "needs_review"
            if not os.environ.get("GEMINI_API_KEY") and expected_type in {"image/jpeg", "image/png"}:
                processing_error = "This image could not be read because OCR is not enabled. Please upload a PDF or Word (.docx) document, or configure GEMINI_API_KEY in Render."
            elif not os.environ.get("GEMINI_API_KEY"):
                processing_error = "No readable text found in this file. Please upload a standard text-based PDF or Word (.docx) document, or configure GEMINI_API_KEY for image OCR."
            else:
                processing_error = "No readable text was found. Try a clearer, text-based PDF or Word document."
    except HTTPException:
        raise
    except Exception as error:
        text = ""
        status = "needs_review"
        if error.__class__.__name__ == "TesseractNotFoundError":
            processing_error = "Resume uploaded, but OCR is unavailable. Upload a text-based PDF or Word document."
        elif expected_type == "application/pdf":
            processing_error = f"Resume uploaded, but text could not be extracted: {error.__class__.__name__}. Try a text-based PDF or Word document."
        else:
            processing_error = f"Resume uploaded, but text could not be extracted: {error.__class__.__name__}. Try a clearer file or Word document."
    storage_path.write_bytes(content)
    profile = detect_profile(text)
    if not profile.get("name") and user.get("name"):
        profile["name"] = user["name"]
    if not profile.get("email") and user.get("email"):
        profile["email"] = user["email"]
    try:
        with connect() as db:
            old = db.execute("SELECT storage_path FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
            db.execute(
                """INSERT INTO resumes
                (candidate_id, filename, storage_path, content_type, extracted_text, parsed_profile_json, processing_status, processing_error, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                filename = excluded.filename, storage_path = excluded.storage_path, content_type = excluded.content_type,
                extracted_text = excluded.extracted_text, parsed_profile_json = excluded.parsed_profile_json,
                processing_status = excluded.processing_status, processing_error = excluded.processing_error,
                uploaded_at = excluded.uploaded_at""",
                (user["id"], name, str(storage_path), expected_type, text, json.dumps(profile), status, processing_error, now()),
            )
            row = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
        if old:
            old_path = Path(old["storage_path"])
            if old_path != storage_path and old_path.is_file():
                old_path.unlink()
    except Exception:
        storage_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return public_resume(row)


@app.get("/api/resume")
def get_resume(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
    if not row:
        return None
    return public_resume(row)


@app.get("/api/resume/profile")
def resume_profile(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No resume found")
    return public_resume(row)


@app.get("/api/resume/download")
def download_resume(user: dict[str, Any] = Depends(require_role("candidate"))) -> FileResponse:
    with connect() as db:
        row = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No resume found")
    path = Path(row["storage_path"]).resolve()
    if not path.is_relative_to(UPLOAD_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Resume file is unavailable")
    return FileResponse(path, filename=row["filename"], media_type=row["content_type"])


@app.get("/api/matches/{job_id}")
def match_job(job_id: int, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id = ? AND status = 'Open'", (job_id,)).fetchone()
        resume = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
        saved_profile = db.execute("SELECT profile_json FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="This role is no longer available")
    if not resume:
        raise HTTPException(status_code=409, detail="Upload a resume to see your match")
    return calculate_match(job, resume, saved_profile["profile_json"])


@app.post("/api/resume/analyze")
def analyze_resume(
    body: MatchBody,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    return match_job(body.job_id, user)


# =============================================================================
# CANDIDATE AI CAREER ASSISTANT & MOCK INTERVIEW ENDPOINTS
# =============================================================================

@app.get("/api/candidate/ai/sessions")
def list_candidate_ai_sessions(
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT s.id, s.session_title, s.created_at, s.updated_at,
                      COUNT(m.id) as message_count
               FROM chat_sessions s
               LEFT JOIN chat_messages m ON m.session_id = s.id
               WHERE s.candidate_id = ?
               GROUP BY s.id
               ORDER BY s.updated_at DESC""",
            (user["id"],),
        ).fetchall()
        resume = db.execute("SELECT filename, extracted_text FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
    has_resume = bool(resume and resume["extracted_text"] and len(resume["extracted_text"].strip()) >= 20)
    resume_name = resume["filename"] if resume else ""
    return {
        "sessions": [dict(r) for r in rows],
        "has_resume": has_resume,
        "resume_name": resume_name,
    }


@app.post("/api/candidate/ai/sessions", status_code=201)
def create_candidate_ai_session(
    body: AIChatSessionCreateBody,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    title = body.title.strip() or "New Conversation"
    timestamp = now()
    with connect() as db:
        cur = db.execute(
            "INSERT INTO chat_sessions (candidate_id, session_title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user["id"], title, timestamp, timestamp),
        )
        session_id = cur.lastrowid
        row = db.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row)


@app.get("/api/candidate/ai/sessions/{session_id}")
def get_candidate_ai_session(
    session_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        session = db.execute(
            "SELECT * FROM chat_sessions WHERE id = ? AND candidate_id = ?",
            (session_id, user["id"]),
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")
        messages = db.execute(
            "SELECT id, sender, message, created_at FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session),
        "messages": [dict(m) for m in messages],
    }


@app.delete("/api/candidate/ai/sessions/{session_id}")
def delete_candidate_ai_session(
    session_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        session = db.execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND candidate_id = ?",
            (session_id, user["id"]),
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")
        db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    return {"ok": True, "detail": "Session deleted"}


@app.post("/api/chat")
@limiter.limit("60/minute")
def chat_with_career_assistant(
    request: Request,
    body: ChatMessageRequest,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    user_msg = body.message.strip()
    if not user_msg:
        raise HTTPException(status_code=422, detail="Message cannot be empty")

    timestamp = now()
    with connect() as db:
        session_id = body.session_id
        last_intent = ""
        if session_id:
            session = db.execute(
                "SELECT * FROM chat_sessions WHERE id = ? AND candidate_id = ?",
                (session_id, user["id"]),
            ).fetchone()
            if not session:
                raise HTTPException(status_code=404, detail="Chat session not found")
            last_intent = session["last_intent"] if "last_intent" in session.keys() else ""
            if not last_intent:
                last_msg = db.execute(
                    "SELECT intent FROM chat_messages WHERE session_id = ? AND sender = 'assistant' AND intent != '' ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if last_msg:
                    last_intent = last_msg["intent"]
        else:
            session_title = user_msg[:35] + ("…" if len(user_msg) > 35 else "")
            cur = db.execute(
                "INSERT INTO chat_sessions (candidate_id, session_title, created_at, updated_at, last_intent) VALUES (?, ?, ?, ?, '')",
                (user["id"], session_title, timestamp, timestamp),
            )
            session_id = cur.lastrowid

        resume_row = db.execute(
            "SELECT filename, extracted_text, parsed_profile_json FROM resumes WHERE candidate_id = ?",
            (user["id"],),
        ).fetchone()

        db.execute(
            "INSERT INTO chat_messages (session_id, sender, message, created_at, intent, confidence) VALUES (?, 'candidate', ?, ?, '', 1.0)",
            (session_id, user_msg, timestamp),
        )

    # Process message with deterministic rule-based engine
    result = career_chatbot.process_message(
        message=user_msg,
        resume_row=resume_row,
        last_intent=last_intent,
        candidate_name=user.get("name") or "Candidate",
    )

    bot_msg = result["response"]
    intent = result["intent"]
    confidence = float(result.get("confidence", 1.0))
    suggested_follow_ups = result.get("suggested_next", [])
    now_ts = now()

    with connect() as db:
        db.execute(
            "INSERT INTO chat_messages (session_id, sender, message, created_at, intent, confidence) VALUES (?, 'assistant', ?, ?, ?, ?)",
            (session_id, bot_msg, now_ts, intent, confidence),
        )
        db.execute(
            "UPDATE chat_sessions SET updated_at = ?, last_intent = ? WHERE id = ?",
            (now_ts, intent, session_id),
        )

    return {
        "message": bot_msg,
        "intent": intent,
        "confidence": confidence,
        "session_id": session_id,
        "created_at": now_ts,
        "suggested_follow_ups": suggested_follow_ups,
    }


@app.post("/api/candidate/ai/chat")
@limiter.limit("60/minute")
def candidate_ai_chat(
    request: Request,
    body: AIChatMessageBody,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    return chat_with_career_assistant(request=request, body=body, user=user)


@app.get("/api/chat/faqs")
def get_chat_faqs(
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    return {
        "categories": career_chatbot.categories,
        "total_faqs": len(career_chatbot.entries),
    }


@app.get("/api/chat/history/{session_id}")
def get_chat_history(
    session_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        session = db.execute(
            "SELECT * FROM chat_sessions WHERE id = ? AND candidate_id = ?",
            (session_id, user["id"]),
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")
        messages = db.execute(
            "SELECT id, sender, message, created_at, intent, confidence FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session),
        "messages": [dict(m) for m in messages],
    }


@app.delete("/api/chat/history/{session_id}")
def clear_chat_history(
    session_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        session = db.execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND candidate_id = ?",
            (session_id, user["id"]),
        ).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")
        db.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        db.execute("UPDATE chat_sessions SET last_intent = '', updated_at = ? WHERE id = ?", (now(), session_id))
    return {"ok": True, "detail": "Conversation history cleared"}


@app.get("/api/chat/health")
def get_chat_health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "engine": "rule-based",
        "version": "1.0.0",
        "total_intents": len(career_chatbot.entries),
        "external_ai_required": False,
    }


@app.post("/api/candidate/ai/mock-interview/start", status_code=201)
@limiter.limit("15/minute")
def start_candidate_mock_interview(
    request: Request,
    body: StartMockInterviewBody,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        resume_row = db.execute(
            "SELECT filename, extracted_text FROM resumes WHERE candidate_id = ?",
            (user["id"],),
        ).fetchone()

    resume_text = (resume_row["extracted_text"] or "").strip() if resume_row else ""
    if body.interview_type == "Resume-Based" and len(resume_text) < 20:
        raise HTTPException(
            status_code=400,
            detail="To start a Resume-Based mock interview, please upload your resume in the 'My profile' section first, or select Technical or HR interview type.",
        )

    # Generate Question 1
    q1 = generate_mock_interview_question(
        job_role=body.job_role,
        interview_type=body.interview_type,
        difficulty=body.difficulty,
        question_index=1,
        total_questions=body.total_questions,
        resume_text=resume_text,
    )

    timestamp = now()
    with connect() as db:
        cur = db.execute(
            """INSERT INTO mock_interviews
            (candidate_id, interview_type, job_role, difficulty, total_questions, current_question_index, status, final_feedback_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, 'In Progress', '{}', ?, ?)""",
            (user["id"], body.interview_type, body.job_role, body.difficulty, body.total_questions, timestamp, timestamp),
        )
        mock_id = cur.lastrowid
        db.execute(
            """INSERT INTO mock_interview_exchanges
            (mock_interview_id, question_index, question, answer, feedback, created_at)
            VALUES (?, 1, ?, '', '', ?)""",
            (mock_id, q1, timestamp),
        )
        interview_row = db.execute("SELECT * FROM mock_interviews WHERE id = ?", (mock_id,)).fetchone()

    return {
        "interview": dict(interview_row),
        "current_question": {
            "question_index": 1,
            "total_questions": body.total_questions,
            "question": q1,
        },
    }


@app.post("/api/candidate/ai/mock-interview/answer")
@limiter.limit("20/minute")
def submit_candidate_mock_interview_answer(
    request: Request,
    body: SubmitMockAnswerBody,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        interview = db.execute(
            "SELECT * FROM mock_interviews WHERE id = ? AND candidate_id = ?",
            (body.mock_interview_id, user["id"]),
        ).fetchone()
        if not interview:
            raise HTTPException(status_code=404, detail="Mock interview session not found")
        if interview["status"] != "In Progress":
            raise HTTPException(status_code=400, detail="This mock interview is already completed or closed")

        exchange = db.execute(
            "SELECT * FROM mock_interview_exchanges WHERE mock_interview_id = ? AND question_index = ?",
            (body.mock_interview_id, body.question_index),
        ).fetchone()
        if not exchange:
            raise HTTPException(status_code=404, detail=f"Question {body.question_index} not found in this session")

        resume_row = db.execute(
            "SELECT extracted_text FROM resumes WHERE candidate_id = ?",
            (user["id"],),
        ).fetchone()

    # Evaluate current answer
    eval_result = evaluate_mock_interview_answer(
        job_role=interview["job_role"],
        interview_type=interview["interview_type"],
        difficulty=interview["difficulty"],
        question=exchange["question"],
        answer=body.answer.strip(),
    )

    # Save answer & feedback in DB
    with connect() as db:
        db.execute(
            """UPDATE mock_interview_exchanges
            SET answer = ?, feedback = ?
            WHERE mock_interview_id = ? AND question_index = ?""",
            (body.answer.strip(), json.dumps(eval_result), body.mock_interview_id, body.question_index),
        )

    # Check if there are more questions
    total_q = interview["total_questions"]
    curr_q = body.question_index

    if curr_q < total_q:
        next_q_index = curr_q + 1
        with connect() as db:
            past_exchanges = [
                dict(r) for r in db.execute(
                    "SELECT question_index, question FROM mock_interview_exchanges WHERE mock_interview_id = ? ORDER BY question_index ASC",
                    (body.mock_interview_id,),
                ).fetchall()
            ]

        resume_text = (resume_row["extracted_text"] or "").strip() if resume_row else ""
        next_question_text = generate_mock_interview_question(
            job_role=interview["job_role"],
            interview_type=interview["interview_type"],
            difficulty=interview["difficulty"],
            question_index=next_q_index,
            total_questions=total_q,
            resume_text=resume_text,
            previous_exchanges=past_exchanges,
        )

        with connect() as db:
            db.execute(
                """INSERT INTO mock_interview_exchanges
                (mock_interview_id, question_index, question, answer, feedback, created_at)
                VALUES (?, ?, ?, '', '', ?)""",
                (body.mock_interview_id, next_q_index, next_question_text, now()),
            )
            db.execute(
                "UPDATE mock_interviews SET current_question_index = ?, updated_at = ? WHERE id = ?",
                (next_q_index, now(), body.mock_interview_id),
            )

        return {
            "is_complete": False,
            "question_index": curr_q,
            "feedback": eval_result,
            "next_question": {
                "question_index": next_q_index,
                "total_questions": total_q,
                "question": next_question_text,
            },
        }
    else:
        # Final Question Answered -> Generate Final Performance Report
        with connect() as db:
            all_exchanges_rows = db.execute(
                "SELECT * FROM mock_interview_exchanges WHERE mock_interview_id = ? ORDER BY question_index ASC",
                (body.mock_interview_id,),
            ).fetchall()

        all_exchanges = []
        for r in all_exchanges_rows:
            d = dict(r)
            try:
                d["feedback_obj"] = json.loads(d["feedback"]) if d["feedback"] else {}
                d["score"] = d["feedback_obj"].get("score", 7)
            except Exception:
                d["score"] = 7
            all_exchanges.append(d)

        final_report = generate_mock_interview_final_report(
            job_role=interview["job_role"],
            interview_type=interview["interview_type"],
            difficulty=interview["difficulty"],
            exchanges=all_exchanges,
        )

        with connect() as db:
            db.execute(
                """UPDATE mock_interviews
                SET status = 'Completed', final_feedback_json = ?, updated_at = ?
                WHERE id = ?""",
                (json.dumps(final_report), now(), body.mock_interview_id),
            )

        return {
            "is_complete": True,
            "question_index": curr_q,
            "feedback": eval_result,
            "final_report": final_report,
        }


@app.get("/api/candidate/ai/mock-interviews")
def list_candidate_mock_interviews(
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, interview_type, job_role, difficulty, total_questions, status,
                      final_feedback_json, created_at, updated_at
               FROM mock_interviews
               WHERE candidate_id = ?
               ORDER BY created_at DESC LIMIT 50""",
            (user["id"],),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["final_feedback"] = json.loads(d["final_feedback_json"]) if d["final_feedback_json"] else {}
        except Exception:
            d["final_feedback"] = {}
        items.append(d)
    return {"items": items}


@app.get("/api/candidate/ai/mock-interviews/{interview_id}")
def get_candidate_mock_interview(
    interview_id: int,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        interview = db.execute(
            "SELECT * FROM mock_interviews WHERE id = ? AND candidate_id = ?",
            (interview_id, user["id"]),
        ).fetchone()
        if not interview:
            raise HTTPException(status_code=404, detail="Mock interview not found")
        exchanges = db.execute(
            "SELECT * FROM mock_interview_exchanges WHERE mock_interview_id = ? ORDER BY question_index ASC",
            (interview_id,),
        ).fetchall()

    d = dict(interview)
    try:
        d["final_feedback"] = json.loads(d["final_feedback_json"]) if d["final_feedback_json"] else {}
    except Exception:
        d["final_feedback"] = {}

    parsed_exchanges = []
    for e in exchanges:
        item = dict(e)
        try:
            item["feedback_obj"] = json.loads(item["feedback"]) if item["feedback"] else {}
        except Exception:
            item["feedback_obj"] = {"feedback": item["feedback"]}
        parsed_exchanges.append(item)

    return {
        "interview": d,
        "exchanges": parsed_exchanges,
    }


@app.get("/api/recruiter/applicants")
def recruiter_applicants(user: dict[str, Any] = Depends(require_role("recruiter"))) -> dict[str, Any]:
    return list_applications(user)


@app.patch("/api/profile")
def update_profile(body: ProfileBody, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    profile = dict(body.profile)
    profile.pop("email", None)
    if len(json.dumps(profile, ensure_ascii=False)) > 8_000:
        raise HTTPException(status_code=422, detail="Profile details must be 8 KB or smaller")
    with connect() as db:
        db.execute("UPDATE users SET name = ?, profile_json = ? WHERE id = ?", (body.name.strip(), json.dumps(profile), user["id"]))
        row = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return public_user(row)


@app.get("/api/admin/jobs")
def admin_jobs(user: dict[str, Any] = Depends(require_role("admin"))) -> dict[str, Any]:
    del user
    with connect() as db:
        rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC, id ASC LIMIT 500").fetchall()
    return {"items": [public_job(row) for row in rows]}


@app.patch("/api/admin/jobs/{job_id}/status")
def update_job_status(
    job_id: int,
    body: JobStatusBody,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    del user
    if body.status not in {"Open", "Closed"}:
        raise HTTPException(status_code=422, detail="Choose Open or Closed")
    with connect() as db:
        result = db.execute("UPDATE jobs SET status = ? WHERE id = ?", (body.status, job_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job listing not found")
    return {"id": job_id, "status": body.status}


@app.patch("/api/admin/users/{user_id}/status")
def update_user_status(
    user_id: int,
    body: UserStatusBody,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    if user_id == user["id"] and not body.active:
        raise HTTPException(status_code=409, detail="You cannot disable your own admin account")
    with connect() as db:
        result = db.execute("UPDATE users SET active = ? WHERE id = ?", (int(body.active), user_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
    return {"id": user_id, "active": body.active}


@app.get("/api/admin/analytics")
def analytics(
    report_range: str = "this_week",
    start_date: str | None = None,
    end_date: str | None = None,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    if report_range == "this_week":
        start, end = monday, monday + timedelta(days=7)
    elif report_range == "last_week":
        start, end = monday - timedelta(days=7), monday
    elif report_range == "this_month":
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        end = next_month
    elif report_range == "last_month":
        this_month_start = today.replace(day=1)
        end = this_month_start
        start = (this_month_start - timedelta(days=1)).replace(day=1)
    elif report_range == "custom":
        try:
            start = date.fromisoformat(start_date or "")
            end = date.fromisoformat(end_date or "") + timedelta(days=1)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Custom reports need valid start_date and end_date values") from error
        if end <= start:
            raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    else:
        raise HTTPException(status_code=422, detail="Choose this_week, last_week, this_month, last_month, or custom")

    range_start = datetime.combine(start, datetime.min.time(), timezone.utc).isoformat()
    range_end = datetime.combine(end, datetime.min.time(), timezone.utc).isoformat()
    with connect() as db:
        counts = {
            "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "candidates": db.execute("SELECT COUNT(*) FROM users WHERE role = 'candidate'").fetchone()[0],
            "recruiters": db.execute("SELECT COUNT(*) FROM users WHERE role = 'recruiter'").fetchone()[0],
            "jobs": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "open_jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'Open'").fetchone()[0],
            "applications": db.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
            "resumes": db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0],
            "in_review": db.execute("SELECT COUNT(*) FROM applications WHERE status = 'Under Review'").fetchone()[0],
        }
        applications_in_range = db.execute(
            """SELECT COUNT(*) FROM applications
            WHERE applied_at >= ? AND applied_at < ?""",
            (range_start, range_end),
        ).fetchone()[0]
        assessment_counts = {
            row["status"]: row["amount"]
            for row in db.execute(
                """SELECT status, COUNT(*) AS amount FROM assessments
                WHERE created_at >= ? AND created_at < ? GROUP BY status""",
                (range_start, range_end),
            )
        }
        sent_count = db.execute(
            """SELECT COUNT(*) FROM assessments
            WHERE email_sent_at >= ? AND email_sent_at < ?""",
            (range_start, range_end),
        ).fetchone()[0]
        sent_completed_count = db.execute(
            """SELECT COUNT(*) FROM assessments
            WHERE email_sent_at >= ? AND email_sent_at < ? AND status = 'Completed'""",
            (range_start, range_end),
        ).fetchone()[0]
        completed_count = db.execute(
            """SELECT COUNT(*) FROM assessments
            WHERE submitted_at >= ? AND submitted_at < ? AND status = 'Completed'""",
            (range_start, range_end),
        ).fetchone()[0]
        average_score = db.execute(
            """SELECT AVG(CAST(score AS REAL) / NULLIF(total_questions, 0) * 100)
            FROM assessments WHERE submitted_at >= ? AND submitted_at < ? AND status = 'Completed'""",
            (range_start, range_end),
        ).fetchone()[0]
        round1_counts = {
            row["result"]: row["amount"]
            for row in db.execute(
                """SELECT result, COUNT(*) AS amount FROM assessments
                WHERE assessment_round = 'Round 1' AND submitted_at >= ? AND submitted_at < ?
                AND result IN ('Passed', 'Failed') GROUP BY result""",
                (range_start, range_end),
            )
        }
        round1_completed = sum(round1_counts.values())
        round1_average = db.execute(
            """SELECT AVG(score_percentage) FROM assessments
            WHERE assessment_round = 'Round 1' AND submitted_at >= ? AND submitted_at < ?
            AND result IN ('Passed', 'Failed')""",
            (range_start, range_end),
        ).fetchone()[0]
        round1_job_results = [
            dict(row) for row in db.execute(
                """SELECT j.id AS job_id, j.title, j.company, x.result, COUNT(*) AS candidates
                FROM assessments x JOIN jobs j ON j.id = x.job_id
                WHERE x.assessment_round = 'Round 1' AND x.submitted_at >= ? AND x.submitted_at < ?
                AND x.result IN ('Passed', 'Failed')
                GROUP BY j.id, x.result ORDER BY j.title, x.result""",
                (range_start, range_end),
            )
        ]
        daily_rows = db.execute(
            """SELECT substr(applied_at, 1, 10) AS day, COUNT(*) AS amount
            FROM applications WHERE applied_at >= ? AND applied_at < ?
            GROUP BY substr(applied_at, 1, 10)""",
            (range_start, range_end),
        ).fetchall()
        daily_counts = {row["day"]: row["amount"] for row in daily_rows}
        day_count = (end - start).days
        daily_trend = [
            {"date": (start + timedelta(days=offset)).isoformat(),
             "applications": daily_counts.get((start + timedelta(days=offset)).isoformat(), 0)}
            for offset in range(day_count)
        ]
        status_counts = {
            row["status"]: row["amount"]
            for row in db.execute(
                """SELECT status, COUNT(*) AS amount FROM applications
                WHERE applied_at >= ? AND applied_at < ? GROUP BY status ORDER BY amount DESC""",
                (range_start, range_end),
            )
        }
        top_jobs = [dict(row) for row in db.execute(
            """SELECT j.id AS job_id, j.title, j.company, COUNT(a.id) AS applications
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.applied_at >= ? AND a.applied_at < ?
            GROUP BY j.id ORDER BY applications DESC, j.title LIMIT 5""",
            (range_start, range_end),
        )]
        top_technologies = [dict(row) for row in db.execute(
            """SELECT technology, COUNT(*) AS assessments
            FROM assessments WHERE created_at >= ? AND created_at < ? AND technology IS NOT NULL
            GROUP BY technology ORDER BY assessments DESC, technology LIMIT 5""",
            (range_start, range_end),
        )]
        weak_tech_rows = db.execute(
            """SELECT technology, COUNT(*) AS failed_count
            FROM assessments WHERE submitted_at >= ? AND submitted_at < ?
            AND result = 'Failed' AND technology IS NOT NULL
            GROUP BY technology ORDER BY failed_count DESC LIMIT 5""",
            (range_start, range_end),
        ).fetchall()
        weak_technologies = [{"technology": r["technology"], "failed_count": r["failed_count"]} for r in weak_tech_rows]

        all_platform_apps = db.execute("SELECT status FROM applications").fetchall()
        total_all_applied = len(all_platform_apps)
        funnel_stages = [
            ("Applied", lambda s: True),
            ("Under Review", lambda s: s not in ("Applied",)),
            ("Shortlisted", lambda s: s in ("Shortlisted", "Assessment Sent", "Assessment Started", "Assessment Completed", "Round 1 Passed", "Round 1 Failed", "Interview", "Accepted", "Selected")),
            ("Assessment Completed", lambda s: s in ("Assessment Completed", "Round 1 Passed", "Round 1 Failed", "Interview", "Accepted", "Selected")),
            ("Round 1 Passed", lambda s: s in ("Round 1 Passed", "Interview", "Accepted", "Selected")),
            ("Interview", lambda s: s in ("Interview", "Accepted", "Selected")),
            ("Accepted", lambda s: s in ("Accepted", "Selected")),
        ]
        conversion_funnel = [
            {
                "stage": stage_name,
                "count": sum(1 for a in all_platform_apps if matcher(a["status"])),
                "conversion_rate": round(sum(1 for a in all_platform_apps if matcher(a["status"])) / total_all_applied * 100, 1) if total_all_applied else 0.0,
            }
            for stage_name, matcher in funnel_stages
        ]

        counts["weekly"] = {
            "range": report_range,
            "start_date": start.isoformat(),
            "end_date": (end - timedelta(days=1)).isoformat(),
            "applications": applications_in_range,
            "assessments": assessment_counts,
            "assessments_sent": sent_count,
            "assessments_completed": completed_count,
            "completion_rate": round(sent_completed_count / sent_count * 100) if sent_count else 0,
            "average_score_percentage": round(average_score, 1) if average_score is not None else None,
            "round1_assessments_completed": round1_completed,
            "round1_passed": round1_counts.get("Passed", 0),
            "round1_failed": round1_counts.get("Failed", 0),
            "round1_pass_rate": round(round1_counts.get("Passed", 0) / round1_completed * 100, 1)
            if round1_completed else 0,
            "round1_average_score_percentage": round(round1_average, 1) if round1_average is not None else None,
            "round1_passed_by_job": [
                item for item in round1_job_results if item["result"] == "Passed"
            ],
            "round1_failed_by_job": [
                item for item in round1_job_results if item["result"] == "Failed"
            ],
            "round1_pass_fail_distribution": {
                "Passed": round1_counts.get("Passed", 0),
                "Failed": round1_counts.get("Failed", 0),
            },
            "status_counts": status_counts,
            "daily_trend": daily_trend,
            "top_jobs": top_jobs,
            "top_technologies": top_technologies,
            "weak_technologies": weak_technologies,
            "conversion_funnel": conversion_funnel,
        }
        counts["users_list"] = [dict(row) for row in db.execute("SELECT id, name, email, role, active, created_at FROM users ORDER BY created_at DESC LIMIT 100")]
        counts["recent_applications"] = [dict(row) for row in db.execute(
            """SELECT u.name AS candidate_name, j.title, j.company, a.status, a.applied_at
            FROM applications a JOIN users u ON u.id = a.candidate_id JOIN jobs j ON j.id = a.job_id
            ORDER BY a.applied_at DESC LIMIT 5"""
        )]
    return counts


# ---------------------------------------------------------------------------
# 1. Account & Security: Email Verification & Password Reset
# ---------------------------------------------------------------------------

@app.post("/api/auth/forgot-password")
@limiter.limit("5/minute")
def forgot_password(request: Request, body: ForgotPasswordBody) -> dict[str, Any]:
    raw_token = None
    with connect() as db:
        user_row = db.execute("SELECT id, name, email FROM users WHERE lower(email) = ?", (body.email.lower(),)).fetchone()
        if user_row:
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.execute(
                "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (user_row["id"], token_hash, expires_at, now()),
            )
            log_audit(db, user_row["id"], "user", "FORGOT_PASSWORD_REQUEST", "user", user_row["id"], {"email": body.email.lower()})
            subject = "SkillSphere: Password Reset Instructions"
            content = (
                f"Hello {user_row['name']},\n\n"
                f"We received a request to reset your SkillSphere password.\n"
                f"Your reset token is: {raw_token}\n"
                f"Use this token on the reset password screen within 1 hour.\n\n"
                f"If you did not request this, please ignore this email."
            )
            try:
                send_email_message(user_row["email"], subject, content)
            except Exception as e:
                logger.warning("Could not send password reset email: %s", e)
    resp: dict[str, Any] = {"ok": True, "message": "If this email is registered, you will receive password reset instructions shortly."}
    if raw_token and os.environ.get("SKILLSPHERE_ENABLE_DEMO", "true").lower() not in {"false", "0", "no"}:
        resp["dev_token"] = raw_token
    return resp


@app.post("/api/auth/reset-password")
@limiter.limit("10/minute")
def reset_password(request: Request, body: ResetPasswordBody) -> dict[str, Any]:
    token_hash = hashlib.sha256(body.token.strip().encode()).hexdigest()
    now_iso = now()
    with connect() as db:
        token_row = db.execute(
            "SELECT id, user_id, expires_at, used FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not token_row or token_row["used"] or token_row["expires_at"] < now_iso:
            raise HTTPException(status_code=400, detail="Invalid or expired password reset token")
        user_id = token_row["user_id"]
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash(body.new_password), user_id))
        db.execute("UPDATE password_reset_tokens SET used = 1 WHERE id = ?", (token_row["id"],))
        log_audit(db, user_id, "user", "PASSWORD_RESET_SUCCESS", "user", user_id)
    return {"ok": True, "message": "Password has been successfully updated. You may now sign in."}


@app.post("/api/auth/verify-email")
@limiter.limit("20/minute")
def verify_email(request: Request, body: VerifyEmailBody) -> dict[str, Any]:
    raw_token = body.token.strip()
    raw_otp = body.otp.strip()
    email = str(body.email).strip().lower() if body.email else ""
    if not raw_token and not (raw_otp and email):
        raise HTTPException(status_code=422, detail="Provide a verification link token or email and 6-digit code.")

    now_iso = now()
    with connect() as db:
        token_row = None
        if raw_token:
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            token_row = db.execute(
                "SELECT * FROM email_verification_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not token_row:
                raise HTTPException(status_code=400, detail="Invalid verification link or code.")
        else:
            user_row = db.execute("SELECT id, email_verified FROM users WHERE email = ?", (email,)).fetchone()
            if not user_row:
                raise HTTPException(status_code=400, detail="Invalid verification link or code.")
            if user_row["email_verified"]:
                raise HTTPException(status_code=400, detail="This email address is already verified. You can sign in.")
            otp_hash = hashlib.sha256(raw_otp.encode()).hexdigest()
            token_row = db.execute(
                """SELECT * FROM email_verification_tokens
                WHERE user_id = ? AND otp_hash = ?
                ORDER BY id DESC LIMIT 1""",
                (user_row["id"], otp_hash),
            ).fetchone()
            if not token_row:
                latest = db.execute(
                    """SELECT id, attempts FROM email_verification_tokens
                    WHERE user_id = ? AND used = 0 ORDER BY id DESC LIMIT 1""",
                    (user_row["id"],),
                ).fetchone()
                if latest:
                    new_attempts = (latest["attempts"] or 0) + 1
                    db.execute(
                        "UPDATE email_verification_tokens SET attempts = ? WHERE id = ?",
                        (new_attempts, latest["id"]),
                    )
                    if new_attempts >= 5:
                        db.execute(
                            "UPDATE email_verification_tokens SET used = 1 WHERE id = ?",
                            (latest["id"],),
                        )
                        raise HTTPException(
                            status_code=429,
                            detail="Too many invalid verification attempts. Please request a new verification email.",
                        )
                raise HTTPException(status_code=400, detail="Invalid verification link or code.")

        if token_row["used"]:
            raise HTTPException(status_code=400, detail="This verification link or code has already been used.")
        if (token_row["attempts"] or 0) >= 5:
            raise HTTPException(
                status_code=429,
                detail="Too many invalid verification attempts. Please request a new verification email.",
            )
        if token_row["expires_at"] < now_iso:
            raise HTTPException(
                status_code=400,
                detail="This verification link or code has expired. Please request a new one.",
            )

        user_id = token_row["user_id"]
        user_check = db.execute("SELECT id, email, email_verified FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user_check:
            raise HTTPException(status_code=404, detail="Account not found.")
        if token_row["email_sent_to"] and token_row["email_sent_to"].lower() != user_check["email"].lower():
            raise HTTPException(
                status_code=400,
                detail="This verification link is no longer valid because the email address was changed.",
            )
        if user_check["email_verified"]:
            db.execute("UPDATE email_verification_tokens SET used = 1 WHERE user_id = ?", (user_id,))
            raise HTTPException(status_code=400, detail="This verification link or code has already been used.")

        db.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
        db.execute("UPDATE email_verification_tokens SET used = 1 WHERE user_id = ?", (user_id,))
        log_audit(db, user_id, "user", "EMAIL_VERIFIED", "user", user_id)
    return {"ok": True, "message": "Email address verified successfully. You can now sign in."}


@app.post("/api/auth/resend-verification")
@limiter.limit("5/minute")
def resend_verification(
    request: Request,
    body: ResendVerificationBody | None = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    target_email = str(body.email).strip().lower() if (body and body.email) else ""
    user_id_from_token: int | None = None
    if not target_email and authorization and authorization.startswith("Bearer "):
        payload = decode_token(authorization[7:])
        user_id_from_token = payload.get("sub")

    if not target_email and not user_id_from_token:
        raise HTTPException(status_code=422, detail="Provide your email address to resend verification.")

    with connect() as db:
        if target_email:
            row = db.execute("SELECT id, name, email, email_verified, active FROM users WHERE email = ?", (target_email,)).fetchone()
        else:
            row = db.execute("SELECT id, name, email, email_verified, active FROM users WHERE id = ?", (user_id_from_token,)).fetchone()
        if not row or not row["active"]:
            raise HTTPException(status_code=404, detail="Account not found for this email address.")
        if row["email_verified"]:
            return {"ok": True, "already_verified": True, "message": "Your email address is already verified."}

        window_start = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        recent_count = db.execute(
            """SELECT COUNT(*) FROM email_verification_tokens
            WHERE user_id = ? AND created_at >= ?""",
            (row["id"], window_start),
        ).fetchone()[0]
        if recent_count >= 3:
            raise HTTPException(
                status_code=429,
                detail="Too many verification emails requested. Please wait a few minutes before trying again.",
            )
        raw_token, otp = issue_verification_challenge(db, row["id"], row["name"], row["email"])
    resp: dict[str, Any] = {
        "ok": True,
        "email": row["email"],
        "message": "Verification link and code sent to your email.",
    }
    if not IS_PRODUCTION and not (os.environ.get("RESEND_API_KEY", "").strip() or os.environ.get("SENDGRID_API_KEY", "").strip() or os.environ.get("SKILLSPHERE_SMTP_HOST", "").strip()):
        resp["dev_token"] = raw_token
        resp["dev_otp"] = otp
    return resp


@app.post("/api/auth/change-verification-email")
@limiter.limit("5/minute")
def change_verification_email(request: Request, body: ChangeVerificationEmailBody) -> dict[str, Any]:
    current_email = str(body.current_email).strip().lower()
    new_email = str(body.new_email).strip().lower()
    if not is_valid_email_address(new_email):
        raise HTTPException(status_code=422, detail="Enter a valid new email address.")
    if current_email == new_email:
        raise HTTPException(status_code=422, detail="New email address must be different from your current email.")

    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (current_email,)).fetchone()
        if not row or not row["active"] or not check_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Current email or password is incorrect.")
        if row["email_verified"]:
            raise HTTPException(status_code=409, detail="This account is already verified.")
        conflict = db.execute("SELECT id FROM users WHERE email = ? AND id != ?", (new_email, row["id"])).fetchone()
        if conflict:
            raise HTTPException(status_code=409, detail="An account with this new email already exists.")
        db.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, row["id"]))
        raw_token, otp = issue_verification_challenge(db, row["id"], row["name"], new_email)
        log_audit(db, row["id"], row["role"], "VERIFICATION_EMAIL_CHANGED", "user", row["id"])
    resp: dict[str, Any] = {
        "ok": True,
        "email": new_email,
        "message": f"Email updated to {new_email}. A new verification link and code have been sent.",
    }
    if not IS_PRODUCTION and not (os.environ.get("RESEND_API_KEY", "").strip() or os.environ.get("SENDGRID_API_KEY", "").strip() or os.environ.get("SKILLSPHERE_SMTP_HOST", "").strip()):
        resp["dev_token"] = raw_token
        resp["dev_otp"] = otp
    return resp


@app.get("/api/auth/verification-status")
def verification_status(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT email_verified FROM users WHERE id = ?", (user["id"],)).fetchone()
    verified = bool(row["email_verified"]) if row and "email_verified" in row.keys() else True
    return {"verified": verified, "email": user["email"]}


@app.get("/api/notifications/preferences")
def get_notification_preferences(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        prefs = get_user_notification_preferences(db, user["id"])
    return {
        "essential_categories": sorted(ESSENTIAL_NOTIFICATION_CATEGORIES),
        "messages_enabled": prefs["messages_enabled"],
        "account_activity_enabled": prefs["account_activity_enabled"],
        "system_alerts_enabled": prefs["system_alerts_enabled"],
        "updated_at": prefs["updated_at"],
    }


@app.put("/api/notifications/preferences")
def update_notification_preferences(
    body: NotificationPreferencesBody,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    ts = now()
    with connect() as db:
        db.execute(
            """INSERT INTO notification_preferences
            (user_id, messages_enabled, account_activity_enabled, system_alerts_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                messages_enabled = excluded.messages_enabled,
                account_activity_enabled = excluded.account_activity_enabled,
                system_alerts_enabled = excluded.system_alerts_enabled,
                updated_at = excluded.updated_at""",
            (
                user["id"],
                int(body.messages_enabled),
                int(body.account_activity_enabled),
                int(body.system_alerts_enabled),
                ts,
            ),
        )
    return {
        "essential_categories": sorted(ESSENTIAL_NOTIFICATION_CATEGORIES),
        "messages_enabled": body.messages_enabled,
        "account_activity_enabled": body.account_activity_enabled,
        "system_alerts_enabled": body.system_alerts_enabled,
        "updated_at": ts,
    }


@app.get("/api/notifications/deliveries")
def list_notification_deliveries(
    limit: int = 50,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with connect() as db:
        if user["role"] == "admin":
            rows = db.execute(
                """SELECT id, notification_id, user_id, recipient_email, category, subject,
                status, attempts, last_error, next_retry_at, sent_at, created_at, updated_at
                FROM email_delivery_logs ORDER BY id DESC LIMIT ?""",
                (min(max(limit, 1), 200),),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT id, notification_id, user_id, recipient_email, category, subject,
                status, attempts, last_error, next_retry_at, sent_at, created_at, updated_at
                FROM email_delivery_logs WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user["id"], min(max(limit, 1), 200)),
            ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/notifications/retry-emails")
def trigger_retry_failed_emails(
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        result = retry_failed_email_deliveries(db)
    return result


# ---------------------------------------------------------------------------
# 2. Candidate Job Discovery: Saved Jobs & Recommendations
# ---------------------------------------------------------------------------

@app.post("/api/jobs/{job_id}/save")
def save_job(job_id: int, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        job = db.execute("SELECT id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job or job["status"] != "Open":
            raise HTTPException(status_code=404, detail="Job opening not found or is closed")
        db.execute(
            "INSERT OR IGNORE INTO saved_jobs (candidate_id, job_id, created_at) VALUES (?, ?, ?)",
            (user["id"], job_id, now()),
        )
    return {"saved": True, "job_id": job_id}


@app.delete("/api/jobs/{job_id}/save")
def unsave_job(job_id: int, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        db.execute("DELETE FROM saved_jobs WHERE candidate_id = ? AND job_id = ?", (user["id"], job_id))
    return {"saved": False, "job_id": job_id}


# ---------------------------------------------------------------------------
# 3. Candidate Resume & Profile Improvements: Versions & Quality Analysis
# ---------------------------------------------------------------------------

@app.get("/api/resume/versions")
def list_resume_versions(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, filename, content_type, version_number, is_active, uploaded_at
            FROM resume_versions WHERE candidate_id = ?
            ORDER BY version_number DESC""",
            (user["id"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/resume/analysis")
def resume_analysis(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        resume = db.execute("SELECT * FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
    if not resume:
        return {
            "has_resume": False,
            "score": 0,
            "level": "No Resume",
            "warnings": ["No resume uploaded yet."],
            "suggestions": ["Upload your resume in PDF or DOCX format to receive tailored recommendations."],
            "detected_contact": {"has_name": False, "has_email": False, "has_phone": False},
            "skill_count": 0,
            "text_length": 0,
        }
    parsed = json.loads(resume["parsed_profile_json"] or "{}")
    quality = calculate_resume_quality(resume["extracted_text"] or "", parsed)
    return {**quality, "has_resume": True, "filename": resume["filename"], "uploaded_at": resume["uploaded_at"]}


# ---------------------------------------------------------------------------
# 4. Recruiter Job Management: Job Editing & Extended Company Profile
# ---------------------------------------------------------------------------

@app.patch("/api/jobs/{job_id}")
def update_job(
    job_id: int,
    body: JobUpdateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = owned_job(db, job_id, user)
        updates = {}
        if body.title is not None:
            updates["title"] = body.title.strip()
        if body.company is not None:
            updates["company"] = body.company.strip()
        if body.description is not None:
            updates["description"] = body.description.strip()
        if body.location is not None:
            updates["location"] = body.location.strip()
        if body.experience is not None:
            updates["experience"] = body.experience.strip()
        if body.job_type is not None:
            updates["job_type"] = body.job_type.strip()
        if body.salary is not None:
            updates["salary"] = body.salary.strip()
        if body.skills is not None:
            updates["skills_json"] = json.dumps([str(s).strip() for s in body.skills if s])
        if body.status is not None:
            updates["status"] = body.status
        if not updates:
            return public_job(job)
        set_clauses = ", ".join(f"{k} = ?" for k in updates.keys())
        params = list(updates.values()) + [job_id]
        db.execute(f"UPDATE jobs SET {set_clauses} WHERE id = ?", params)
        log_audit(db, user["id"], user["role"], "UPDATE_JOB", "job", job_id, details=updates)
        updated = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return public_job(updated)


@app.get("/api/company/profile")
def get_company_profile(user: dict[str, Any] = Depends(require_role("recruiter", "admin"))) -> dict[str, Any]:
    profile = user.get("profile") or {}
    return {
        "company": profile.get("company", ""),
        "website": profile.get("website", ""),
        "logo_url": profile.get("logo_url", ""),
        "industry": profile.get("industry", ""),
        "company_size": profile.get("company_size", ""),
        "description": profile.get("description", ""),
    }


@app.put("/api/company/profile")
@app.patch("/api/company/profile")
def update_company_profile(body: CompanyProfileBody, user: dict[str, Any] = Depends(require_role("recruiter", "admin"))) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT profile_json FROM users WHERE id = ?", (user["id"],)).fetchone()
        existing = json.loads(row["profile_json"] or "{}") if row else {}
        existing.update({
            "company": body.company.strip(),
            "website": (body.website or "").strip(),
            "logo_url": (body.logo_url or "").strip(),
            "industry": (body.industry or "").strip(),
            "company_size": (body.company_size or "").strip(),
            "description": (body.description or "").strip(),
        })
        db.execute("UPDATE users SET profile_json = ? WHERE id = ?", (json.dumps(existing), user["id"]))
        log_audit(db, user["id"], user["role"], "UPDATE_COMPANY_PROFILE", "user", user["id"], details=existing)
    return existing


# ---------------------------------------------------------------------------
# 5. Direct Candidate-Recruiter Communication: Messaging
# ---------------------------------------------------------------------------

@app.get("/api/messages/conversations")
def get_conversations(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT m.*,
                CASE WHEN m.sender_id = ? THEN u2.name ELSE u1.name END AS partner_name,
                CASE WHEN m.sender_id = ? THEN u2.role ELSE u1.role END AS partner_role,
                CASE WHEN m.sender_id = ? THEN u2.email ELSE u1.email END AS partner_email,
                CASE WHEN m.sender_id = ? THEN m.recipient_id ELSE m.sender_id END AS partner_id
            FROM messages m
            JOIN users u1 ON u1.id = m.sender_id
            JOIN users u2 ON u2.id = m.recipient_id
            WHERE m.sender_id = ? OR m.recipient_id = ?
            ORDER BY m.created_at DESC""",
            (user["id"], user["id"], user["id"], user["id"], user["id"], user["id"]),
        ).fetchall()
        conversations: dict[int, dict[str, Any]] = {}
        for r in rows:
            p_id = r["partner_id"]
            if p_id not in conversations:
                unread = db.execute(
                    "SELECT COUNT(*) FROM messages WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
                    (p_id, user["id"]),
                ).fetchone()[0]
                conversations[p_id] = {
                    "partner_id": p_id,
                    "partner_name": r["partner_name"],
                    "partner_role": r["partner_role"],
                    "partner_email": r["partner_email"],
                    "last_message": r["content"],
                    "last_message_time": r["created_at"],
                    "unread_count": unread,
                    "application_id": r["application_id"],
                }
    return {"items": list(conversations.values())}


@app.get("/api/messages/thread/{partner_id}")
def get_message_thread(partner_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        partner = db.execute("SELECT id, name, email, role FROM users WHERE id = ?", (partner_id,)).fetchone()
        if not partner:
            raise HTTPException(status_code=404, detail="Conversation partner not found")
        now_iso = now()
        db.execute(
            "UPDATE messages SET read_at = ? WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL",
            (now_iso, partner_id, user["id"]),
        )
        msgs = db.execute(
            """SELECT m.*, u.name AS sender_name
            FROM messages m JOIN users u ON u.id = m.sender_id
            WHERE (m.sender_id = ? AND m.recipient_id = ?) OR (m.sender_id = ? AND m.recipient_id = ?)
            ORDER BY m.created_at ASC""",
            (user["id"], partner_id, partner_id, user["id"]),
        ).fetchall()
    return {"partner": dict(partner), "messages": [dict(m) for m in msgs]}


@app.post("/api/messages")
def send_message(body: MessageSendBody, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        recipient = db.execute("SELECT id, name, email, role FROM users WHERE id = ?", (body.recipient_id,)).fetchone()
        if not recipient:
            raise HTTPException(status_code=404, detail="Recipient not found")
        now_iso = now()
        cursor = db.execute(
            """INSERT INTO messages (application_id, job_id, sender_id, recipient_id, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (body.application_id, None, user["id"], body.recipient_id, body.content.strip(), now_iso),
        )
        msg_id = cursor.lastrowid
        add_notification(
            db,
            body.recipient_id,
            "direct_message",
            f"New message from {user['name']}",
            body.content.strip()[:100],
            application_id=body.application_id,
            action_url="/?page=Dashboard",
        )
        row = db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    return dict(row)


@app.get("/api/messages/unread-count")
def get_unread_messages_count(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_id = ? AND read_at IS NULL",
            (user["id"],),
        ).fetchone()[0]
    return {"unread_count": count}


@app.get("/api/applications/{application_id}/messages")
def get_application_messages(application_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        app_row = db.execute(
            """SELECT a.id, a.candidate_id, j.recruiter_id, j.title, j.company
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?""",
            (application_id,),
        ).fetchone()
        if not app_row:
            raise HTTPException(status_code=404, detail="Application not found")
        if user["role"] == "candidate" and app_row["candidate_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden: You can only view messages for your own application")
        if user["role"] == "recruiter" and app_row["recruiter_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden: You can only view messages for your own jobs")
        db.execute(
            "UPDATE messages SET read_at = ? WHERE application_id = ? AND recipient_id = ? AND read_at IS NULL",
            (now(), application_id, user["id"]),
        )
        rows = db.execute(
            """SELECT m.*, u.name AS sender_name, u.role AS sender_role
            FROM messages m JOIN users u ON u.id = m.sender_id
            WHERE m.application_id = ?
            ORDER BY m.created_at ASC""",
            (application_id,),
        ).fetchall()
    return {"application_id": application_id, "messages": [dict(r) for r in rows]}


@app.post("/api/applications/{application_id}/messages")
def post_application_message(application_id: int, body: dict[str, Any], user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    content = str(body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message content cannot be empty")
    with connect() as db:
        app_row = db.execute(
            """SELECT a.id, a.candidate_id, j.recruiter_id, j.id AS job_id
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?""",
            (application_id,),
        ).fetchone()
        if not app_row:
            raise HTTPException(status_code=404, detail="Application not found")
        if user["role"] == "candidate":
            if app_row["candidate_id"] != user["id"]:
                raise HTTPException(status_code=403, detail="Forbidden")
            recipient_id = app_row["recruiter_id"]
        elif user["role"] == "recruiter":
            if app_row["recruiter_id"] != user["id"]:
                raise HTTPException(status_code=403, detail="Forbidden")
            recipient_id = app_row["candidate_id"]
        else:
            recipient_id = app_row["candidate_id"]
        now_iso = now()
        cursor = db.execute(
            """INSERT INTO messages (application_id, job_id, sender_id, recipient_id, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (application_id, app_row["job_id"], user["id"], recipient_id, content, now_iso),
        )
        add_notification(
            db,
            recipient_id,
            "direct_message",
            f"New message from {user['name']}",
            content[:100],
            application_id=application_id,
            action_url="/?page=Dashboard",
        )
        row = db.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# 6. Question Bank Enhancements: Duplication & Reusable Collections
# ---------------------------------------------------------------------------

@app.post("/api/jobs/{job_id}/questions/{question_id}/duplicate")
def duplicate_job_question(
    job_id: int,
    question_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        orig = db.execute("SELECT * FROM job_questions WHERE id = ? AND job_id = ?", (question_id, job_id)).fetchone()
        if not orig:
            raise HTTPException(status_code=404, detail="Question not found")
        now_iso = now()
        new_q_text = f"{orig['question']} (Copy)"
        cursor = db.execute(
            """INSERT INTO job_questions
            (job_id, owner_id, recruiter_id, technology, question, options_json, correct_index,
             difficulty, active, source_type, explanation, learning_resource_url, subtopic, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'manual', ?, ?, ?, ?, ?)""",
            (
                job_id, user["id"], user["id"], orig["technology"], new_q_text, orig["options_json"],
                orig["correct_index"], orig["difficulty"], orig["explanation"], orig["learning_resource_url"],
                orig["subtopic"], now_iso, now_iso,
            ),
        )
        row = db.execute("SELECT * FROM job_questions WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {**dict(row), "options": json.loads(row["options_json"]), "options_json": None}


@app.get("/api/question-collections")
def list_question_collections(user: dict[str, Any] = Depends(require_role("recruiter", "admin"))) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT c.*, COUNT(i.id) AS question_count
            FROM question_collections c
            LEFT JOIN question_collection_items i ON i.collection_id = c.id
            WHERE c.recruiter_id = ?
            GROUP BY c.id
            ORDER BY c.created_at DESC""",
            (user["id"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/question-collections", status_code=201)
def create_question_collection(
    body: QuestionCollectionCreateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    now_iso = now()
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO question_collections (recruiter_id, name, description, technology, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (user["id"], body.name.strip(), (body.description or "").strip(), (body.technology or "").strip(), now_iso, now_iso),
        )
        row = db.execute("SELECT * FROM question_collections WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@app.get("/api/question-collections/{collection_id}")
def get_question_collection(
    collection_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        coll = db.execute("SELECT * FROM question_collections WHERE id = ? AND recruiter_id = ?", (collection_id, user["id"])).fetchone()
        if not coll:
            raise HTTPException(status_code=404, detail="Question collection not found")
        items = db.execute("SELECT * FROM question_collection_items WHERE collection_id = ?", (collection_id,)).fetchall()
    return {
        **dict(coll),
        "items": [{**dict(i), "options": json.loads(i["options_json"]), "options_json": None} for i in items],
    }


@app.post("/api/question-collections/{collection_id}/import-to-job/{job_id}")
def import_collection_to_job(
    collection_id: int,
    job_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        items = db.execute("SELECT * FROM question_collection_items WHERE collection_id = ?", (collection_id,)).fetchall()
        now_iso = now()
        created = 0
        for item in items:
            db.execute(
                """INSERT INTO job_questions
                (job_id, owner_id, recruiter_id, technology, question, options_json, correct_index,
                 difficulty, active, source_type, explanation, learning_resource_url, subtopic, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'collection', ?, ?, ?, ?, ?)""",
                (
                    job_id, user["id"], user["id"], item["technology"], item["question"], item["options_json"],
                    item["correct_index"], item["difficulty"], item["explanation"], item["learning_resource_url"],
                    item["subtopic"], now_iso, now_iso,
                ),
            )
            created += 1
        summary = job_question_summary(db, job_id)
    return {"created": created, "summary": summary}


# ---------------------------------------------------------------------------
# 7. Candidate Technical Practice Module
# ---------------------------------------------------------------------------

@app.get("/api/practice/technologies")
def list_practice_technologies(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM candidate_technologies WHERE candidate_id = ? ORDER BY technology ASC",
            (user["id"],),
        ).fetchall()
        if not rows:
            resume = db.execute("SELECT parsed_profile_json FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
            if resume and resume["parsed_profile_json"]:
                profile = json.loads(resume["parsed_profile_json"])
                skills = profile.get("skills", [])
                now_iso = now()
                for s in skills[:6]:
                    db.execute(
                        "INSERT OR IGNORE INTO candidate_technologies (candidate_id, technology, proficiency, created_at) VALUES (?, ?, 'Intermediate', ?)",
                        (user["id"], s.strip().title(), now_iso),
                    )
                rows = db.execute("SELECT * FROM candidate_technologies WHERE candidate_id = ? ORDER BY technology ASC", (user["id"],)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/practice/technologies", status_code=201)
def add_practice_technology(body: PracticeTechBody, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    tech = body.technology.strip().title()
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO candidate_technologies (candidate_id, technology, proficiency, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], tech, body.proficiency or "Intermediate", now()),
        )
        row = db.execute("SELECT * FROM candidate_technologies WHERE candidate_id = ? AND technology = ?", (user["id"], tech)).fetchone()
    return dict(row)


@app.delete("/api/practice/technologies/{technology}")
def remove_practice_technology(technology: str, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        db.execute("DELETE FROM candidate_technologies WHERE candidate_id = ? AND lower(technology) = lower(?)", (user["id"], technology))
    return {"removed": True, "technology": technology}


@app.get("/api/practice/questions")
def get_practice_questions(
    technology: str | None = None,
    difficulty: str | None = None,
    limit: int = 10,
    user: dict[str, Any] = Depends(require_role("candidate")),
) -> dict[str, Any]:
    with connect() as db:
        conditions = []
        params = []
        if technology:
            conditions.append("lower(technology) = lower(?)")
            params.append(technology)
        if difficulty and difficulty in {"Easy", "Medium", "Hard"}:
            conditions.append("difficulty = ?")
            params.append(difficulty)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT id, technology, question, options_json, difficulty, explanation, subtopic FROM practice_questions {where_clause} ORDER BY RANDOM() LIMIT ?"
        params.append(min(limit, 25))
        rows = db.execute(query, tuple(params)).fetchall()
        items = []
        for r in rows:
            opts = json.loads(r["options_json"])
            items.append({
                "id": r["id"],
                "technology": r["technology"],
                "question": r["question"],
                "options": opts,
                "difficulty": r["difficulty"],
                "explanation": r["explanation"],
                "subtopic": r["subtopic"],
            })
    return {"items": items, "count": len(items)}


@app.post("/api/practice/submit")
def submit_practice_attempt(body: PracticeSubmitBody, user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    sel_idx = body.selected_index if body.selected_index is not None else body.selected_option_index
    if sel_idx is None:
        raise HTTPException(status_code=422, detail="Selected option index required")
    with connect() as db:
        q = db.execute("SELECT * FROM practice_questions WHERE id = ?", (body.question_id,)).fetchone()
        if not q:
            raise HTTPException(status_code=404, detail="Question not found")
        is_correct = 1 if sel_idx == q["correct_index"] else 0
        db.execute(
            """INSERT INTO practice_attempts (candidate_id, question_id, selected_index, is_correct, attempted_at)
            VALUES (?, ?, ?, ?, ?)""",
            (user["id"], body.question_id, sel_idx, is_correct, now()),
        )
    return {
        "is_correct": bool(is_correct),
        "correct_index": q["correct_index"],
        "explanation": q["explanation"],
        "technology": q["technology"],
    }


@app.get("/api/practice/analytics")
def practice_analytics(user: dict[str, Any] = Depends(require_role("candidate"))) -> dict[str, Any]:
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM practice_attempts WHERE candidate_id = ?", (user["id"],)).fetchone()[0]
        correct = db.execute("SELECT COUNT(*) FROM practice_attempts WHERE candidate_id = ? AND is_correct = 1", (user["id"],)).fetchone()[0]
        accuracy = round(correct / total * 100, 1) if total > 0 else 0
        tech_rows = db.execute(
            """SELECT q.technology, COUNT(a.id) AS total_attempts, SUM(a.is_correct) AS correct_attempts
            FROM practice_attempts a JOIN practice_questions q ON q.id = a.question_id
            WHERE a.candidate_id = ?
            GROUP BY q.technology
            ORDER BY total_attempts DESC""",
            (user["id"],),
        ).fetchall()
        breakdown = []
        for r in tech_rows:
            tot = r["total_attempts"]
            cor = r["correct_attempts"] or 0
            breakdown.append({
                "technology": r["technology"],
                "attempts": tot,
                "correct": cor,
                "accuracy": round(cor / tot * 100, 1) if tot > 0 else 0,
            })
    return {
        "total_attempts": total,
        "correct_attempts": correct,
        "accuracy_percentage": accuracy,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# 8. Assessment Improvements: Score Adjustments & Retry Action
# ---------------------------------------------------------------------------

@app.post("/api/assessments/{assessment_id}/adjust-score")
def adjust_assessment_score(
    assessment_id: int,
    body: AssessmentScoreAdjustBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        assess = db.execute(
            """SELECT a.*, j.recruiter_id, j.title, j.company
            FROM assessments a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?""",
            (assessment_id,),
        ).fetchone()
        if not assess:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if user["role"] == "recruiter" and assess["recruiter_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        orig_score = assess["score_percentage"]
        orig_result = assess["result"]
        new_result = body.new_result or ("Passed" if body.new_score >= 70.0 else "Failed")
        now_iso = now()
        db.execute(
            """INSERT INTO assessment_score_adjustments
            (assessment_id, recruiter_id, original_score, new_score, original_result, new_result, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (assessment_id, user["id"], orig_score, body.new_score, orig_result, new_result, body.reason.strip(), now_iso),
        )
        db.execute(
            "UPDATE assessments SET score_percentage = ?, result = ? WHERE id = ?",
            (body.new_score, new_result, assessment_id),
        )
        log_audit(db, user["id"], user["role"], "ADJUST_ASSESSMENT_SCORE", "assessment", assessment_id, {
            "original_score": orig_score,
            "new_score": body.new_score,
            "reason": body.reason,
        })
        updated = db.execute("SELECT * FROM assessments WHERE id = ?", (assessment_id,)).fetchone()
    return dict(updated)


@app.post("/api/assessments/{assessment_id}/retry")
def retry_assessment(
    assessment_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        assess = db.execute(
            """SELECT a.*, j.recruiter_id, j.title, j.company, u.email, u.name
            FROM assessments a JOIN jobs j ON j.id = a.job_id
            JOIN users u ON u.id = a.candidate_id
            WHERE a.id = ?""",
            (assessment_id,),
        ).fetchone()
        if not assess:
            raise HTTPException(status_code=404, detail="Assessment not found")
        if user["role"] == "recruiter" and assess["recruiter_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        new_raw_token = secrets.token_urlsafe(32)
        new_token_hash = hashlib.sha256(new_raw_token.encode()).hexdigest()
        now_iso = now()
        db.execute(
            """UPDATE assessments
            SET token_hash = ?, status = 'Sent', result = NULL, score_percentage = NULL,
                starts_at = NULL, submitted_at = NULL
            WHERE id = ?""",
            (new_token_hash, assessment_id),
        )
        subject = f"SkillSphere: Assessment Invitation for {assess['title']}"
        content = (
            f"Hello {assess['name']},\n\n"
            f"You have been invited to retake the technical assessment for {assess['title']} at {assess['company']}.\n"
            f"Click the link below to begin your assessment:\n"
            f"{os.environ.get('APP_BASE_URL', 'https://skillsphere.onrender.com')}/?assessment={new_raw_token}\n\n"
            f"Best regards,\nThe SkillSphere Team"
        )
        try:
            send_email_message(assess["email"], subject, content)
        except Exception as e:
            logger.warning("Could not send assessment retry email: %s", e)
        log_audit(db, user["id"], user["role"], "RETRY_ASSESSMENT", "assessment", assessment_id)
    return {"ok": True, "token": new_raw_token, "detail": "Assessment retry link generated and candidate notified."}


@app.get("/api/assessments/compare")
def compare_assessments(
    job_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, job_id, user)
        rows = db.execute(
            """SELECT a.id, a.candidate_id, u.name AS candidate_name, u.email AS candidate_email,
                a.status, a.result, a.score_percentage, a.proctoring_warning_count,
                a.submitted_at, app.match_percentage
            FROM assessments a
            JOIN users u ON u.id = a.candidate_id
            LEFT JOIN applications app ON app.id = a.application_id
            WHERE a.job_id = ?
            ORDER BY a.score_percentage DESC NULLS LAST""",
            (job_id,),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 9. Asynchronous Video Assessment Module
# ---------------------------------------------------------------------------

@app.post("/api/video-assessments", status_code=201)
def create_video_assessment(
    body: VideoAssessmentCreateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        owned_job(db, body.job_id, user)
        now_iso = now()
        cursor = db.execute(
            """INSERT INTO video_assessments (job_id, recruiter_id, title, description, time_limit_seconds, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Published', ?, ?)""",
            (body.job_id, user["id"], body.title.strip(), (body.description or "").strip(), body.time_limit_seconds, now_iso, now_iso),
        )
        va_id = cursor.lastrowid
        for idx, q in enumerate(body.questions, start=1):
            db.execute(
                """INSERT INTO video_assessment_questions (video_assessment_id, question_order, question_text, max_duration_seconds, prep_time_seconds, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (va_id, idx, str(q.get("question_text", "")).strip(), int(q.get("max_duration_seconds", body.time_limit_seconds)), int(q.get("prep_time_seconds", 30)), now_iso),
            )
        va = db.execute("SELECT * FROM video_assessments WHERE id = ?", (va_id,)).fetchone()
        qs = db.execute("SELECT * FROM video_assessment_questions WHERE video_assessment_id = ? ORDER BY question_order ASC", (va_id,)).fetchall()
    return {**dict(va), "questions": [dict(q) for q in qs]}


@app.get("/api/jobs/{job_id}/video-assessment")
def get_job_video_assessment(
    job_id: int,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    with connect() as db:
        va = db.execute("SELECT * FROM video_assessments WHERE job_id = ? AND status = 'Published'", (job_id,)).fetchone()
        if not va:
            return {"configured": False, "assessment": None}
        qs = db.execute("SELECT * FROM video_assessment_questions WHERE video_assessment_id = ? ORDER BY question_order ASC", (va["id"],)).fetchall()
    return {"configured": True, "assessment": {**dict(va), "questions": [dict(q) for q in qs]}}


@app.post("/api/video-assessments/{video_id}/assign")
def assign_video_assessment(
    video_id: int,
    application_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        va = db.execute("SELECT * FROM video_assessments WHERE id = ?", (video_id,)).fetchone()
        if not va:
            raise HTTPException(status_code=404, detail="Video assessment not found")
        app_row = db.execute(
            """SELECT a.id, a.candidate_id, u.email, u.name, j.title, j.company
            FROM applications a JOIN users u ON u.id = a.candidate_id
            JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?""",
            (application_id,),
        ).fetchone()
        if not app_row:
            raise HTTPException(status_code=404, detail="Application not found")
        raw_token = secrets.token_urlsafe(32)
        now_iso = now()
        deadline = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        cursor = db.execute(
            """INSERT INTO video_assignments (video_assessment_id, application_id, candidate_id, token, status, deadline_at, invited_at)
            VALUES (?, ?, ?, ?, 'Invited', ?, ?)""",
            (video_id, application_id, app_row["candidate_id"], raw_token, deadline, now_iso),
        )
        assign_id = cursor.lastrowid
        subject = f"SkillSphere: Asynchronous Video Interview for {app_row['title']}"
        content = (
            f"Hello {app_row['name']},\n\n"
            f"You have been invited to complete a video response assessment for {app_row['title']} at {app_row['company']}.\n"
            f"Please complete your video responses by {deadline[:10]}:\n"
            f"{os.environ.get('APP_BASE_URL', 'https://skillsphere.onrender.com')}/?video_assignment={raw_token}\n\n"
            f"Best regards,\nThe SkillSphere Team"
        )
        try:
            send_email_message(app_row["email"], subject, content)
        except Exception as e:
            logger.warning("Could not send video assessment email: %s", e)
    return {"assignment_id": assign_id, "token": raw_token, "deadline": deadline}


@app.get("/api/video-assessments/assignment/{token}")
def get_video_assignment(token: str) -> dict[str, Any]:
    with connect() as db:
        assign = db.execute("SELECT * FROM video_assignments WHERE token = ?", (token,)).fetchone()
        if not assign:
            raise HTTPException(status_code=404, detail="Video assignment not found")
        va = db.execute("SELECT * FROM video_assessments WHERE id = ?", (assign["video_assessment_id"],)).fetchone()
        qs = db.execute("SELECT * FROM video_assessment_questions WHERE video_assessment_id = ? ORDER BY question_order ASC", (va["id"],)).fetchall()
        responses = db.execute("SELECT * FROM video_responses WHERE assignment_id = ?", (assign["id"],)).fetchall()
    return {
        "assignment": dict(assign),
        "assessment": dict(va),
        "questions": [dict(q) for q in qs],
        "responses": [dict(r) for r in responses],
    }


@app.post("/api/video-assessments/assignment/{token}/submit")
def submit_video_assignment(token: str, body: VideoResponseSubmitBody) -> dict[str, Any]:
    with connect() as db:
        assign = db.execute("SELECT * FROM video_assignments WHERE token = ?", (token,)).fetchone()
        if not assign or assign["status"] == "Completed":
            raise HTTPException(status_code=400, detail="Assignment already completed or invalid")
        now_iso = now()
        for resp in body.responses:
            db.execute(
                """INSERT INTO video_responses (assignment_id, question_id, video_url, duration_seconds, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (assign["id"], resp["question_id"], resp["video_url"], int(resp.get("duration_seconds", 0)), now_iso),
            )
        db.execute("UPDATE video_assignments SET status = 'Submitted', completed_at = ? WHERE id = ?", (now_iso, assign["id"]))
    return {"ok": True, "message": "Video assessment submitted successfully."}


@app.post("/api/video-assessments/responses/{response_id}/evaluate")
def evaluate_video_response(
    response_id: int,
    body: VideoEvaluateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        now_iso = now()
        db.execute(
            """UPDATE video_responses
            SET score = ?, recruiter_feedback = ?, evaluated_at = ?
            WHERE id = ?""",
            (body.score, body.feedback.strip(), now_iso, response_id),
        )
        row = db.execute("SELECT * FROM video_responses WHERE id = ?", (response_id,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# 10. Interview Hub Improvements: Scorecards & Dossier
# ---------------------------------------------------------------------------

@app.get("/api/interviews/{interview_id}/scorecard")
def get_interview_scorecard(
    interview_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        sc = db.execute("SELECT * FROM interview_scorecards WHERE interview_id = ?", (interview_id,)).fetchone()
    if not sc:
        empty = {
            "interview_id": interview_id,
            "technical_rating": 3,
            "communication_rating": 3,
            "problem_solving_rating": 3,
            "culture_fit_rating": 3,
            "overall_recommendation": "Neutral",
            "recommendation": "Neutral",
            "strengths": "",
            "areas_for_growth": "",
            "notes": "",
        }
        return {**empty, "scorecard": empty, "submitted": False}
    sc_dict = dict(sc)
    sc_dict["recommendation"] = "Strong Yes" if sc_dict.get("overall_recommendation") == "Strong Hire" else ("Yes" if sc_dict.get("overall_recommendation") == "Hire" else sc_dict.get("overall_recommendation"))
    return {**sc_dict, "scorecard": sc_dict, "submitted": True}


@app.post("/api/interviews/{interview_id}/scorecard")
def save_interview_scorecard(
    interview_id: int,
    body: InterviewScorecardBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        iv = db.execute("SELECT id, application_id, recruiter_id FROM interviews WHERE id = ?", (interview_id,)).fetchone()
        if not iv:
            raise HTTPException(status_code=404, detail="Interview not found")
        now_iso = now()
        db.execute(
            """INSERT INTO interview_scorecards
            (interview_id, application_id, recruiter_id, technical_rating, communication_rating,
             problem_solving_rating, culture_fit_rating, overall_recommendation, strengths,
             areas_for_growth, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(interview_id) DO UPDATE SET
            technical_rating = excluded.technical_rating,
            communication_rating = excluded.communication_rating,
            problem_solving_rating = excluded.problem_solving_rating,
            culture_fit_rating = excluded.culture_fit_rating,
            overall_recommendation = excluded.overall_recommendation,
            strengths = excluded.strengths,
            areas_for_growth = excluded.areas_for_growth,
            notes = excluded.notes,
            updated_at = excluded.updated_at""",
            (
                interview_id, iv["application_id"], user["id"],
                body.technical_rating, body.communication_rating,
                body.problem_solving_rating, body.culture_fit_rating,
                body.overall_recommendation, (body.strengths or "").strip(),
                (body.areas_for_growth or "").strip(), (body.notes or "").strip(),
                now_iso, now_iso,
            ),
        )
        log_audit(db, user["id"], user["role"], "SUBMIT_INTERVIEW_SCORECARD", "interview", interview_id)
        row = db.execute("SELECT * FROM interview_scorecards WHERE interview_id = ?", (interview_id,)).fetchone()
        row_dict = dict(row)
        row_dict["recommendation"] = "Strong Yes" if row_dict.get("overall_recommendation") == "Strong Hire" else ("Yes" if row_dict.get("overall_recommendation") == "Hire" else row_dict.get("overall_recommendation"))
    return {**row_dict, "scorecard": row_dict}


@app.get("/api/interviews/{interview_id}/dossier")
def get_candidate_dossier(
    interview_id: int,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        iv = db.execute(
            """SELECT i.*, a.candidate_id, u.name AS candidate_name, u.email AS candidate_email,
                j.title AS job_title, j.company AS job_company, a.match_percentage, a.status AS application_status
            FROM interviews i
            JOIN applications a ON a.id = i.application_id
            JOIN users u ON u.id = a.candidate_id
            JOIN jobs j ON j.id = a.job_id
            WHERE i.id = ?""",
            (interview_id,),
        ).fetchone()
        if not iv:
            raise HTTPException(status_code=404, detail="Interview not found")
        resume = db.execute("SELECT parsed_profile_json, extracted_text FROM resumes WHERE candidate_id = ?", (iv["candidate_id"],)).fetchone()
        assessment = db.execute("SELECT score_percentage, result, proctoring_warning_count, starts_at, submitted_at FROM assessments WHERE application_id = ?", (iv["application_id"],)).fetchone()
        app_note = db.execute("SELECT internal_notes FROM applications WHERE id = ?", (iv["application_id"],)).fetchone()
        notes = [{"note": app_note["internal_notes"]}] if app_note and app_note["internal_notes"] else []
        scorecard = db.execute("SELECT * FROM interview_scorecards WHERE interview_id = ?", (interview_id,)).fetchone()
    return {
        "candidate": {
            "name": iv["candidate_name"],
            "email": iv["candidate_email"],
            "match_percentage": iv["match_percentage"],
            "parsed_profile": json.loads(resume["parsed_profile_json"] or "{}") if resume else {},
            "resume_excerpt": (resume["extracted_text"] or "")[:800] if resume else "",
        },
        "assessment": dict(assessment) if assessment else None,
        "notes": [dict(n) for n in notes],
        "scorecard": dict(scorecard) if scorecard else None,
    }


# ---------------------------------------------------------------------------
# 11. Support Ticket System & Escalation
# ---------------------------------------------------------------------------

@app.post("/api/tickets", status_code=201)
def create_support_ticket(body: TicketCreateBody, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        ticket_count = db.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0]
        ticket_number = f"TICK-{1000 + ticket_count + 1}"
        now_iso = now()
        cursor = db.execute(
            """INSERT INTO support_tickets (ticket_number, user_id, subject, category, priority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'Open', ?, ?)""",
            (ticket_number, user["id"], body.subject.strip(), body.category, body.priority, now_iso, now_iso),
        )
        ticket_id = cursor.lastrowid
        db.execute(
            """INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, is_internal_note, message, created_at)
            VALUES (?, ?, 0, 0, ?, ?)""",
            (ticket_id, user["id"], body.message.strip(), now_iso),
        )
        log_audit(db, user["id"], user["role"], "CREATE_SUPPORT_TICKET", "support_ticket", ticket_id, {"ticket_number": ticket_number})
        row = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
    return dict(row)


@app.get("/api/tickets")
def list_user_tickets(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            """SELECT t.*, COUNT(m.id) AS message_count
            FROM support_tickets t
            LEFT JOIN ticket_messages m ON m.ticket_id = t.id AND m.is_internal_note = 0
            WHERE t.user_id = ?
            GROUP BY t.id
            ORDER BY t.updated_at DESC""",
            (user["id"],),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@app.get("/api/tickets/{ticket_id}")
def get_ticket_detail(ticket_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        t = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if user["role"] != "admin" and t["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        note_filter = "" if user["role"] == "admin" else "AND m.is_internal_note = 0"
        msgs = db.execute(
            f"""SELECT m.*, u.name AS sender_name, u.role AS sender_role
            FROM ticket_messages m JOIN users u ON u.id = m.sender_id
            WHERE m.ticket_id = ? {note_filter}
            ORDER BY m.created_at ASC""",
            (ticket_id,),
        ).fetchall()
    return {"ticket": dict(t), "messages": [dict(m) for m in msgs]}


@app.post("/api/tickets/{ticket_id}/messages")
def reply_to_ticket(ticket_id: int, body: TicketMessageBody, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        t = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if user["role"] != "admin" and t["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        is_admin = 1 if user["role"] == "admin" else 0
        is_internal = 1 if is_admin and body.is_internal_note else 0
        now_iso = now()
        cursor = db.execute(
            """INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, is_internal_note, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (ticket_id, user["id"], is_admin, is_internal, body.message.strip(), now_iso),
        )
        new_status = t["status"]
        if is_admin and not is_internal and t["status"] == "Open":
            new_status = "Waiting Customer"
        elif not is_admin and t["status"] in {"Waiting Customer", "Resolved"}:
            new_status = "In Progress"
        db.execute("UPDATE support_tickets SET status = ?, updated_at = ? WHERE id = ?", (new_status, now_iso, ticket_id))
        msg_row = db.execute("SELECT * FROM ticket_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
    try:
        asyncio.create_task(support_chat_manager.broadcast(
            ticket_id,
            {"type": "message", "message": dict(msg_row), "sender_name": user["name"], "is_admin": bool(is_admin)},
            is_internal=bool(is_internal),
        ))
    except Exception:
        pass
    return dict(msg_row)


@app.get("/api/admin/tickets")
def admin_list_tickets(
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        conditions = []
        params = []
        if status:
            conditions.append("t.status = ?")
            params.append(status)
        if priority:
            conditions.append("t.priority = ?")
            params.append(priority)
        if category:
            conditions.append("t.category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = db.execute(
            f"""SELECT t.*, u.name AS user_name, u.email AS user_email, u.role AS user_role,
                adm.name AS assigned_admin_name
            FROM support_tickets t
            JOIN users u ON u.id = t.user_id
            LEFT JOIN users adm ON adm.id = t.assigned_admin_id
            {where}
            ORDER BY t.updated_at DESC""",
            tuple(params),
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@app.patch("/api/admin/tickets/{ticket_id}")
def admin_update_ticket(
    ticket_id: int,
    body: TicketStatusUpdateBody,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        t = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        updates = {}
        if body.status:
            updates["status"] = body.status
        if body.priority:
            updates["priority"] = body.priority
        if body.assigned_admin_id is not None:
            updates["assigned_admin_id"] = body.assigned_admin_id
        if updates:
            updates["updated_at"] = now()
            set_str = ", ".join(f"{k} = ?" for k in updates.keys())
            db.execute(f"UPDATE support_tickets SET {set_str} WHERE id = ?", list(updates.values()) + [ticket_id])
            log_audit(db, user["id"], "admin", "UPDATE_TICKET_STATUS", "support_ticket", ticket_id, details=updates)
        updated = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
    return dict(updated)


# ---------------------------------------------------------------------------
# 12. Live Human-Agent Handoff & WebSocket Chat
# ---------------------------------------------------------------------------

@app.get("/api/admin/availability")
def get_admin_availability(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        count = db.execute("SELECT COUNT(*) FROM admin_availability WHERE is_online = 1").fetchone()[0]
    return {"online": count > 0, "available_admins": count}


@app.post("/api/admin/availability")
def set_admin_availability(online: bool = True, user: dict[str, Any] = Depends(require_role("admin"))) -> dict[str, Any]:
    with connect() as db:
        now_iso = now()
        db.execute(
            """INSERT INTO admin_availability (admin_id, is_online, active_chats_count, max_concurrent_chats, last_heartbeat_at)
            VALUES (?, ?, 0, 5, ?)
            ON CONFLICT(admin_id) DO UPDATE SET is_online = excluded.is_online, last_heartbeat_at = excluded.last_heartbeat_at""",
            (user["id"], 1 if online else 0, now_iso),
        )
    return {"admin_id": user["id"], "is_online": online}


@app.post("/api/tickets/{ticket_id}/ws-auth-ticket")
def generate_ws_auth_ticket(ticket_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with connect() as db:
        t = db.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if user["role"] != "admin" and t["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        raw_ticket = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        db.execute(
            "INSERT INTO websocket_auth_tickets (ticket_id, user_id, role, support_ticket_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (raw_ticket, user["id"], user["role"], ticket_id, expires_at, now()),
        )
    return {"ticket": raw_ticket, "expires_in": 60}


@app.websocket("/ws/tickets/{ticket_id}")
async def support_ticket_websocket(websocket: WebSocket, ticket_id: int, ticket: str = "") -> None:
    if not ticket:
        await websocket.close(code=1008)
        return
    now_iso = now()
    with connect() as db:
        row = db.execute(
            "SELECT * FROM websocket_auth_tickets WHERE ticket_id = ? AND used = 0 AND expires_at >= ?",
            (ticket, now_iso),
        ).fetchone()
        if not row:
            await websocket.close(code=1008)
            return
        user_row = db.execute("SELECT id, name, role FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if not user_row:
            await websocket.close(code=1008)
            return
        db.execute("UPDATE websocket_auth_tickets SET used = 1 WHERE ticket_id = ?", (ticket,))
    conn_id = secrets.token_hex(8)
    await support_chat_manager.connect(ticket_id, conn_id, websocket, user_row["id"], user_row["role"], user_row["name"])
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "message")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "typing":
                await support_chat_manager.broadcast(ticket_id, {
                    "type": "typing",
                    "user_name": user_row["name"],
                    "is_typing": bool(data.get("is_typing", True)),
                })
            elif msg_type == "message":
                text = str(data.get("message", "")).strip()
                if text:
                    is_internal = bool(data.get("is_internal_note", False)) if user_row["role"] == "admin" else False
                    with connect() as db:
                        cur = db.execute(
                            """INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, is_internal_note, message, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (ticket_id, user_row["id"], 1 if user_row["role"] == "admin" else 0, 1 if is_internal else 0, text, now()),
                        )
                        m_row = db.execute("SELECT * FROM ticket_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
                    await support_chat_manager.broadcast(
                        ticket_id,
                        {"type": "message", "message": dict(m_row), "sender_name": user_row["name"], "is_admin": user_row["role"] == "admin"},
                        is_internal=is_internal,
                    )
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        support_chat_manager.disconnect(ticket_id, conn_id)


# ---------------------------------------------------------------------------
# 13. Administration, Moderation & Auditability
# ---------------------------------------------------------------------------

@app.get("/api/admin/audit-logs")
def get_admin_audit_logs(
    action: str | None = None,
    target_type: str | None = None,
    limit: int = 100,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        conditions = []
        params = []
        if action:
            conditions.append("a.action = ?")
            params.append(action)
        if target_type:
            conditions.append("a.target_type = ?")
            params.append(target_type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""SELECT a.*, u.name AS user_name, u.email AS user_email
            FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id
            {where}
            ORDER BY a.created_at DESC LIMIT ?"""
        params.append(min(limit, 200))
        rows = db.execute(query, tuple(params)).fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@app.post("/api/admin/jobs/{job_id}/moderate")
def moderate_job(
    job_id: int,
    body: JobModerateBody,
    user: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Job listing not found")
        note = body.note.strip() if body.note else ""
        if body.action == "flag":
            db.execute("UPDATE jobs SET flagged = 1, moderation_note = ? WHERE id = ?", (note, job_id))
        elif body.action == "unflag":
            db.execute("UPDATE jobs SET flagged = 0, moderation_note = ? WHERE id = ?", (note, job_id))
        elif body.action == "archive":
            db.execute("UPDATE jobs SET status = 'Archived', is_archived = 1, archived_at = ?, moderation_note = ? WHERE id = ?", (now(), note, job_id))
        elif body.action == "restore":
            db.execute("UPDATE jobs SET status = 'Open', is_archived = 0, moderation_note = ? WHERE id = ?", (note, job_id))
        log_audit(db, user["id"], "admin", f"MODERATE_JOB_{body.action.upper()}", "job", job_id, {"action": body.action, "note": note})
        updated = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return public_job(updated)


# ---------------------------------------------------------------------------
# 14. Internationalization & Multilingual Assistant
# ---------------------------------------------------------------------------

@app.post("/api/chat/multilingual")
def multilingual_chat(body: MultilingualChatBody, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    text = body.message.strip()
    pref_lang = body.preferred_language
    detected = "en"
    if any("\u0900" <= c <= "\u097f" for c in text):
        detected = "hi"
    elif any(w in text.lower() for w in ["kya", "kaise", "mera", "meri", "karo", "nhi", "nahi", "job", "batao", "madad"]):
        detected = "hinglish"
    elif any(w in text.lower() for w in ["hola", "como", "trabajo", "curriculum", "entrevista", "ayuda", "gracias", "puedo"]):
        detected = "es"
    elif any(w in text.lower() for w in ["bonjour", "comment", "emploi", "entretien", "merci", "aide", "cv"]):
        detected = "fr"
    elif any(w in text.lower() for w in ["hallo", "wie", "arbeit", "lebenslauf", "interview", "danke", "hilfe"]):
        detected = "de"

    lang = pref_lang or detected

    responses = {
        "hi": f"नमस्ते {user['name']}! मैं आपका SkillSphere करियर सहायक हूँ। मैं आपके रिज्यूम सुधारने, तकनीकी तैयारी करने और जॉब सर्च में मदद कर सकता हूँ। आप क्या जानना चाहते हैं?",
        "hinglish": f"Hello {user['name']}! Main aapka SkillSphere career assistant hoon. Aap apne resume improve karne, interview prep aur job applications ke baare me pooch sakte hain. Main kaise madad karoon?",
        "es": f"¡Hola {user['name']}! Soy tu asistente de carrera de SkillSphere. Puedo ayudarte a optimizar tu currículum, prepararte para entrevistas y explorar vacantes. ¿En qué puedo asistirte hoy?",
        "fr": f"Bonjour {user['name']} ! Je suis votre assistant de carrière SkillSphere. Je peux vous aider à optimiser votre CV, préparer vos entretiens et trouver des opportunités. Que souhaitez-vous savoir ?",
        "de": f"Hallo {user['name']}! Ich bin Ihr SkillSphere Karriere-Assistent. Ich kann Ihnen bei der Optimierung Ihres Lebenslaufs und der Vorbereitung auf Vorstellungsgespräche helfen. Wie kann ich heute helfen?",
        "en": f"Hello {user['name']}! I am your SkillSphere Career Assistant. I can help you improve your resume, prepare for technical & HR interviews, and navigate open opportunities. What would you like help with today?",
    }

    reply = responses.get(lang, responses["en"])
    suggest_ticket = any(kw in text.lower() for kw in ["issue", "bug", "broken", "complaint", "error", "fail", "stuck", "frustrated", "support", "ticket", "human", "agent", "problem"])

    return {
        "reply": reply,
        "detected_language": detected,
        "active_language": lang,
        "suggest_ticket": suggest_ticket,
        "ticket_prefill": {
            "category": "Technical",
            "subject": f"Assistance required: {text[:60]}",
            "message": text,
        } if suggest_ticket else None,
    }


@app.get("/api/workspace/bootstrap")
def workspace_bootstrap(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    role = user.get("role")

    if role == "recruiter":
        jobs_data = list_jobs(page=1, page_size=100, mine=True, user=user)
    else:
        jobs_data = list_jobs(page=1, page_size=6, mine=False, user=user)

    if role == "admin":
        apps_data = {"items": [], "total": 0}
    else:
        apps_data = list_applications(user=user)

    interviews_data = list_interviews(user=user)

    upcoming_data = {"items": []}
    if role == "candidate":
        try:
            upcoming_data = upcoming_interviews(user=user)
        except Exception:
            upcoming_data = {"items": []}

    assessments_data = assessment_dashboard(user=user)
    notifications_data = list_notifications(unread=False, limit=50, user=user)

    result: dict[str, Any] = {
        "user": user,
        "jobs": jobs_data,
        "applications": apps_data,
        "interviews": interviews_data,
        "upcoming_interviews": upcoming_data,
        "assessments": assessments_data,
        "notifications": notifications_data,
    }

    if role == "candidate":
        try:
            result["resume"] = get_resume(user=user)
        except Exception:
            result["resume"] = None
        try:
            result["candidate_analytics"] = candidate_analytics(user=user)
        except Exception:
            result["candidate_analytics"] = None
    elif role == "recruiter":
        try:
            result["recruiter_analytics"] = recruiter_analytics(report_range="this_week", user=user)
        except Exception:
            result["recruiter_analytics"] = None
        try:
            result["live_assessments"] = get_recruiter_live_assessments(user=user)
        except Exception:
            result["live_assessments"] = {"items": []}
    elif role == "admin":
        try:
            result["admin_jobs"] = admin_jobs(user=user)
        except Exception:
            result["admin_jobs"] = {"items": []}
        try:
            result["analytics"] = analytics(report_range="this_week", user=user)
        except Exception:
            result["analytics"] = None

    return result


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")


if __name__ == "__main__":
    import uvicorn
    server_port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=server_port, reload=False)
