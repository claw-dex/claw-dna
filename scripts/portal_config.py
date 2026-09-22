#!/usr/bin/env python3
"""
portal_config.py — Unified portal configuration management.

Manages public hostname, timezone, and authentication for the agent portal.
Combines functionality from legacy portal-hostname.py and portal-auth.py.

Usage:
    # Hostname management
    uv run python scripts/portal_config.py hostname --set https://example.com
    uv run python scripts/portal_config.py hostname --show
    uv run python scripts/portal_config.py hostname --clear

    # Timezone management
    uv run python scripts/portal_config.py timezone --set America/New_York
    uv run python scripts/portal_config.py timezone --show
    uv run python scripts/portal_config.py timezone --clear

    # Auth management
    uv run python scripts/portal_config.py auth --enable user:pass
    uv run python scripts/portal_config.py auth --disable
    uv run python scripts/portal_config.py auth --reapply
    uv run python scripts/portal_config.py auth --show
    uv run python scripts/portal_config.py auth --rollback

Exit codes: 0 = success, 1 = error
"""

import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ── Configuration ─────────────────────────────────────────────────

MEMORY = Path("/agent/memory")
CONFIG_PATH = MEMORY / "portal_config.json"

CADDY_ADMIN = "http://localhost:2019"
KEEPASS_DB = Path("/home/agent/.keepass/credentials.kdbx")
KEEPASS_PORTAL_BASIC_AUTH = "PORTAL_BASIC_AUTH"
KEEPASS_GROUP = "System"


# ── Helper Functions ──────────────────────────────────────────────


def _read_json_safe(path: Path, default: dict) -> dict:
    """Safely read JSON file with fallback to default."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _write_json_atomic(path: Path, data: dict) -> None:
    """Atomically write JSON to file using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def _save_portal_config(key: str, value: str) -> None:
    """Save a key-value pair to portal_config.json, preserving other keys."""
    config = _read_json_safe(CONFIG_PATH, {})
    config[key] = value
    _write_json_atomic(CONFIG_PATH, config)


# ── Backward Compatibility ───────────────────────────────────────


def _detect_legacy_mode():
    """Detect if invoked via old script names (symlinks)."""
    invoked_as = Path(sys.argv[0]).name
    if invoked_as == "portal-hostname.py":
        return "hostname"
    elif invoked_as == "portal-auth.py":
        return "auth"
    return None


# ── Hostname Subcommand ───────────────────────────────────────────


def _set_hostname(url: str) -> None:
    """Validate and save the public URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print("ERROR: URL must be a valid http:// or https:// URL with a hostname.")
        sys.exit(1)

    if re.search(r"[\s\x00-\x1f]", url):
        print("ERROR: URL must not contain whitespace or control characters.")
        sys.exit(1)

    url = url.rstrip("/")

    # Use _save_portal_config to preserve other keys (timezone, etc.)
    _save_portal_config("public_url", url)

    print(f"Public hostname set: {url}")
    print(f"Config saved to: {CONFIG_PATH}")


def _show_hostname() -> None:
    """Print the current public URL or 'not configured'."""
    if not CONFIG_PATH.exists():
        print("Public hostname: not configured")
        print("Run with --set <url> to configure.")
        return
    try:
        config = json.loads(CONFIG_PATH.read_text())
        print(f"Public hostname: {config.get('public_url', '?')}")
    except Exception as e:
        print(f"ERROR reading config: {e}")


def _clear_hostname() -> None:
    """Remove the public URL configuration."""
    if not CONFIG_PATH.exists():
        print("Public hostname is not configured. Nothing to clear.")
        return

    try:
        config = _read_json_safe(CONFIG_PATH, {})
        if "public_url" not in config:
            print("Public hostname is not configured. Nothing to clear.")
            return

        # Remove public_url from config
        del config["public_url"]

        # Write back the remaining config
        _write_json_atomic(CONFIG_PATH, config)

        print("Public hostname cleared.")
    except Exception as e:
        print(f"ERROR clearing hostname: {e}")
        sys.exit(1)


def cmd_hostname(args):
    """Handle hostname subcommand."""
    if args.show:
        _show_hostname()
    elif args.clear:
        _clear_hostname()
    elif args.set:
        _set_hostname(args.set)
    else:
        print("Usage: portal_config.py hostname --set <url> | --show | --clear")
        sys.exit(1)


# ── Timezone Subcommand ───────────────────────────────────────────


def _set_timezone(tz: str) -> None:
    """Validate and save the timezone."""
    try:
        zoneinfo.ZoneInfo(tz)  # Validate timezone
    except zoneinfo.ZoneInfoNotFoundError:
        print(f"ERROR: Invalid timezone: {tz}", file=sys.stderr)
        print(f"Use IANA timezone names (e.g., America/New_York, Europe/London, UTC)")
        sys.exit(1)

    _save_portal_config("timezone", tz)
    print(f"Timezone set: {tz}")
    print(f"Config saved to: {CONFIG_PATH}")


def _show_timezone() -> None:
    """Print the current timezone or 'not configured'."""
    if not CONFIG_PATH.exists():
        print("Timezone: not configured")
        print("Run with --set <timezone> to configure.")
        return
    try:
        config = json.loads(CONFIG_PATH.read_text())
        tz = config.get("timezone")
        if tz:
            print(f"Timezone: {tz}")
        else:
            print("Timezone: not configured")
            print("Run with --set <timezone> to configure.")
    except Exception as e:
        print(f"ERROR reading config: {e}")


def _clear_timezone() -> None:
    """Remove the timezone configuration."""
    if not CONFIG_PATH.exists():
        print("Timezone is not configured. Nothing to clear.")
        return

    try:
        config = _read_json_safe(CONFIG_PATH, {})
        if "timezone" not in config:
            print("Timezone is not configured. Nothing to clear.")
            return

        # Remove timezone from config
        del config["timezone"]

        # Write back the remaining config
        _write_json_atomic(CONFIG_PATH, config)

        print("Timezone cleared.")
    except Exception as e:
        print(f"ERROR clearing timezone: {e}")
        sys.exit(1)


def cmd_timezone(args):
    """Handle timezone subcommand."""
    if args.show:
        _show_timezone()
    elif args.clear:
        _clear_timezone()
    elif args.set:
        _set_timezone(args.set)
    else:
        print("Usage: portal_config.py timezone --set <timezone> | --show | --clear")
        sys.exit(1)


# ── Auth Subcommand ───────────────────────────────────────────────


def _caddy_api(method: str, path: str, data: dict | None = None) -> tuple[int, str]:
    """Send a request to the Caddy Admin API. Returns (status_code, body)."""
    url = f"{CADDY_ADMIN}{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def _get_server_name() -> str | None:
    """Find the server name that listens on :8080."""
    status, body = _caddy_api("GET", "/config/apps/http/servers")
    if status != 200:
        return None
    try:
        servers = json.loads(body)
    except json.JSONDecodeError:
        return None
    for name, cfg in servers.items():
        for addr in cfg.get("listen", []):
            if ":8080" in addr:
                return name
    return None


def _auth_exists() -> bool:
    """Check if the portal_auth route is present in the running config."""
    status, _ = _caddy_api("GET", "/id/portal_auth")
    return status == 200


def _build_auth_route(username: str, password_hash: str) -> dict:
    """Build the JSON authentication route object.

    /webhook/* is excluded so external servers can POST without credentials.
    """
    return {
        "@id": "portal_auth",
        "match": [{"not": [{"path": ["/webhook/*"]}]}],
        "handle": [
            {
                "handler": "authentication",
                "providers": {
                    "http_basic": {
                        "accounts": [
                            {
                                "username": username,
                                "password": password_hash,
                            }
                        ],
                        "hash": {"algorithm": "bcrypt"},
                    }
                },
            }
        ],
    }


def _prepend_auth_route(server_name: str, route: dict) -> bool:
    """Prepend the auth route to the server's route list. Returns True on success."""
    routes_path = f"/config/apps/http/servers/{server_name}/routes"
    status, body = _caddy_api("GET", routes_path)
    if status != 200:
        print(f"ERROR: Could not read routes: {status} {body}")
        return False

    try:
        routes = json.loads(body)
    except json.JSONDecodeError:
        print(f"ERROR: Could not parse routes: {body}")
        return False

    routes.insert(0, route)
    status, body = _caddy_api("PATCH", routes_path, routes)
    if status != 200:
        print(f"ERROR: Could not update routes: {status} {body}")
        return False
    return True


def _bcrypt_hash(password: str) -> str | None:
    """Generate a bcrypt hash using caddy hash-password."""
    try:
        result = subprocess.run(
            ["caddy", "hash-password"],
            input=password + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        print(f"WARNING: caddy hash-password failed: {e}", file=sys.stderr)
    return None


def _open_keepass():
    """Open the KeePass database. Returns (kp, None) or (None, error_msg)."""
    if not KEEPASS_DB.exists():
        return (
            None,
            f"KeePass database not found at {KEEPASS_DB}. Run 'keepass.py init' first.",
        )
    try:
        from pykeepass import PyKeePass

        kp = PyKeePass(str(KEEPASS_DB), password="")
        return kp, None
    except Exception as exc:
        return None, f"Failed to open KeePass database: {exc}"


def _find_or_create_group(kp, group_name: str):
    """Find or create a top-level group by name."""
    found = kp.find_groups(name=group_name, first=True)
    if found:
        return found
    return kp.add_group(kp.root_group, group_name)


def _save_to_keepass(username: str, password: str, password_hash: str) -> bool:
    """Store portal credentials in KeePass. Returns True on success."""
    kp, err = _open_keepass()
    if err:
        print(f"ERROR: {err}")
        return False
    try:
        group = _find_or_create_group(kp, KEEPASS_GROUP)
        existing = kp.find_entries(
            title=KEEPASS_PORTAL_BASIC_AUTH, group=group, first=True
        )
        if existing:
            existing.username = username
            existing.password = password
            existing.notes = f"password_hash={password_hash}"
        else:
            kp.add_entry(
                group,
                title=KEEPASS_PORTAL_BASIC_AUTH,
                username=username,
                password=password,
                notes=f"password_hash={password_hash}",
            )
        kp.save()
        return True
    except Exception as exc:
        print(f"ERROR: Failed to save credentials to KeePass: {exc}")
        return False


def _load_from_keepass() -> dict | None:
    """Load portal credentials from KeePass. Returns dict or None."""
    kp, err = _open_keepass()
    if err:
        return None
    entry = kp.find_entries(title=KEEPASS_PORTAL_BASIC_AUTH, first=True)
    if not entry:
        return None
    # Extract password_hash from notes
    password_hash = ""
    if entry.notes:
        for line in entry.notes.splitlines():
            if line.startswith("password_hash="):
                password_hash = line[len("password_hash=") :]
                break
    return {
        "username": entry.username or "",
        "password": entry.password or "",
        "password_hash": password_hash,
    }


def _delete_from_keepass() -> None:
    """Delete portal credentials from KeePass (no-op if missing or on error)."""
    kp, err = _open_keepass()
    if err:
        return
    try:
        entry = kp.find_entries(title=KEEPASS_PORTAL_BASIC_AUTH, first=True)
        if entry:
            kp.delete_entry(entry)
            kp.save()
    except Exception:
        pass  # best-effort cleanup


def _enable_auth(credential: str):
    """Enable auth with provided credentials (username:password)."""
    if ":" not in credential:
        print("ERROR: Credentials must be in 'username:password' format.")
        sys.exit(1)

    username, password = credential.split(":", 1)
    if not username or not password:
        print("ERROR: Both username and password are required (username:password).")
        sys.exit(1)

    if _auth_exists():
        print("Auth already enabled. Use --disable first to reset credentials.")
        sys.exit(0)

    server_name = _get_server_name()
    if not server_name:
        print("ERROR: Could not find server listening on :8080 via Admin API.")
        sys.exit(1)

    password_hash = _bcrypt_hash(password)
    if not password_hash:
        print("ERROR: Could not generate bcrypt hash. Is 'caddy' in PATH?")
        sys.exit(1)

    route = _build_auth_route(username, password_hash)
    if not _prepend_auth_route(server_name, route):
        sys.exit(1)

    if not _save_to_keepass(username, password, password_hash):
        print("WARNING: Auth enabled but credentials could not be saved to KeePass.")

    print("Auth enabled!")
    print(f"  Username: {username}")
    print(f"  Password: {password}")
    print(
        f"  Credentials saved to KeePass ({KEEPASS_GROUP}/{KEEPASS_PORTAL_BASIC_AUTH})"
    )


def _disable_auth():
    """Remove auth via Admin API and delete credentials."""
    if not _auth_exists():
        print("Auth is not enabled.")
        return

    status, body = _caddy_api("DELETE", "/id/portal_auth")
    if status != 200:
        print(f"ERROR: Could not remove auth route: {status} {body}")
        sys.exit(1)

    _delete_from_keepass()
    print("Auth disabled.")


def _reapply_auth():
    """Re-apply auth from saved credentials (called after Caddy restarts)."""
    creds = _load_from_keepass()
    if not creds:
        return

    if _auth_exists():
        return

    server_name = _get_server_name()
    if not server_name:
        print("WARNING: Could not find server on :8080 — skipping auth reapply.")
        return

    username = creds.get("username", "agent")
    password_hash = creds.get("password_hash")

    # Re-hash if password_hash missing from KeePass entry
    if not password_hash:
        password = creds.get("password")
        if not password:
            print("WARNING: No password or hash in KeePass — skipping reapply.")
            return
        password_hash = _bcrypt_hash(password)
        if not password_hash:
            print("WARNING: Could not generate bcrypt hash — skipping reapply.")
            return

    route = _build_auth_route(username, password_hash)
    if _prepend_auth_route(server_name, route):
        print("Auth re-applied from KeePass credentials.")
    else:
        print("WARNING: Failed to re-apply auth.")


def _show_creds():
    """Show current portal credentials."""
    creds = _load_from_keepass()
    if not creds:
        print("No credentials configured. Run with --enable to set up auth.")
        return
    print(f"Username: {creds.get('username', '?')}")
    print(f"Password: {creds.get('password', '?')}")
    print(f"Stored in: KeePass ({KEEPASS_GROUP}/{KEEPASS_PORTAL_BASIC_AUTH})")


def _rollback_auth():
    """Force-remove auth route from Caddy and best-effort delete credentials."""
    if not _auth_exists():
        print("Auth is not enabled — nothing to rollback.")
        return

    status, body = _caddy_api("DELETE", "/id/portal_auth")
    if status != 200:
        print(f"ERROR: Could not remove auth route: {status} {body}")
        sys.exit(1)

    # Also clean up KeePass entry if it exists
    _delete_from_keepass()
    print("Auth rolled back (route removed, credential deleted).")


def cmd_auth(args):
    """Handle auth subcommand."""
    if args.enable:
        _enable_auth(args.enable)
    elif args.disable:
        _disable_auth()
    elif args.reapply:
        _reapply_auth()
    elif args.show:
        _show_creds()
    elif args.rollback:
        _rollback_auth()
    else:
        print(
            "Usage: portal_config.py auth --enable user:pass | --disable | --reapply | --show | --rollback"
        )
        sys.exit(1)


# ── Main Entry Point ──────────────────────────────────────────────


def main():
    import argparse

    legacy_mode = _detect_legacy_mode()
    if legacy_mode:
        # Backward compatibility: inject subcommand for old script names
        sys.argv.insert(1, legacy_mode)

    parser = argparse.ArgumentParser(
        description="Unified portal configuration management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # hostname subparser
    hostname_parser = subparsers.add_parser("hostname", help="Manage public hostname")
    hostname_group = hostname_parser.add_mutually_exclusive_group(required=True)
    hostname_group.add_argument("--set", metavar="URL", help="Set public hostname")
    hostname_group.add_argument(
        "--show", action="store_true", help="Show current hostname"
    )
    hostname_group.add_argument("--clear", action="store_true", help="Clear hostname")

    # timezone subparser
    timezone_parser = subparsers.add_parser("timezone", help="Manage timezone")
    timezone_group = timezone_parser.add_mutually_exclusive_group(required=True)
    timezone_group.add_argument(
        "--set", metavar="TZ", help="Set timezone (e.g., America/New_York)"
    )
    timezone_group.add_argument(
        "--show", action="store_true", help="Show current timezone"
    )
    timezone_group.add_argument("--clear", action="store_true", help="Clear timezone")

    # auth subparser
    auth_parser = subparsers.add_parser("auth", help="Manage authentication")
    auth_group = auth_parser.add_mutually_exclusive_group(required=True)
    auth_group.add_argument(
        "--enable", metavar="USER:PASS", help="Enable auth with credentials"
    )
    auth_group.add_argument("--disable", action="store_true", help="Disable auth")
    auth_group.add_argument(
        "--reapply", action="store_true", help="Re-apply from saved credentials"
    )
    auth_group.add_argument(
        "--show", action="store_true", help="Show current credentials"
    )
    auth_group.add_argument(
        "--rollback", action="store_true", help="Force-remove auth route"
    )

    args = parser.parse_args()

    if args.subcommand == "hostname":
        cmd_hostname(args)
    elif args.subcommand == "timezone":
        cmd_timezone(args)
    elif args.subcommand == "auth":
        cmd_auth(args)


if __name__ == "__main__":
    main()
