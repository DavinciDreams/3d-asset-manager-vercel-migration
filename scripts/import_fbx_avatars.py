"""Import legacy FBX avatar models into the asset store.

This is for FBX files that represent character/avatar models, such as Meshy
characters rigged in Mixamo. It intentionally does not treat every FBX as an
animation clip; by default it imports only `*Character_output.fbx` files.

Examples:
    python scripts/import_fbx_avatars.py --dry-run
    python scripts/import_fbx_avatars.py --dry-run --tag robot
    python scripts/import_fbx_avatars.py --api-base https://3d.flobots.xyz --tag robot --backfill-conversions
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - local convenience only
    load_dotenv = None


DEFAULT_SOURCES = [
    Path(r"Z:\3d\assets\legacy\3d\fbx"),
]

DEFAULT_PATTERN = "*Character_output.fbx"
DEFAULT_TAGS = ["avatar", "humanoid", "rigged"]
DEFAULT_TYPES = ["avatar", "humanoid", "rigged", "fbx"]
DEFAULT_STYLES = ["stylized"]


def _load_env() -> None:
    if load_dotenv:
        load_dotenv(ROOT / ".env")


def _env_token(explicit_env: str | None) -> str:
    names = [
        explicit_env,
        "ASSET_MANAGER_IMPORT_TOKEN",
        "TELLUS_ADMIN_API_TOKEN",
        "ASSET_MANAGER_API_TOKEN",
        "API_UPLOAD_TOKEN",
    ]
    for name in names:
        if not name:
            continue
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def _clean_title(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"_?Meshy_AI_?", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_?biped(?:\s*\(\d+\))?_?", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_?Character_output$", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_?texture_fbx(?:_\d+)?$", " ", stem, flags=re.IGNORECASE)
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        stem = "Legacy Character"
    title = stem.title()
    if "Avatar" not in title:
        title = f"{title} Avatar"
    return title[:80].strip()


def _scan_sources(sources: list[Path], pattern: str, name_filter: str | None) -> tuple[list[Path], Counter]:
    stats: Counter = Counter()
    matches: list[Path] = []
    seen: set[Path] = set()
    name_re = re.compile(name_filter, re.IGNORECASE) if name_filter else None
    for source in sources:
        if not source.exists():
            print(f"Missing source: {source}")
            stats["missing_sources"] += 1
            continue
        candidates = source.rglob(pattern) if source.is_dir() else [source]
        for path in candidates:
            if not path.is_file():
                continue
            if path.suffix.lower() != ".fbx":
                stats["ignored_non_fbx"] += 1
                continue
            if name_re and not name_re.search(path.name):
                stats["ignored_name_filter"] += 1
                continue
            resolved = path.resolve()
            if resolved in seen:
                stats["duplicates_in_scan"] += 1
                continue
            seen.add(resolved)
            stats["fbx"] += 1
            matches.append(path)
    return matches, stats


def _print_scan_summary(paths: list[Path], stats: Counter, pattern: str, name_filter: str | None) -> None:
    print(
        "Scan summary: "
        f"pattern={pattern!r} name_filter={name_filter or '-'} matched={len(paths)} "
        f"fbx={stats.get('fbx', 0)} ignored_name_filter={stats.get('ignored_name_filter', 0)} "
        f"missing_sources={stats.get('missing_sources', 0)}"
    )


def _runtime_metadata(path: Path, digest: str) -> str:
    return json.dumps({
        "rig": {"type": "humanoid", "source": "mixamo-compatible-fbx"},
        "upload": {
            "source": "fbx-avatar-import",
            "content_hash": digest,
            "original_path": str(path),
        },
    })


def _trigger_conversion_backfill(api_base: str, token: str) -> None:
    import requests

    response = requests.post(
        f"{api_base.rstrip('/')}/api/admin/conversion-backfill",
        headers={"Authorization": f"Bearer {token}"},
        params={"force": "true"},
        timeout=90,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"conversion backfill failed: HTTP {response.status_code} {response.text[:300]}")
    print(f"Conversion backfill queued: {response.text[:500]}")


def import_via_api(args) -> int:
    try:
        import requests
    except ImportError as error:
        raise SystemExit("API mode requires the requests package.") from error

    _load_env()
    token = _env_token(args.api_token_env)
    if not token and not args.dry_run:
        raise SystemExit(
            "Set ASSET_MANAGER_IMPORT_TOKEN, TELLUS_ADMIN_API_TOKEN, ASSET_MANAGER_API_TOKEN, "
            "or pass --api-token-env before running a live import."
        )

    sources = args.source or [path for path in DEFAULT_SOURCES if path.exists()]
    if not sources:
        raise SystemExit("No FBX avatar sources found. Pass --source.")
    paths, stats = _scan_sources(sources, args.pattern, args.name_filter)
    _print_scan_summary(paths, stats, args.pattern, args.name_filter)
    if not paths:
        raise SystemExit("No matching FBX avatar files found. Check --source, --pattern, and --name-filter.")

    tags = [*DEFAULT_TAGS, *args.tag]
    asset_types = [*DEFAULT_TYPES, *args.asset_type]
    styles = [*DEFAULT_STYLES, *args.style]
    api_base = args.api_base.rstrip("/")
    uploaded = skipped = failed = 0
    for index, path in enumerate(paths, 1):
        if args.limit and (uploaded + skipped + failed) >= args.limit:
            break
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        title = args.name or _clean_title(path)
        description = args.description or (
            "Rigged humanoid avatar FBX imported from the legacy character library. "
            "The asset store will generate viewable GLB and VRM variants during conversion."
        )
        print(f"[{index}] upload {title!r} from {path}")
        if args.dry_run:
            continue
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    f"{api_base}/api/upload",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "name": title,
                        "description": description,
                        "is_public": "true" if not args.private else "false",
                        "tags": ",".join(tags),
                        "asset_category": "person",
                        "asset_styles": ",".join(styles),
                        "asset_types": ",".join(asset_types),
                        "runtime_metadata": _runtime_metadata(path, digest),
                    },
                    files={"file": (path.name, handle, "application/octet-stream")},
                    timeout=180,
                )
            body = response.text[:300]
            if response.status_code == 409 or "duplicate" in response.text.lower():
                skipped += 1
                print(f"[{index}] skip duplicate {path.name}: HTTP {response.status_code} {body}")
            elif response.status_code >= 400:
                failed += 1
                print(f"[{index}] fail {path.name}: HTTP {response.status_code} {body}")
            else:
                uploaded += 1
                try:
                    model = response.json().get("model") or {}
                    print(f"[{index}] uploaded id={model.get('id')} conversion={model.get('conversion_status')}")
                except Exception:
                    print(f"[{index}] uploaded HTTP {response.status_code}")
        except Exception as error:
            failed += 1
            print(f"[{index}] fail {path}: {str(error)[:300]}")

    print(f"Done. uploaded={uploaded} skipped={skipped} failed={failed} dry_run={args.dry_run}")
    if args.backfill_conversions:
        if args.dry_run:
            print("Dry run: conversion backfill not queued.")
        elif uploaded or skipped:
            _trigger_conversion_backfill(api_base, token)
        else:
            print("No uploaded or duplicate models; conversion backfill not queued.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", type=Path, help="FBX file or directory. Repeatable.")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN, help=f"File glob inside source dirs. Default: {DEFAULT_PATTERN}")
    parser.add_argument("--name-filter", help="Optional regex filter applied to the filename.")
    parser.add_argument("--api-base", default="https://3d.flobots.xyz", help="Asset-store API base URL.")
    parser.add_argument("--api-token-env", help="Environment variable holding the bearer token.")
    parser.add_argument("--tag", action="append", default=[], help="Extra marketplace tag. Repeatable, e.g. --tag robot.")
    parser.add_argument("--asset-type", action="append", default=[], help="Extra asset type. Repeatable.")
    parser.add_argument("--style", action="append", default=[], help="Extra style tag. Repeatable.")
    parser.add_argument("--name", help="Override title for a single-file import.")
    parser.add_argument("--description", help="Override description.")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--backfill-conversions", action="store_true")
    args = parser.parse_args()
    return import_via_api(args)


if __name__ == "__main__":
    raise SystemExit(main())
