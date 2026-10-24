"""Credential-free character preparation using SoWork's public frontend catalogs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

import httpx

PUBLIC_ROOT = "https://app.sowork.com/characters/v5/"
CATALOG_FILES = ("customizer_config.json", "character_art_database.json")
MAX_JSON_BYTES = 12 * 1024 * 1024
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}\Z")


class AppearanceError(ValueError):
    pass


def _object(value, label):
    if not isinstance(value, dict):
        raise AppearanceError("invalid_" + label)
    return value


def _component(value):
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value):
        raise AppearanceError("invalid_character_identifier")
    return value


def _decode(data):
    try:
        return _object(json.loads(data), "json_object")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AppearanceError("invalid_json") from error


def _read_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AppearanceError("expected_regular_json_file")
    with path.open("rb") as file:
        data = file.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise AppearanceError("json_too_large")
    return data


def read_json(path):
    return _decode(_read_bytes(path))


def _get(client, url, maximum):
    # Fixed public host, no profile, proxy credentials, OAuth or redirects.
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise AppearanceError("public_download_http_" + str(response.status_code))
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result) > maximum:
                    raise AppearanceError("public_download_too_large")
            return bytes(result)
    except httpx.HTTPError as error:
        raise AppearanceError("public_download_failed") from error


def _client(transport=None):
    return httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(30, connect=10))


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _publish(output, files):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise AppearanceError("output_must_be_new_directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()  # Exclusive creation also protects against a raced destination.
    except FileExistsError as error:
        raise AppearanceError("output_must_be_new_directory") from error
    for name, data in files.items():
        with (output / name).open("xb") as file:
            file.write(data)
    return output


class Catalog:
    def __init__(self, customizer, art):
        self.customizer = _object(customizer, "customizer")
        self.art = _object(art, "art")
        _object(self.customizer.get("bodyTypes"), "customizer_bodies")
        _object(self.art.get("bodyTypes"), "art_bodies")
        _object(self.art.get("assets"), "art_assets")

    @classmethod
    def load(cls, directory):
        directory = Path(directory)
        if directory.is_symlink():
            raise AppearanceError("expected_regular_catalog_directory")
        manifest = read_json(directory / "manifest.json")
        if manifest.get("schema") != "sowork-appearance-catalog/v1":
            raise AppearanceError("unsupported_catalog_manifest")
        sources = _object(manifest.get("sources"), "catalog_sources")
        values = {}
        for name in CATALOG_FILES:
            metadata = _object(sources.get(name), "catalog_source")
            data = _read_bytes(directory / name)
            if (metadata.get("url") != PUBLIC_ROOT + name
                    or metadata.get("sha256") != hashlib.sha256(data).hexdigest()):
                raise AppearanceError("catalog_hash_or_source_mismatch")
            values[name] = _decode(data)
        return cls(values[CATALOG_FILES[0]], values[CATALOG_FILES[1]])

    def validate(self, config):
        config = _object(config, "character_config")
        if set(config) != {"bodyType", "slots", "version"} or type(config.get("version")) is not int or config["version"] != 5:
            raise AppearanceError("expected_complete_v5_character_config")
        body = _component(config["bodyType"])
        body_info = _object(self.art["bodyTypes"].get(body), "body_type")
        allowed = _object(body_info.get("slots"), "body_slots")
        slots = _object(config["slots"], "slots")
        if len(slots) > 64 or set(slots) - set(allowed):
            raise AppearanceError("unknown_character_slot")
        required = {slot for slot, meta in allowed.items()
                    if _object(meta, "slot_metadata").get("optional") is not True}
        missing = sorted(required - set(slots))
        if missing:
            raise AppearanceError("missing_required_slots:" + ",".join(missing))
        custom = self.customizer["bodyTypes"].get(body, {})
        custom_slots = _object(custom.get("slots", {}), "customizer_slots")
        checked = []
        for slot, asset in slots.items():
            _component(slot)
            prefix = body + "_" + slot + "_"
            if not isinstance(asset, str) or not asset.startswith(prefix) or "+" not in asset:
                raise AppearanceError("asset_does_not_match_slot:" + slot)
            item_name, variant = asset[len(prefix):].rsplit("+", 1)
            _component(item_name)
            _component(variant)
            slot_meta = _object(custom_slots.get(slot, {}), "customizer_slot")
            item = _object(slot_meta.get("items", {}).get(item_name, {}), "customizer_item")
            warnings = []
            resolved = asset
            if item:
                variation = item.get("variations", {}).get(variant)
                if not isinstance(variation, dict):
                    raise AppearanceError("unknown_variant:" + slot)
                if item.get("disabled") or variation.get("disabled"):
                    raise AppearanceError("disabled_selection:" + slot)
                if slot_meta.get("disabled") and slot not in required:
                    warnings.append("slot_hidden_in_customizer")
                swatch = _object(variation.get("swatch", {}), "swatch")
                if swatch.get("type") == "transform":
                    resolved = prefix + item_name + "+" + _component(item.get("baseVariation"))
                for key in ("accessKey", "isPremium", "isReferral", "productId"):
                    if item.get(key) or variation.get(key):
                        warnings.append("check_account_entitlement:" + key)
            else:
                warnings.append("art_only_selection_not_offered_by_customizer")
            art = self.art["assets"].get(resolved)
            if not isinstance(art, dict):
                raise AppearanceError("missing_art_asset:" + slot)
            preview = _object(art.get("preview"), "preview")
            version, kind = preview.get("ver"), preview.get("type")
            if type(version) is not int or not 0 <= version <= 1000000 or kind not in {"atlas", "sprite"}:
                raise AppearanceError("unsupported_preview_format")
            filename = resolved + "_preview_" + kind + "_" + str(version) + ".png"
            checked.append({"slot": slot, "selected": asset, "resolved_art": resolved,
                            "preview_url": PUBLIC_ROOT + quote(filename, safe=""),
                            "warnings": warnings})
        return {"catalog_valid": True, "server_validated": False,
                "visual_review_required": True, "slots": checked,
                "note": "Catalog membership does not verify garment cut, resemblance or account entitlement."}


def download_catalog(output, *, transport=None):
    if Path(output).exists() or Path(output).is_symlink():
        raise AppearanceError("output_must_be_new_directory")
    files = {}
    with _client(transport) as client:
        for name in CATALOG_FILES:
            files[name] = _get(client, PUBLIC_ROOT + name, MAX_JSON_BYTES)
    Catalog(_decode(files[CATALOG_FILES[0]]), _decode(files[CATALOG_FILES[1]]))
    manifest = {"schema": "sowork-appearance-catalog/v1",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "provenance": "Official frontend assets; not a stable REST catalog contract.",
                "sources": {name: {"url": PUBLIC_ROOT + name,
                                    "sha256": hashlib.sha256(data).hexdigest()}
                            for name, data in files.items()}}
    files["manifest.json"] = _json_bytes(manifest)
    directory = _publish(output, files)
    return {"catalog": str(directory), "sources": manifest["sources"], "credentials_loaded": False}


def download_previews(catalog, config, output, *, transport=None):
    if Path(output).exists() or Path(output).is_symlink():
        raise AppearanceError("output_must_be_new_directory")
    validation = catalog.validate(config)
    files, records = {}, []
    with _client(transport) as client:
        for selected in validation["slots"]:
            data = _get(client, selected["preview_url"], MAX_PREVIEW_BYTES)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise AppearanceError("invalid_png_preview")
            filename = selected["slot"] + ".png"
            files[filename] = data
            records.append({**selected, "file": filename, "sha256": hashlib.sha256(data).hexdigest()})
    files["manifest.json"] = _json_bytes({"schema": "sowork-appearance-previews/v1",
                                         "visual_review_completed": False, "previews": records})
    directory = _publish(output, files)
    return {"previews": str(directory), "visual_review_completed": False,
            "note": "Inspect every selected garment and hairstyle before presenting the final JSON."}


def setup_parser(commands):
    parser = commands.add_parser("appearance", help="Prepare character JSON without profile credentials or app writes")
    actions = parser.add_subparsers(dest="appearance_command", required=True)
    catalog = actions.add_parser("catalog", help="Download public v5 catalogs into a new directory")
    catalog.add_argument("--output", type=Path, required=True)
    for name, help_text in (("validate", "Check selected IDs and color base art offline"),
                            ("previews", "Download actual public garment previews for visual review")):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--catalog", type=Path, required=True)
        action.add_argument("--config", type=Path, required=True)
        if name == "previews":
            action.add_argument("--output", type=Path, required=True)


def dispatch(args):
    if args.appearance_command == "catalog":
        return download_catalog(args.output)
    catalog, config = Catalog.load(args.catalog), read_json(args.config)
    if args.appearance_command == "validate":
        return catalog.validate(config)
    return download_previews(catalog, config, args.output)
