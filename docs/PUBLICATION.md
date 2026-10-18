# Publication readiness

The independent source is published at [jumper-lab/hermes-sowork-platform](https://github.com/jumper-lab/hermes-sowork-platform). [GitHub CI](https://github.com/jumper-lab/hermes-sowork-platform/actions/workflows/ci.yml) checks the published commits. This is not yet an accepted catalog entry or a production upgrade.

The public source export intentionally excludes private pilot history, deployment controllers, operator evidence, production identifiers, credentials, self-updaters and runtime patchers. Preserve private operational records separately; publish the fresh export rather than pushing the pilot Git history.

Implemented admission changes:

- Active-profile credentials use the public Hermes secret route; no direct credential-store bootstrap. 1Password belongs to Hermes configuration.
- No plugin code replacement/update/rollback commands. Hermes' native updater owns changes to a reviewed commit.
- Version/platform/dependency metadata, runtime contract check and fixed-scope example.
- MIT license, operational disclosure, installation/migration documentation, synthetic tests and pinned CI.
- Optional preview reflects native core/provider support; no automatic core/provider patch.

## Local validation results

Local Mac validation passed 202 tests against each pinned Hermes target with
Python 3.11.15. A new Python 3.14.7 environment built through Hermes PM also passed all
202 tests on the current pinned target. The final preparation report records
the clean-export reproduction separately. The exported tree was then reproduced
with its own verification script: 202 tests passed with Python 3.14.7. Wheel
entry-point discovery registered the platform and CLI in an empty profile; the
source distribution contains no private pilot payload or self-updater. An old 0.20.5 checkout was rejected by `check-runtime` before resolving
credentials or starting a receiver.

Native catalog validation, including `--install-deps` through Hermes PM, passed
all 14 checks: security scan `safe`, no runtime core overrides, declarations
matching registrations. [Validator report](VALIDATION.json). There is one
warning listing undocumented Hermes imports used by adapter/scheduler/preview
integration. This is disclosed review debt, not a promise that future versions
are compatible. The validator also correctly classifies the platform as running
inside the gateway process, not an isolated plugin host.

GitHub CI results are linked above and remain separate from local evidence. Production acceptance of the older
private deployment is historical evidence only and is not presented as a new
candidate activation. No SSH, credential read, gateway command, allowlist change
or production migration was performed for this preparation.

## Submission sequence

1. Review the fresh export, its manifest and validation report.
2. The independent repository has been created and the reviewed source published. The preparation scripts themselves do not publish or deploy.
3. Run GitHub CI and native `hermes plugins validate --install-deps` against that repository's exact commit.
4. Prepare a community catalog entry (`category: platform`, `platforms: [linux, macos]`, `requires_hermes: ">=0.22.2"`) with the real repository URL and a full 40-character commit SHA. Declare the three credential names and no model tools/hooks/middleware.
5. Disclose network/storage/background behavior and any validator cautions/private-import warnings. A passing local validator does not waive maintainer review.

Missing public API contracts (native voice-note presentation, buttons, permanent edit/delete and reaction events) remain documented limitations, not hidden claims. The bounded reaction test is experimental, opt-in and never approves an action.
