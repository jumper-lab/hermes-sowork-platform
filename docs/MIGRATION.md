# Migration and recovery

## 0.13 conversation routing

No database or existing configuration migration is required. Without `extra.channels`, the original single-human/private-DM policy remains in force. Installing this release does not authorize new users or join/enable channels. Original primary-DM delivery identities, journal scope, cursor, receipts and thread references are retained.

To expand access later, review `examples/channels.yaml`, the app's existing membership/grants and the exact inbox subscription event list. Retain any existing primary DM in the explicit mapping; set both native allowlists to their documented conversation-user unions. Review pending/uncertain work before changing routes, verify with `check-connection`, and use the native lifecycle only for the selected profile. The guided setup continues to pair one private DM initially. Read README's conversation rules before enabling shared channels.

This candidate does not change an existing installation or service. A privately managed 0.9.0 deployment can continue operating independently while publication validation proceeds.

For a later operator-authorized migration:

1. Confirm the exact profile, single allowed human, pinned DM, scopes and exclusive subscription. Keep credentials in Hermes' own profile secret configuration, including native 1Password references.
2. Review the new plugin commit/package and run its isolated tests and native validation. Use `check-runtime` before resolving credentials.
3. Preserve a code backup. Drain active work and inspect pending deliveries, uncertain inputs and unfinished visuals. Do not proceed through an ambiguous delivery.
4. Use Hermes' native installation/update mechanism. Stop/start only the intended profile's gateway when necessary; never run another consumer on its subscription.
5. Recheck identity, DM membership, allowlist and other channel settings. Verify text, attachments, thread routing, preview/cancellation and an authorized reminder as applicable.
6. Recover code through the native installer/operator backup if necessary. Never restore state, jobs, sessions, old cursor or outbox alongside code. Reconcile ambiguous receipts before resending.

`maintenance apply`, `maintenance rollback` and `release` were private operator commands and are intentionally absent. A direct offline `--home` inspection is not a credential-loading mechanism; `check-connection` must run through Hermes with the intended profile active.

The public package exports no deployment controller, SSH launcher, production identifiers, credential files or private history. Do not publish the old pilot repository's Git history.

## 0.11 journal retirement

The additive schema upgrade keeps current queued work, cursor and receipts. Once
cleanup retires completed payloads, only 0.11-or-newer compatible code understands
the compact replay barriers. Do not downgrade to 0.10/0.9 against that cleaned
journal or leave old plugin workers running against it. Recover with compatible
code and operator reconciliation; never restore an old database/cursor to make
a downgrade appear to work. Retired payloads are intentionally not recoverable
from the compact digest. Existing private installations are not upgraded here.
