# Hermes 0.23.2 preparation

## Current decision: official Hermes only

The user decided to retire the Jumper core fork after the local validation.
The deployment target is the **unmodified official Hermes v0.23.2**, commit
`ae2a81ccc00e9b7c06301523e5bb09608397149c`. The plugin changes remain valid:
the official source independently passed 402 full-suite tests and the 34-test
setup follow-up, including two profiles in a shared gateway. CI already uses
official upstream commits; no workflow or runtime-code change is required by
this decision.

The imported Kanban branch `codex/jumper-hermes-0.23.2` / `ea55ee2` is retained
only as historical review evidence. It is **not a deployment dependency or a
migration gate**. Do not port or reapply its patch. The historical fork-specific
voice controls and session permission-name persistence are likewise not work
items to restore in core. Preserve their configuration inventory for comparison;
use native settings where equivalent and record any behavior differences before
cutover. Darla's per-job cron model override is independent of that Kanban patch.

The **Checar a VPS Ops** chat independently confirmed that the SoWork solution
works on official 0.23.2. Its subsequent DirectSDK investigation is a separate
plugin/update track; authenticated operation and profile isolation still need
VPS validation. State backups, credentials, systemd, Caddy/webhooks and the
shared-gateway cutover remain operational work even with an unmodified core.
No production action is authorized or performed by this documentation update.

## Reconciled baseline (2026-11-17)

The local review and the **Checar a VPS Ops** chat independently reproduced the
removed `tools.browser_extension_router` import on Hermes
`ae2a81ccc00e9b7c06301523e5bb09608397149c` (v0.23.2). The local review also
identified the moved standalone caption helpers. These are plugin changes;
they do not require changing profiles, subscriptions or message journals.

The versions inspected are different evidence, not conflicting conclusions:
the VPS reports installed SoWork 0.13.0 and Hermes `79af3f6` with six locally
modified files; this workspace tested SoWork 0.14.0 and published 0.15.0
(`a956b5078208f59b3bb012d0184f33d5767e755c`). The GitHub Jumper fork's
`jumper/production` branch (`be375ce1136`) is older than the live VPS source.
Do not use that branch alone as the production preservation baseline.

The VPS study produced a core/Kanban port. Its historical reviewed candidate is
`ea55ee288ed2159e27a5f21926139b643cfde6af`, branch
`codex/jumper-hermes-0.23.2`, based on the release above. The code-only bundle
was imported locally without changing either production or this workspace's
core checkout; SHA-256
`6347414559714030ee8bf26e1b187875584520972e108c7a5b4531afad74f60c`.
The peer reports 207 tests and 21 repository checks passing; those results do
not replace the combined plugin/core test evidence recorded here.

This plugin branch owns SoWork
compatibility, contract tests and isolated shared-gateway validation. Reading
the peer chat is coordination evidence, not authorization to deploy its work.

## VPS artifacts required before deployment

Keep code-only artifacts under version control where appropriate. Keep live
configuration and state inventories private. Never copy secret values or chat
databases into this repository.

| Artifact | Required evidence and purpose |
|---|---|
| Live core source | Exact HEAD, remotes and `git diff --binary` for all six modified files. The peer captured `work/hermes-update-study/live-customizations.patch`, SHA-256 `0dc7a7b635833a639e392e17097af89e9d64aa475176080cbfd14bf092dadf8e`; recheck it before deployment. |
| Historical Kanban customization | Archive worker-model/provider settings and card-pin precedence for comparison. The custom default is not a deployment requirement after the official-core decision. Upstream streaming already supersedes two local changes; do not reapply the patch. |
| Installed plugins | Exact source revisions, local diffs and install metadata for each profile, including SoWork and the private DirectSDK plugin. A source export of installed SoWork 0.13 exists in the peer study. |
| DirectSDK | Code, manifest, synthetic tests and launch configuration shape. Basic import worked in the VPS study; two tests rely on old core APIs. Authenticated execution and cancellation are not yet certified. |
| Profile configuration | Sanitized model/worker routes, platform settings, allowlists, channel prompts, 1Password mapping field names, voice command settings, and configured paths. Preserve actual values on the VPS; no `.env`, token, auth or bootstrap copies here. |
| Services and launchers | User units/drop-ins, PATH, environment-file **paths**, `HERMES_BIN`, DirectSDK command/config-directory settings, sandbox HOME and profile binding. A shared service cannot inherit one profile's secrets process-wide. |
| Proxy/integrations | Caddy routes, webhook ports and consumers, GitHub/Recall callbacks, and custom dashboard launch/proxy. New ingress uses `/p/<profile>/…`; verify signatures and the real callback path on the VPS. |
| State/rollback | On-VPS backups plus schema/count/hash inventories for sessions, cron delivery queues, SoWork inbox/cursor/outbox/uploads and pending/uncertain work. Code rollback must keep the latest journal, not restore an old cursor. |
| Voice | FishAudio `command` provider settings and owned scripts. This mechanism still exists upstream; synthetic transport tests do not certify the real voice service. |

## Operational blockers outside this plugin

- Global `gateway.auto_multiplex_migration` is explicitly disabled. Migration
  must be reviewed and invoked deliberately; no local test may change it.
- `gateway migrate --backends` in the target version is a report, not a
  replacement for migrating the custom dashboard service and its proxy.
- Fifteen live gateways, custom drop-ins and old webhook-port consumers need
  an ordered cutover and return plan. Local macOS tests cannot certify systemd,
  Caddy, real OAuth, 1Password service accounts or callback delivery.
- Existing OAuth failures and the failing Operador cron are independent
  operational findings. Updating the core does not prove those are resolved.
- The historical fork also had ElevenLabs voice controls and session-scoped
  permission-name persistence. They are not ported by the Kanban candidate.
  Native per-profile secret **values** are not proof of equivalent per-session
  permission **names**. Preserve this distinction when approving the migration.
- DirectSDK's two failing study tests expect the original provider file path
  (the new loader copies providers into scoped namespaces) and the removed
  `ContextCompressor._truncate_tool_call_args_at` helper. Those are test-contract
  failures, not evidence that authenticated generation or cancellation works.
- The peer's follow-up confirmed that Darla's BestBuy cron has its own native
  job model/provider override. It does not depend on `kanban.worker_model`.
  Whether active Kanban cards still need the custom default is a separate
  inventory question. The official-core decision above supersedes the earlier
  candidate-preservation plan; no live job is migrated by this preparation.

## Validation boundary

The local suite uses synthetic identities, separate disposable profile homes,
real native imports and loopback services. Required coverage includes two
SoWork installations in one gateway process, profile-bound credentials and
authorization, independent cursors/outboxes, reconnect/restart and duplicate
event/delivery handling. Record exact tested commits, test totals and remaining
VPS checks with the final evidence. No production deployment is authorized.

The shared-gateway process tests use two synthetic installations with distinct
OAuth credentials and the same inbound message ID/channel ID. They verify
independent conversation history, journal ownership, cursor advancement and
outbox keys, reject foreign envelopes, and kill only their disposable child
after server acceptance but before receipt persistence. Restart must reuse the
saved receipt key without calling the model again or duplicating delivery.
The native CLI status is also checked for both profiles: a served profile owns
no status file, so the plugin projects only that profile's state from the
live host after native ownership verification.

## Local implementation and evidence (2026-11-17)

Two local plugin branches are stacked on public main `a956b507`:

| Branch/commit | Review scope |
|---|---|
| `codex/sowork-hermes-0232`: `f788146`, `7b7f1c6` | Reconciled preservation inventory; removed-browser initialization fix; native outbound caption compatibility; startup contract checks; version 0.16.0 and immutable 0.23.2 CI target. |
| `codex/sowork-shared-gateway-0232`: `b4105f1`, `829b8d1`, `28c69ca`, `7c0133b` | Verified served-profile status; real two-profile/restart tests; inert offline status without host records; the same pre-credential contract checks for standalone proactive delivery. |
| `codex/sowork-shared-gateway-0232`: `ab2d364` | Test-only correction: publish the cross-process config-lock verdict by atomic rename. The old signal file could exist while its JSON was still empty. No production config-lock behavior changes. |
| Core `codex/jumper-hermes-0.23.2`: `ea55ee2` | Imported peer-authored Kanban candidate; four files relative to upstream, no duplicate local core implementation. |

The complete plugin suite at runtime-code commit `7c0133b` ran through native
`scripts/run_tests.sh`, four workers, file retries **zero**. Each source revision
had its own native PM environment; reusing a different source's editable
environment can silently mix core modules and is not valid evidence.

| Hermes source | Local platform | Result |
|---|---|---|
| Official v0.23.2 `ae2a81ccc00e9b7c06301523e5bb09608397149c` | macOS ARM64, Python 3.14.7 | 402 passed, 0 failed |
| Combined Kanban candidate `ea55ee288ed2159e27a5f21926139b643cfde6af` | macOS ARM64, Python 3.14.7 | 402 passed, 0 failed |
| Older VPS base `79af3f6cea8067284a7ea5725078578b3f790adb` | macOS ARM64, Python 3.14.7 | 402 passed, 0 failed |

After the test-signal-only commit `ab2d364`, the entire setup test file was run
again through the native runner against all three sources: **34 passed per
source, zero failures and zero retries**. No runtime code changed after the
402-test runs. The final documentation updates do not change executable code.

All three include the two real shared-gateway process tests; none substitutes a
mocked gateway for profile routing, credential lookup, durable admission or
crash recovery. Existing authorization, proactive identity, thread, media,
preview and setup tests run in the same suite. No actual provider generation,
1Password account or SoWork workspace is used.

The final public export also passed native `hermes plugins validate --json`.
Dependency installation was validated separately with `--install-deps` in a
disposable home. The validator reports **no core override** and retains its
warning about private core imports. A platform plugin runs inside the gateway;
the validator's `host_ready: false` describes process isolation, not a failed
registration. Internal contracts remain a maintenance risk across later Hermes
releases and are checked before both adapter startup and standalone delivery.

Reproduce the full local suite from a clean checkout and matching native PM
environment:

```sh
python scripts/export_public.py --output /tmp/sowork-public
python /tmp/sowork-public/scripts/verify.py --hermes /path/to/hermes-source --venv /path/to/matching-native-env --output /tmp/sowork-results
```

The CI matrix now includes the immutable 0.23.2 commit on both Linux and macOS,
for eight jobs in total. **GitHub CI has not been dispatched for these local
branches.** The peer's Linux core results are separate evidence, not a Linux
plugin run. No branch was pushed, production gateway restarted, subscription changed,
allowlist edited or production migration performed by this preparation.

Before any production cutover, close the VPS artifact/rollback inventory above,
validate the private DirectSDK path with real authentication and cancellation,
confirm voice scripts, and review the fifteen-gateway service/proxy migration.
The official-core decision removes the Kanban port from the rollout plan.
Historical candidate test results are retained as evidence; they do not resolve
unrelated OAuth/cron failures. Production access and all other channels remain
untouched.
