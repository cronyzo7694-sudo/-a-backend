"""User feedback / test-request messages."""

from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UserMessage(db.Model):
    __tablename__ = "user_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    name = db.Column(db.String(120), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    message_type = db.Column(db.String(32), nullable=False, default="request")  # request | feedback | other
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="new")  # new | read | done
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "email": self.email,
            "message_type": self.message_type,
            "message": self.message,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
