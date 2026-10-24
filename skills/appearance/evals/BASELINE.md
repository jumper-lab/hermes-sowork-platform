# Observed appearance baseline

Observed in a human-guided setup on 2026-10-21, before this skill existed.
These are development regressions, not a clean-room benchmark or holdout.

| Case | Initial output | Observed failure | Correction |
| --- | --- | --- | --- |
| Long black trousers | standardPants described as trousers | Human saw shorts; official preview confirmed a short cut. | Inspect previews; select a visibly full-length cut. |
| Shirt under a padded vest | vest described as the reference outfit | Human saw a sleeveless top; preview confirmed bare arms. | Inspect sleeves and disclose the one-slot approximation. |
| Eye correction | Portrait interpreted as blue eyes | Human specified green. | Preserve green through clothing edits. |
| Palette variation | Literal art lookup | Valid transformed colors have only base art. | Resolve baseVariation for art, retaining the selected color in JSON. |

The human-guided corrections establish these gaps, not performance on unseen
portraits, other models, account entitlements or authenticated writes.
