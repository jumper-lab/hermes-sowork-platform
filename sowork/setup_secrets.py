"""Map references, then delegate resolution to the active profile's native backend."""
from __future__ import annotations

import copy
import inspect
from pathlib import Path
import re

from .admin import AdminError, managed_config_present, read_config, write_config
from .config import Credentials, DEFAULT_CREDENTIAL_ENV


def require_active_profile(home):
    from hermes_constants import get_hermes_home
    from agent.secret_scope import current_secret_scope_home
    scoped = current_secret_scope_home()
    if home != get_hermes_home().resolve() or (scoped and home != Path(scoped).resolve()):
        raise AdminError("setup_requires_active_profile")


def validate_reference(value):
    # Native references may use names or UUIDs and optional sections. Prefer
    # UUIDs for rename stability; never accept plaintext or a whole-item URL.
    part = r"[^/\x00-\x1f\x7f]+"
    if not isinstance(value, str) or not re.fullmatch("op://" + part + "/" + part + "/(?:" + part + "/)?" + part, value):
        raise AdminError("setup_requires_onepassword_field_reference")
    return value


def check_token_profile(home):
    """Token onboarding is for a fresh binding, never a secret rotation."""
    from agent.secret_scope import get_secret
    from hermes_cli.config import get_env_path, is_managed, load_env
    from hermes_cli.managed_scope import is_env_managed

    require_active_profile(home)
    config, _ = read_config(home)
    env_path = get_env_path()
    names = DEFAULT_CREDENTIAL_ENV.values()
    if (managed_config_present() or is_managed() or env_path != home / ".env"
            or env_path.is_symlink() or any(is_env_managed(name) for name in names)):
        raise AdminError("setup_token_requires_writable_profile_secrets")
    if env_path.exists() and (not env_path.is_file() or env_path.stat().st_mode & 0o077):
        raise AdminError("setup_token_requires_private_profile_dotenv")
    if "sowork" in config.get("platforms", {}):
        raise AdminError("setup_token_requires_unbound_profile")
    env = load_env()
    refs = config.get("secrets", {}).get("onepassword", {}).get("env", {})
    if any(name in env or name in refs or get_secret(name) for name in names):
        raise AdminError("setup_token_existing_credentials_use_standard_setup")


def save_token_credentials(home, credential):
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    from hermes_cli.config import save_env_value

    check_token_profile(home)
    # Use Hermes' writer. The installation ID is saved last. Until all three
    # resolve together, setup cannot publish an active platform binding.
    try:
        for field, name in DEFAULT_CREDENTIAL_ENV.items():
            save_env_value(name, getattr(credential, field))
        scope = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            if Credentials.from_scope(home=home) != credential:
                raise ValueError()
        finally:
            reset_secret_scope(scope)
    except (OSError, RuntimeError, ValueError):
        raise AdminError("setup_token_secret_save_incomplete_review_profile_and_installation") from None


def credentials(home, ui):
    require_active_profile(home)
    config, sha = read_config(home)
    names = list(DEFAULT_CREDENTIAL_ENV.values())
    try:
        return Credentials.from_scope(home=home)
    except ValueError:
        pass
    source = config.get("secrets", {}).get("onepassword", {})
    if source.get("enabled") is not True or "onepassword" not in config.get("secrets", {}).get("sources", ["onepassword"]):
        ui.write("Supply the three SoWork credentials through this profile's Hermes secret source, "
                 "or use sowork setup --setup-token for a new SoWork installation.")
        raise AdminError("setup_configure_native_secret_source_first")
    candidate = copy.deepcopy(config)
    refs = candidate["secrets"]["onepassword"].setdefault("env", {})
    for name in names:
        if not refs.get(name):
            refs[name] = validate_reference(ui.reference(name + " — paste its 1Password field reference (op://): ").strip())
        else:
            validate_reference(refs[name])
    changed = candidate != config
    if changed:
        if not ui.confirm("Save these three references in this profile's native 1Password mapping?"):
            raise AdminError("setup_cancelled")
        write_config(home, config, candidate, sha)
    # The public native API owns all bootstrap, cache and provider handling.
    # Never read .env, invoke op, export bootstrap or reset another profile.
    from hermes_cli.env_loader import reset_secret_source_cache, hydrate_profile_secret_sources
    try:
        inspect.signature(reset_secret_source_cache).bind(home)
    except TypeError:
        raise AdminError("setup_requires_profile_scoped_secret_refresh") from None
    reset_secret_source_cache(home)
    values = hydrate_profile_secret_sources(home)
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    token = set_secret_scope({name: values.get(name, "") for name in names}, profile_home=str(home))
    try:
        return Credentials.from_scope(home=home)
    except ValueError:
        raise AdminError("setup_native_secret_resolution_failed") from None
    finally:
        reset_secret_scope(token)
