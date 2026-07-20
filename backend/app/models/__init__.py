"""SQLAlchemy models — Exam OS CBT domain layer.

Import order is intentional: parent entities before children so relationship
string resolutions and metadata registration remain stable under
``db.create_all()`` and Alembic autogenerate.

Public exports (stable — routes/services import these names)::

    User, Subject, Chapter, Question, QuestionOption,
    Exam, ExamSection, ExamQuestion, Attempt, AttemptAnswer

Do not rename table names, class names, or ``__all__`` entries — they are part
of the application contract.
"""

from __future__ import annotations

from app.models.user import User
from app.models.subject import Subject
from app.models.chapter import Chapter
from app.models.question import Question, QuestionOption
from app.models.exam import Exam, ExamSection, ExamQuestion
from app.models.attempt import Attempt, AttemptAnswer
from app.models.bank import (
    QuestionBank,
    Topic,
    Tag,
    QuestionTag,
    ImportJob,
    AuditLog,
)
from app.models.platform import Plan, Subscription, Payment, Coupon, WalletLedger
from app.models.auth_otp import PhoneOtp
from app.models.notification import (
    NotificationTemplate,
    NotificationPreference,
    Notification,
    NotificationDelivery,
)
from app.models.knowledge import QuestionAppearance, KnowledgeIngestionJob

__all__ = [
    "User",
    "Subject",
    "Chapter",
    "Question",
    "QuestionOption",
    "Exam",
    "ExamSection",
    "ExamQuestion",
    "Attempt",
    "AttemptAnswer",
    "QuestionBank",
    "Topic",
    "Tag",
    "QuestionTag",
    "ImportJob",
    "AuditLog",
    "Plan",
    "Subscription",
    "Payment",
    "Coupon",
    "WalletLedger",
    "PhoneOtp",
    "NotificationTemplate",
    "NotificationPreference",
    "Notification",
    "NotificationDelivery",
    "QuestionAppearance",
    "KnowledgeIngestionJob",
]
