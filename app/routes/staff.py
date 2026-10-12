"""Pages for teachers and the admin: dashboard, students, course content, exams & marks."""
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import ALLOWED_CONTENT, MAX_UPLOAD_MB, STANDARDS, SUBJECT_COLOURS
from ..db import get_db
from ..deps import (allowed_standards, check_standard, flash, form_data, json_data, pick_standard, render,
                    staff_only)
from ..models import Content, Exam, FeeInstalment, Payment, Result, Student, Subject, Teacher, User
from ..services import (audit, create_student, exam_roster, exam_scores, exam_subjects, fee_totals,
                        fees_bar_svg, import_students, read_rows, save_marks, student_progress, subjects_for,
                        trend_svg, clean_phone)
from ..storage import files

router = APIRouter()


def _admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Only the admin can do this.")


# ================================================================== dashboard
@router.get("/staff")
def dashboard(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    if user.role == "admin":
        return _admin_dashboard(request, user, db)
    classes = []
    for std in allowed_standards(user):
        count = db.scalar(select(func.count(Student.id)).where(Student.standard == std, Student.status == "active"))
        exams = db.scalars(select(Exam).where(Exam.standard == std).order_by(Exam.exam_date.desc())).all()
        files_count = db.scalar(select(func.count(Content.id)).join(Subject)
                                .where(Subject.standard == std, Subject.archived.is_(False)))
        classes.append({"standard": std, "students": count, "drafts": sum(not e.published for e in exams),
                        "latest": next((e for e in exams if e.published), None), "files": files_count})
    return render(request, "staff/teacher_home.html", db, classes=classes, nav="home")


def _admin_dashboard(request: Request, user: User, db: Session):
    students = db.scalars(select(Student).where(Student.status == "active")).all()
    totals = fee_totals(db, [s.id for s in students])
    by_class: dict[int, list[float]] = {}
    for s in students:
        t = totals[s.id]
        row = by_class.setdefault(s.standard, [0.0, 0.0])
        row[0] += t["paid"]
        row[1] += t["due"]
    overdue = [f for f in db.scalars(
        select(FeeInstalment).join(Student).where(Student.status == "active",
                                                  FeeInstalment.due_date < date.today(),
                                                  FeeInstalment.paid_amount < FeeInstalment.amount)
        .order_by(FeeInstalment.due_date).limit(8))]
    kpi = {
        "students": len(students),
        "teachers": db.scalar(select(func.count(Teacher.id)).join(User).where(User.active.is_(True))),
        "collected": sum(t["paid"] for t in totals.values()),
        "pending": sum(t["due"] for t in totals.values()),
        "overdue": sum(t["overdue"] for t in totals.values()),
        "files": db.scalar(select(func.count(Content.id))),
        "month": db.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.paid_on >= date.today().replace(day=1))),
    }
    billed = kpi["collected"] + kpi["pending"]
    kpi["rate"] = round(kpi["collected"] / billed * 100) if billed else 0
    chart = fees_bar_svg([(f"Cl {k}", v[0], v[1]) for k, v in sorted(by_class.items())])
    recent = db.scalars(select(Payment).order_by(Payment.id.desc()).limit(5)).all()
    return render(request, "admin/dashboard.html", db, kpi=kpi, chart=chart, overdue=overdue,
                  recent=recent, nav="home")


# ================================================================== students
@router.get("/staff/students")
def students(request: Request, standard: str | None = None, user: User = Depends(staff_only),
             db: Session = Depends(get_db)):
    passed = standard == "passed" and user.role == "admin"
    std = None if passed else pick_standard(user, standard)
    q = select(Student).join(User)
    q = q.where(Student.status == "passed_out") if passed else q.where(Student.standard == std,
                                                                         Student.status == "active")
    rows = db.scalars(q.order_by(User.active.desc(), User.name)).all()
    return render(request, "staff/students.html", db, rows=rows, standard=std, passed=passed,
                  classes=allowed_standards(user), nav="students")


@router.post("/staff/students/new")
async def add_student(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    try:
        std = int(form.get("standard", ""))
    except ValueError:
        raise HTTPException(400, "Choose a class.")
    check_standard(user, std)
    name = str(form.get("name", "")).strip()
    if len(name) < 2:
        flash(request, "Enter the student's full name.", "error")
        return RedirectResponse(f"/staff/students?standard={std}", 303)
    new_user, pw = create_student(db, name[:120], std, str(form.get("parent_name", ""))[:120],
                                  clean_phone(form.get("parent_phone")))
    audit(db, user.id, "student.created", f"{new_user.login_id} class {std}")
    db.commit()
    return render(request, "staff/credentials.html", db, created=[(new_user, pw)], errors=[],
                  back=f"/staff/students?standard={std}", title="Student added 🎉", nav="students")


def _student_for(db: Session, user: User, student_id: int) -> Student:
    st = db.get(Student, student_id)
    if not st:
        raise HTTPException(404, "Student not found.")
    if user.role != "admin":
        check_standard(user, st.standard)
    return st


@router.get("/staff/students/{student_id:int}")
def student_detail(student_id: int, request: Request, user: User = Depends(staff_only),
                   db: Session = Depends(get_db)):
    st = _student_for(db, user, student_id)
    progress = student_progress(db, st)
    chart = trend_svg([(p["exam"].name, p["percent"]) for p in progress])
    fees = None
    if user.role == "admin":
        fees = fee_totals(db, [st.id])[st.id]
    return render(request, "staff/student_detail.html", db, st=st, progress=list(reversed(progress)),
                  chart=chart, fees=fees, classes=allowed_standards(user), nav="students")


@router.post("/staff/students/{student_id:int}/edit")
async def edit_student(student_id: int, request: Request, user: User = Depends(staff_only),
                       db: Session = Depends(get_db)):
    form = await form_data(request)
    st = _student_for(db, user, student_id)
    name = str(form.get("name", "")).strip()
    if len(name) < 2:
        raise HTTPException(400, "Enter the student's full name.")
    try:
        std = int(form.get("standard", st.standard))
    except ValueError:
        std = st.standard
    if std != st.standard:
        check_standard(user, std)
    st.user.name = name[:120]
    st.standard = std
    st.parent_name = str(form.get("parent_name", ""))[:120]
    st.parent_phone = clean_phone(form.get("parent_phone"))
    audit(db, user.id, "student.updated", st.user.login_id)
    db.commit()
    flash(request, f"Saved {st.user.name}'s details ✓")
    return RedirectResponse(f"/staff/students?standard={st.standard}", 303)


@router.get("/staff/students/import/template.csv")
def import_template(user: User = Depends(staff_only)):
    body = "name,standard,parent_name,parent_phone\nAarav Sharma,5,Suresh Sharma,9876543210\n"
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=students_template.csv"})


@router.get("/staff/students/import")
def import_page(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    _admin(user)
    return render(request, "staff/import.html", db, nav="students")


@router.post("/staff/students/import")
async def import_run(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    _admin(user)
    form = await form_data(request)
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", ""):
        flash(request, "Choose a CSV or Excel file.", "error")
        return RedirectResponse("/staff/students/import", 303)
    data = await upload.read()
    try:
        rows = read_rows(upload.filename, data)
    except Exception as exc:  # bad/corrupt file
        flash(request, f"Couldn't read that file: {exc}" if isinstance(exc, ValueError)
              else "Couldn't read that file. Save it as .xlsx or .csv and try again.", "error")
        return RedirectResponse("/staff/students/import", 303)
    created, errors = import_students(db, rows, allowed_standards(user))
    audit(db, user.id, "students.imported", f"{len(created)} created, {len(errors)} skipped")
    db.commit()
    return render(request, "staff/credentials.html", db, created=created, errors=errors,
                  back="/staff/students/import", title=f"Imported {len(created)} student(s)", nav="students")


# ================================================================== content
@router.get("/staff/content")
def content(request: Request, standard: str | None = None, user: User = Depends(staff_only),
            db: Session = Depends(get_db)):
    std = pick_standard(user, standard)
    subjects = subjects_for(db, std) if std else []
    items = db.scalars(select(Content).where(Content.subject_id.in_([s.id for s in subjects]))
                       .order_by(Content.chapter, Content.created_at)).all()
    grouped = {s.id: {} for s in subjects}
    for c in items:
        grouped[c.subject_id].setdefault(c.chapter, []).append(c)
    chapters = sorted({c.chapter for c in items})
    return render(request, "staff/content.html", db, standard=std, subjects=subjects, grouped=grouped,
                  chapters=chapters, classes=allowed_standards(user), max_mb=MAX_UPLOAD_MB, nav="content")


@router.post("/staff/content")
async def upload(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    subj = db.get(Subject, int(form.get("subject_id") or 0))
    if not subj or subj.archived:
        raise HTTPException(400, "Choose a subject.")
    check_standard(user, subj.standard)
    back = f"/staff/content?standard={subj.standard}"
    upload = form.get("file")
    chapter = str(form.get("chapter", "")).strip()[:120]
    if not upload or not getattr(upload, "filename", ""):
        flash(request, "Choose a file to upload.", "error")
        return RedirectResponse(back, 303)
    if not chapter:
        flash(request, "Enter a chapter name.", "error")
        return RedirectResponse(back, 303)
    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_CONTENT:
        flash(request, "Only PDF or Word (.docx) files can be uploaded.", "error")
        return RedirectResponse(back, 303)
    data = await upload.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        flash(request, f"File is larger than {MAX_UPLOAD_MB} MB. Compress it and try again.", "error")
        return RedirectResponse(back, 303)
    magic_ok = data.startswith(b"%PDF") if ext == ".pdf" else data.startswith(b"PK\x03\x04")
    if not magic_ok:
        flash(request, f"That file doesn't look like a real {ext[1:].upper()} file.", "error")
        return RedirectResponse(back, 303)
    title = str(form.get("title", "")).strip()[:200] or Path(upload.filename).stem[:200]
    key = f"c_{uuid.uuid4().hex}{ext}"
    files.save(key, data, ALLOWED_CONTENT[ext])
    db.add(Content(subject_id=subj.id, chapter=chapter, title=title, file_path=key, file_type=ext[1:],
                   size_bytes=len(data), uploaded_by=user.id))
    audit(db, user.id, "content.uploaded", f"Class {subj.standard} {subj.name}: {title}")
    db.commit()
    flash(request, f"Uploaded “{title}” to {subj.name} ✓")
    return RedirectResponse(back, 303)


@router.post("/staff/content/{content_id}/delete")
async def delete_content(content_id: int, request: Request, user: User = Depends(staff_only),
                         db: Session = Depends(get_db)):
    await form_data(request)
    c = db.get(Content, content_id)
    if not c:
        raise HTTPException(404, "File not found.")
    std = c.subject.standard
    check_standard(user, std)
    files.delete(c.file_path)
    audit(db, user.id, "content.deleted", c.title)
    db.delete(c)
    db.commit()
    flash(request, "File deleted.")
    return RedirectResponse(f"/staff/content?standard={std}", 303)


# ================================================================== exams & marks
@router.get("/staff/exams")
def exams(request: Request, standard: str | None = None, user: User = Depends(staff_only),
          db: Session = Depends(get_db)):
    std = pick_standard(user, standard)
    items = db.scalars(select(Exam).where(Exam.standard == std)
                       .order_by(Exam.exam_date.desc(), Exam.id.desc())).all() if std else []
    counts = dict(db.execute(select(Result.exam_id, func.count(func.distinct(Result.student_id)))
                             .where(Result.exam_id.in_([e.id for e in items])).group_by(Result.exam_id)).all())
    class_size = db.scalar(select(func.count(Student.id)).where(Student.standard == std,
                                                                Student.status == "active")) or 0
    return render(request, "staff/exams.html", db, standard=std, exams=items, counts=counts,
                  class_size=class_size, classes=allowed_standards(user), nav="exams")


@router.post("/staff/exams")
async def create_exam(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    try:
        std = int(form.get("standard", ""))
        max_marks = float(form.get("max_marks") or 100)
        exam_date = date.fromisoformat(str(form.get("exam_date")))
    except ValueError:
        raise HTTPException(400, "Check the class, date and maximum marks.")
    check_standard(user, std)
    name = str(form.get("name", "")).strip()[:120]
    if not name or not 1 <= max_marks <= 1000:
        flash(request, "Enter an exam name and maximum marks between 1 and 1000.", "error")
        return RedirectResponse(f"/staff/exams?standard={std}", 303)
    exam = Exam(standard=std, name=name, exam_date=exam_date, max_marks=max_marks)
    db.add(exam)
    db.flush()
    audit(db, user.id, "exam.created", f"Class {std}: {name}")
    db.commit()
    flash(request, f"“{name}” created. Enter marks below.")
    return RedirectResponse(f"/staff/exams/{exam.id}", 303)


def _exam_for(db: Session, user: User, exam_id: int) -> Exam:
    exam = db.get(Exam, exam_id)
    if not exam:
        raise HTTPException(404, "Exam not found.")
    check_standard(user, exam.standard)
    return exam


@router.get("/staff/exams/{exam_id}")
def marks_page(exam_id: int, request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    exam = _exam_for(db, user, exam_id)
    roster = exam_roster(db, exam)
    subjects = exam_subjects(db, exam)
    marks = {(r.student_id, r.subject_id): r.marks for r in
             db.scalars(select(Result).where(Result.exam_id == exam.id))}
    from ..models import ExamRemark
    remarks = {r.student_id: r.remark for r in db.scalars(select(ExamRemark).where(ExamRemark.exam_id == exam.id))}
    return render(request, "staff/marks.html", db, exam=exam, roster=roster, subjects=subjects, marks=marks,
                  remarks=remarks, nav="exams")


@router.post("/staff/exams/{exam_id}")
async def marks_save(exam_id: int, request: Request, user: User = Depends(staff_only),
                     db: Session = Depends(get_db)):
    """Form fallback (no JavaScript)."""
    form = await form_data(request)
    exam = _exam_for(db, user, exam_id)
    cells = {k: str(v) for k, v in form.items() if k.startswith(("m_", "r_"))}
    saved, problems = save_marks(db, exam, cells)
    audit(db, user.id, "marks.saved", f"{exam.name} (class {exam.standard})", merge_minutes=15)
    db.commit()
    if problems:
        flash(request, f"{len(problems)} mark(s) were invalid and skipped: " + "; ".join(problems[:3]), "error")
    flash(request, f"Marks saved ✓ ({saved} change{'s' if saved != 1 else ''})")
    return RedirectResponse(f"/staff/exams/{exam.id}", 303)


@router.post("/staff/exams/{exam_id}/autosave")
async def marks_autosave(exam_id: int, request: Request, user: User = Depends(staff_only),
                         db: Session = Depends(get_db)):
    data = await json_data(request)
    exam = _exam_for(db, user, exam_id)
    cells = {str(k): str(v) for k, v in (data.get("cells") or {}).items()}
    saved, problems = save_marks(db, exam, cells)
    if saved:
        audit(db, user.id, "marks.saved", f"{exam.name} (class {exam.standard})", merge_minutes=15)
    db.commit()
    return JSONResponse({"saved": saved, "problems": problems, "at": datetime.now().strftime("%I:%M %p").lstrip("0")})


@router.post("/staff/exams/{exam_id}/publish")
async def publish(exam_id: int, request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    form = await form_data(request)
    exam = _exam_for(db, user, exam_id)
    exam.published = form.get("unpublish") != "1"
    audit(db, user.id, "exam.published" if exam.published else "exam.unpublished", exam.name)
    db.commit()
    flash(request, "Results published 🎉 Students can see them now." if exam.published
          else "Results hidden from students.")
    return RedirectResponse(f"/staff/exams/{exam.id}", 303)


@router.post("/staff/exams/{exam_id}/delete")
async def delete_exam(exam_id: int, request: Request, user: User = Depends(staff_only),
                      db: Session = Depends(get_db)):
    await form_data(request)
    _admin(user)
    exam = _exam_for(db, user, exam_id)
    std = exam.standard
    audit(db, user.id, "exam.deleted", f"{exam.name} (class {std})")
    db.delete(exam)
    db.commit()
    flash(request, "Exam deleted.")
    return RedirectResponse(f"/staff/exams?standard={std}", 303)
