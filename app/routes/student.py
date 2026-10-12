from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import ALLOWED_CONTENT, ALLOWED_IMAGES, SUBJECT_COLOURS
from ..db import get_db
from ..deps import allowed_standards, current_user, get_settings, render, student_only
from ..models import Content, FeeInstalment, Notice, Payment, Subject, User
from ..services import leaderboard, student_progress, subjects_for, trend_svg
from ..storage import files

router = APIRouter()


def _fees(db: Session, student_id: int) -> list[FeeInstalment]:
    return list(db.scalars(select(FeeInstalment).where(FeeInstalment.student_id == student_id)
                           .order_by(FeeInstalment.due_date, FeeInstalment.id)))


@router.get("/s")
def home(request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    st = user.student
    notices = db.scalars(select(Notice).where(
        or_(Notice.standard.is_(None), Notice.standard == st.standard),
        or_(Notice.expires_on.is_(None), Notice.expires_on >= date.today()))
        .order_by(Notice.created_at.desc(), Notice.id.desc()).limit(6)).all()
    progress = student_progress(db, st)
    fees = _fees(db, st.id)
    next_due = next((f for f in fees if f.outstanding > 0), None)
    subjects = subjects_for(db, st.standard)
    return render(request, "student/home.html", db, st=st, notices=notices,
                  latest=progress[-1] if progress else None,
                  previous=progress[-2] if len(progress) > 1 else None,
                  next_due=next_due, due_total=sum(f.outstanding for f in fees),
                  subjects=subjects, tab=0)


@router.get("/s/courses")
def courses(request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    subjects = subjects_for(db, user.student.standard)
    counts = dict(db.execute(select(Content.subject_id, func.count(Content.id))
                             .where(Content.subject_id.in_([s.id for s in subjects]))
                             .group_by(Content.subject_id)).all())
    return render(request, "student/courses.html", db, subjects=subjects, counts=counts, tab=1)


@router.get("/s/courses/{subject_id}")
def subject(subject_id: int, request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    subj = db.get(Subject, subject_id)
    if not subj or subj.standard != user.student.standard or subj.archived:
        raise HTTPException(404, "Subject not found.")
    items = db.scalars(select(Content).where(Content.subject_id == subj.id)
                       .order_by(Content.chapter, Content.created_at)).all()
    chapters: dict[str, list[Content]] = {}
    for c in items:
        chapters.setdefault(c.chapter, []).append(c)
    return render(request, "student/subject.html", db, subject=subj, chapters=chapters,
                  colour=SUBJECT_COLOURS.get(subj.colour, "#4f46e5"), tab=1)


@router.get("/s/results")
def results(request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    progress = student_progress(db, user.student)
    chart = trend_svg([(p["exam"].name, p["percent"]) for p in progress])
    return render(request, "student/results.html", db, progress=list(reversed(progress)), chart=chart, tab=2)


@router.get("/s/leaderboard")
def board(request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    if get_settings(db)["leaderboard_enabled"] != "1":
        return render(request, "student/leaderboard.html", db, disabled=True, tab=3)
    exam, rows = leaderboard(db, user.student.standard)
    mine = next((r for r in rows if r[1].id == user.student.id), None)
    return render(request, "student/leaderboard.html", db, exam=exam, podium=rows[:3], rest=rows[3:10],
                  mine=mine, total=len(rows), tab=3)


@router.get("/s/fees")
def fees(request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    items = _fees(db, user.student.id)
    s = get_settings(db)
    due = sum(f.outstanding for f in items)
    due_now = sum(f.outstanding for f in items if f.due_date <= date.today())
    pay_amount = due_now or due  # pay what's overdue first; otherwise the full balance
    billed = sum(f.amount for f in items)
    paid = sum(f.paid_amount for f in items)
    upi_link = None
    if pay_amount > 0 and s["upi_id"]:
        upi_link = (f"upi://pay?pa={quote(s['upi_id'])}&pn={quote(s['upi_payee'])}&am={pay_amount:.0f}"
                    f"&cu=INR&tn={quote('Fees ' + user.login_id)}")
    return render(request, "student/fees.html", db, items=items, due=due, due_now=due_now, pay_amount=pay_amount,
                  billed=billed, paid=paid, upi_link=upi_link, tab=4)


@router.get("/s/profile")
def profile(request: Request, user: User = Depends(student_only), db: Session = Depends(get_db)):
    return render(request, "student/profile.html", db, st=user.student, tab=-1)


# ---------------------------------------------------------------- shared by all roles
@router.get("/files/{content_id}")
def download(content_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    c = db.get(Content, content_id)
    if not c:
        raise HTTPException(404, "File not found.")
    std = c.subject.standard
    if user.role == "student":
        if user.student.standard != std or c.subject.archived:
            raise HTTPException(403, "This file is for another class.")
    elif std not in allowed_standards(user):
        raise HTTPException(403, "You are not assigned to this class.")
    data = files.read(c.file_path)
    if data is None:
        raise HTTPException(404, "File is missing on the server.")
    ext = "." + c.file_type
    safe = "".join(ch for ch in c.title if ch.isalnum() or ch in " -_").strip() or "file"
    disposition = "inline" if c.file_type == "pdf" else "attachment"
    return Response(data, media_type=ALLOWED_CONTENT[ext], headers={
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(safe + ext)}",
        "Cache-Control": "private, max-age=3600"})


@router.get("/receipt/{payment_id}")
def receipt(payment_id: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    pay = db.get(Payment, payment_id)
    if not pay:
        raise HTTPException(404, "Receipt not found.")
    owner = pay.instalment.student
    if user.role == "teacher" or (user.role == "student" and user.student.id != owner.id):
        raise HTTPException(403, "You can't view this receipt.")
    return render(request, "receipt.html", db, pay=pay, st=owner)


@router.get("/upi-qr")
def upi_qr(user: User = Depends(current_user), db: Session = Depends(get_db)):
    name = get_settings(db)["upi_qr"]
    data = files.read(name) if name else None
    if data is None:
        raise HTTPException(404, "No QR code uploaded.")
    ext = "." + name.rsplit(".", 1)[-1].lower()
    return Response(data, media_type=ALLOWED_IMAGES.get(ext, "image/png"),
                    headers={"Cache-Control": "private, max-age=86400"})
