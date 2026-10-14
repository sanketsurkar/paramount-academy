"""Business rules: IDs, subjects, results & ranks, fees, import, promotion, charts."""
import csv
import io
from datetime import date, timedelta
from html import escape

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import DEFAULT_SUBJECTS, STANDARDS, SUBJECT_COLOURS, SUBJECT_ICONS
from .models import (AuditLog, Content, Exam, ExamRemark, FeeInstalment, Payment, Result, Student,
                     Subject, Teacher, TeacherStandard, User, utc_now)
from .security import hash_password, temp_password

PAYMENT_METHODS = ["UPI", "Cash", "Cheque", "Bank"]


def audit(db: Session, user_id: int | None, action: str, detail: str = "", merge_minutes: int = 0) -> None:
    """Record an action. merge_minutes > 0 updates the same user's recent identical entry instead
    of adding a new row (used by marks autosave, which fires often)."""
    if merge_minutes:
        recent = db.scalar(select(AuditLog).where(
            AuditLog.user_id == user_id, AuditLog.action == action, AuditLog.detail == detail,
            AuditLog.created_at >= utc_now() - timedelta(minutes=merge_minutes)))
        if recent:
            recent.created_at = utc_now()
            return
    db.add(AuditLog(user_id=user_id, action=action, detail=detail[:300]))


# ----------------------------------------------------------------- IDs & accounts
def next_student_id(db: Session) -> str:
    prefix = f"TPA{date.today():%y}-"
    ids = db.scalars(select(User.login_id).where(User.login_id.like(prefix + "%"))).all()
    n = max((int(i.split("-")[1]) for i in ids if i.split("-")[1].isdigit()), default=0) + 1
    return f"{prefix}{n:04d}"


def next_teacher_id(db: Session) -> str:
    ids = db.scalars(select(User.login_id).where(User.login_id.like("TCH-%"))).all()
    n = max((int(i[4:]) for i in ids if i[4:].isdigit()), default=0) + 1
    return f"TCH-{n:03d}"


def create_student(db: Session, name: str, standard: int, parent_name: str = "", parent_phone: str = "",
                   password: str | None = None) -> tuple[User, str]:
    pw = password or temp_password()
    user = User(login_id=next_student_id(db), name=name.strip(), role="student",
                password_hash=hash_password(pw), must_change_password=password is None)
    user.student = Student(standard=standard, parent_name=parent_name.strip(), parent_phone=parent_phone.strip())
    db.add(user)
    db.flush()
    return user, pw


def create_teacher(db: Session, name: str, phone: str, subjects: str, standards: list[int],
                   password: str | None = None) -> tuple[User, str]:
    pw = password or temp_password()
    user = User(login_id=next_teacher_id(db), name=name.strip(), role="teacher",
                password_hash=hash_password(pw), must_change_password=password is None)
    user.teacher = Teacher(phone=phone.strip(), subjects=subjects.strip(),
                           standards=[TeacherStandard(standard=s) for s in sorted(set(standards))])
    db.add(user)
    db.flush()
    return user, pw


def clean_phone(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "".join(ch for ch in str(value or "") if ch.isdigit() or ch == "+")[:15]


# ----------------------------------------------------------------- subjects
def subjects_for(db: Session, standard: int, include_archived: bool = False) -> list[Subject]:
    q = select(Subject).where(Subject.standard == standard)
    if not include_archived:
        q = q.where(Subject.archived.is_(False))
    return list(db.scalars(q.order_by(Subject.archived, Subject.position, Subject.id)))


def ensure_default_subjects(db: Session) -> None:
    """Give every standard the default subjects - only standards that have never had any."""
    for std in STANDARDS:
        if db.scalar(select(func.count(Subject.id)).where(Subject.standard == std)):
            continue
        for pos, (name, colour, icon) in enumerate(DEFAULT_SUBJECTS):
            db.add(Subject(standard=std, name=name, colour=colour, icon=icon, position=pos))
    db.commit()


def subject_usage(db: Session, subject_id: int) -> tuple[int, int]:
    files = db.scalar(select(func.count(Content.id)).where(Content.subject_id == subject_id)) or 0
    marks = db.scalar(select(func.count(Result.id)).where(Result.subject_id == subject_id)) or 0
    return files, marks


def validate_subject(name: str, colour: str, icon: str) -> str | None:
    if not 2 <= len(name) <= 40:
        return "Subject name must be 2–40 characters."
    if colour not in SUBJECT_COLOURS:
        return "Pick a colour."
    if icon not in SUBJECT_ICONS:
        return "Pick an icon."
    return None


def add_subject(db: Session, standard: int, name: str, colour: str, icon: str) -> tuple[str, Subject]:
    """Returns ('added'|'restored'|'exists', subject). Re-adding an archived subject restores it."""
    existing = db.scalar(select(Subject).where(Subject.standard == standard,
                                               func.lower(Subject.name) == name.lower()))
    if existing and not existing.archived:
        return "exists", existing
    top = db.scalar(select(func.max(Subject.position)).where(Subject.standard == standard,
                                                             Subject.archived.is_(False)))
    position = (top if top is not None else -1) + 1
    if existing:
        existing.archived, existing.position = False, position
        existing.colour, existing.icon = colour, icon
        return "restored", existing
    subj = Subject(standard=standard, name=name, colour=colour, icon=icon, position=position)
    db.add(subj)
    db.flush()
    return "added", subj


def remove_subject(db: Session, subj: Subject) -> str:
    """Delete an unused subject; archive one that has notes or marks so history stays correct."""
    files, marks = subject_usage(db, subj.id)
    if files or marks:
        subj.archived = True
        return "archived"
    db.delete(subj)
    return "deleted"


def move_subject(db: Session, subj: Subject, direction: str) -> None:
    items = subjects_for(db, subj.standard)
    for pos, s in enumerate(items):  # normalise positions first
        s.position = pos
    i = next(i for i, s in enumerate(items) if s.id == subj.id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(items):
        items[i].position, items[j].position = items[j].position, items[i].position


# ----------------------------------------------------------------- results, ranks, progress
def exam_scores(db: Session, exam: Exam, student_ids: set[int] | None = None) -> dict[int, float]:
    """student_id -> average % across the subjects they have marks for."""
    rows = db.execute(select(Result.student_id, func.sum(Result.marks), func.count(Result.id))
                      .where(Result.exam_id == exam.id).group_by(Result.student_id)).all()
    out = {}
    for sid, total, count in rows:
        if count and (student_ids is None or sid in student_ids):
            out[sid] = round(total / (count * exam.max_marks) * 100, 1)
    return out


def rank(scores: dict[int, float]) -> list[tuple[int, int, float]]:
    """[(rank, student_id, percent)] - tied scores share a rank (1, 2, 2, 4)."""
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    out, prev, current = [], None, 0
    for i, (sid, pct) in enumerate(ordered, start=1):
        if pct != prev:
            current, prev = i, pct
        out.append((current, sid, pct))
    return out


def leaderboard(db: Session, standard: int):
    """Latest published exam of this standard that current students took -> ranked rows."""
    current = {s.id: s for s in db.scalars(select(Student).where(
        Student.standard == standard, Student.status == "active"))}
    exams = db.scalars(select(Exam).where(Exam.standard == standard, Exam.published.is_(True))
                       .order_by(Exam.exam_date.desc(), Exam.id.desc()))
    for exam in exams:
        scores = exam_scores(db, exam, set(current))
        if scores:
            return exam, [(r, current[sid], pct) for r, sid, pct in rank(scores)]
    return None, []


def student_progress(db: Session, st: Student) -> list[dict]:
    exams = db.scalars(select(Exam).join(Result, Result.exam_id == Exam.id)
                       .where(Result.student_id == st.id, Exam.published.is_(True))
                       .distinct().order_by(Exam.exam_date, Exam.id)).all()
    out = []
    for exam in exams:
        results = db.scalars(select(Result).where(Result.exam_id == exam.id, Result.student_id == st.id)).all()
        results = sorted(results, key=lambda r: (r.subject.position, r.subject.id))
        subjects = [{"name": r.subject.name, "colour": SUBJECT_COLOURS.get(r.subject.colour, "#4f46e5"),
                     "icon": r.subject.icon, "marks": r.marks,
                     "percent": round(r.marks / exam.max_marks * 100, 1)} for r in results]
        total = sum(r.marks for r in results)
        out_of = len(results) * exam.max_marks
        ranked = rank(exam_scores(db, exam))
        my_rank = next((r for r, sid, _ in ranked if sid == st.id), None)
        remark = db.scalar(select(ExamRemark.remark).where(ExamRemark.exam_id == exam.id,
                                                           ExamRemark.student_id == st.id))
        out.append({"exam": exam, "subjects": subjects, "total": total, "out_of": out_of,
                    "percent": round(total / out_of * 100, 1) if out_of else 0,
                    "rank": my_rank, "class_size": len(ranked), "remark": remark})
    return out


def exam_roster(db: Session, exam: Exam) -> list[Student]:
    """Students currently in the exam's class, plus anyone who already has marks in it."""
    with_marks = select(Result.student_id).where(Result.exam_id == exam.id)
    q = (select(Student).join(User).where(
        ((Student.standard == exam.standard) & (Student.status == "active") & User.active.is_(True))
        | Student.id.in_(with_marks)).order_by(User.name))
    return list(db.scalars(q))


def exam_subjects(db: Session, exam: Exam) -> list[Subject]:
    """Active subjects of the class, plus archived ones that already have marks in this exam."""
    used = select(Result.subject_id).where(Result.exam_id == exam.id)
    q = select(Subject).where(Subject.standard == exam.standard,
                              (Subject.archived.is_(False)) | Subject.id.in_(used))
    return list(db.scalars(q.order_by(Subject.archived, Subject.position, Subject.id)))


def save_marks(db: Session, exam: Exam, cells: dict[str, str]) -> tuple[int, list[str]]:
    """cells: {"m_<student>_<subject>": "45", "r_<student>": "Good work"}. Empty mark = delete.
    Returns (number saved, list of problems)."""
    roster = {s.id: s for s in exam_roster(db, exam)}
    subjects = {s.id: s for s in exam_subjects(db, exam)}
    existing = {(r.student_id, r.subject_id): r for r in
                db.scalars(select(Result).where(Result.exam_id == exam.id))}
    remarks = {r.student_id: r for r in db.scalars(select(ExamRemark).where(ExamRemark.exam_id == exam.id))}
    saved, problems = 0, []
    for key, raw in cells.items():
        parts = key.split("_")
        value = str(raw or "").strip()
        try:
            if parts[0] == "m" and len(parts) == 3:
                sid, subj_id = int(parts[1]), int(parts[2])
                if sid not in roster or subj_id not in subjects:
                    continue
                current = existing.get((sid, subj_id))
                if value == "":
                    if current:
                        db.delete(current)
                        saved += 1
                    continue
                mark = float(value)
                if not 0 <= mark <= exam.max_marks:
                    raise ValueError
                if current:
                    if current.marks != mark:
                        current.marks = mark
                        saved += 1
                else:
                    db.add(Result(exam_id=exam.id, student_id=sid, subject_id=subj_id, marks=mark))
                    saved += 1
            elif parts[0] == "r" and len(parts) == 2:
                sid = int(parts[1])
                if sid not in roster:
                    continue
                current = remarks.get(sid)
                if not value:
                    if current:
                        db.delete(current)
                        saved += 1
                elif current:
                    if current.remark != value[:300]:
                        current.remark = value[:300]
                        saved += 1
                else:
                    db.add(ExamRemark(exam_id=exam.id, student_id=sid, remark=value[:300]))
                    saved += 1
        except ValueError:
            who = roster.get(int(parts[1])) if len(parts) > 1 and parts[1].isdigit() else None
            problems.append(f"{who.user.name if who else 'A student'}: '{value}' is not between 0 and "
                            f"{exam.max_marks:g}")
    return saved, problems


# ----------------------------------------------------------------- fees
def fee_totals(db: Session, student_ids: list[int]) -> dict[int, dict]:
    out = {sid: {"billed": 0.0, "paid": 0.0, "due": 0.0, "overdue": 0.0} for sid in student_ids}
    if not student_ids:
        return out
    for f in db.scalars(select(FeeInstalment).where(FeeInstalment.student_id.in_(student_ids))):
        t = out[f.student_id]
        t["billed"] += f.amount
        t["paid"] += f.paid_amount
        t["due"] += f.outstanding
        if f.status == "Overdue":
            t["overdue"] += f.outstanding
    return out


def next_receipt_no(db: Session) -> str:
    prefix = f"RCPT-{date.today().year}-"
    nums = db.scalars(select(Payment.receipt_no).where(Payment.receipt_no.like(prefix + "%"))).all()
    n = max((int(r.rsplit("-", 1)[1]) for r in nums), default=0) + 1
    return f"{prefix}{n:04d}"


def record_payment(db: Session, inst: FeeInstalment, amount: float, method: str, reference: str,
                   by_user: int, paid_on: date | None = None) -> Payment:
    if method not in PAYMENT_METHODS:
        raise ValueError("Choose a payment method.")
    if amount <= 0 or amount > inst.outstanding + 0.01:
        raise ValueError(f"Amount must be between ₹1 and {inst.outstanding:,.0f} (the balance).")
    if method == "UPI" and not reference.strip():
        raise ValueError("Enter the UPI transaction reference (UTR).")
    pay = Payment(instalment_id=inst.id, amount=round(amount, 2), method=method,
                  reference=reference.strip()[:80], receipt_no=next_receipt_no(db),
                  paid_on=paid_on or date.today(), recorded_by=by_user)
    inst.paid_amount = round(inst.paid_amount + amount, 2)
    db.add(pay)
    db.flush()
    return pay


# ----------------------------------------------------------------- import & promotion
def read_rows(filename: str, data: bytes) -> list[dict]:
    """Rows from a CSV or Excel file, with headers normalised to lower_snake_case."""
    name = filename.lower()
    rows: list[list] = []
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
    elif name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ValueError("Upload a .csv or .xlsx file.")
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        return []
    headers = [str(h or "").strip().lower().replace(" ", "_") for h in rows[0]]
    return [dict(zip(headers, r)) for r in rows[1:]]


def import_students(db: Session, rows: list[dict], allowed: list[int]):
    created, errors = [], []
    if len(rows) > 1000:
        return created, ["Import at most 1000 rows at a time."]
    for i, row in enumerate(rows, start=2):
        name = str(row.get("name") or "").strip()
        raw_std = row.get("standard", row.get("class"))
        try:
            std = int(float(raw_std))
        except (TypeError, ValueError):
            std = None
        if len(name) < 2:
            errors.append(f"Row {i}: name is missing.")
            continue
        if std not in STANDARDS:
            errors.append(f"Row {i} ({name}): class must be 1–12.")
            continue
        if std not in allowed:
            errors.append(f"Row {i} ({name}): class {std} is not allowed for you.")
            continue
        user, pw = create_student(db, name[:120], std, str(row.get("parent_name") or "")[:120],
                                  clean_phone(row.get("parent_phone")))
        created.append((user, pw))
    return created, errors


def promote_all(db: Session) -> dict:
    moved = passed = 0
    for st in db.scalars(select(Student).where(Student.status == "active")):
        if st.standard >= 12:
            st.status = "passed_out"
            passed += 1
        else:
            st.standard += 1
            moved += 1
    return {"moved": moved, "passed": passed}


# ----------------------------------------------------------------- charts (SVG, animated with CSS)
def trend_svg(points: list[tuple[str, float]]) -> str:
    if len(points) < 2:
        return ""
    w, h, left, right, top, bottom = 340, 180, 30, 10, 30, 30
    pad = 26  # keeps the first/last value labels clear of the axis labels
    step = (w - left - right - 2 * pad) / (len(points) - 1)
    xy = [(left + pad + i * step, top + (100 - p) / 100 * (h - top - bottom)) for i, (_, p) in enumerate(points)]
    grid = "".join(
        f'<line class="g" x1="{left}" x2="{w - right}" y1="{top + (100 - v) / 100 * (h - top - bottom):.1f}" '
        f'y2="{top + (100 - v) / 100 * (h - top - bottom):.1f}"/>'
        f'<text class="yl" x="{left - 6}" y="{top + (100 - v) / 100 * (h - top - bottom) + 4:.1f}">{v}</text>'
        for v in (0, 50, 100))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{left},{h - bottom} " + line + f" {w - right},{h - bottom}"
    dots = "".join(
        f'<circle class="d" style="--i:{i}" cx="{x:.1f}" cy="{y:.1f}" r="5"/>'
        f'<text class="v" style="--i:{i}" x="{x:.1f}" y="{y - 10:.1f}">{p:g}%</text>'
        f'<text class="xl" x="{x:.1f}" y="{h - 10}">{escape(label[:12])}</text>'
        for i, ((x, y), (label, p)) in enumerate(zip(xy, points)))
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Progress across exams">'
            f'<defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="currentColor" '
            f'stop-opacity=".28"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>'
            f'{grid}<polygon class="area" points="{area}" fill="url(#ga)"/>'
            f'<polyline class="l" pathLength="1" points="{line}"/>{dots}</svg>')


def fees_bar_svg(rows: list[tuple[str, float, float]]) -> str:
    """rows: [(label, collected, pending)] -> grouped animated bars."""
    if not rows:
        return ""
    w, h, left, bottom, top = 340, 190, 8, 28, 16
    peak = max(max(c + p for _, c, p in rows), 1)
    slot = (w - left * 2) / len(rows)
    bar = min(slot * 0.5, 44)
    parts = []
    for i, (label, col, pen) in enumerate(rows):
        x = left + i * slot + (slot - bar) / 2
        hc = col / peak * (h - top - bottom)
        hp = pen / peak * (h - top - bottom)
        base = h - bottom
        parts.append(
            f'<g style="--i:{i}"><rect class="b-col" x="{x:.1f}" y="{base - hc:.1f}" width="{bar:.1f}" height="{hc:.1f}" rx="6"/>'
            f'<rect class="b-pen" x="{x:.1f}" y="{base - hc - hp:.1f}" width="{bar:.1f}" height="{hp:.1f}" rx="6"/>'
            f'<text class="xl" x="{x + bar / 2:.1f}" y="{h - 9}">{escape(label)}</text></g>')
    return (f'<svg class="chart bars" viewBox="0 0 {w} {h}" role="img" aria-label="Fees by class">'
            f'<line class="g" x1="{left}" x2="{w - left}" y1="{h - bottom}" y2="{h - bottom}"/>{"".join(parts)}</svg>')
