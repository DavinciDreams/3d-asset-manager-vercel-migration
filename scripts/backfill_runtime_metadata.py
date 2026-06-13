"""Backfill derived runtime metadata for existing GLB/GLTF assets.

Adds any missing structural metadata that new uploads now derive by default:
mesh stats, physical bounds/dimensions, rigged/animated asset types, and
animation clip names. Safe to re-run; existing metadata wins.

Usage:
    python scripts/backfill_runtime_metadata.py --dry-run
    python scripts/backfill_runtime_metadata.py
    python scripts/backfill_runtime_metadata.py --limit 50
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.api import _file_derived_metadata, _merge_runtime_metadata, _merge_tags  # noqa: E402
from app.db import models as models_table  # noqa: E402
from app.models import Model3D  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _arg_value(flag, default=None, cast=str):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return cast(sys.argv[i + 1])
            except (TypeError, ValueError):
                return default
    return default


def main(dry_run=False, limit=None):
    app = create_app()
    with app.app_context():
        engine = app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(
                select(models_table.c.id, models_table.c.name, models_table.c.file_format)
                .where(models_table.c.file_format.in_(["glb", "gltf"]))
                .order_by(models_table.c.upload_date.desc())
            ).mappings().all()

        if limit:
            rows = rows[:limit]

        changed = skipped = failed = 0
        print(f"Scanning {len(rows)} GLB/GLTF model(s). dry_run={dry_run}")
        for idx, row in enumerate(rows, 1):
            model = Model3D.get_by_id(row["id"])
            if not model:
                skipped += 1
                continue
            try:
                data, fmt = model.get_viewable_data()
                if not data:
                    skipped += 1
                    print(f"[{idx}] skip {row['name']!r}: no viewable data")
                    continue
                derived_types, derived_runtime = _file_derived_metadata(data, (fmt or model.file_format or "").lower())
                next_runtime = _merge_runtime_metadata(model.runtime_metadata, derived_runtime)
                next_types = _merge_tags(model.asset_types, derived_types)
                if next_runtime == (model.runtime_metadata or {}) and next_types == (model.asset_types or []):
                    skipped += 1
                    continue
                changed += 1
                print(f"[{idx}] update {row['name']!r} ({model.id})")
                if dry_run:
                    continue
                model.runtime_metadata = next_runtime
                model.asset_types = next_types
                model.save()
            except Exception as error:
                failed += 1
                print(f"[{idx}] fail {row['name']!r} ({row['id']}): {str(error)[:200]}")

        print(f"Done. changed={changed} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv, limit=_arg_value("--limit", cast=int))
