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
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = Path(os.environ.get("SKILLSPHERE_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
QUESTION_BANK_UPLOAD_DIR = DATA_DIR / "question_bank_uploads"
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
    PROCTORING_FACE_MISSING_SECONDS = int(os.environ.get("SKILLSPHERE_PROCTORING_FACE_MISSING_SECONDS", "10"))
    PROCTORING_MAX_WARNINGS = int(os.environ.get("SKILLSPHERE_PROCTORING_MAX_WARNINGS", "3"))
except ValueError as error:
    raise RuntimeError("Proctoring warning thresholds must be positive integers") from error
PROCTORING_AUTO_SUBMIT = environment_bool("SKILLSPHERE_PROCTORING_AUTO_SUBMIT", "false")
if PROCTORING_FACE_MISSING_SECONDS < 1 or PROCTORING_MAX_WARNINGS < 1:
    raise RuntimeError("Proctoring warning thresholds must be positive integers")
logger = logging.getLogger(__name__)

app = FastAPI(title="SkillSphere API", version="1.0.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return cursor.lastrowid


def email_notification(db: sqlite3.Connection, user_id: int, title: str, message: str) -> str:
    user = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return "Failed"
    host = os.environ.get("SKILLSPHERE_SMTP_HOST", "")
    if not host:
        if IS_PRODUCTION:
            logger.error("Notification email delivery is unavailable because SMTP is not configured")
            return "Failed"
        return "Development"
    sender = os.environ.get("SKILLSPHERE_SMTP_FROM") or os.environ.get("SKILLSPHERE_SMTP_USERNAME")
    username = os.environ.get("SKILLSPHERE_SMTP_USERNAME", "")
    password = os.environ.get("SKILLSPHERE_SMTP_PASSWORD", "")
    if not sender or (username and not password):
        logger.error("Notification email configuration is incomplete")
        return "Failed"
    email = EmailMessage()
    email["Subject"] = title
    email["From"] = sender
    email["To"] = user["email"]
    email.set_content(message)
    try:
        port = int(os.environ.get("SKILLSPHERE_SMTP_PORT", "587"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                if username:
                    server.login(username, password)
                server.send_message(email)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if username:
                    server.login(username, password)
                server.send_message(email)
        return "Sent"
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send notification email")
        return "Failed"


def notify_user(
    db: sqlite3.Connection,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    *,
    application_id: int | None = None,
    assessment_id: int | None = None,
    email: bool = False,
    action_url: str = "",
) -> int:
    delivery = email_notification(db, user_id, title, message) if email else "Not Sent"
    notification_id = add_notification(
        db, user_id, notification_type, title, message,
        application_id=application_id, assessment_id=assessment_id,
        email_delivery_status=delivery, action_url=action_url,
    )
    if delivery == "Failed":
        admins = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchall()
        for admin in admins:
            add_notification(
                db, admin["id"], "email_failure", "Notification email failed",
                f"Email notification delivery failed for a {notification_type} update.",
                application_id=application_id, assessment_id=assessment_id,
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


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
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
    user["profile"] = json.loads(user.pop("profile_json") or "{}")
    return user


def get_current_user(authorization: str | None = None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue")
    payload = decode_token(authorization[7:])
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (payload.get("sub"),)).fetchone()
    if not row or not row["active"]:
        raise HTTPException(status_code=401, detail="Account no longer exists")
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
    description: str = Field(min_length=20, max_length=10_000)
    location: str = Field(min_length=2, max_length=100)
    experience: str = Field(min_length=2, max_length=80)
    job_type: str = Field(default="Full-time", max_length=30)
    salary: str = Field(default="", max_length=80)
    skills: list[str] = Field(min_length=1, max_length=30)


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
    answers: dict[int, int] = Field(max_length=20)


class AssessmentProctoringStartBody(BaseModel):
    consent_given: bool
    camera_permission_granted: bool


class AssessmentProctoringEventBody(BaseModel):
    event_type: str = Field(pattern="^(camera_granted|camera_denied|assessment_started|face_missing|face_returned|multiple_faces|single_face_restored|tab_hidden|window_blur|window_focus|camera_stopped)$")
    duration_seconds: int | None = Field(default=None, ge=0, le=3600)


class QuestionBody(BaseModel):
    technology: str = Field(min_length=1, max_length=50)
    question: str = Field(min_length=10, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    difficulty: str = Field(pattern="^(Easy|Medium|Hard)$")
    active: bool = True


class QuestionUpdateBody(QuestionBody):
    pass


class QuestionImportItem(BaseModel):
    source_index: int = Field(ge=0)
    technology: str = Field(min_length=1, max_length=50)
    difficulty: str = Field(pattern="^(Easy|Medium|Hard)$")
    question: str = Field(min_length=10, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct_index: int = Field(ge=0, le=3)
    include: bool = True


class QuestionImportConfirmBody(BaseModel):
    questions: list[QuestionImportItem] = Field(max_length=500)


class ApplicationStatusBody(BaseModel):
    status: str
    note: str = Field(default="", max_length=2000)


class InternalNoteBody(BaseModel):
    note: str = Field(max_length=4000)


class NotificationReadBody(BaseModel):
    is_read: bool = True


class InterviewBody(BaseModel):
    application_id: int
    scheduled_at: str = Field(min_length=10, max_length=40)
    teams_url: str = Field(min_length=1, max_length=2048, strip_whitespace=True)


class InterviewInvitationBody(BaseModel):
    scheduled_at: str = Field(min_length=10, max_length=40)
    timezone: str = Field(min_length=1, max_length=80)
    duration_minutes: int = Field(ge=5, le=480)
    meeting_platform: str = Field(pattern="^(Google Meet|Microsoft Teams|Zoom|Other)$")
    meeting_url: str = Field(min_length=1, max_length=2048, strip_whitespace=True)
    recruiter_message: str = Field(default="", max_length=2000)


class InterviewUpdateBody(BaseModel):
    scheduled_at: str | None = Field(default=None, min_length=10, max_length=40)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    meeting_platform: str | None = Field(default=None, pattern="^(Google Meet|Microsoft Teams|Zoom|Other)$")
    meeting_url: str | None = Field(default=None, min_length=1, max_length=2048, strip_whitespace=True)
    recruiter_message: str | None = Field(default=None, max_length=2000)


class InterviewWorkspaceBody(BaseModel):
    code: str = Field(max_length=50_000)
    language: str = Field(default="python", pattern="^(python|javascript|typescript|java|cpp)$")


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
                status TEXT NOT NULL DEFAULT 'Open' CHECK(status IN ('Open', 'Closed')),
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
                status TEXT NOT NULL CHECK(status IN ('Not Sent', 'Sent', 'Started', 'Completed', 'Expired', 'Manual Topic Required')),
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
                meeting_platform TEXT NOT NULL DEFAULT 'Microsoft Teams',
                meeting_url TEXT NOT NULL DEFAULT '',
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                recruiter_message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Scheduled'
                    CHECK(status IN ('Scheduled', 'Rescheduled', 'Cancelled', 'Completed')),
                invitation_sent_at TEXT,
                candidate_viewed_at TEXT,
                code TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'python',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "active" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
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
        ):
            if column not in job_question_columns:
                db.execute(f"ALTER TABLE job_questions ADD COLUMN {column} {definition}")
        db.execute(
            """UPDATE job_questions SET recruiter_id = owner_id
            WHERE recruiter_id IS NULL"""
        )
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
        ):
            if column not in assessment_columns:
                db.execute(f"ALTER TABLE assessments ADD COLUMN {column} {definition}")
        db.execute(
            """UPDATE assessments SET proctoring_required = 0
            WHERE status = 'Started' AND starts_at IS NOT NULL
            AND proctoring_consent_at IS NULL AND proctoring_started_at IS NULL"""
        )
        interview_columns = {row["name"] for row in db.execute("PRAGMA table_info(interviews)")}
        for column, definition in (
            ("meeting_platform", "TEXT NOT NULL DEFAULT 'Microsoft Teams'"),
            ("meeting_url", "TEXT NOT NULL DEFAULT ''"),
            ("duration_minutes", "INTEGER NOT NULL DEFAULT 60"),
            ("timezone", "TEXT NOT NULL DEFAULT 'UTC'"),
            ("recruiter_message", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'Scheduled' CHECK(status IN ('Scheduled', 'Rescheduled', 'Cancelled', 'Completed'))"),
            ("invitation_sent_at", "TEXT"),
            ("candidate_viewed_at", "TEXT"),
        ):
            if column not in interview_columns:
                db.execute(f"ALTER TABLE interviews ADD COLUMN {column} {definition}")
        db.execute(
            """UPDATE interviews SET meeting_url = teams_url
            WHERE meeting_url = '' AND teams_url != ''"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_interviews_application_schedule ON interviews(application_id, scheduled_at)"
        )
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
            """SELECT x.application_id, a.status, x.result, x.score_percentage,
            x.correct_answer_count, x.total_questions, x.pass_percentage
            FROM assessments x JOIN applications a ON a.id = x.application_id
            WHERE x.status = 'Completed' AND x.result IS NOT NULL
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
        db.execute(
            """INSERT INTO application_status_history
            (application_id, status, actor_role, created_at)
            SELECT a.id, a.status, 'system', a.applied_at FROM applications a
            WHERE NOT EXISTS (
                SELECT 1 FROM application_status_history h WHERE h.application_id = a.id
            )"""
        )
        if IS_PRODUCTION:
            admin_email = os.environ.get("SKILLSPHERE_ADMIN_EMAIL")
            admin_password = os.environ.get("SKILLSPHERE_ADMIN_PASSWORD")
            if admin_email and admin_password and not db.execute(
                "SELECT 1 FROM users WHERE email = ?", (admin_email,)
            ).fetchone():
                db.execute(
                    "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                    ("SkillSphere Admin", admin_email, password_hash(admin_password), now()),
                )
            return
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        users = [
            ("Alex Morgan", "candidate@skillsphere.dev", password_hash("Demo123!"), "candidate", '{"location":"Brooklyn, NY"}', now()),
            ("Jamie Rivera", "recruiter@skillsphere.dev", password_hash("Demo123!"), "recruiter", '{"company":"Northstar"}', now()),
            ("Avery Chen", "admin@skillsphere.dev", password_hash("Demo123!"), "admin", "{}", now()),
        ]
        if os.environ.get("SKILLSPHERE_ENV", "development").lower() == "production":
            admin_email = os.environ.get("SKILLSPHERE_ADMIN_EMAIL")
            admin_password = os.environ.get("SKILLSPHERE_ADMIN_PASSWORD")
            if admin_email and admin_password:
                db.execute(
                    "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
                    ("SkillSphere Admin", admin_email, password_hash(admin_password), now()),
                )
            return
        db.executemany(
            "INSERT INTO users (name, email, password_hash, role, profile_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            users,
        )
        recruiter_id = db.execute("SELECT id FROM users WHERE email = ?", ("recruiter@skillsphere.dev",)).fetchone()["id"]
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


def public_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["skills"] = json.loads(job.pop("skills_json"))
    return job


def public_resume(row: sqlite3.Row) -> dict[str, Any]:
    resume = dict(row)
    resume.pop("storage_path", None)
    resume.pop("extracted_text", None)
    resume["parsed_profile"] = json.loads(resume.pop("parsed_profile_json") or "{}")
    return resume


def public_application(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def detect_profile(text: str) -> dict[str, Any]:
    email = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I)
    phone = re.search(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)", text)
    name = None
    for line in (line.strip() for line in text.splitlines()):
        if line and len(line) < 70 and not re.search(r"[@\d]", line) and len(line.split()) <= 5:
            name = line
            break
    known_skills = (
        "python", "javascript", "typescript", "react", "fastapi", "sql", "postgresql",
        "figma", "ux research", "product design", "project management", "excel",
        "tableau", "data analysis", "html", "css", "node.js", "communication",
        "leadership", "machine learning", "java", "aws", "docker",
    )
    skills = [
        skill.title()
        for skill in known_skills
        if re.search(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])", text, re.I)
    ]
    return {
        "name": name or "",
        "email": email.group(0) if email else "",
        "phone": phone.group(0).strip() if phone else "",
        "skills": skills,
        "summary": "",
    }


def calculate_match(
    job: sqlite3.Row | dict[str, Any],
    resume: sqlite3.Row | dict[str, Any],
    saved_profile_json: str,
) -> dict[str, Any]:
    required = json.loads(job["skills_json"])
    resume_profile = json.loads(resume["parsed_profile_json"] or "{}")
    saved_profile = json.loads(saved_profile_json or "{}")
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


def relevant_balanced_question_sample(
    questions: list[sqlite3.Row],
    topic_weights: dict[str, int],
    count: int,
) -> list[sqlite3.Row]:
    groups = {
        topic: [question for question in questions if question["technology"].casefold() == topic.casefold()]
        for topic in topic_weights
    }
    active_topics = [topic for topic, group in groups.items() if group]
    if sum(len(group) for group in groups.values()) < count or not active_topics:
        return []
    total_weight = sum(topic_weights[topic] for topic in active_topics)
    desired = {topic: count * topic_weights[topic] / total_weight for topic in active_topics}
    allocation = {topic: min(len(groups[topic]), int(desired[topic])) for topic in active_topics}
    remaining = count - sum(allocation.values())
    while remaining:
        candidates = [topic for topic in active_topics if allocation[topic] < len(groups[topic])]
        if not candidates:
            return []
        candidates.sort(
            key=lambda topic: (desired[topic] - allocation[topic], topic_weights[topic]),
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
    if match_percentage < 70:
        return
    questions = db.execute(
        """SELECT id, technology, question, options_json, correct_index, difficulty
        FROM job_questions WHERE job_id = ? AND active = 1 ORDER BY id""",
        (job["id"],),
    ).fetchall()
    available_topics: dict[str, int] = {}
    for question in questions:
        available_topics[question["technology"]] = available_topics.get(question["technology"], 0) + 1
    topic_weights, reasoning = relevant_question_topic_weights(
        job, resume, saved_profile_json, available_topics
    )
    relevant_questions = [
        question for question in questions
        if question["technology"].casefold() in {topic.casefold() for topic in topic_weights}
    ]
    manual_topics: list[str] = []
    if not topic_weights:
        manual_topics = [topic for topic, count in available_topics.items() if count >= 20]
    if len(relevant_questions) < 20 and not manual_topics:
        additional_needed = 20 - len(relevant_questions)
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
                    f"{additional_needed} more relevant active question(s) are needed for {job['title']}.",
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
        cursor = db.execute(
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


def extract_resume_text(content: bytes, content_type: str) -> str:
    if content_type == "application/pdf":
        from pypdf import PdfReader
        import io

        try:
            reader = PdfReader(io.BytesIO(content))
        except Exception as error:
            raise HTTPException(status_code=422, detail="This PDF is unreadable or corrupted. Please check the file and try again.") from error
        if len(reader.pages) > 40:
            raise HTTPException(status_code=422, detail="This PDF has too many pages. Please upload a document with 40 pages or fewer.")
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if text:
            return text
        import pypdfium2 as pdfium
        import pytesseract

        document = pdfium.PdfDocument(content)
        pages = []
        for index in range(len(document)):
            image = document[index].render(scale=1.5).to_pil()
            pages.append(pytesseract.image_to_string(image))
        return "\n".join(pages).strip()
    from PIL import Image
    import io
    import pytesseract

    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        image = Image.open(io.BytesIO(content)).convert("RGB")
    except Exception as error:
        raise HTTPException(status_code=422, detail="This image is unreadable or corrupted. Please check the file and try again.") from error
    return pytesseract.image_to_string(image).strip()


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/register", status_code=201)
def register(body: RegisterBody) -> dict[str, Any]:
    role = body.role.lower()
    if role not in {"candidate", "recruiter"}:
        raise HTTPException(status_code=422, detail="Choose a job seeker or recruiter account")
    try:
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (body.name.strip(), body.email.lower(), password_hash(body.password), role, now()),
            )
            row = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="An account with this email already exists") from error
    user = public_user(row)
    return {"access_token": encode_token(row), "token_type": "bearer", "user": user}


@app.post("/api/auth/login")
def login(body: LoginBody) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE email = ?", (body.email.lower(),)).fetchone()
    if not row or not row["active"] or not check_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
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
        clauses.append("j.recruiter_id = ?")
        values.append(user["id"])
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
            f"SELECT j.* FROM jobs j WHERE {where} ORDER BY {sort_options[sort_by]}, j.id ASC LIMIT ? OFFSET ?",
            [*values, page_size, (page - 1) * page_size],
        ).fetchall()
    return {"items": [public_job(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    del user
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id = ? AND status = 'Open'", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="This role is no longer available")
    return public_job(row)


@app.post("/api/jobs", status_code=201)
def create_job(body: JobBody, user: dict[str, Any] = Depends(require_role("recruiter"))) -> dict[str, Any]:
    skills = list(dict.fromkeys(skill.strip() for skill in body.skills if skill.strip()))
    if not skills:
        raise HTTPException(status_code=422, detail="Add at least one required skill")
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO jobs (recruiter_id, title, company, description, location, experience, job_type, salary, skills_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["id"], body.title.strip(), body.company.strip(), body.description.strip(), body.location.strip(),
             body.experience.strip(), body.job_type.strip(), body.salary.strip(), json.dumps(skills), now()),
        )
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        admins = db.execute("SELECT id FROM users WHERE role = 'admin' AND active = 1").fetchall()
        for admin in admins:
            add_notification(
                db, admin["id"], "job_posted", "New job posted",
                f"{body.title} was posted by {body.company}.",
            )
    return public_job(row)


def owned_job(db: sqlite3.Connection, job_id: int, user: dict[str, Any]) -> sqlite3.Row:
    job = db.execute(
        "SELECT * FROM jobs WHERE id = ? AND (? = 'admin' OR recruiter_id = ?)",
        (job_id, user["role"], user["id"]),
    ).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job listing not found for your account")
    return job


def validated_question(body: QuestionBody) -> tuple[str, str, list[str], int, str, int]:
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
    )


def job_question_summary(db: sqlite3.Connection, job_id: int) -> dict[str, Any]:
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
         "ready": row["active_count"] >= 20, "additional_needed": max(0, 20 - row["active_count"])}
        for row in technology_rows
    ]
    active_total = sum(topic["active_count"] for topic in topics)
    needed = max(0, 20 - active_total)
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
        "active_count": active_total,
        "difficulty_counts": total_by_difficulty,
        "ready": active_total >= 20,
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


def question_import_preview(row: sqlite3.Row) -> dict[str, Any]:
    questions = json.loads(row["preview_json"] or "[]")
    valid_questions = [item for item in questions if item["valid"]]
    invalid_questions = [item for item in questions if not item["valid"]]
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
    technology, question, options, correct_index, difficulty, active = validated_question(body)
    with connect() as db:
        job = owned_job(db, job_id, user)
        try:
            cursor = db.execute(
                """INSERT INTO job_questions
                (job_id, owner_id, recruiter_id, technology, question, options_json, correct_index,
                 difficulty, active, source_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)""",
                (
                    job_id, job["recruiter_id"], job["recruiter_id"], technology, question, json.dumps(options),
                    correct_index, difficulty, active, now(), now(),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="That question already exists for this job") from error
        row = db.execute("SELECT * FROM job_questions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        summary = job_question_summary(db, job_id)
    if summary["ready"]:
        auto_send_ready_assessments(job_id)
    return {**dict(row), "options": options, "options_json": None, "summary": summary}


@app.patch("/api/jobs/{job_id}/questions/{question_id}")
def update_job_question(
    job_id: int,
    question_id: int,
    body: QuestionUpdateBody,
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    technology, question, options, correct_index, difficulty, active = validated_question(body)
    with connect() as db:
        owned_job(db, job_id, user)
        try:
            result = db.execute(
                """UPDATE job_questions SET technology = ?, question = ?, options_json = ?,
                correct_index = ?, difficulty = ?, active = ?, updated_at = ?
                WHERE id = ? AND job_id = ?""",
                (technology, question, json.dumps(options), correct_index, difficulty, active, now(), question_id, job_id),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="That question already exists for this job") from error
        if not result.rowcount:
            raise HTTPException(status_code=404, detail="Question not found for this job")
        row = db.execute("SELECT * FROM job_questions WHERE id = ?", (question_id,)).fetchone()
        summary = job_question_summary(db, job_id)
    if summary["ready"]:
        auto_send_ready_assessments(job_id)
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
                CASE WHEN x.id IS NULL AND a.status = 'Shortlisted' AND a.match_percentage >= 70
                THEN 'Pending Question Bank' ELSE x.status END AS assessment_status,
                x.technology AS assessment_topic, x.assessment_name, x.assessment_round,
                x.email_sent_at AS assessment_email_sent_at, x.starts_at AS assessment_started_at,
                x.submitted_at AS assessment_submitted_at, x.score AS assessment_score,
                x.score_percentage AS assessment_score_percentage, x.result AS assessment_result,
                x.correct_answer_count AS assessment_correct_count,
                x.incorrect_answer_count AS assessment_incorrect_count
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
    items = []
    for row in rows:
        item = dict(row)
        item["matched_skills"] = json.loads(item.pop("matched_skills_json") or "[]")
        item["missing_skills"] = json.loads(item.pop("missing_skills_json") or "[]")
        interview = interviews_by_application.get(item["id"])
        if interview and user["role"] == "candidate" and interview["status"] in {"Cancelled", "Completed"}:
            interview["meeting_url"] = ""
        item["interview"] = interview
        if (
            user["role"] == "candidate"
            and os.environ.get("SKILLSPHERE_ASSESSMENT_SHOW_SCORE", "true").lower() != "true"
        ):
            item["assessment_score"] = None
            item["assessment_score_percentage"] = None
            item["assessment_correct_count"] = None
            item["assessment_incorrect_count"] = None
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
            if not item["status"].startswith("Round 1 "):
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
    smtp_host = os.environ.get("SKILLSPHERE_SMTP_HOST", "")
    if not smtp_host:
        if IS_PRODUCTION:
            raise HTTPException(status_code=503, detail="Assessment email is not configured on this server")
        logger.warning("Development assessment email for %s: %s", recipient, assessment_url)
        return "development"

    sender = os.environ.get("SKILLSPHERE_SMTP_FROM") or os.environ.get("SKILLSPHERE_SMTP_USERNAME")
    username = os.environ.get("SKILLSPHERE_SMTP_USERNAME", "")
    password = os.environ.get("SKILLSPHERE_SMTP_PASSWORD", "")
    if not sender or (username and not password):
        raise HTTPException(status_code=503, detail="SMTP sender or credentials are incomplete")
    if IS_PRODUCTION and not os.environ.get("SKILLSPHERE_PUBLIC_BASE_URL"):
        raise HTTPException(status_code=503, detail="Set SKILLSPHERE_PUBLIC_BASE_URL before sending assessment links")

    message = EmailMessage()
    message["Subject"] = f"Technical assessment: {job_title}"
    message["From"] = sender
    message["To"] = recipient
    expiry_label = datetime.fromisoformat(expires_at).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message.set_content(
        f"""You have been shortlisted for {job_title} at {company}.

Please complete this 20-question {technology} assessment. You will have
{ASSESSMENT_DURATION_MINUTES} minutes after starting, and this link expires
on {expiry_label}.

Start your assessment: {assessment_url}

The assessment is individual and can only be submitted once.
"""
    )
    port = int(os.environ.get("SKILLSPHERE_SMTP_PORT", "587"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, timeout=20) as server:
                if username:
                    server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if username:
                    server.login(username, password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        logger.exception("Could not send technical assessment email")
        raise HTTPException(status_code=502, detail="Could not send the assessment email") from error
    return "email"


def managed_assessment(
    db: sqlite3.Connection,
    application_id: int,
    user: dict[str, Any],
) -> sqlite3.Row:
    row = db.execute(
        """SELECT a.id AS application_id, a.candidate_id, a.job_id, a.status AS application_status,
        a.match_percentage, u.email AS candidate_email, j.title, j.company,
        j.recruiter_id AS owner_id,
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
    if managed["match_percentage"] is None or managed["match_percentage"] < 70:
        raise HTTPException(status_code=409, detail="Only candidates with a match of 70% or higher are eligible")
    if not managed["assessment_id"]:
        raise HTTPException(status_code=409, detail="Assessment data is unavailable for this application")
    if not managed["technology"]:
        raise HTTPException(status_code=409, detail="Select an assessment topic before sending the assessment")
    allowed = {"Sent", "Expired"} if reissue else {"Not Sent"}
    if managed["assessment_status"] not in allowed:
        if reissue:
            raise HTTPException(status_code=409, detail="Only sent or expired assessments can be reissued")
        raise HTTPException(status_code=409, detail="Use reissue for an assessment that has already been sent")

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
        f"""SELECT id, technology, question, options_json, correct_index, difficulty FROM job_questions
        WHERE job_id = ? AND lower(technology) IN ({placeholders}) AND active = 1""",
        (managed["job_id"], *(topic.casefold() for topic in topics)),
    ).fetchall()
    selected = relevant_balanced_question_sample(questions, topic_weights, 20)
    if len(selected) != 20:
        raise HTTPException(
            status_code=409,
            detail="The selected question topics need at least 20 active questions in total.",
        )
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    created = datetime.now(timezone.utc)
    expires_at = (created.timestamp() + ASSESSMENT_TTL_HOURS * 3600)
    expiry = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    assessment_url = f"{PUBLIC_BASE_URL}/?{urlencode({'assessment': token})}"
    delivery = assessment_delivery(
        managed["candidate_email"], managed["title"], managed["company"], managed["technology"], expiry, assessment_url
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
            (assessment_id, question_id, question_order, question_snapshot, options_snapshot, correct_index)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                managed["assessment_id"], legacy_question["id"], order, question["question"],
                json.dumps(options), options.index(correct_option),
            ),
        )
    db.execute(
        """UPDATE assessments SET token_hash = ?, status = 'Sent', starts_at = NULL,
        expires_at = ?, submitted_at = NULL, score = NULL, total_questions = 20,
        assessment_name = ?, assessment_round = 'Round 1', pass_percentage = ?,
        score_percentage = NULL, correct_answer_count = NULL, incorrect_answer_count = NULL,
        result = NULL, result_processed_at = NULL, time_taken_seconds = NULL,
        duration_minutes = ?, email_sent_at = ?, proctoring_required = ?,
        proctoring_consent_at = NULL, camera_permission_granted_at = NULL,
        proctoring_started_at = NULL, proctoring_warning_count = 0,
        proctoring_flagged_at = NULL
        WHERE id = ?""",
        (
            token_hash, expiry, ROUND1_ASSESSMENT_NAME, ROUND1_PASS_PERCENTAGE,
            ASSESSMENT_DURATION_MINUTES,
            now() if delivery == "email" else None,
            int(PROCTORING_REQUIRED),
            managed["assessment_id"],
        ),
    )
    add_notification(
        db,
        managed["candidate_id"],
        "assessment_sent",
        "Your assessment is ready",
        f"Your {managed['technology']} assessment for {managed['title']} is available. The secure link was sent to your registered email.",
        application_id=managed["application_id"],
        assessment_id=managed["assessment_id"],
        action_url=assessment_url if delivery == "development" else "",
        email_delivery_status="Sent" if delivery == "email" else "Development",
    )
    add_notification(
        db, managed["owner_id"], "assessment_sent", "Assessment link sent",
        f"A {managed['technology']} assessment link was sent to {managed['candidate_email']} for {managed['title']}.",
        application_id=managed["application_id"], assessment_id=managed["assessment_id"],
        email_delivery_status="Sent" if delivery == "email" else "Development",
    )
    return {
        "status": "Sent",
        "delivery": delivery,
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
            if not summary["ready"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A job-specific question bank needs at least 20 active questions in one topic. "
                        f"Add {summary['additional_needed']} more active questions."
                    ),
                )
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
             WHERE q.assessment_id = x.id AND q.selected_answer IS NOT NULL) AS answered_count
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
        item["score_percentage"] = (
            item["score_percentage"]
            if item["score_percentage"] is not None
            else round(item["score"] / item["total_questions"] * 100, 2)
            if item["score"] is not None and item["total_questions"] else None
        )
        if (
            user["role"] == "candidate"
            and os.environ.get("SKILLSPHERE_ASSESSMENT_SHOW_SCORE", "true").lower() != "true"
        ):
            item["score"] = None
            item["score_percentage"] = None
            item["correct_answer_count"] = None
            item["incorrect_answer_count"] = None
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
    user: dict[str, Any] = Depends(require_role("recruiter", "admin")),
) -> dict[str, Any]:
    with connect() as db:
        conditions = "x.id = ? AND (? = 'admin' OR j.recruiter_id = ?)"
        row = db.execute(
            f"""SELECT x.id, x.application_id, x.technology, x.status, x.match_percentage,
            x.assessment_name, x.assessment_round, x.pass_percentage,
            x.starts_at, x.expires_at, x.submitted_at, x.score, x.score_percentage,
            x.total_questions, x.correct_answer_count, x.incorrect_answer_count, x.result,
            x.result_processed_at, x.time_taken_seconds, a.status AS application_status,
            x.duration_minutes, j.title, j.company, u.name AS candidate_name, u.email AS candidate_email
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            JOIN users u ON u.id = x.candidate_id
            JOIN applications a ON a.id = x.application_id WHERE {conditions}""",
            (assessment_id, user["role"], user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Assessment not found for your job listings")
        if row["status"] != "Completed" or row["result"] not in {"Passed", "Failed"}:
            raise HTTPException(status_code=409, detail="Question-by-question review is available after submission")
        questions = db.execute(
            """SELECT q.question_order, q.question_snapshot, q.options_snapshot,
            q.selected_answer, q.correct_index, q.is_correct
            FROM assessment_questions q WHERE q.assessment_id = ? ORDER BY q.question_order""",
            (assessment_id,),
        ).fetchall()
    details = []
    for question in questions:
        options = json.loads(question["options_snapshot"])
        details.append({
            "order": question["question_order"],
            "question": question["question_snapshot"],
            "options": options,
            "selected_answer": question["selected_answer"],
            "correct_answer": question["correct_index"],
            "is_correct": bool(question["is_correct"]) if question["is_correct"] is not None else None,
        })
    return {**dict(row), "questions": details}


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
            candidate.name AS candidate_name, candidate.email AS candidate_email
            FROM assessments x JOIN jobs j ON j.id = x.job_id
            JOIN users candidate ON candidate.id = x.candidate_id
            JOIN users recruiter ON recruiter.id = j.recruiter_id
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
        if managed["match_percentage"] is None or managed["match_percentage"] < 70:
            raise HTTPException(status_code=409, detail="Only candidates with a match of 70% or higher are eligible")
        if managed["application_status"] in {"Withdrawn", "Rejected", "Accepted", "Selected"}:
            raise HTTPException(status_code=409, detail="This application is no longer eligible for an assessment")
        if managed["assessment_status"] != "Manual Topic Required":
            raise HTTPException(status_code=409, detail="A manual topic can only be set when automatic selection is unavailable")
            # Manual selections must still have a complete, relevant job-specific bank.
        bank_count = db.execute(
            """SELECT COUNT(*) FROM job_questions
            WHERE job_id = ? AND lower(technology) = lower(?) AND active = 1""",
            (managed["job_id"], body.technology),
        ).fetchone()[0]
        if bank_count < 20:
            raise HTTPException(
                status_code=409,
                detail=f"Add {20 - bank_count} more active {body.technology} questions before selecting this topic",
            )
        db.execute(
            """UPDATE assessments SET technology = ?, status = 'Not Sent',
            question_topics_json = ?, question_topic_weights_json = ?, selection_reasoning = ?
            WHERE id = ?""",
            (
                body.technology, json.dumps([body.technology]),
                json.dumps({body.technology: 1}),
                "The recruiter selected this topic because no relevant topic could be inferred automatically.",
                managed["assessment_id"],
            ),
        )
    return {"technology": body.technology, "status": "Not Sent"}


def public_assessment_for_token(
    db: sqlite3.Connection,
    token: str,
    *,
    check_deadline: bool = True,
) -> tuple[sqlite3.Row | None, str | None]:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute(
        """SELECT x.*, j.title, j.company, a.status AS application_status
        FROM assessments x JOIN jobs j ON j.id = x.job_id
        JOIN applications a ON a.id = x.application_id
        WHERE x.token_hash = ?""",
        (token_hash,),
    ).fetchone()
    if not row:
        return None, "This assessment link is invalid or has already been used"
    if row["application_status"] in {"Withdrawn", "Rejected", "Accepted", "Selected"}:
        db.execute(
            "UPDATE assessments SET token_hash = NULL, status = 'Expired' WHERE id = ? AND status != 'Completed'",
            (row["id"],),
        )
        return row, "This assessment is no longer available"
    if row["status"] not in {"Sent", "Started"}:
        return row, "This assessment link is invalid or has already been used"
    if check_deadline and row["expires_at"]:
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
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
        if not body.consent_given or not body.camera_permission_granted:
            insert_proctoring_event(db, row["id"], "camera_denied", "info")
            return {"started": False, "required": bool(row["proctoring_required"])}
        if row["proctoring_consent_at"] is None or row["camera_permission_granted_at"] is None:
            consented_at = now()
            db.execute(
                """UPDATE assessments SET proctoring_consent_at = ?,
                camera_permission_granted_at = ? WHERE id = ?""",
                (consented_at, consented_at, row["id"]),
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
        if row and not error and row["proctoring_required"] and (
            row["proctoring_consent_at"] is None or row["camera_permission_granted_at"] is None
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
                    status = "Completed"
                    changed = db.execute(
                        """UPDATE assessments SET status = ?, submitted_at = ?, score = ?,
                        score_percentage = ?, correct_answer_count = ?, incorrect_answer_count = ?,
                        result = ?, result_processed_at = ?, time_taken_seconds = ?,
                        token_hash = NULL
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
                                    application_status = "Assessment Completed"
                                else:
                                    application_status = application["status"]
                                if application_status != application["status"]:
                                    record_application_status(
                                        db, application["id"], "Assessment Completed",
                                        {"id": None, "role": "candidate"},
                                    )
                                final_status = f"Round 1 {result}"
                                if application_status != final_status:
                                    db.execute(
                                        "UPDATE applications SET status = ? WHERE id = ?",
                                        (final_status, application["id"]),
                                    )
                                    record_application_status(
                                        db, application["id"], final_status,
                                        {"id": None, "role": "system"},
                                        f"Round 1 score: {score_percentage:g}% ({score}/{len(questions)} correct); "
                                        f"passing score: {row['pass_percentage']:g}%.",
                                    )
                            result_label = f"{score_percentage:g}% ({score}/{len(questions)})"
                            if result == "Passed":
                                candidate_title = "You passed Round 1"
                                candidate_message = (
                                    f"You scored {result_label} on the Round 1 assessment for "
                                    f"{row['title']}. The recruiter will review your profile for next steps."
                                )
                            else:
                                candidate_title = "Round 1 result"
                                candidate_message = (
                                    f"You scored {result_label} on the Round 1 assessment for {row['title']}. "
                                    f"This result is below the required passing score of {row['pass_percentage']:g}%."
                                )
                            notify_user(
                                db, application["recruiter_id"], "round1_assessment_completed",
                                "Round 1 assessment completed",
                                f"{application['candidate_name']} completed the Round 1 assessment for "
                                f"{row['title']} with {result_label}: {result}.",
                                application_id=application["id"], assessment_id=row["id"], email=True,
                            )
                            notify_user(
                                db, application["candidate_id"], "round1_result",
                                candidate_title, candidate_message,
                                application_id=application["id"], assessment_id=row["id"], email=True,
                            )
                            admins = db.execute(
                                "SELECT id FROM users WHERE role = 'admin' AND active = 1"
                            ).fetchall()
                            for admin in admins:
                                add_notification(
                                    db, admin["id"], "round1_assessment_completed",
                                    "Round 1 assessment completed",
                                    f"{application['candidate_name']} completed Round 1 for {row['title']}: "
                                    f"{result_label}, {result}.",
                                    application_id=application["id"], assessment_id=row["id"],
                                )
                        visible_score = (
                            score
                            if os.environ.get("SKILLSPHERE_ASSESSMENT_SHOW_SCORE", "true").lower() == "true"
                            else None
                        )
                        passed_message = (
                            "Congratulations, you have cleared Round 1."
                            if result == "Passed"
                            else "Thank you for completing Round 1. You did not reach the required passing score."
                        )
                        response = {
                            "status": status,
                            "score": visible_score,
                            "total_questions": len(questions),
                            "assessment_name": row["assessment_name"],
                            "assessment_round": row["assessment_round"],
                            "job_title": row["title"],
                            "pass_percentage": row["pass_percentage"],
                            "correct_answer_count": score,
                            "incorrect_answer_count": len(questions) - score,
                            "score_percentage": score_percentage,
                            "result": result,
                            "submitted_at": submitted_at,
                            "time_taken_seconds": time_taken_seconds,
                            "message": passed_message,
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


def public_interview(
    interview: sqlite3.Row | dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    result = dict(interview)
    if user["role"] == "candidate" and result["status"] in {"Cancelled", "Completed"}:
        result["meeting_url"] = ""
        result["teams_url"] = ""
    return result


def interview_details(db: sqlite3.Connection, interview_id: int, user: dict[str, Any]) -> sqlite3.Row:
    access_clause, access_values = interview_access_clause(user)
    row = db.execute(
        f"""SELECT i.id, i.application_id, i.recruiter_id, i.scheduled_at, i.teams_url,
            COALESCE(NULLIF(i.meeting_url, ''), i.teams_url) AS meeting_url,
            i.meeting_platform, i.duration_minutes, i.timezone, i.recruiter_message,
            i.status, i.invitation_sent_at, i.candidate_viewed_at,
            i.code, i.language, i.created_at, i.updated_at, a.candidate_id, a.job_id,
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
                i.meeting_platform, i.duration_minutes, i.timezone, i.recruiter_message,
                i.status, i.invitation_sent_at, i.candidate_viewed_at,
                i.language, i.created_at, i.updated_at, a.candidate_id, a.job_id,
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
    cursor = db.execute(
        """INSERT INTO interviews
        (application_id, recruiter_id, scheduled_at, teams_url, meeting_platform, meeting_url,
         duration_minutes, timezone, recruiter_message, status, invitation_sent_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Scheduled', ?, ?, ?)""",
        (
        application["id"], application["recruiter_id"], scheduled_at,
        meeting_url if body.meeting_platform == "Microsoft Teams" else "",
        body.meeting_platform, meeting_url, body.duration_minutes, body.timezone,
        body.recruiter_message, timestamp, timestamp, timestamp,
        ),
    )
    if application["status"] != "Interview":
        db.execute("UPDATE applications SET status = 'Interview' WHERE id = ?", (application["id"],))
    record_application_status(
        db, application["id"], "Interview", user,
        f"Interview scheduled for {scheduled_at}.",
    )
    interview = dict(interview_details(db, cursor.lastrowid, user))
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
    if "meeting_url" in updates:
        updates["meeting_url"] = validate_meeting_url(updates["meeting_url"])
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


@app.put("/api/interviews/{interview_id}/workspace")
def update_interview_workspace(
    interview_id: int,
    body: InterviewWorkspaceBody,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    if user["role"] not in {"candidate", "recruiter"}:
        raise HTTPException(status_code=403, detail="This action is not available for your account")
    with connect() as db:
        interview_details(db, interview_id, user)
        updated_at = now()
        db.execute(
            "UPDATE interviews SET code = ?, language = ?, updated_at = ? WHERE id = ?",
            (body.code, body.language, updated_at, interview_id),
        )
        interview = interview_details(db, interview_id, user)
    return dict(interview)


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
        "Assessment Completed": {"Interview", "Accepted", "Rejected"},
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
                "SELECT result FROM assessments WHERE application_id = ?", (application_id,)
            ).fetchone()
            expected_result = "Passed" if new_status == "Round 1 Passed" else "Failed"
            if not assessment or assessment["result"] != expected_result:
                raise HTTPException(
                    status_code=409,
                    detail="Round 1 pipeline stages are updated automatically from the assessment result",
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
                status = CASE WHEN status = 'Completed' THEN status ELSE 'Expired' END
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
        row = db.execute(
            """SELECT r.storage_path, r.filename, r.content_type FROM applications a
            JOIN jobs j ON j.id = a.job_id JOIN resumes r ON r.id = a.resume_id
            WHERE a.id = ? AND (? = 'admin' OR j.recruiter_id = ?)""",
            (application_id, user["role"], user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Resume not found for your job listings")
    path = Path(row["storage_path"]).resolve()
    if not path.is_relative_to(UPLOAD_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Resume file is unavailable")
    return FileResponse(path, filename=row["filename"], media_type=row["content_type"])


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
    mime_by_suffix = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    expected_type = mime_by_suffix.get(suffix)
    if not expected_type:
        raise HTTPException(status_code=415, detail="Use a PDF, JPG, JPEG, or PNG resume")
    if expected_type == "application/pdf" and not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="This file does not contain a valid PDF")
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
            processing_error = "No readable text was found. Try a clearer, text-based PDF or image."
    except HTTPException:
        raise
    except Exception as error:
        text = ""
        status = "needs_review"
        if error.__class__.__name__ == "TesseractNotFoundError":
            processing_error = "Resume uploaded, but OCR is unavailable. Install Tesseract OCR and try again."
        elif expected_type == "application/pdf":
            processing_error = f"Resume uploaded, but text could not be extracted: {error.__class__.__name__}. Try a text-based PDF."
        else:
            processing_error = f"Resume uploaded, but text could not be extracted: {error.__class__.__name__}. Try a clearer image."
    storage_path.write_bytes(content)
    profile = detect_profile(text)
    try:
        with connect() as db:
            old = db.execute("SELECT storage_path FROM resumes WHERE candidate_id = ?", (user["id"],)).fetchone()
            cursor = db.execute(
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
    elif report_range == "custom":
        try:
            start = date.fromisoformat(start_date or "")
            end = date.fromisoformat(end_date or "") + timedelta(days=1)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Custom reports need valid start_date and end_date values") from error
        if end <= start:
            raise HTTPException(status_code=422, detail="end_date must be on or after start_date")
    else:
        raise HTTPException(status_code=422, detail="Choose this_week, last_week, or custom")

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
        }
        counts["users_list"] = [dict(row) for row in db.execute("SELECT id, name, email, role, active, created_at FROM users ORDER BY created_at DESC LIMIT 100")]
        counts["recent_applications"] = [dict(row) for row in db.execute(
            """SELECT u.name AS candidate_name, j.title, j.company, a.status, a.applied_at
            FROM applications a JOIN users u ON u.id = a.candidate_id JOIN jobs j ON j.id = a.job_id
            ORDER BY a.applied_at DESC LIMIT 5"""
        )]
    return counts


app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
