from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database.connection import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    recipient_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    type = Column(String, nullable=False, index=True)
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    priority = Column(String, nullable=False, default="NORMAL")
    action_url = Column(String, nullable=True)
    read_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    metadata_json = Column(Text, nullable=True)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    in_app_enabled = Column(Boolean, default=True, nullable=False)
    sound_enabled = Column(Boolean, default=True, nullable=False)
    sound_volume = Column(Integer, default=55, nullable=False)
    email_enabled = Column(Boolean, default=True, nullable=False)
    browser_enabled = Column(Boolean, default=False, nullable=False)
    urgent_enabled = Column(Boolean, default=True, nullable=False)
    quiet_hours_start = Column(String, nullable=True)
    quiet_hours_end = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EmailOutbox(Base):
    __tablename__ = "email_outbox"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    notification_id = Column(Integer, ForeignKey("notifications.id"), nullable=True, index=True)
    recipient_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    invitation_id = Column(Integer, ForeignKey("organization_invitations.id"), nullable=True, index=True)
    to_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body_text = Column(Text, nullable=False)
    body_html = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="PENDING", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    claimed_at = Column(DateTime, nullable=True, index=True)
    provider = Column(String, nullable=True)
    provider_message_id = Column(String, nullable=True)
    next_attempt_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_error = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)


class WebPushSubscription(Base):
    __tablename__ = "web_push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    endpoint = Column(Text, nullable=False, unique=True)
    p256dh = Column(Text, nullable=False)
    auth = Column(Text, nullable=False)
    device_label = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    active = Column(Boolean, default=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    disabled_at = Column(DateTime, nullable=True)
