# Security

Access is restricted to one configured human and one DM. Groups, additional humans, wildcard bindings, unverified thread destinations and arbitrary reaction targets are refused. Emoji never represents approval.

Credentials must arrive through Hermes' profile secret interface. Do not paste credentials into issues, logs or fixtures. For a suspected leak, revoke/rotate the affected credential through its owner and remove it from any public artifacts. Report an issue without secret values or private conversation content; a private reporting route can be configured when the public repository is created.

Operational risks and data storage are disclosed in README.md. The plugin's dependency policy permits the oldest validated httpx minor release through an upper bound, while Hermes' own constraints remain authoritative. CI installs through Hermes' package manager; do not modify a live Hermes environment to validate this plugin.
