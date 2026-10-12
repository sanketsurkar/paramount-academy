"""Admin only: teachers, subjects, fees, notices, settings, year-end promotion, audit log."""
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import ALLOWED_IMAGES, STANDARDS
from ..db import get_db
from ..deps import admin_only, flash, form_data, get_settings, render
from ..models import (AuditLog, Content, FeeInstalment, Notice, Payment, Result, Setting, Student, Subject,
                      Teacher, TeacherStandard, User)
from ..security import hash_password, temp_password
from ..services import (PAYMENT_METHODS, add_subject, audit, create_teacher, fee_totals, move_subject,
                        promote_all, record_payment, remove_subject, subject_usage, subjects_for, validate_subject)
from ..storage import files

router = APIRouter(prefix="/admin")


def _standards(form) -> list[int]:
    out = []
    for v in form.getlist("standards"):
        try:
            if int(v) in STANDARDS:
                out.append(int(v))
        except ValueError:
            pass
    return sorted(set(out))


def _std(raw, default: int = 1) -> int:
    try:
        v = int(raw)
        return v if v in STANDARDS else default
    except (TypeError, ValueError):
        return default


# ================================================================== teachers & accounts
@router.get("/teachers")
def teachers(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(Teacher).join(User).order_by(User.active.desc(), User.name)).all()
    return render(request, "admin/teachers.html", db, rows=rows, nav="teachers")


@router.post("/teachers")
async def add_teacher(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    name = str(form.get("name", "")).strip()
    if len(name) < 2:
        flash(request, "Enter the teacher's name.", "error")
        return RedirectResponse("/admin/teachers", 303)
    t_user, pw = create_teacher(db, name[:120], str(form.get("phone", ""))[:15],
                                str(form.get("subjects", ""))[:200], _standards(form))
    audit(db, user.id, "teacher.created", t_user.login_id)
    db.commit()
    return render(request, "staff/credentials.html", db, created=[(t_user, pw)], errors=[],
                  back="/admin/teachers", title="Teacher added 🎉", nav="teachers")


@router.post("/teachers/{teacher_id}/edit")
async def edit_teacher(teacher_id: int, request: Request, user: User = Depends(admin_only),
                       db: Session = Depends(get_db)):
    form = await form_data(request)
    t = db.get(Teacher, teacher_id)
    if not t:
        raise HTTPException(404, "Teacher not found.")
    name = str(form.get("name", "")).strip()
    if len(name) >= 2:
        t.user.name = name[:120]
    t.phone = str(form.get("phone", ""))[:15]
    t.subjects = str(form.get("subjects", ""))[:200]
    t.standards.clear()
    db.flush()
    t.standards.extend(TeacherStandard(standard=s) for s in _standards(form))
    audit(db, user.id, "teacher.updated", t.user.login_id)
    db.commit()
    flash(request, f"Saved {t.user.name} ✓")
    return RedirectResponse("/admin/teachers", 303)


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, request: Request, user: User = Depends(admin_only),
                         db: Session = Depends(get_db)):
    await form_data(request)
    target = db.get(User, user_id)
    if not target or target.id == user.id:
        raise HTTPException(400, "Use Change password for your own account.")
    pw = temp_password()
    target.password_hash = hash_password(pw)
    target.must_change_password = True
    audit(db, user.id, "password.reset", target.login_id)
    db.commit()
    back = "/admin/teachers" if target.role == "teacher" else f"/staff/students?standard={target.student.standard}"
    return render(request, "staff/credentials.html", db, created=[(target, pw)], errors=[], back=back,
                  title="Password reset 🔑", nav="students" if target.role == "student" else "teachers")


@router.post("/users/{user_id}/toggle")
async def toggle_user(user_id: int, request: Request, user: User = Depends(admin_only),
                      db: Session = Depends(get_db)):
    await form_data(request)
    target = db.get(User, user_id)
    if not target or target.id == user.id:
        raise HTTPException(400, "You can't deactivate your own account.")
    target.active = not target.active
    audit(db, user.id, "user.activated" if target.active else "user.deactivated", target.login_id)
    db.commit()
    flash(request, f"{target.name} {'can sign in again' if target.active else 'can no longer sign in'}.")
    if target.role == "teacher":
        return RedirectResponse("/admin/teachers", 303)
    return RedirectResponse(f"/staff/students?standard={target.student.standard}", 303)


# ================================================================== subjects
@router.get("/subjects")
def subjects(request: Request, standard: str | None = None, user: User = Depends(admin_only),
             db: Session = Depends(get_db)):
    std = _std(standard)
    items = subjects_for(db, std, include_archived=True)
    usage = {s.id: subject_usage(db, s.id) for s in items}
    counts = dict(db.execute(select(Subject.standard, func.count(Subject.id)).where(Subject.archived.is_(False))
                             .group_by(Subject.standard)).all())
    return render(request, "admin/subjects.html", db, standard=std, active=[s for s in items if not s.archived],
                  archived=[s for s in items if s.archived], usage=usage, counts=counts, nav="subjects")


@router.post("/subjects")
async def subject_add(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    std = _std(form.get("standard"))
    name = " ".join(str(form.get("name", "")).split())
    colour, icon = str(form.get("colour", "")), str(form.get("icon", ""))
    problem = validate_subject(name, colour, icon)
    if problem:
        flash(request, problem, "error")
        return RedirectResponse(f"/admin/subjects?standard={std}", 303)
    targets = sorted(set([std] + _standards(form)))
    results = {"added": [], "restored": [], "exists": []}
    for t in targets:
        outcome, _ = add_subject(db, t, name, colour, icon)
        results[outcome].append(t)
    audit(db, user.id, "subject.added", f"{name} -> classes {', '.join(map(str, targets))}")
    db.commit()
    parts = []
    if results["added"]:
        parts.append(f"added to Class {', '.join(map(str, results['added']))}")
    if results["restored"]:
        parts.append(f"restored in Class {', '.join(map(str, results['restored']))}")
    if results["exists"]:
        parts.append(f"already in Class {', '.join(map(str, results['exists']))}")
    flash(request, f"{name}: " + "; ".join(parts), "ok" if results["added"] or results["restored"] else "error")
    return RedirectResponse(f"/admin/subjects?standard={std}", 303)


def _subject(db: Session, subject_id: int) -> Subject:
    s = db.get(Subject, subject_id)
    if not s:
        raise HTTPException(404, "Subject not found.")
    return s


@router.post("/subjects/{subject_id}/edit")
async def subject_edit(subject_id: int, request: Request, user: User = Depends(admin_only),
                       db: Session = Depends(get_db)):
    form = await form_data(request)
    s = _subject(db, subject_id)
    name = " ".join(str(form.get("name", "")).split())
    colour, icon = str(form.get("colour", "")), str(form.get("icon", ""))
    problem = validate_subject(name, colour, icon)
    clash = db.scalar(select(Subject).where(Subject.standard == s.standard, Subject.id != s.id,
                                            func.lower(Subject.name) == name.lower()))
    if problem or clash:
        flash(request, problem or f"Class {s.standard} already has a subject called {name}.", "error")
        return RedirectResponse(f"/admin/subjects?standard={s.standard}", 303)
    old = s.name
    s.name, s.colour, s.icon = name, colour, icon
    audit(db, user.id, "subject.updated", f"Class {s.standard}: {old} -> {name}")
    db.commit()
    flash(request, f"Saved {name} ✓")
    return RedirectResponse(f"/admin/subjects?standard={s.standard}", 303)


@router.post("/subjects/{subject_id}/move")
async def subject_move(subject_id: int, request: Request, user: User = Depends(admin_only),
                       db: Session = Depends(get_db)):
    form = await form_data(request)
    s = _subject(db, subject_id)
    if not s.archived:
        move_subject(db, s, "up" if form.get("direction") == "up" else "down")
        db.commit()
    return RedirectResponse(f"/admin/subjects?standard={s.standard}", 303)


@router.post("/subjects/{subject_id}/remove")
async def subject_remove(subject_id: int, request: Request, user: User = Depends(admin_only),
                         db: Session = Depends(get_db)):
    await form_data(request)
    s = _subject(db, subject_id)
    std, name = s.standard, s.name
    outcome = remove_subject(db, s)
    audit(db, user.id, f"subject.{outcome}", f"Class {std}: {name}")
    db.commit()
    flash(request, f"{name} archived — its notes and marks are kept, and you can restore it anytime."
          if outcome == "archived" else f"{name} deleted from Class {std}.")
    return RedirectResponse(f"/admin/subjects?standard={std}", 303)


@router.post("/subjects/{subject_id}/restore")
async def subject_restore(subject_id: int, request: Request, user: User = Depends(admin_only),
                          db: Session = Depends(get_db)):
    await form_data(request)
    s = _subject(db, subject_id)
    add_subject(db, s.standard, s.name, s.colour, s.icon)
    audit(db, user.id, "subject.restored", f"Class {s.standard}: {s.name}")
    db.commit()
    flash(request, f"{s.name} restored ✓")
    return RedirectResponse(f"/admin/subjects?standard={s.standard}", 303)


@router.post("/subjects/{subject_id}/copy")
async def subject_copy(subject_id: int, request: Request, user: User = Depends(admin_only),
                       db: Session = Depends(get_db)):
    form = await form_data(request)
    s = _subject(db, subject_id)
    targets = [t for t in _standards(form) if t != s.standard]
    if not targets:
        flash(request, "Tick at least one other class.", "error")
        return RedirectResponse(f"/admin/subjects?standard={s.standard}", 303)
    done = [t for t in targets if add_subject(db, t, s.name, s.colour, s.icon)[0] != "exists"]
    audit(db, user.id, "subject.copied", f"{s.name} -> classes {', '.join(map(str, done)) or 'none'}")
    db.commit()
    skipped = len(targets) - len(done)
    msg = f"{s.name} added to Class {', '.join(map(str, done))}" if done else f"{s.name} was already in those classes"
    flash(request, msg + (f" ({skipped} already had it)" if done and skipped else "") + (" ✓" if done else ""),
          "ok" if done else "error")
    return RedirectResponse(f"/admin/subjects?standard={s.standard}", 303)


# ================================================================== fees
@router.get("/fees")
def fees(request: Request, standard: str | None = None, user: User = Depends(admin_only),
         db: Session = Depends(get_db)):
    std = _std(standard, 0) or None
    if std is None:  # default to first class that has students
        std = db.scalar(select(func.min(Student.standard)).where(Student.status == "active")) or 1
    rows = db.scalars(select(Student).join(User).where(Student.standard == std, Student.status == "active")
                      .order_by(User.name)).all()
    totals = fee_totals(db, [s.id for s in rows])
    summary = {k: sum(t[k] for t in totals.values()) for k in ("billed", "paid", "due", "overdue")}
    return render(request, "admin/fees.html", db, standard=std, rows=rows, totals=totals, summary=summary,
                  nav="fees")


@router.post("/fees/bulk")
async def fees_bulk(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    std = _std(form.get("standard"))
    try:
        amount = float(form.get("amount") or 0)
        due = date.fromisoformat(str(form.get("due_date")))
    except ValueError:
        amount, due = 0, None
    title = str(form.get("title", "")).strip()[:120]
    if not title or amount <= 0 or not due:
        flash(request, "Enter a title, an amount and a due date.", "error")
        return RedirectResponse(f"/admin/fees?standard={std}", 303)
    students = db.scalars(select(Student).where(Student.standard == std, Student.status == "active")).all()
    for st in students:
        db.add(FeeInstalment(student_id=st.id, title=title, amount=amount, due_date=due))
    audit(db, user.id, "fees.bulk", f"Class {std}: {title} ₹{amount:.0f} x {len(students)}")
    db.commit()
    flash(request, f"Added “{title}” for {len(students)} students in Class {std} ✓")
    return RedirectResponse(f"/admin/fees?standard={std}", 303)


@router.get("/fees/student/{student_id}")
def student_fees(student_id: int, request: Request, user: User = Depends(admin_only),
                 db: Session = Depends(get_db)):
    st = db.get(Student, student_id)
    if not st:
        raise HTTPException(404, "Student not found.")
    items = db.scalars(select(FeeInstalment).where(FeeInstalment.student_id == st.id)
                       .order_by(FeeInstalment.due_date, FeeInstalment.id)).all()
    return render(request, "admin/student_fees.html", db, st=st, items=items, totals=fee_totals(db, [st.id])[st.id],
                  methods=PAYMENT_METHODS, nav="fees")


@router.post("/fees/student/{student_id}/add")
async def add_instalment(student_id: int, request: Request, user: User = Depends(admin_only),
                         db: Session = Depends(get_db)):
    form = await form_data(request)
    st = db.get(Student, student_id)
    if not st:
        raise HTTPException(404, "Student not found.")
    try:
        amount = float(form.get("amount") or 0)
        due = date.fromisoformat(str(form.get("due_date")))
    except ValueError:
        amount, due = 0, None
    title = str(form.get("title", "")).strip()[:120]
    if not title or amount <= 0 or not due:
        flash(request, "Enter a title, an amount and a due date.", "error")
    else:
        db.add(FeeInstalment(student_id=st.id, title=title, amount=amount, due_date=due))
        audit(db, user.id, "fees.added", f"{st.user.login_id}: {title} ₹{amount:.0f}")
        db.commit()
        flash(request, f"Added “{title}” ✓")
    return RedirectResponse(f"/admin/fees/student/{st.id}", 303)


@router.post("/fees/instalment/{inst_id}/pay")
async def pay(inst_id: int, request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    inst = db.get(FeeInstalment, inst_id)
    if not inst:
        raise HTTPException(404, "Instalment not found.")
    back = str(form.get("back") or f"/admin/fees/student/{inst.student_id}")
    if not back.startswith("/"):
        back = "/staff"
    try:
        amount = float(form.get("amount") or 0)
        payment = record_payment(db, inst, amount, str(form.get("method", "")), str(form.get("reference", "")), user.id)
    except ValueError as exc:
        flash(request, str(exc) if "Amount" in str(exc) or "UPI" in str(exc) or "method" in str(exc)
              else "Enter a valid amount.", "error")
        return RedirectResponse(back, 303)
    audit(db, user.id, "payment.recorded", f"{payment.receipt_no} {inst.student.user.login_id} ₹{amount:.0f}")
    db.commit()
    flash(request, f"Payment recorded ✓ Receipt {payment.receipt_no}")
    return RedirectResponse(f"/receipt/{payment.id}", 303)


@router.post("/fees/instalment/{inst_id}/delete")
async def delete_instalment(inst_id: int, request: Request, user: User = Depends(admin_only),
                            db: Session = Depends(get_db)):
    await form_data(request)
    inst = db.get(FeeInstalment, inst_id)
    if not inst:
        raise HTTPException(404, "Instalment not found.")
    sid = inst.student_id
    if inst.payments:
        flash(request, "This instalment has payments, so it can't be deleted.", "error")
    else:
        audit(db, user.id, "fees.deleted", f"{inst.student.user.login_id}: {inst.title}")
        db.delete(inst)
        db.commit()
        flash(request, "Instalment deleted.")
    return RedirectResponse(f"/admin/fees/student/{sid}", 303)


# ================================================================== notices
@router.get("/notices")
def notices(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notice).order_by(Notice.created_at.desc(), Notice.id.desc())).all()
    return render(request, "admin/notices.html", db, rows=rows, nav="notices")


@router.post("/notices")
async def add_notice(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    title = str(form.get("title", "")).strip()[:150]
    if not title:
        flash(request, "Enter a notice title.", "error")
        return RedirectResponse("/admin/notices", 303)
    std = form.get("standard")
    try:
        expires = date.fromisoformat(str(form.get("expires_on"))) if form.get("expires_on") else None
    except ValueError:
        expires = None
    db.add(Notice(title=title, body=str(form.get("body", ""))[:1000],
                  standard=int(std) if std and str(std).isdigit() and int(std) in STANDARDS else None,
                  expires_on=expires))
    audit(db, user.id, "notice.posted", title)
    db.commit()
    flash(request, "Notice posted 📢")
    return RedirectResponse("/admin/notices", 303)


@router.post("/notices/{notice_id}/delete")
async def delete_notice(notice_id: int, request: Request, user: User = Depends(admin_only),
                        db: Session = Depends(get_db)):
    await form_data(request)
    n = db.get(Notice, notice_id)
    if n:
        db.delete(n)
        db.commit()
    flash(request, "Notice removed.")
    return RedirectResponse("/admin/notices", 303)


# ================================================================== settings
@router.get("/settings")
def settings_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", db, nav="settings")


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)

    def put(key: str, value: str) -> None:
        row = db.get(Setting, key)
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))

    put("academy_name", str(form.get("academy_name", "")).strip()[:80] or "The Paramount Academy")
    put("upi_id", str(form.get("upi_id", "")).strip()[:80])
    put("upi_payee", str(form.get("upi_payee", "")).strip()[:80])
    put("phone", str(form.get("phone", "")).strip()[:30])
    put("leaderboard_enabled", "1" if form.get("leaderboard_enabled") else "0")

    qr = form.get("upi_qr")
    if qr is not None and getattr(qr, "filename", ""):
        ext = Path(qr.filename).suffix.lower()
        data = await qr.read()
        ok = ext in ALLOWED_IMAGES and len(data) <= 2 * 1024 * 1024 and (
            data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8"))
        if not ok:
            flash(request, "QR code must be a PNG or JPG image under 2 MB.", "error")
            db.commit()
            return RedirectResponse("/admin/settings", 303)
        old = get_settings(db)["upi_qr"]
        key = f"qr_{uuid.uuid4().hex[:10]}{ext}"
        files.save(key, data, ALLOWED_IMAGES[ext])
        put("upi_qr", key)
        if old:
            files.delete(old)
    if form.get("remove_qr"):
        old = get_settings(db)["upi_qr"]
        if old:
            files.delete(old)
        put("upi_qr", "")
    audit(db, user.id, "settings.updated")
    db.commit()
    flash(request, "Settings saved ✓")
    return RedirectResponse("/admin/settings", 303)


# ================================================================== year end & audit
@router.get("/promote")
def promote_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Student.standard, func.count(Student.id)).where(Student.status == "active")
                             .group_by(Student.standard)).all())
    return render(request, "admin/promote.html", db, counts=counts, nav="promote")


@router.post("/promote")
async def promote(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    if str(form.get("confirm", "")).strip() != "PROMOTE":
        flash(request, "Type PROMOTE in capitals to confirm.", "error")
        return RedirectResponse("/admin/promote", 303)
    result = promote_all(db)
    audit(db, user.id, "year.promoted", f"{result['moved']} moved, {result['passed']} passed out")
    db.commit()
    flash(request, f"Done 🎓 {result['moved']} students moved up, {result['passed']} marked passed out.")
    return RedirectResponse("/admin/promote", 303)


@router.get("/audit")
def audit_log(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.execute(select(AuditLog, User.name).outerjoin(User, User.id == AuditLog.user_id)
                      .order_by(AuditLog.id.desc()).limit(300)).all()
    return render(request, "admin/audit.html", db, rows=rows, nav="audit")
