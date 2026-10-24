"""Plugin CLI plus direct entry, which bypasses Hermes' eager secret bootstrap."""
import argparse
import asyncio
import json
from pathlib import Path

from .admin import AdminError, EDITABLE, check_connection, configure
from .appearance import AppearanceError
from .diagnostics import inspect


def setup_parser(parser):
    parser.add_argument("--home", type=Path, help="Exact Hermes profile directory; defaults to the current profile")
    parser.add_argument("--json", action="store_true", help="Sanitized structured output")
    commands = parser.add_subparsers(dest="sowork_command", required=True)
    commands.add_parser("status", help="Local status without resolving credentials")
    commands.add_parser("doctor", help="Local diagnostics; never repairs or resends")
    commands.add_parser("check-preview", help="Local core/provider streaming compatibility; no model or secrets")
    commands.add_parser("check-runtime", help="Check Hermes gateway contracts before credentials or network")
    commands.add_parser("check-connection", help="Read-only API check using the active Hermes profile")
    from .appearance import setup_parser as setup_appearance
    setup_appearance(commands)
    setup = commands.add_parser("setup", help="Connect this profile through native secrets and a private DM pairing code")
    setup.add_argument("--owner-id", help="Optional already-confirmed human ID; never a wildcard")
    setup.add_argument("--subscription-id", help="Explicitly reviewed exclusive subscription for recovery; otherwise setup creates its own")
    config = commands.add_parser("config", help="Inspect or review limited SoWork-only settings")
    actions = config.add_subparsers(dest="config_command", required=True)
    actions.add_parser("show", help="Show public bindings and resource flags")
    edit = actions.add_parser("set", help="Review a change; no write without --apply and the reviewed checksum")
    edit.add_argument("feature", choices=EDITABLE)
    edit.add_argument("value", choices=("on", "off"))
    edit.add_argument("--apply", action="store_true")
    edit.add_argument("--expected-config-sha")


def dispatch(args):
    try:
        if args.sowork_command != "appearance":
            from hermes_constants import get_hermes_home
            if args.home is not None and args.home.expanduser().is_symlink():
                raise AdminError("profile_symlink")
            home = (args.home or get_hermes_home()).expanduser().resolve()
        if args.sowork_command == "appearance":
            from .appearance import dispatch as prepare_appearance
            result = prepare_appearance(args)
        elif args.sowork_command == "setup":
            from .setup import interactive
            result = interactive(home, args)
        elif args.sowork_command == "check-runtime":
            from .runtime import MINIMUM_HERMES, missing_contracts
            missing = missing_contracts()
            result = {"minimum_hermes": MINIMUM_HERMES, "issues": missing,
                      "supported": not missing, "credentials_resolved": False}
        elif args.sowork_command == "check-connection":
            result = asyncio.run(check_connection(home))
        elif args.sowork_command == "check-preview":
            from .compatibility import check_preview
            result = check_preview(home)
        elif args.sowork_command == "config" and args.config_command == "set":
            result = configure(home, args.feature, args.value == "on", apply=args.apply,
                               expected_sha=args.expected_config_sha)
        else:
            result = inspect(home)
            if args.sowork_command == "doctor":
                from .compatibility import check_preview
                result["preview_compatibility"] = check_preview(home)
                result["issues"] = sorted(set(result["issues"] + result["preview_compatibility"]["issues"]))
            if args.sowork_command == "config":
                for key in ("gateway", "state", "issues"):
                    result.pop(key, None)
        code = 1 if args.sowork_command in ("doctor", "check-preview", "check-runtime") and result.get("issues") else 0
    except (AdminError, AppearanceError) as error:
        result, code = {"error": str(error)}, 2
    except Exception:
        result, code = {"error": "local_operation_failed"}, 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    setup_parser(parser)
    return dispatch(parser.parse_args(argv))
