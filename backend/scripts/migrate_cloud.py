"""Check and apply Supabase migrations against a cloud project.

Reads credentials from backend/.env.diagnose (git-ignored), falling back to
backend/.env and process environment:

    SUPABASE_URL=https://<project-ref>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=...
    SUPABASE_ACCESS_TOKEN=...   # optional; required only to execute SQL

Usage:
    python scripts/migrate_cloud.py check   # print current schema version
    python scripts/migrate_cloud.py apply   # apply missing migrations

Never prints keys. Executes each migration file inside its own request so a
failed file can be reported precisely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for env_file in (ROOT / ".env.diagnose", ROOT / ".env"):
        if not env_file.exists():
            continue
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ACCESS_TOKEN"):
        if key not in values:
            values[key] = os.environ.get(key, "")
    return values


def _require(values: dict[str, str], *keys: str) -> None:
    missing = [key for key in keys if not values.get(key)]
    if missing:
        raise SystemExit(
            "missing in backend/.env.diagnose: " + ", ".join(missing)
        )


def _post_json(url: str, payload: dict, headers: dict[str, str], timeout: int = 30):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return response.status, body


def current_schema_version(values: dict[str, str]) -> int | None:
    base = values["SUPABASE_URL"].rstrip("/")
    url = f"{base}/rest/v1/rpc/polybot_schema_version"
    headers = {
        "apikey": values["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {values['SUPABASE_SERVICE_ROLE_KEY']}",
        "Accept": "application/json",
    }
    try:
        status, body = _post_json(url, {}, headers)
    except urllib.error.HTTPError as exc:
        print(f"schema version query failed: HTTP {exc.code}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"schema version query failed: {type(exc).__name__}", file=sys.stderr)
        return None
    try:
        return int(json.loads(body))
    except (ValueError, TypeError):
        print(f"unexpected schema version response ({status}): {body[:200]}", file=sys.stderr)
        return None


def missing_migrations(current: int) -> list[Path]:
    result: list[Path] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        match = re.match(r"(\d{4})_", path.name)
        if match and int(match.group(1)) > current:
            result.append(path)
    return result


def apply_migration(values: dict[str, str], project_ref: str, path: Path) -> bool:
    sql = path.read_text(encoding="utf-8")
    url = f"https://api.supabase.com/v1/projects/{project_ref}/database/query"
    headers = {"Authorization": f"Bearer {values['SUPABASE_ACCESS_TOKEN']}"}
    try:
        status, body = _post_json(url, {"query": sql}, headers, timeout=120)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"  FAILED {path.name} (HTTP {exc.code}): {detail}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"  FAILED {path.name}: {type(exc).__name__}", file=sys.stderr)
        return False
    if status >= 300:
        print(f"  FAILED {path.name} (HTTP {status}): {body[:500]}", file=sys.stderr)
        return False
    print(f"  applied {path.name}")
    return True


def project_ref_from_url(url: str) -> str:
    match = re.search(r"https://([^.]+)\.supabase\.co", url)
    if not match:
        raise SystemExit(f"cannot extract project ref from SUPABASE_URL: {url}")
    return match.group(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "apply"))
    args = parser.parse_args()
    values = _load_env()
    _require(values, "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")

    current = current_schema_version(values)
    if current is None:
        raise SystemExit("could not read schema version; check URL/key in backend/.env")
    print(f"current schema version on cloud: {current}")
    missing = missing_migrations(current)
    print(f"migrations missing: {len(missing)} -> {[p.name for p in missing]}")
    if args.command == "check":
        return

    if not missing:
        print("database is up to date; nothing to apply")
        return
    _require(values, "SUPABASE_ACCESS_TOKEN")
    project_ref = project_ref_from_url(values["SUPABASE_URL"])
    for index, path in enumerate(missing, start=1):
        print(f"[{index}/{len(missing)}] {path.name}")
        if not apply_migration(values, project_ref, path):
            raise SystemExit(f"aborted at {path.name}; fix and rerun")
        time.sleep(1)

    after = current_schema_version(values)
    print(f"schema version after apply: {after}")
    if after != 18:
        raise SystemExit("schema version did not reach 18 after apply")
    print("OK: cloud schema is at version 18")


if __name__ == "__main__":
    main()
