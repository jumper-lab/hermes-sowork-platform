# Compatibility

The publication candidate targets Hermes 0.22.2 and newer with the required native gateway contracts. The version floor is metadata; `check-runtime` also checks actual contracts before adapter construction, credentials or network. An old 0.20.5 checkout is intentionally refused rather than partially starting and later failing during delivery.

Validation targets are immutable public commits:

| Target | Commit |
|---|---|
| Hermes v0.22.2 | `d923a5eb487a17c0a68801bba9ef0f56c81040a8` |
| Public main inspected on 2026-10-18 | `e868508f5d341bc8dcaa827eb1d61c45ad14ddca` |

CI uses those commits on Linux/macOS with Python 3.14. Local validation uses the prepared Hermes Python 3.11 environment and disposable profiles; exact results belong in PUBLICATION.md. A configured matrix is not evidence that GitHub CI has already run.

The plugin preserves Hermes' own authorization/session/delivery contracts. The gateway/scheduler internals it currently integrates with are covered by real-import tests and disclosed for review; compatibility with every future Hermes revision is not guaranteed. A catalog validator may warn about undocumented imports. Updating the minimum or pin requires reviewing those contracts again.

## Streaming

The current public stable/main cores reject streaming for external-process providers, even when a provider declares a streaming capability. The private operational deployment used separately reviewed core/provider patches; those patches are not bundled, applied or required for basic operation of this public plugin. `check-preview` reports the real native selector outcome for each persisted route. For a blocked route, activity status and the final response remain available; token preview needs a compatible core/provider. HTTP/OAuth routes with native streaming support have independent tests. Do not advertise universal Opus/DirectSDK preview compatibility.

## Supported access and hosts

One explicit human and one private DM; optional threads under that DM. No wildcard or multi-user mode. POSIX locks support Linux/macOS. Python 3.11–3.14 is admitted by package metadata; only tested combinations may be claimed as verified. No test requires real tokens or a running production gateway.
