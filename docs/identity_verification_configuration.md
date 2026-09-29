# Identity verification rollout

The identity verification UI is fail-closed by default. The server must have
the UI kill switch enabled, a complete sandbox provider configuration, and an
authorized rollout mode before it returns `available: true` or exposes the
provider Client ID and Flow ID.

## Configuration

`IDENTITY_VERIFICATION_UI_ENABLED` is the kill switch and defaults to `false`.
`IDENTITY_VERIFICATION_ROLLOUT_MODE` defaults to `off` and accepts:

- `off`: no user can start verification.
- `canary`: only positive numeric user IDs listed in
  `IDENTITY_VERIFICATION_CANARY_USER_IDS` can start verification. The list is
  comma-separated; spaces and duplicate IDs are normalized. Any malformed
  token invalidates the complete list.
- `all`: all existing identity-eligible roles may access the feature. This
  mode is not intended for the current production rollout.

`IDENTITY_VERIFICATION_CANARY_USER_IDS` is never sent to the browser. ROOT and
ADMIN do not bypass the canary. Invalid modes, empty or malformed lists, an
incomplete provider configuration, and a disabled kill switch all fail closed.

The server applies the same decision to the status endpoint, provider
configuration response, and attempt creation endpoint. Existing signed
webhooks for previously created attempts remain processable if a user is later
removed from the canary list.
