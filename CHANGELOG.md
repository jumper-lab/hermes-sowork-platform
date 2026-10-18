# Changelog

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
