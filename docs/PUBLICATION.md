# Publication readiness

The independent source is published at [jumper-lab/hermes-sowork-platform](https://github.com/jumper-lab/hermes-sowork-platform). [GitHub CI](https://github.com/jumper-lab/hermes-sowork-platform/actions/workflows/ci.yml) checks the published commits. This is not yet an accepted catalog entry or a production upgrade.

The public source export intentionally excludes private pilot history, deployment controllers, operator evidence, production identifiers, credentials, self-updaters and runtime patchers. Preserve private operational records separately; publish the fresh export rather than pushing the pilot Git history.

Implemented admission changes:

- Active-profile credentials use the public Hermes secret route; no direct credential-store bootstrap. 1Password belongs to Hermes configuration.
- No plugin code replacement/update/rollback commands. Hermes' native updater owns changes to a reviewed commit.
- Version/platform/dependency metadata, runtime contract check and fixed-scope example.
- MIT license, operational disclosure, installation/migration documentation, synthetic tests and pinned CI.
- Optional preview reflects native core/provider support; no automatic core/provider patch.

## 0.15 appearance workflow validation

The full native suite passed 394 tests across 16 files with Python 3.14.7 against
pinned Hermes revision `e868508f5d341bc8dcaa827eb1d61c45ad14ddca`. Tests include
credential-free startup, offline catalog validation, bounded public downloads,
optional native skill registration and clean-export resource preservation.
Native admission of the clean export passed all 14 checks, including security
scan `safe` and no core override. The previously disclosed private-import
warning remains; the appearance helper adds no Hermes imports.

On 2026-10-23, the helper fetched the two official public frontend catalogs and
validated both bundled presets. The selected garment, hairstyle and glasses
PNGs were opened for visual review. This verifies the pieces and their cuts,
not the final assembled in-app appearance or account entitlement. No credentials,
app/NPC writes, production profiles or gateway commands were used.

The portable skill checker passed 66 checks; the independent Jumper audit passed
68 static checks, a new optional-slot edit case, four negative checks and an
offline public-image replay. The independent audit found no concrete P0–P2 code
bug. Its skill-efficacy score is capped at 6.0/10 with low confidence because a
paired behavioral baseline, protected model holdout and coexistence trials have
not been run. The bundled skill therefore remains a `yellow` candidate; these
results do not certify model generalization or authenticated administration.
GitHub CI separately verifies the PR commit on Linux/macOS and all three pinned
Hermes revisions.

## 0.11 robustness validation

Python 3.14.7, in a fresh Hermes PM environment with dev/test groups and the native
messaging extra: 230 tests passed against each pinned Hermes revision. This
includes a separate `hermes gateway run` child, synthetic active-profile secrets,
a loopback SoWork REST/SSE server and a local scripted model. The tests exercise
single-human admission, same-thread delivery, HTTP idempotency after ambiguous
acceptance, reconnect cursor, a real file-tool result, gateway restart without
repeating the tool turn, and `/stop` retiring the thread preview. SQLite tests
cover old-schema migration, indexed reads, hourly bounded cleanup, transactional
rollback, unresolved record preservation and replay prevention after retirement.

The clean export passed native catalog admission with dependency installation:
14 checks, security scan safe, no core override. The remaining private-import
warning is explicit; the checked queue fallback still needs the proposed generic
upstream context propagation before it can be removed. See
[proposal](HERMES-API-PROPOSAL.md). No production profile/gateway was accessed.
The PR's exact-commit GitHub CI is the independent Linux/macOS verification.

## 0.10 local validation results

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
4. Prepare a community catalog entry (`category: platform`, `platforms: [linux, macos]`, `requires_hermes: ">=0.21.5"`) with the real repository URL and a full 40-character commit SHA. Declare the three credential names and no model tools/hooks/middleware.
5. Disclose network/storage/background behavior and any validator cautions/private-import warnings. A passing local validator does not waive maintainer review.

Missing public API contracts (native voice-note presentation, buttons, permanent edit/delete and reaction events) remain documented limitations, not hidden claims. The bounded reaction test is experimental, opt-in and never approves an action.
