from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now() -> datetime:
    return datetime.now()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(10))  # admin | teacher | student
    password_hash: Mapped[str] = mapped_column(String(200))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    student: Mapped["Student | None"] = relationship(back_populates="user", uselist=False)
    teacher: Mapped["Teacher | None"] = relationship(back_populates="user", uselist=False)


class Student(Base):
    __tablename__ = "students"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    standard: Mapped[int] = mapped_column(Integer, index=True)
    parent_name: Mapped[str] = mapped_column(String(120), default="")
    parent_phone: Mapped[str] = mapped_column(String(15), default="")
    joined_on: Mapped[date] = mapped_column(Date, default=date.today)
    status: Mapped[str] = mapped_column(String(15), default="active")  # active | passed_out

    user: Mapped[User] = relationship(back_populates="student")


class Teacher(Base):
    __tablename__ = "teachers"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    phone: Mapped[str] = mapped_column(String(15), default="")
    subjects: Mapped[str] = mapped_column(String(200), default="")

    user: Mapped[User] = relationship(back_populates="teacher")
    standards: Mapped[list["TeacherStandard"]] = relationship(cascade="all, delete-orphan")

    @property
    def standard_list(self) -> list[int]:
        return sorted(s.standard for s in self.standards)


class TeacherStandard(Base):
    __tablename__ = "teacher_standards"
    __table_args__ = (UniqueConstraint("teacher_id", "standard"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    standard: Mapped[int] = mapped_column(Integer)


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = (UniqueConstraint("standard", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    standard: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(40))
    colour: Mapped[str] = mapped_column(String(20), default="indigo")
    icon: Mapped[str] = mapped_column(String(8), default="📘")
    position: Mapped[int] = mapped_column(Integer, default=0)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Content(Base):
    __tablename__ = "content"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="CASCADE"), index=True)
    chapter: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200))
    file_path: Mapped[str] = mapped_column(String(200))
    file_type: Mapped[str] = mapped_column(String(10))  # pdf | docx
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    subject: Mapped[Subject] = relationship()


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    standard: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(120))
    exam_date: Mapped[date] = mapped_column(Date)
    max_marks: Mapped[float] = mapped_column(Float, default=100)  # per subject
    published: Mapped[bool] = mapped_column(Boolean, default=False)


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (UniqueConstraint("exam_id", "student_id", "subject_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="CASCADE"))
    marks: Mapped[float] = mapped_column(Float)

    subject: Mapped[Subject] = relationship()


class ExamRemark(Base):
    __tablename__ = "exam_remarks"
    __table_args__ = (UniqueConstraint("exam_id", "student_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    remark: Mapped[str] = mapped_column(String(300))


class FeeInstalment(Base):
    __tablename__ = "fee_instalments"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    amount: Mapped[float] = mapped_column(Float)
    paid_amount: Mapped[float] = mapped_column(Float, default=0)
    due_date: Mapped[date] = mapped_column(Date)

    student: Mapped[Student] = relationship()
    payments: Mapped[list["Payment"]] = relationship(back_populates="instalment", order_by="Payment.id")

    @property
    def outstanding(self) -> float:
        return max(round(self.amount - self.paid_amount, 2), 0)

    @property
    def status(self) -> str:
        if self.paid_amount >= self.amount:
            return "Paid"
        if self.due_date < date.today():
            return "Overdue"
        if self.paid_amount > 0:
            return "Partial"
        return "Due"


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    instalment_id: Mapped[int] = mapped_column(ForeignKey("fee_instalments.id", ondelete="CASCADE"))
    amount: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(15))  # UPI | Cash | Cheque | Bank
    reference: Mapped[str] = mapped_column(String(80), default="")
    receipt_no: Mapped[str] = mapped_column(String(20), unique=True)
    paid_on: Mapped[date] = mapped_column(Date, default=date.today)
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    instalment: Mapped[FeeInstalment] = relationship(back_populates="payments")


class Notice(Base):
    __tablename__ = "notices"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text, default="")
    standard: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = everyone
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(300), default="")
    # created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
