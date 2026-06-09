"""One-off: delete specific models by exact name.

Uses Model3D.delete(), which (as of the FK-cleanup fix) also removes the
model's variants and clears optimization_jobs that reference it -- the reason
these could not be deleted from the UI on Postgres.

Usage (from project root, with the app's env vars set):

    python scripts/delete_models_by_name.py --dry-run   # show what would go
    python scripts/delete_models_by_name.py             # actually delete

Edit TARGET_NAMES below to change the list.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import models as models_table  # noqa: E402
from app.models import Model3D  # noqa: E402
from sqlalchemy import select  # noqa: E402

TARGET_NAMES = [
    "Butterfly Mariposa Lily (Game Optimized)",
    "Pink Camellia (Game Optimized)",
    "seasonal white oak v1 ref summer",
]


def main(dry_run=False):
    app = create_app()
    with app.app_context():
        engine = app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(
                select(models_table.c.id, models_table.c.name)
                .where(models_table.c.name.in_(TARGET_NAMES))
            ).mappings().all()

        if not rows:
            print("No matching models found.")
            return

        # Group by name so we can warn on duplicates / report misses.
        found_names = {}
        for row in rows:
            found_names.setdefault(row["name"], []).append(row["id"])

        for name in TARGET_NAMES:
            ids = found_names.get(name)
            if not ids:
                print(f"  [miss] {name!r}: not found")
            elif len(ids) > 1:
                print(f"  [warn] {name!r}: {len(ids)} matches -> {ids}")

        print(f"\n{len(rows)} model(s) matched. {'(dry-run)' if dry_run else ''}")
        deleted = failed = 0
        for row in rows:
            model_id, name = row["id"], row["name"]
            if dry_run:
                print(f"  would delete: {name!r} ({model_id})")
                continue
            try:
                model = Model3D.get_by_id(model_id)
                if not model:
                    print(f"  [miss] {model_id}: vanished before delete")
                    continue
                model.delete()
                print(f"  [deleted] {name!r} ({model_id})")
                deleted += 1
            except Exception as e:
                print(f"  [fail] {name!r} ({model_id}): {e}")
                failed += 1

        if not dry_run:
            print(f"\nDone. deleted={deleted} failed={failed}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
