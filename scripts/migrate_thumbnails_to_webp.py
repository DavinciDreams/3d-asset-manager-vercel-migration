"""One-time migration: transcode existing PNG thumbnails to WebP.

New thumbnails are stored as WebP at upload time (see api._encode_thumbnail_webp).
This script back-fills thumbnails that were captured before that change so the
whole gallery benefits from the smaller WebP payloads.

It works against whichever FILE_STORE the app is configured for (Postgres
DatabaseFileStore or MinIO/S3 S3FileStore) -- it only uses the fs.get/put/delete
interface plus the models table.

Usage (from the project root, with the app's env vars set):

    python scripts/migrate_thumbnails_to_webp.py            # migrate
    python scripts/migrate_thumbnails_to_webp.py --dry-run  # report only

Safe to re-run: thumbnails already stored as image/webp are skipped.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.api import _encode_thumbnail_webp  # noqa: E402
from app.db import models as models_table  # noqa: E402
from sqlalchemy import select, update  # noqa: E402


def main(dry_run=False):
    app = create_app()
    with app.app_context():
        fs = app.config["FILE_STORE"]
        engine = app.config["DB_ENGINE"]

        with engine.begin() as conn:
            rows = conn.execute(
                select(models_table.c.id, models_table.c.thumbnail_file_id)
                .where(models_table.c.thumbnail_file_id.isnot(None))
            ).mappings().all()

        total = len(rows)
        converted = skipped = failed = 0
        print(f"Found {total} model(s) with a thumbnail.")

        for row in rows:
            model_id = row["id"]
            thumb_id = row["thumbnail_file_id"]
            try:
                stored = fs.get(thumb_id)
            except Exception as e:
                print(f"  [skip] {model_id}: cannot read thumbnail {thumb_id}: {e}")
                failed += 1
                continue

            content_type = (getattr(stored, "content_type", None) or "").lower()
            if content_type == "image/webp":
                skipped += 1
                continue

            data = stored.read() or b""
            if not data:
                print(f"  [skip] {model_id}: empty thumbnail")
                skipped += 1
                continue

            webp_bytes, webp_ct, webp_ext = _encode_thumbnail_webp(data)
            if webp_ct != "image/webp":
                # Pillow missing or decode failed; leave the PNG in place.
                print(f"  [skip] {model_id}: could not encode WebP (got {webp_ct})")
                skipped += 1
                continue

            saved = len(data) - len(webp_bytes)
            print(f"  [conv] {model_id}: {len(data)} -> {len(webp_bytes)} bytes "
                  f"({saved} saved){' (dry-run)' if dry_run else ''}")

            if dry_run:
                converted += 1
                continue

            try:
                new_id = fs.put(
                    webp_bytes,
                    filename=f"thumb_{model_id}.webp",
                    content_type="image/webp",
                    metadata={"model_id": model_id, "kind": "thumbnail"},
                )
                with engine.begin() as conn:
                    conn.execute(
                        update(models_table)
                        .where(models_table.c.id == str(model_id))
                        .values(thumbnail_file_id=str(new_id))
                    )
                # Only delete the old file after the pointer is updated.
                try:
                    fs.delete(thumb_id)
                except Exception as e:
                    print(f"    (warning) old thumbnail {thumb_id} not deleted: {e}")
                converted += 1
            except Exception as e:
                print(f"  [fail] {model_id}: {e}")
                failed += 1

        print(f"\nDone. converted={converted} skipped={skipped} failed={failed} "
              f"(of {total}){' [DRY RUN]' if dry_run else ''}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
