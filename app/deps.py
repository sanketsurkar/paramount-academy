"""Request helpers: current user, role checks, CSRF, flash messages, template rendering."""
import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import BASE_DIR, IS_PRODUCTION, STANDARDS, SUBJECT_COLOURS, SUBJECT_ICONS, VERSION
from .db import get_db
from .models import Setting, User

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
INDIA_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

DEFAULT_SETTINGS = {
    "academy_name": "The Paramounts Academy",
    "upi_id": "",
    "upi_payee": "The Paramounts Academy",
    "phone": "",
    "upi_qr": "",
    "leaderboard_enabled": "1",
}


# ----------------------------------------------------------------- template filters
def inr(value) -> str:
    """Indian number format: 125000 -> ₹1,25,000"""
    n = round(float(value or 0))
    s, sign = str(abs(n)), "-" if n < 0 else ""
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}"


def num(value) -> str:
    v = round(float(value or 0), 1)
    return str(int(v)) if v == int(v) else f"{v}"


def fmt_date(value) -> str:
    if not value:
        return ""
    return value.strftime("%d %b %Y").lstrip("0")


def fmt_audit_time(value: datetime) -> str:
    """Format a UTC audit timestamp in India Standard Time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(INDIA_TIMEZONE).strftime("%d %b, %I:%M %p")


def short_name(name: str) -> str:
    parts = (name or "").split()
    return parts[0] if len(parts) < 2 else f"{parts[0]} {parts[-1][0]}."


def initials(name: str) -> str:
    parts = (name or "?").split()
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


templates.env.filters.update(money=inr, num=num, d=fmt_date, audit_time=fmt_audit_time,
                             short=short_name, initials=initials)
templates.env.globals.update(COLOURS=SUBJECT_COLOURS, ICONS=SUBJECT_ICONS, STANDARDS=STANDARDS,
                             VERSION=VERSION, is_production=IS_PRODUCTION)


# ----------------------------------------------------------------- redirects & auth
class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def get_settings(db: Session) -> dict:
    values = dict(DEFAULT_SETTINGS)
    values.update({s.key: s.value for s in db.scalars(select(Setting))})
    return values


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        request.session.pop("uid", None)
        raise Redirect("/login")
    if user.must_change_password and request.url.path not in ("/change-password", "/logout"):
        raise Redirect("/change-password")
    request.state.user = user
    return user


def student_only(user: User = Depends(current_user)) -> User:
    if user.role != "student":
        raise HTTPException(403, "This page is for students.")
    return user


def staff_only(user: User = Depends(current_user)) -> User:
    if user.role not in ("teacher", "admin"):
        raise HTTPException(403, "This page is for teachers and the admin.")
    return user


def admin_only(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Only the admin can do this.")
    return user


def allowed_standards(user: User) -> list[int]:
    if user.role == "admin":
        return list(STANDARDS)
    if user.role == "teacher" and user.teacher:
        return user.teacher.standard_list
    return []


def check_standard(user: User, standard: int) -> None:
    if standard not in allowed_standards(user):
        raise HTTPException(403, "You are not assigned to this class.")


def pick_standard(user: User, raw) -> int | None:
    """Class chosen in the page's class picker, falling back to the first allowed one."""
    allowed = allowed_standards(user)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = None
    if value in allowed:
        return value
    return allowed[0] if allowed else None


# ----------------------------------------------------------------- CSRF & forms
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def _csrf_ok(request: Request, supplied: str | None) -> bool:
    expected = request.session.get("csrf")
    return bool(expected and supplied and secrets.compare_digest(str(supplied), expected))


async def form_data(request: Request):
    form = await request.form()
    if not _csrf_ok(request, form.get("csrf")):
        raise HTTPException(400, "Your session expired. Refresh the page and try again.")
    return form


async def json_data(request: Request) -> dict:
    if not _csrf_ok(request, request.headers.get("x-csrf-token")):
        raise HTTPException(400, "Your session expired. Refresh the page and try again.")
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "Invalid request.")
    return data if isinstance(data, dict) else {}


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flashes", [])
    request.session["flashes"] = request.session["flashes"] + [[kind, message]]


# ----------------------------------------------------------------- rendering
def render(request: Request, name: str, db: Session, status_code: int = 200, **ctx):
    user = getattr(request.state, "user", None)
    ctx.setdefault("error", None)
    context = {
        "user": user,
        "settings": get_settings(db),
        "csrf": csrf_token(request),
        "flashes": request.session.pop("flashes", []),
        "now": datetime.now(),
        "today": date.today(),
        "partial": request.headers.get("x-partial") == "1",
        "path": request.url.path,
        **ctx,
    }
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    response.headers["Vary"] = "X-Partial, Cookie"
    return response
