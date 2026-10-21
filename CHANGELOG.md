# Changelog

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
