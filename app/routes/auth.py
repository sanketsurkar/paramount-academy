from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, flash, form_data, render
from ..models import User
from ..security import hash_password, login_limiter, password_problem, verify_password
from ..services import audit

router = APIRouter()
HOME = {"student": "/s", "teacher": "/staff", "admin": "/staff"}


@router.get("/")
def index(request: Request):
    return RedirectResponse("/home" if request.session.get("uid") else "/login", 303)


@router.get("/home")
def home(user: User = Depends(current_user)):
    return RedirectResponse(HOME[user.role], 303)


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get("uid"):
        return RedirectResponse("/home", 303)
    return render(request, "login.html", db)


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await form_data(request)
    login_id = str(form.get("login_id", "")).strip().upper()
    password = str(form.get("password", ""))
    if login_limiter.blocked(login_id):
        return render(request, "login.html", db, status_code=429, login_id=login_id,
                      error="Too many attempts. Please wait one minute.")
    user = db.scalar(select(User).where(User.login_id == login_id))
    if not user or not user.active or not verify_password(password, user.password_hash):
        login_limiter.fail(login_id)
        return render(request, "login.html", db, status_code=401, login_id=login_id,
                      error="Wrong ID or password.")
    login_limiter.reset(login_id)
    csrf = request.session.get("csrf")
    request.session.clear()
    request.session.update({"uid": user.id, "csrf": csrf})
    first = user.name.split()[0]
    if not user.must_change_password:
        flash(request, f"Welcome back, {first}! 👋")
    return RedirectResponse("/change-password" if user.must_change_password else HOME[user.role], 303)


@router.get("/change-password")
def change_password_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "change_password.html", db)


@router.post("/change-password")
async def change_password(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await form_data(request)
    current, new, confirm = (str(form.get(k, "")) for k in ("current", "new", "confirm"))
    if not verify_password(current, user.password_hash):
        error = "Current password is wrong."
    elif new != confirm:
        error = "New passwords don't match."
    elif new == current:
        error = "Choose a different password from the current one."
    else:
        error = password_problem(new)
    if error:
        return render(request, "change_password.html", db, status_code=400, error=error)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    audit(db, user.id, "password.changed", user.login_id)
    db.commit()
    flash(request, "Password updated 🔒")
    return RedirectResponse(HOME[user.role], 303)


@router.post("/logout")
async def logout(request: Request):
    await form_data(request)
    request.session.clear()
    return RedirectResponse("/logged-out", 303)


@router.get("/logged-out")
def logged_out(request: Request, db: Session = Depends(get_db)):
    return render(request, "logged_out.html", db)
