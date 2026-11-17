# Hermes 0.23.2 preparation

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

The VPS chat owns the core/Kanban port. This plugin branch owns SoWork
compatibility, contract tests and isolated shared-gateway validation. Reading
the peer chat is coordination evidence, not authorization to deploy its work.

## VPS artifacts required before deployment

Keep code-only artifacts under version control where appropriate. Keep live
configuration and state inventories private. Never copy secret values or chat
databases into this repository.

| Artifact | Required evidence and purpose |
|---|---|
| Live core source | Exact HEAD, remotes and `git diff --binary` for all six modified files. The peer captured `work/hermes-update-study/live-customizations.patch`, SHA-256 `0dc7a7b635833a639e392e17097af89e9d64aa475176080cbfd14bf092dadf8e`; recheck it before deployment. |
| Kanban customization | Preserve worker-model/provider defaults and card-pin precedence. Upstream streaming support already supersedes two local streaming changes; do not blindly reapply the whole patch. Core port and evidence belong to the VPS chat. |
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

## Validation boundary

The local suite uses synthetic identities, separate disposable profile homes,
real native imports and loopback services. Required coverage includes two
SoWork installations in one gateway process, profile-bound credentials and
authorization, independent cursors/outboxes, reconnect/restart and duplicate
event/delivery handling. Record exact tested commits, test totals and remaining
VPS checks with the final evidence. No production deployment is authorized.
