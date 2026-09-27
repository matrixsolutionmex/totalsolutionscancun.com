# Stripe Live Activation Runbook

This runbook is intentionally operational. It contains no Stripe secrets and
does not activate live payments by itself.

## Preconditions

- Stripe account and business verification are complete.
- Live Checkout, refunds, disputes, and webhook endpoint are enabled in Stripe.
- Production has a live secret key, publishable key, webhook signing secret,
  and live price identifiers configured through the deployment secret store.
- `STRIPE_EXPECTED_LIVEMODE=true` is set in the same deployment as the live
  keys. The application rejects recognizable test keys in this mode and
  rejects webhook objects whose `livemode` does not match the policy.
- The webhook endpoint receives the raw request body and verifies the Stripe
  signature before parsing the event.

## Migration 084

Apply `backend/migrations/084_stripe_reconciliation_foundation.sql` through the
normal migration procedure before enabling live webhooks. It is deliberately
separate from application startup and does not modify historical payments,
ledger entries, organizations, or users.

The migration creates:

- `stripe_webhook_events`, keyed by the provider Event ID, with attempts,
  processing status, timestamps, and sanitized failure details.
- `stripe_payment_adjustments`, keyed to the payment and provider adjustment,
  for refunds, disputes, and dispute reversals.

Take a database backup and verify the two tables and indexes before enabling
the live endpoint. Rollback is a deployment rollback plus a migration rollback
approved by the database owner; do not delete financial history as an emergency
rollback action.

## Event handling

The webhook stores the Event ID before accounting work and locks an existing
record on retry. A succeeded Event ID is a no-op. Processing failures are
stored as `FAILED` with a bounded, sanitized error and remain retryable.

Supported accounting events:

- payment success and failure events already used by Checkout/subscriptions;
- `charge.refunded` for partial and full refunds;
- `charge.dispute.created`, `.updated`, and `.closed` for dispute lifecycle;
- a won dispute creates exactly one compensating reversal.

Refund and dispute ledger entries use deterministic idempotency keys. Provider
amounts and currencies are checked against the stored payment, and event
metadata must remain in the payment's organization.

## Canary procedure

1. Apply migration 084 and verify schema.
2. Configure live secrets and `STRIPE_EXPECTED_LIVEMODE=true` without logging
   values.
3. Configure the live Stripe webhook and verify signature delivery.
4. Create one controlled live Checkout payment approved by the business owner.
5. Confirm one payment ledger entry, one provider Event ID, and the expected
   organization/payment association.
6. Test a small refund and confirm one adjustment and one refund ledger entry.
7. Confirm the duplicate delivery is a no-op.
8. Monitor failures, retries, disputes, and reconciliation before widening
   traffic.

Never test live behavior with a real customer, an unapproved charge, or a
manual SQL edit to financial tables.

## Reconciliation and incident response

Checkout reconciliation must validate session ID, completion/payment status,
amount, currency, metadata, organization, and expected livemode before it
reuses the normal accounting path. Investigate any mismatch as a provider or
configuration incident. Preserve webhook rows and ledger history for audit.

If a live/test mismatch, signature failure, duplicate adjustment, cross-tenant
metadata mismatch, or unexplained balance appears, disable new checkout traffic
at the deployment layer, preserve the database records, and escalate to the
financial owner. Do not force-push, delete rows, or switch modes in place.
