-- Mission 082: documentation migration for installations that do not run Alembic.
-- The application also registers these tables through Base.metadata.create_all.
-- No financial, payment, ledger, wallet or invoice tables are changed here.

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    email VARCHAR(320) NOT NULL,
    normalized_email VARCHAR(320) NOT NULL,
    name VARCHAR(160), company VARCHAR(240), segment VARCHAR(32) NOT NULL,
    source VARCHAR(32) NOT NULL, country VARCHAR(8), state VARCHAR(80), city VARCHAR(120),
    language VARCHAR(16) NOT NULL, status VARCHAR(24) NOT NULL,
    fit_score INTEGER NOT NULL, fit_score_breakdown TEXT NOT NULL,
    fit_score_version VARCHAR(24) NOT NULL, fit_score_calculated_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_campaign_contact_org_email UNIQUE (organization_id, normalized_email)
);

CREATE TABLE IF NOT EXISTS campaign_suppressions (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    normalized_email VARCHAR(320) NOT NULL,
    reason VARCHAR(32) NOT NULL,
    created_by_user_id INTEGER REFERENCES users(id), created_at TIMESTAMP NOT NULL,
    CONSTRAINT uq_campaign_suppression_org_email UNIQUE (organization_id, normalized_email)
);

CREATE TABLE IF NOT EXISTS technician_referrals (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    referrer_type VARCHAR(24) NOT NULL, referrer_id INTEGER NOT NULL,
    referral_code VARCHAR(32) NOT NULL UNIQUE, referred_name VARCHAR(160),
    referred_phone VARCHAR(40), referred_email VARCHAR(320), normalized_phone VARCHAR(40),
    normalized_email VARCHAR(320), referred_user_id INTEGER REFERENCES users(id),
    referred_technician_id INTEGER REFERENCES users(id), source VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL, approved_by INTEGER REFERENCES users(id),
    rejected_by INTEGER REFERENCES users(id), rejection_reason VARCHAR(240),
    created_at TIMESTAMP NOT NULL, registered_at TIMESTAMP, approved_at TIMESTAMP,
    rejected_at TIMESTAMP, updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_rewards (
    id INTEGER PRIMARY KEY,
    organization_id INTEGER NOT NULL REFERENCES organizations(id),
    referral_id INTEGER NOT NULL UNIQUE REFERENCES technician_referrals(id),
    reward_type VARCHAR(32) NOT NULL, reward_value NUMERIC(8,2) NOT NULL,
    reward_cap_amount NUMERIC(12,2) NOT NULL, currency VARCHAR(8) NOT NULL,
    status VARCHAR(32) NOT NULL, reserved_service_order_id INTEGER REFERENCES service_orders(id),
    reserved_at TIMESTAMP, used_service_order_id INTEGER REFERENCES service_orders(id),
    used_at TIMESTAMP, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS segmentation_referral_audit_events (
    id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES organizations(id),
    actor_id INTEGER REFERENCES users(id), event_type VARCHAR(48) NOT NULL,
    contact_id INTEGER REFERENCES campaign_contacts(id), referral_id INTEGER REFERENCES technician_referrals(id),
    reward_id INTEGER REFERENCES referral_rewards(id), service_order_id INTEGER REFERENCES service_orders(id),
    previous_status VARCHAR(32), new_status VARCHAR(32), metadata_json TEXT, created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_campaign_contacts_organization_id ON campaign_contacts (organization_id);
CREATE INDEX IF NOT EXISTS ix_campaign_contacts_segment ON campaign_contacts (segment);
CREATE INDEX IF NOT EXISTS ix_campaign_contacts_status ON campaign_contacts (status);
CREATE INDEX IF NOT EXISTS ix_campaign_contacts_org_segment_score ON campaign_contacts (organization_id, segment, fit_score);
CREATE INDEX IF NOT EXISTS ix_campaign_suppressions_organization_id ON campaign_suppressions (organization_id);
CREATE INDEX IF NOT EXISTS ix_technician_referrals_organization_id ON technician_referrals (organization_id);
CREATE INDEX IF NOT EXISTS ix_technician_referrals_status ON technician_referrals (status);
CREATE INDEX IF NOT EXISTS ix_technician_referrals_org_status ON technician_referrals (organization_id, status);
CREATE INDEX IF NOT EXISTS ix_referral_rewards_organization_id ON referral_rewards (organization_id);
CREATE INDEX IF NOT EXISTS ix_referral_rewards_status ON referral_rewards (status);
CREATE INDEX IF NOT EXISTS ix_referral_rewards_referral_id ON referral_rewards (referral_id);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_organization_id ON segmentation_referral_audit_events (organization_id);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_actor_id ON segmentation_referral_audit_events (actor_id);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_event_type ON segmentation_referral_audit_events (event_type);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_contact_id ON segmentation_referral_audit_events (contact_id);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_referral_id ON segmentation_referral_audit_events (referral_id);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_reward_id ON segmentation_referral_audit_events (reward_id);
CREATE INDEX IF NOT EXISTS ix_segmentation_referral_audit_events_service_order_id ON segmentation_referral_audit_events (service_order_id);
