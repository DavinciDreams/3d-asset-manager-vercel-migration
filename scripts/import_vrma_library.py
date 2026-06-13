"""Import a VRMA animation library or source animation files into the asset store.

This writes through the app's configured DB_ENGINE and FILE_STORE, so run it in
the same environment as the asset-store service for production imports.

Examples:
    python scripts/import_vrma_library.py --dry-run
    python scripts/import_vrma_library.py --owner-username lisa
    python scripts/import_vrma_library.py --source "Z:\\3d\\assets\\animation-rigs\\legacy\\animations"
    python scripts/import_vrma_library.py --api-base https://3d.flobots.xyz --format fbx --source "Z:\\3d\\assets\\legacy\\3d\\fbx"
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("AI_AUTOTAG_WORKER", "0")
os.environ.setdefault("AI_AUTOTAG_ON_UPLOAD", "0")


DEFAULT_SOURCES = [
    Path(r"C:\Users\lmwat\3dchat\3dchat\public\animations\vrma"),
    Path(r"Z:\3d\assets\animation-rigs\legacy\animations"),
]

DEFAULT_FBX_SOURCES = [
    Path(r"Z:\3d\assets\legacy\3d\fbx"),
]

DEFAULT_MANIFESTS = [
    Path(r"C:\Users\lmwat\3dchat\3dchat\scripts\animation-list.json"),
]

CORE_NAMES = {
    "VRMA_02": ("Greeting", "Greeting animation", "social"),
    "VRMA_03": ("Peace", "Peace sign animation", "social"),
    "VRMA_04": ("Shoot", "Shoot animation", "action"),
    "VRMA_05": ("Spin", "Spin animation", "dance"),
    "VRMA_06": ("Model Pose", "Model pose animation", "pose"),
    "VRMA_07": ("Squat", "Squat animation", "exercise"),
}


def _title_from_stem(stem):
    cleaned = re.sub(r"\([^)]*\)", "", stem)
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() or stem


def _load_manifest(paths):
    meta = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            print(f"Could not read manifest {path}: {error}")
            continue
        for item in data.get("animations") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("name") or "").strip()
            if not key:
                continue
            meta[key.lower()] = {
                "title": str(item.get("mixamoName") or item.get("name") or key).strip(),
                "description": str(item.get("description") or "").strip(),
                "category": str(item.get("category") or "").strip().lower(),
            }
    for key, (title, description, category) in CORE_NAMES.items():
        meta.setdefault(key.lower(), {"title": title, "description": description, "category": category})
    return meta


def _file_metadata(path, manifest):
    stem = path.stem
    if stem.lower().startswith("animations_"):
        stem = stem[len("Animations_"):]
    item = manifest.get(stem.lower(), {})
    title = item.get("title") or _title_from_stem(stem)
    category = item.get("category") or _infer_category(stem)
    description = item.get("description") or f"Humanoid VRMA animation clip: {title}."
    ext = path.suffix.lower().lstrip(".")
    tags = ["animation-library", "humanoid-animation", "avatar-animation", "animation", ext]
    if ext == "vrma":
        tags.append("vrma-library")
    elif ext in {"fbx", "bvh"}:
        tags.append("animation-source")
    if category:
        tags.append(category)
    return title, description, category, tags


def _infer_category(stem):
    text = _title_from_stem(stem).lower()
    if any(word in text for word in ("idle", "stand", "pose")):
        return "idle"
    if any(word in text for word in ("walk", "run", "jog", "crawl", "climb")):
        return "locomotion"
    if any(word in text for word in ("dance", "breakdance", "samba", "rumba", "ballet")):
        return "dance"
    if any(word in text for word in ("punch", "kick", "attack", "block", "gun", "stab")):
        return "action"
    if any(word in text for word in ("talk", "wave", "kiss", "nod", "gesture", "salute")):
        return "gesture"
    return "humanoid"


def _iter_animation_files(sources, formats):
    seen = set()
    suffixes = {f".{fmt.lower().lstrip('.')}" for fmt in formats}
    for source in sources:
        if not source.exists():
            print(f"Missing source: {source}")
            continue
        if source.is_dir():
            paths = (path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)
        else:
            paths = [source] if source.suffix.lower() in suffixes else []
        for path in paths:
            if path.suffix.lower() == ".fbx" and not path.name.lower().startswith("animations_"):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path


def import_library(sources, manifests, owner_username=None, public=True, dry_run=False, limit=None, formats=None):
    from app import create_app
    from app.models import Model3D, User

    formats = formats or ["vrma"]
    app = create_app()
    manifest = _load_manifest(manifests)
    with app.app_context():
        owner = User.get_by_username(owner_username) if owner_username else None
        if owner_username and not owner:
            raise SystemExit(f"Owner username not found: {owner_username}")

        fs = app.config["FILE_STORE"]
        changed = skipped = failed = 0
        for index, path in enumerate(_iter_animation_files(sources, formats), 1):
            if limit and (changed + skipped + failed) >= limit:
                break
            try:
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                existing = Model3D.get_by_content_hash(digest)
                title, description, category, tags = _file_metadata(path, manifest)
                if existing:
                    skipped += 1
                    print(f"[{index}] skip duplicate {path.name}: {existing.name} ({existing.id})")
                    continue

                changed += 1
                print(f"[{index}] import {title!r} from {path}")
                if dry_run:
                    continue

                file_id = fs.put(
                    data,
                    filename=path.name,
                    content_type="application/octet-stream",
                    metadata={
                        "kind": path.suffix.lower().lstrip("."),
                        "original_filename": path.name,
                        "source_path": str(path),
                        "content_hash": digest,
                    },
                )
                ext = path.suffix.lower().lstrip(".")
                model = Model3D(
                    name=title,
                    description=description,
                    file_format=ext,
                    file_size=len(data),
                    content_hash=digest,
                    original_filename=path.name,
                    user_id=owner.id if owner else None,
                    is_public=public,
                    gridfs_file_id=str(file_id),
                    tags=Model3D.normalize_tags(tags),
                    asset_category=Model3D.normalize_category("animation"),
                    asset_styles=Model3D.normalize_tags(["humanoid", "vrm"]),
                    asset_types=Model3D.normalize_tags(["animation", "humanoid", ext]),
                    runtime_metadata=Model3D.normalize_runtime_metadata({
                        "animations": [{"name": title}],
                        "behaviors": ["avatar-animation"],
                        "upload": {
                            "source": "vrma-library-import",
                            "content_hash": digest,
                        },
                    }),
                )
                model.save()
                if ext in {"fbx", "bvh"}:
                    from app.conversion import enqueue
                    enqueue(model, enabled=app.config.get("ENABLE_CONVERSION", True))
            except Exception as error:
                failed += 1
                print(f"[{index}] fail {path}: {str(error)[:300]}")

        print(f"Done. imported={changed} skipped={skipped} failed={failed} dry_run={dry_run}")


def import_library_via_api(sources, manifests, api_base, token_env, public=True, dry_run=False, limit=None, formats=None):
    try:
        import requests
    except ImportError as error:
        raise SystemExit("API mode requires the requests package.") from error

    token = os.environ.get(token_env or "")
    if not token and not dry_run:
        raise SystemExit(f"Set {token_env} to a bearer token before running API import.")

    manifest = _load_manifest(manifests)
    api_base = api_base.rstrip("/")
    formats = formats or ["vrma"]
    changed = skipped = failed = 0
    for index, path in enumerate(_iter_animation_files(sources, formats), 1):
        if limit and (changed + skipped + failed) >= limit:
            break
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            title, description, category, tags = _file_metadata(path, manifest)
            ext = path.suffix.lower().lstrip(".")
            changed += 1
            print(f"[{index}] upload {title!r} to {api_base} from {path}")
            if dry_run:
                continue
            runtime_metadata = {
                "animations": [{"name": title}],
                "behaviors": ["avatar-animation"],
                "upload": {
                    "source": "vrma-library-import",
                    "content_hash": digest,
                },
            }
            with path.open("rb") as handle:
                response = requests.post(
                    f"{api_base}/api/upload",
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "name": title,
                        "description": description,
                        "is_public": "true" if public else "false",
                        "tags": ",".join(tags),
                        "asset_category": "animation",
                        "asset_styles": "humanoid,vrm",
                        "asset_types": f"animation,humanoid,{ext}",
                        "runtime_metadata": json.dumps(runtime_metadata),
                    },
                    files={"file": (path.name, handle, "application/octet-stream")},
                    timeout=90,
                )
            if response.status_code == 409 or "duplicate" in response.text.lower():
                skipped += 1
                changed -= 1
                print(f"[{index}] skip duplicate {path.name}: HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                failed += 1
                changed -= 1
                print(f"[{index}] fail {path.name}: HTTP {response.status_code} {response.text[:300]}")
                continue
        except Exception as error:
            failed += 1
            print(f"[{index}] fail {path}: {str(error)[:300]}")

    print(f"Done. uploaded={changed} skipped={skipped} failed={failed} dry_run={dry_run}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", type=Path, help="VRMA file or directory. Repeatable.")
    parser.add_argument("--manifest", action="append", type=Path, help="animation-list.json file. Repeatable.")
    parser.add_argument("--format", action="append", choices=["vrma", "fbx", "bvh", "all"], help="Animation source format to import. Repeatable. Default: vrma.")
    parser.add_argument("--owner-username", help="Asset owner username, e.g. lisa.")
    parser.add_argument("--private", action="store_true", help="Import as private assets.")
    parser.add_argument("--api-base", help="Upload through a live asset-store API instead of direct DB/FILE_STORE.")
    parser.add_argument("--api-token-env", default="ASSET_MANAGER_IMPORT_TOKEN", help="Environment variable holding the bearer token for --api-base.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    raw_formats = args.format or ["vrma"]
    formats = ["vrma", "fbx", "bvh"] if "all" in raw_formats else raw_formats
    if args.source:
        sources = args.source
    elif formats == ["vrma"]:
        sources = [path for path in DEFAULT_SOURCES if path.exists()]
    elif formats == ["fbx"]:
        sources = [path for path in DEFAULT_FBX_SOURCES if path.exists()]
    else:
        sources = [path for path in [*DEFAULT_SOURCES, *DEFAULT_FBX_SOURCES] if path.exists()]
    manifests = args.manifest or [path for path in DEFAULT_MANIFESTS if path.exists()]
    if not sources:
        raise SystemExit("No VRMA sources found. Pass --source.")
    if args.api_base:
        import_library_via_api(
            sources=sources,
            manifests=manifests,
            api_base=args.api_base,
            token_env=args.api_token_env,
            public=not args.private,
            dry_run=args.dry_run,
            limit=args.limit,
            formats=formats,
        )
        return
    import_library(
        sources=sources,
        manifests=manifests,
        owner_username=args.owner_username,
        public=not args.private,
        dry_run=args.dry_run,
        limit=args.limit,
        formats=formats,
    )


if __name__ == "__main__":
    main()
