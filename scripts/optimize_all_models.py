"""Backfill: generate a game-optimized GLB variant for every GLB/GLTF model
that doesn't already have one.

New uploads auto-optimize (api._maybe_autostart_game_optimization); this script
back-fills assets that existed before that. It runs the optimizer SYNCHRONOUSLY,
one model at a time, so it is safe to run as a one-off on Coolify (no reliance
on background threads that would die when the script exits).

Requires gltfpack on PATH (same as the live optimizer).

Usage (from project root, with the app's env vars set):

    python scripts/optimize_all_models.py --dry-run   # list what would run
    python scripts/optimize_all_models.py             # optimize all
    python scripts/optimize_all_models.py --limit 20  # cap how many this run

Safe to re-run: models that already have a 'game' variant are skipped.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.api import _run_game_optimizer, GAME_OPTIMIZE_DEFAULTS  # noqa: E402
from app.db import models as models_table  # noqa: E402
from app.models import Model3D, ModelVariant  # noqa: E402
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
        import shutil
        if not shutil.which('gltfpack') and not dry_run:
            print('ERROR: gltfpack is not on PATH; cannot optimize. Aborting.')
            return

        engine = app.config['DB_ENGINE']
        with engine.begin() as conn:
            rows = conn.execute(
                select(models_table.c.id, models_table.c.name, models_table.c.file_format)
                .where(models_table.c.file_format.in_(['glb', 'gltf']))
            ).mappings().all()

        # Filter out models that already have a 'game' variant.
        todo = []
        already = 0
        for row in rows:
            if ModelVariant.get(row['id'], 'game'):
                already += 1
                continue
            todo.append(row)

        total_glb = len(rows)
        print(f"GLB/GLTF models: {total_glb} | already optimized: {already} | "
              f"to optimize: {len(todo)}")
        if limit:
            todo = todo[:limit]
            print(f"Limiting this run to {len(todo)} model(s).")

        if dry_run:
            for row in todo:
                print(f"  would optimize: {row['name']!r} ({row['id']})")
            print(f"\n[DRY RUN] {len(todo)} model(s) would be optimized.")
            return

        done = failed = 0
        for idx, row in enumerate(todo, 1):
            model_id, name = row['id'], row['name']
            print(f"[{idx}/{len(todo)}] optimizing {name!r} ({model_id})...", flush=True)
            try:
                model = Model3D.get_by_id(model_id)
                if not model:
                    print("    [skip] model vanished")
                    continue
                result = _run_game_optimizer(
                    model, model.user_id, dict(GAME_OPTIMIZE_DEFAULTS))
                orig = result.get('original_size') or 0
                opt = result.get('optimized_size') or 0
                pct = (result.get('savings_ratio') or 0) * 100
                print(f"    [done] {orig/1024/1024:.1f}MB -> {opt/1024/1024:.1f}MB "
                      f"({pct:.0f}% smaller)")
                done += 1
            except Exception as e:
                print(f"    [fail] {str(e)[:200]}")
                failed += 1

        print(f"\nDone. optimized={done} failed={failed} (of {len(todo)} attempted; "
              f"{already} already had a variant)")


if __name__ == '__main__':
    main(dry_run='--dry-run' in sys.argv, limit=_arg_value('--limit', cast=int))
