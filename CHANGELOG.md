# Changelog

## 0.13.0 — explicit conversation routes and channel prompts

- Add opt-in routes for multiple 1:1 DMs, group DMs and public/private channels, each with explicit authorized users. Keep the existing single-human/DM format and its access unchanged. Validate app membership and the exact subscription/scopes without joining channels, modifying grants or enrolling anyone.
- Forward configured channel prompts through Hermes' native event interface. Threads inherit their conversation's context; native per-user/channel/thread sessions, approvals and cancellation remain authoritative. No Flow integration, tool-permission changes or core patches.
- Bind reactive text/files and durable retries to the original channel, thread and sender; scope thread lookup and private previews to their actual conversation/requester. Restrict cron and standalone sends to configured destinations and preserve original primary-DM delivery keys.
- Extend read-only diagnostics and add native gateway process tests for concurrent channels, multiple users, prompt/history isolation, inherited thread prompts, private previews, cancellation and restart deduplication. Deployment and access expansion remain separate operator actions.

## 0.12.0 — interactive profile onboarding

- Add `hermes sowork setup`: validate the active profile, use its native credentials or map missing 1Password field references, discover the app/workspace and pair one human through a fresh code in a private DM. The code nominates a sender; explicit confirmation and API membership validation authorize the binding.
- With explicit consent, create a dedicated inbox-only subscription while preserving existing subscriptions. Persist setup progress before creation, never blindly retry an ambiguous POST, and resume an owned subscription after cancellation or timeout. Explicit subscription adoption is available for reviewed recovery.
- Merge only this profile's SoWork connection, display overrides and three native secret references, preserving other configuration and comments with checksum review and private backups. Do not change global streaming, model, memory, secrets files or other profiles. Activation uses Hermes' native gateway lifecycle; setup itself starts no receiver/model and sends no chat messages.
- Test native 1Password hydration with a synthetic authenticated executable, real loopback HTTP, pairing origin checks, restart-free configuration, cancellation/recovery, active-consumer locks and concurrent config protection. Exercise the full native CLI through a disposable terminal.
- Use dependency-only metadata and directory discovery so separate profiles can retain different releases without colliding in the shared Python workspace. Let setup collect native secret references instead of asking for raw credentials during installation.
- Verify diagnostic snapshot ownership with Hermes' native process identity resolver, including native snapshots without a home stamp. Reject stale, foreign or unverified processes instead of trusting a live PID alone.
- Stage native config serialization in a private copy, recheck the live checksum after preparation and publish under Hermes' shared config locks. Preserve intervening edits by refusing the write, and test serialization with a real native plugin-settings writer in another process. Uncoordinated writers that bypass the lock remain outside the transactional guarantee.
- Pin the exact home and resolve its native profile in the activation instruction; never treat the descriptive `custom` label as a named profile. Cover default, named and custom homes, spaces and inherited profile-name pins.

## 0.11.3 — preserve model identifiers in native commands

- Decode SoWork's Markdown escapes around square brackets in `/model` commands before native dispatch. An identifier such as `model[1m]` previously persisted with literal backslashes and could fail with an upstream model-not-found error. Ordinary messages and the original event payload remain unchanged.

## 0.11.2 — recover temporary event-route failures

- A generic HTTP 404 from polling or SSE no longer permanently stops a valid receiver. Recheck the original installation, scopes, subscription ownership and enabled DM membership before retrying with bounded backoff and the unchanged cursor. Deleted subscriptions, changed bindings and explicit API errors still stop reception.
- Identify polling versus streaming in safe errors and receiver warnings; retain native fatal status in offline diagnostics. Tokens, response bodies and cursor values are excluded from those errors.
- Exercise temporary polling/SSE failures and duplicate replay through a real gateway process; cover revoked installations, changed owners, deleted subscriptions and token errors separately.

## 0.11.1 — admit the existing native runtime

- Admit Hermes 0.21.5 development installs that satisfy the existing gateway contracts, with the exact older runtime added to Linux/macOS CI. The 0.22.2 metadata floor previously blocked native installation despite passing the runtime contract check.
- Keep the pre-credential contract checks and native installer. No host version override, Hermes core update or profile configuration change is needed.

## 0.11.0 — delivery compatibility and operational robustness

- Route cron identity through the public PluginContext API; isolate and check the legacy queue/tool correlation seams before secrets or network. Keep existing queue delivery working without patching Hermes.
- Full process tests load the installed plugin in a disposable Hermes gateway and exercise REST/SSE, one-human ACL, thread routing, a real file tool, ambiguous acceptance retries, reconnect, process restart and stop during preview.
- Keep adapter bookkeeping out of native admission metadata; the full gateway test exposed and reproduces the previous rejection.
- Add indexed queue reads and bounded hourly cleanup of completed payloads older than 30 days, with durable hashed replay barriers and preservation of unresolved work and thread routes.
- Production profiles, secrets and gateways are not migrated or restarted by this release.

## 0.10.0 — publication candidate

- Profile-bound credentials use Hermes' public secret interface for both transport and administrative connection checks. No direct credential-file or 1Password bootstrap access.
- Removed private pilot launchers and self-replacing maintenance/update commands from the public runtime. Updates belong to Hermes' installer/updater.
- Declared supported Hermes/Python/platform metadata and added pre-credential runtime checks.
- Generic participant labels, English prototype UI, explicit optional preview capability and truthful fallback documentation.
- Added public source export, synthetic security/compatibility regressions, clean-install checks and CI.
- Private DM scope and exactly one authorized human remain mandatory. Production installations are not migrated automatically.

## 0.9.0 — private deployment baseline

Private DM, attachments, threads, proactive delivery, previews and the bounded reaction experiment were exercised in a controlled installation. Publication candidate changes are validated separately; those earlier user tests do not constitute a new 0.10.0 production activation.
