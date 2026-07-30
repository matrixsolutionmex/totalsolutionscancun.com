from datetime import datetime

from pydantic import BaseModel, Field


class NotificationResponse(BaseModel):
    id: int
    recipient_user_id: int
    actor_user_id: int | None = None
    actor_name: str | None = None
    type: str
    title: str
    message: str
    lead_id: int | None = None
    priority: str
    action_url: str | None = None
    read_at: datetime | None = None
    created_at: datetime
    metadata: dict | None = None


class NotificationPreferenceResponse(BaseModel):
    in_app_enabled: bool = True
    sound_enabled: bool = True
    sound_volume: int = 55
    email_enabled: bool = True
    browser_enabled: bool = False
    urgent_enabled: bool = True
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class NotificationPreferenceUpdate(BaseModel):
    in_app_enabled: bool | None = None
    sound_enabled: bool | None = None
    sound_volume: int | None = Field(default=None, ge=0, le=100)
    email_enabled: bool | None = None
    browser_enabled: bool | None = None
    urgent_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class WebPushKeys(BaseModel):
    p256dh: str
    auth: str


class WebPushSubscriptionCreate(BaseModel):
    endpoint: str
    keys: WebPushKeys
    device_label: str | None = Field(default=None, max_length=120)


class WebPushDeactivateRequest(BaseModel):
    endpoint: str


class WebPushSubscriptionResponse(BaseModel):
    id: int
    device_label: str | None = None
    active: bool
    created_at: datetime
    last_used_at: datetime | None = None


class WebPushStateResponse(BaseModel):
    supported: bool
    subscribed: bool
    vapid_public_key: str | None = None
    subscriptions: list[WebPushSubscriptionResponse] = Field(default_factory=list)
