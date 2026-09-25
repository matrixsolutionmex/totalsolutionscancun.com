# Email Administration Review

Audit date: 2026-09-24
Domain: `totalsolutionscancun.com`
Scope: post-deploy infrastructure validation; no DNS, mailbox, alias, or credential changes were made by this validation.

## Verified Infrastructure

- DNS provider: Cloudflare, verified from authoritative nameservers `kim.ns.cloudflare.com` and `scott.ns.cloudflare.com`.
- Target human mail provider: Zoho Mail, domain verified, Mail Lite active.
- Inbound mail provider: Zoho Mail, verified by public MX records.
- Outbound human mail provider: Zoho Mail, verified by human test.
- Transactional provider: SMTP/Resend configuration exists in Railway; the active provider was not revealed by this audit.
- Cloudflare Email Routing: NOT VERIFIED. No provider access was available.

## DNS Authentication Audit

- SPF: PASS. Apex SPF authorizes Zoho Mail.
- DKIM: PASS. `zmail._domainkey` is present for Zoho; existing `resend._domainkey` records were preserved.
- DMARC: PASS. `_dmarc` exists with `p=none` and reports to `dmarc@totalsolutionscancun.com`.
- Catch-all: NOT ENABLED / not independently verified. Keep it disabled.

The DKIM public-key contents are intentionally not copied into this document.

## Provider Access

MAILBOX CREATION AVAILABLE: YES

REQUIRES HUMAN ACTION: YES

PROVIDER: Cloudflare controls DNS; Zoho Mail controls the verified domain.

ADMIN URL: `https://mailadmin.zoho.com/` for Zoho administration; `https://dash.cloudflare.com/` for DNS.

WEBMAIL URL: `https://mail.zoho.com/`

ZOHO ORGANIZATION: ACCESSIBLE - the Zoho account is signed in and `totalsolutionscancun.com` is verified.

ZOHO ADMIN SESSION: FOUND. The domain is verified and the Mail Lite plan is active.

ZOHO PLAN: Mail Lite, one license active.

LICENSES: 1

DOMAIN VERIFICATION: PASS via Zoho TXT verification.

CORPORATE USER: `weverton@totalsolutionscancun.com` is the active principal user. Zoho shows 9 additional addresses on the same user.

PRIMARY: `weverton@totalsolutionscancun.com`

PRIVACY: `privacidad@totalsolutionscancun.com`

GENERAL: `contacto@totalsolutionscancun.com`

COMMERCIAL: `comercial@totalsolutionscancun.com`

SUPPORT: `soporte@totalsolutionscancun.com`

The domain, primary address, and the human-tested privacy/general/support addresses were confirmed in Zoho. No additional user license was purchased.

## Mailboxes And Aliases

| Address | Purpose | Status |
| --- | --- | --- |
| `privacidad@totalsolutionscancun.com` | Privacy, opt-out, data rights, compliance | CONFIRMED BY HUMAN TEST |
| `contacto@totalsolutionscancun.com` | General institutional contact | CONFIRMED BY HUMAN TEST |
| `comercial@totalsolutionscancun.com` | Human-approved partnerships and commercial contact | ALIAS REPORTED / VERIFY INDIVIDUALLY |
| `soporte@totalsolutionscancun.com` | Customer, technician, and operational support | CONFIRMED BY HUMAN TEST |
| `notificaciones@totalsolutionscancun.com` | Reserved transactional sender | NOT_CONFIRMED |

Aliases reported on the primary account; individual delivery tests were not run by this audit:

- `ventas@` -> `comercial@`
- `alianzas@` -> `comercial@`
- `proveedores@` -> `comercial@`
- `legal@` -> `privacidad@`
- `facturacion@` -> `contacto@` unless a finance mailbox is later confirmed
- `dmarc@` -> a monitored mailbox selected by the owner

Do not create a public `admin@` address. Do not create a paid `no-reply@` mailbox unless the provider requires it; use a sender identity with a real Reply-To instead.

## CRM Configuration

The CRM reads `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and TLS/SSL settings, or a Resend API key through the existing implementation. These values were not present in the local process and no production values were retrieved.

Current transactional email status: CONFIGURED IN RAILWAY; active provider UNKNOWN without revealing credentials.

`PRIVACY_CONTACT_EMAIL` is configured in Railway as `privacidad@totalsolutionscancun.com`. Do not place any provider password or API key in Git, documentation, frontend code, or chat.

Post-deploy privacy validation: `/health`, all three privacy-notice language routes, and `/preferencias-comunicacion` returned HTTP 200. The privacy notice now reports `READY` in ES, EN, and PT after the confirmed legal environment fields were configured in Railway.

The public opt-out returned a generic success response for an artificial QA address. Suppression persistence and audit-event creation require an authenticated administrative inspection; no production admin credentials were used by this validation.

## Access Instructions For Weverton

- Account: use an owner/provider invitation or password-reset flow.
- Login URL: provider-specific, currently UNKNOWN.
- First access method: provider invitation or forced password reset.
- MFA: must be enabled by the owner/provider; current status UNKNOWN.
- Recovery: configure provider recovery methods under the owner account.
- Passwords: never store in this repository or send through chat.

## Contacts and Safety

- Emails sent: 0
- Prospects contacted: none
- Daltile: not contacted
- Scan Global: not contacted
- Liverpool: not contacted
- Outreach sending: disabled
