# Changelog

## 0.10.0 — publication candidate

- Profile-bound credentials use Hermes' public secret interface for both transport and administrative connection checks. No direct credential-file or 1Password bootstrap access.
- Removed private pilot launchers and self-replacing maintenance/update commands from the public runtime. Updates belong to Hermes' installer/updater.
- Declared supported Hermes/Python/platform metadata and added pre-credential runtime checks.
- Generic participant labels, English prototype UI, explicit optional preview capability and truthful fallback documentation.
- Added public source export, synthetic security/compatibility regressions, clean-install checks and CI.
- Private DM scope and exactly one authorized human remain mandatory. Production installations are not migrated automatically.

## 0.9.0 — private deployment baseline

Private DM, attachments, threads, proactive delivery, previews and the bounded reaction experiment were exercised in a controlled installation. Publication candidate changes are validated separately; those earlier user tests do not constitute a new 0.10.0 production activation.
