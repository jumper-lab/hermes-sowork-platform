---
name: appearance
description: >
  Use when a user wants a SoWork bot's appearance JSON from a portrait or a
  character interview, or help with app profile setup. Inspect real garment
  previews and validate complete v5 character JSON.
metadata:
  version: "1.0.0"
  review-status: "yellow"
  review-note: "Candidate based on observed setup regressions; live app writes and external holdout unverified."
---

# SoWork appearance — inspect the garment, then deliver the JSON

Produce complete SoWork v5 character JSON, with a plain-language description of
what the selected pieces actually show. This workflow prepares local files;
app-definition and live NPC changes require a separate, explicitly requested action.

## Start with the reference

1. Use the supplied portrait/reference sheet or ask a short appearance interview.
   Record body type, skin, eyes, hair, outfit and accessories. Human corrections
   take precedence over a visual guess: retain them in later edits.
   Ask about genuinely undecided traits; a reference already answers most questions.
2. Resolve this skill's plugin root: `SKILL.md` is under `skills/appearance/`.
   Use the installed Hermes Python with the absolute path to `soworkctl.py` there.
   The direct entry avoids Hermes' eager secret bootstrap. The helpers need Python
   3.11+ and the plugin's `httpx` dependency, not an app token or a configured profile.
3. Choose a persistent working directory outside profile config/state and the
   installed plugin tree. Use new subdirectories for downloads. If the user asks
   about a photo, description, permissions, events or webhook, read
   [app profile setup](references/app-profile.md) for that branch.

## Consult and inspect

4. From the plugin root, fetch the public catalog snapshot:

   ```sh
   python soworkctl.py --json appearance catalog --output /path/to/work/catalog
   ```

   Inspect `customizer_config.json` for bodies, slots, item variants and access
   flags; `character_art_database.json` supplies required slots and real base art.
   These are official frontend assets, not a stable REST catalog API. A failed
   fetch requires a prior validated snapshot or a config exported by the human.
   Without either, explain the missing catalog and continue with manual selection.
5. Draft a complete config. For an existing character, preserve unrelated slots.
   For `characterA`, required face/body slots can be hidden in the customizer;
   preserve valid defaults and coordinate the mouth's tone with the body.
   [Sabrina](assets/sabrina.json) and [Newton](assets/newton.json) are corrected
   example presets, not defaults for every person. Use only actual compatible
   item/variant pairs. Report Premium/referral/access flags as entitlement checks,
   not proof that the current account can use them.
6. Validate, then fetch the selected pieces' actual previews:

   ```sh
   python soworkctl.py --json appearance validate --catalog /path/to/work/catalog --config /path/to/work/character.json
   python soworkctl.py --json appearance previews --catalog /path/to/work/catalog --config /path/to/work/character.json --output /path/to/work/previews
   ```

   Validation resolves transformed colors through `baseVariation` for art lookup,
   while retaining the chosen color in the output. Missing required slots,
   unknown variants, disabled items and mismatched body/slot IDs must be corrected.
7. **Open and visually inspect the preview images before final delivery.**
   Check trouser length, sleeves, hairstyle and accessories. If image viewing is
   unavailable, state that visual review is incomplete and ask the human to check
   the SoWork preview. A downloaded PNG or `catalog_valid` result alone is not
   visual verification. A raw preview can show base colors; selected colors are
   transforms applied by SoWork and the final composite still needs its own preview.

   Observed traps: `standardPants` is short; `vest` has bare arms. Their names are
   insufficient evidence of long trousers or a shirt layered under a padded vest.
   The corrected presets use full-length `standardJeans`; Sabrina's `tuxedoShirt`
   has long sleeves. Two choices assigned to the same `shirt` slot replace each
   other. Describe a one-piece approximation rather than promising both layers.

## Deliver and verify the handoff

8. Save the complete JSON to a persistent user-accessible file, include it in the
   reply for pasting into SoWork's character field, and summarize the actual
   selections and visual approximations. Keep explicit corrections, such as green
   eyes, intact. Distinguish catalog validation, inspected piece previews and the
   human's final in-app preview; none proves that a live app was changed.
9. For later edits, change only the requested traits, then repeat validation and
   inspect affected previews. For direct changes, first follow the identity and
   authorization distinctions in [app profile setup](references/app-profile.md).
   The helpers have no authenticated write operation and never restart a gateway.

Done means: the complete v5 JSON exists, required slots/variants resolve, all
requested garment cuts were visually checked or explicitly left for human review,
corrections are preserved, and any unperformed live action is accurately reported.

Gateway reception failures belong to transport diagnostics. Generating a new
photorealistic portrait belongs to an image workflow, not this character catalog.
