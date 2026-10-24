# Character appearance workflow

Load the bundled skill with `skill_view("sowork-platform:appearance")` on Hermes
versions with plugin-skill support. It turns a portrait or short interview into
complete v5 JSON for the app editor. Older runtimes can read
[the same workflow](../skills/appearance/SKILL.md) directly and use the CLI.
The plugin does not add a model tool or rebuild the conversation prompt.

## Credential-free preparation

Run from the plugin directory with its prepared Hermes Python:

```sh
python soworkctl.py --json appearance catalog --output /path/to/work/catalog
python soworkctl.py --json appearance validate --catalog /path/to/work/catalog --config /path/to/work/character.json
python soworkctl.py --json appearance previews --catalog /path/to/work/catalog --config /path/to/work/character.json --output /path/to/work/previews
```

The equivalent `hermes sowork --json appearance ...` commands are registered too.
Use the direct entry when credential-free startup matters: the general Hermes CLI
may eagerly hydrate its profile secrets before dispatching any plugin command.

Only the catalog and preview commands access the network. They make anonymous
HTTPS GETs to fixed paths under `https://app.sowork.com/characters/v5/`, with no
redirects, environment proxies or authentication. Downloads have byte limits;
outputs require a new directory and never replace existing files. The snapshot
manifest records source URLs, retrieval time and SHA-256 hashes. Validation works
offline against those recorded bytes and needs no configured profile.

A valid catalog reference does not establish a visual match. **Open the downloaded
PNGs and inspect garment cuts**, including sleeves and trouser length. Palette
variants can reference base-color art transformed by the renderer; their selected
IDs remain unchanged in JSON, while raw previews may show the base color.
The final assembled appearance should be checked in SoWork. These helpers do not
render a complete avatar or perform app/NPC writes.

## Presets

- [Sabrina](../skills/appearance/assets/sabrina.json): blonde hair, explicitly
  corrected green eyes, black long-sleeve buttoned shirt, full-length black jeans
  and black shoes. The shirt approximates the reference shirt-plus-padded-vest
  outfit: the catalog's `vest` is sleeveless and occupies the same shirt slot.
- [Newton](../skills/appearance/assets/newton.json): short dark hair, brown eyes,
  rectangular black glasses, black sweatshirt, full-length black jeans and black
  shoes. The sweatshirt approximates the reference turtleneck.

These JSONs contain no credentials, app/installation IDs or private portrait
files. Treat them as examples to review, not universal person defaults. Validate
against a fresh catalog before reuse. Catalog/access metadata cannot prove a
particular account's item entitlement.

## App setup and evaluation

[App profile setup](../skills/appearance/references/app-profile.md) covers photo
formats, description, ownership, NPC identity, permissions, events and webhook.
The [baseline](../skills/appearance/evals/BASELINE.md) records observed mistakes;
[public cases](../skills/appearance/evals/evals.json) exercise their corrections.
The skill is a candidate with `review-status: yellow`. Deterministic CLI tests,
source/export registration checks and independent review are separate from live
model efficacy, unseen-portrait holdouts and authenticated write verification.
