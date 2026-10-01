-- Allow audited invalidation of incorrect draft compensation policies.
-- Manual migration only. No data updates, backfill, seeds or financial writes.

ALTER TABLE technician_compensation_policies
    DROP CONSTRAINT IF EXISTS ck_compensation_policy_status;
ALTER TABLE technician_compensation_policies
    ADD CONSTRAINT ck_compensation_policy_status
    CHECK (status IN ('DRAFT', 'ACTIVE', 'RETIRED', 'VOID'));

ALTER TABLE technician_compensation_events
    DROP CONSTRAINT IF EXISTS ck_compensation_event_type;
ALTER TABLE technician_compensation_events
    ADD CONSTRAINT ck_compensation_event_type
    CHECK (event_type IN (
        'POLICY_CREATED', 'POLICY_ACTIVATED', 'POLICY_VOIDED',
        'SNAPSHOT_PROPOSED', 'SNAPSHOT_FROZEN', 'SNAPSHOT_VOIDED'
    ));
