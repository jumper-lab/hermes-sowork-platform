# SoWork platform for Hermes

[![Plugin compatibility](https://github.com/jumper-lab/hermes-sowork-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/jumper-lab/hermes-sowork-platform/actions/workflows/ci.yml)

An independent Hermes gateway plugin for **one explicitly authorized human and one private SoWork DM**, including threads in that DM. Uses installation OAuth, REST delivery and SSE with cursor recovery. No new model tools or prompt changes during a conversation.

## Supported scope

- Text, images, documents, audio and video through Hermes' existing media interfaces.
- Native Hermes sessions, tools, skills, textual approvals, `/reset` and `/stop`.
- Optional isolated threads, cron delivery, processing reactions and private incremental previews.
- Optional bounded reaction experiment: `/reaction-test` observes 👀 on its own test message, for at most 120 seconds and 12 reads including baseline. Its sole effect is removing its private indicator. `/reaction-status` reports the result. **An emoji never authorizes or executes anything.** Main DM only.
- Local status/configuration diagnostics and read-only connection verification.

Audio is delivered as an attachment, not a native voice-note UI. Interactive buttons, permanent message editing/deletion, general reaction events, other users and group/channel conversations are outside this release. Preview availability also depends on Hermes and the selected provider; unsupported routes fall back to a final response. No provider/core patch is installed by this plugin.

## Requirements

- Hermes **0.21.5 or newer**, subject to the gateway contract check (`check-runtime`). See [compatibility](docs/COMPATIBILITY.md) for exact tested commits.
- Linux or macOS; Python 3.11–3.14. Windows is not supported because the plugin uses POSIX file locks.
- An existing SoWork app installation and its installation credentials. Setup creates an exclusive inbox-only subscription and confirms one human/DM; it never expands granted scopes.

## Installation

Select the intended Hermes profile, install/enable the plugin with the native installer, then run setup in a terminal:

```sh
hermes --profile my-agent plugins install https://github.com/jumper-lab/hermes-sowork-platform --enable
hermes --profile my-agent sowork setup
```

Use the same profile for both commands. This is not yet a catalog listing. Manual configuration remains available through the [non-secret example](examples/config.yaml).

Setup shows the authenticated app and workspace, the features it can enable and the active profile. It asks before creating a subscription, prints a fresh code to send to that app in a private 1:1 DM, and asks you to confirm the verified human before saving. An API-declared installation owner, or optional `--owner-id <confirmed-id>`, constrains who may pair; a workspace app without an owner still requires the private code and terminal confirmation. Other senders never become authorized automatically. The setup code never invokes the model.

Global streaming is preserved. If it is off or incompatible, setup leaves private previews off. Threads require existing scopes; setup does not request new permissions. Attachments, status reactions, a private activity indicator and reminders to the pinned DM are enabled. The reaction prototype remains off. No existing channel, model, memory or session configuration is copied or replaced.

Configure these credentials **in Hermes**, through its supported profile secret sources:

| Name | Value |
|---|---|
| `SOWORK_USERNAME` | SoWork installation OAuth client ID |
| `SOWORK_CREDENTIAL` | SoWork installation OAuth client secret |
| `SOWORK_INSTALLATION_ID` | SoWork installation ID |

1Password remains supported **through Hermes' own secret-source configuration**. Configure that backend for the intended profile first. If any of the three credentials is missing, setup asks for the corresponding `op://vault/item/field` references and confirms saving them in the existing native mapping. Copy field references, not credential values or a whole-item web link; UUID references are preferable because vault/item renames do not break them. The existing service account or desktop authentication stays under Hermes' control.

The plugin reads final credentials through `agent.secret_scope`. Setup delegates a refresh to Hermes' public, profile-scoped secret-source API. It does not open credential files, invoke `op`, read keychains, write secrets into YAML/`.env`, or load another profile's secrets. A missing bound secret never borrows a process-level credential. Ordinary status/diagnostic commands remain offline.

With the intended profile selected:

```sh
hermes sowork --json check-runtime
hermes sowork --json status
hermes sowork --json check-connection
hermes sowork --json check-preview
```

`check-connection` performs OAuth and GET-only verification of the installation, scopes, subscription and DM membership. It sends no message and starts no receiver/model. Direct `python /path/to/plugin/soworkctl.py --json status` is an offline entry; it does not bootstrap secrets. For connection checks, use Hermes with the intended active profile. `--home` can inspect a different profile offline, but cannot authorize access to its secrets.

After saving, setup prints the native gateway restart command for the selected profile; it does not restart services itself. Restart that profile's gateway when its current work is finished, then send a test DM. Channels served by that same gateway briefly reconnect. Other profiles must remain untouched. Never start a second consumer against the same subscription.

If pairing times out or you cancel the final save, run setup again: its private checkpoint reuses only the subscription it recorded. A lost subscription-creation receipt is ambiguous, so setup stops instead of retrying and possibly creating a duplicate. Inspect the app's subscriptions, verify exclusive ownership, then use `sowork setup --subscription-id <reviewed-id>` to resume. Existing unrelated subscriptions are never deleted or adopted automatically. Setup cannot silently replace an existing configured binding; rerunning verifies it without changing it. JSON/noninteractive setup is rejected rather than auto-approving a human or side effect.

## Access and API scopes

The configuration must pin the workspace, app, subscription, human and DM IDs. `allow_from` must contain **exactly** that human. The plugin retains Hermes' authorization checks in addition to its own binding checks.

Baseline scopes: `chat.dm`, `chat.write`, `events.subscribe:chat.direct_message_created`. Threads additionally require already-consented `chat.history` and `events.subscribe:chat.thread_reply_created`; the exclusive subscription must contain exactly the enabled events. The bounded reaction experiment also needs `chat.history`. Setup can create its own subscription with these already-granted events after confirmation; the gateway transport never creates one. Permission is never requested automatically.

## Updates and recovery

Use **Hermes' native plugin installer/updater**, pinned to the reviewed commit. There is no plugin self-update, package apply or code rollback command in this distribution. Existing privately managed installations are not automatically converted or restarted. See [migration and recovery](docs/MIGRATION.md).

Inbox/cursor and immutable outbox state live inside the owning profile.
Generic HTTP 404 responses from polling or SSE trigger a read-only recheck of the
original installation and subscription before retrying with backoff (up to 30
seconds). Every reconnect first polls from the saved cursor; an unavailable SSE
route therefore still permits polling delivery. Explicit API errors, revoked
grants and changed/deleted subscriptions remain fatal. Receiver warnings identify
the failed operation without exposing tokens, cursors or response bodies.
Hourly bounded cleanup removes terminal payloads older than 30 days. Queued,
claimed, failed, uncertain and pending-delivery records, unfinished uploads and
visuals, and thread routes are preserved. Compact SHA-256 replay barriers remain:
retired inputs cannot invoke the agent again, and a retired outbound operation
requires reconciliation instead of posting again. Those digests and routing/review
records still grow with use; cleanup bounds retained completed payloads, not the
entire database. SQLite reuses freed pages without an automatic VACUUM. A claimed input interrupted by a crash becomes uncertain and is not replayed into tools. Ambiguous sends preserve their original idempotency key/body. Retention gaps and old pending deliveries require operator reconciliation; there is no general exactly-once guarantee. Never restore an old cursor to force a replay or roll back the database with the code.

## Operational disclosure

The plugin connects to `api.sowork.com` and API-issued signed attachment storage URLs. It keeps OAuth access tokens in memory, uses a separate unauthenticated storage client and rejects redirects. It runs a background SSE receiver and optional bounded visual/reaction tasks. Media is cached under the profile; inbox/outbox contain conversation data and delivery receipts. Local diagnostics read this profile's config, gateway snapshot and relevant SQLite routing/state metadata. Preview compatibility invokes a credential-free temporary Python child and loads installed provider code; it does not call a model. The adapter uses the public PluginContext cron identity API. The supported Hermes queue drain still needs a checked compatibility fallback; see the [upstream API proposal](docs/HERMES-API-PROPOSAL.md). Other gateway/provider seams remain version-sensitive and are checked by the compatibility suite. No telemetry is added.

## Development and publication

Use `scripts/verify.py` with a supported Hermes checkout and its prepared test interpreter (dev/test groups and the native messaging extra for the shared process harness); it delegates to Hermes' canonical test runner in a disposable source/profile copy. [CI](https://github.com/jumper-lab/hermes-sowork-platform/actions/workflows/ci.yml) exercises pinned stable/current commits on Linux and macOS, including a real gateway child, a loopback SoWork REST/SSE server and a scripted local model. The endpoint redirect exists only in the child test shim; production has no alternate API setting. Local results are recorded separately in [publication readiness](docs/PUBLICATION.md).

```sh
python scripts/verify.py --hermes /path/to/hermes --venv /path/to/test-venv --output /tmp/sowork-results
python scripts/export_public.py --output /tmp/sowork-public-source
```

The export contains only public source, synthetic tests, examples, license and CI; no pilot records, production IDs or deployment scripts. Review it and publish as a fresh repository rather than pushing the private pilot history. A catalog submission then pins that repository's exact commit. MIT license; maintained by Bruno Maya / Jumper Studio. This is a community integration, not an official SoWork product.
