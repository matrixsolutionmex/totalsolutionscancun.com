# Technician earning automation rollout

This runbook covers the server-side rollout gate for automatic positive earning recognition. The master flag is disabled by default and this control does not create payouts, transfers, or bank records.

## Configuration

Set these variables together in the deployment environment:

```text
TECHNICIAN_EARNING_AUTOMATION_ENABLED=false
TECHNICIAN_EARNING_AUTOMATION_ROLLOUT_MODE=off
TECHNICIAN_EARNING_AUTOMATION_CANARY_ORGANIZATION_IDS=
```

`off` blocks new positive earnings. `canary` requires a comma-separated allowlist of positive integer organization IDs. `all` permits eligible ACTIVE organizations. Any invalid mode, empty canary list, non-positive ID, duplicate ID, or allowlist supplied outside `canary` fails closed for new positive earnings.

The rollout decision is derived from the organization on the service order/payment and is never taken from the actor, frontend, or provider metadata. ROOT and GERENTE have no bypass. The frontend receives only the technician's own state; it does not receive rollout mode or the allowlist.

## Activation and rollback

1. Keep the master flag false while validating the deployment.
2. Set the master flag true with mode `canary` and the approved organization IDs.
3. Monitor positive recognition, rejected gate reason codes, reconciliation idempotency, and database errors.
4. To pause, set the master flag false. Payments continue normally; automatic recognition is skipped.
5. To remove an organization, remove its ID from the allowlist or set mode `off`.

Pausing or removing an organization does not abandon earnings already recognized. Refunds, dispute adjustments, reversals, and warranty/guarantee transitions for existing earnings remain processable while the master flag is enabled. Invalid rollout configuration blocks new positives but does not block those safety adjustments. The runner never performs payouts and is not started by application startup.

## Replay after a pause

Payment events occurring while automation is paused are not silently marked complete by this gate. Before reactivation, reconcile the provider event history with the existing idempotency keys and run the approved replay/reconciliation procedure. Review duplicates and partial provider delivery before enabling a wider scope.

## Required positive-earning gates

The master flag and rollout gate are only one part of eligibility. A new positive earning also requires an ACTIVE organization, an ACTIVE and operational technician membership, a post-cutoff service order, a FROZEN tenant-matching snapshot, a confirmed payment, and consistent tenant, currency, and technician identifiers.

## Safety adjustments

Refunds and disputes are tied to existing earnings and preserve reversal links. A dispute recovery without an existing positive earning is recorded as a provider fact without creating a new positive earning. No path in this rollout performs payout, withdrawal, or transfer.

## Monitoring

Alert on invalid configuration, repeated `AUTOMATION_ROLLOUT_INVALID`, unexpected `AUTOMATION_SCHEMA_UNAVAILABLE`, reconciliation conflicts, duplicate provider events, and any non-zero positive recognition outside the approved organization scope. Audit counts of positive earnings, reversals, and reconciliation events by organization and currency without logging provider payloads or secrets.
