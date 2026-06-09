# Project State Log — 3D Asset Manager

## Architecture Overview

A Flask web app for uploading, viewing, browsing, and optimizing 3D assets
(GLB/GLTF/VRM/VRMA/OBJ/FBX/etc.). Server-rendered Jinja templates + vanilla JS;
3D rendering is client-side via `<model-viewer>` and a custom Three.js viewer.

**Deployment:** Migrated from **Vercel → Coolify**. Backend storage migrated from
**MongoDB/GridFS → Postgres + MinIO (S3-compatible)**.

### Storage (IMPORTANT — naming is legacy)
- `current_app.config['FILE_STORE']` is chosen in `app/db.py::create_file_store()`:
  - If `S3_ENDPOINT_URL` + `S3_BUCKET` env vars set → `S3FileStore` (MinIO; bytes in
    MinIO, metadata in Postgres `asset_files` table).
  - Else → `DatabaseFileStore` (bytes in the Postgres `asset_files.data` column).
- The names `gridfs_file_id`, `fs.get/put/delete` are **leftover from the Mongo era** —
  they now hit Postgres/MinIO, NOT Mongo. `pymongo` is still in requirements.txt but
  unused by the live path (only `scripts/migrate_mongo_to_postgres.py` uses it).
- `StoredFile` exposes `.content_type`, `.size`, `.read()`, `.object_key`, `.storage_backend`.
- `fs.get_range(file_id, start, end)` → `(chunk, total, content_type)`; S3 pushes the
  Range to `get_object`, DB slices in memory.

## File Inventory

### Core app (`app/`)
- `__init__.py` — `create_app()` factory; registers blueprints: `auth_bp` (/auth),
  `main_bp` (/), `api_bp` (/api).
- `db.py` — engine + file stores (`DatabaseFileStore`, `S3FileStore`, `get_range`),
  table definitions (`asset_files`, `models`, `users`, `optimization_jobs`, etc.).
- `models.py` — `Model3D`, `User`, etc. `Model3D.file_extension` is a property == `file_format`.
- `api.py` — REST API: thumbnail/preview upload+serve, `/api/models` (browse JSON),
  game optimization jobs (`/model/<id>/optimize-game`), export, etc.
- `main.py` — page routes (index, browse, dashboard, model_detail, upload); `Pagination` helper.
- `conversion.py`, `ai_enrichment.py`, `openapi.py`, `auth.py`.

### Templates (`app/templates/`)
- `base_3d.html` — base layout + the custom **Three.js viewer** (`window.AssetThreeViewer`)
  AND the `<model-viewer>` web component. Used for GLB/Draco/meshopt.
- `_vrm_viewer.html` — `window.VRMViewer` for VRM avatars (@pixiv/three-vrm).
- `browse.html` — public gallery, **infinite scroll** + viewport-only lazy media.
- `dashboard.html` — owner's model table; thumbnails, row actions incl. optimize icon.
- `model_detail.html` — full viewer + optimize panel + animation controls.
- `_preview_capture.html` — client-side WebM preview + PNG thumbnail capture.

### Scripts (`scripts/`)
- `migrate_mongo_to_postgres.py` — historical Mongo→Postgres data migration.
- `migrate_thumbnails_to_webp.py` — back-fill existing PNG thumbnails → WebP (`--dry-run`).

## Current Status

### Working Features (verified on `main`, 2026-06-09)
- ✅ Three.js viewer brightness matches `<model-viewer>` — `NeutralToneMapping` +
  `RoomEnvironment` IBL in both `base_3d.html` and `_vrm_viewer.html`.
- ✅ Preview/thumbnail framing — `frameObject()` fits bounding sphere vs min(vFov,hFov),
  centered, ~15% padding (`padding = 1.15`).
- ✅ Dashboard thumbnails fill (object-cover), 96px, title column 22% with wrap.
- ✅ Dashboard one-click "Optimize for Game" icon (GLB/GLTF only).
- ✅ Browse infinite scroll via `GET /api/models` + IntersectionObserver; videos
  load/play only in viewport, pause when off-screen.
- ✅ WebP thumbnails at upload (Pillow); ETag + immutable cache on thumbnails/previews;
  HTTP Range (206) streaming on preview video.

### Known Issues / TODO after deploy
- ⚠️ Deploy must `pip install` updated requirements (Pillow) or new thumbnails stay PNG.
- ⚠️ Run `python scripts/migrate_thumbnails_to_webp.py` on Coolify to convert existing PNGs.
- Range handling does a tiny probe + real fetch (2 calls); fine for small WebM, cheap on S3.

## Dependencies
- Flask 3.1, SQLAlchemy 2.0, psycopg (Postgres), boto3 (MinIO/S3), Pillow (WebP), gunicorn.
- Frontend CDN: three@0.184.0, model-viewer@4.0.0, @pixiv/three-vrm@3.5.3, Tailwind, FontAwesome.

## Development Notes
- Windows dev env; PowerShell. No JS build step — templates are served directly, just reload.
- Live site: 3d.flobots.xyz (Coolify). Default branch: `main` (auto-deploys).
- Thumbnail/preview file ids are **immutable per capture** — used as strong ETags;
  regenerating creates a new id which busts caches.

## Recent Changes

### 2026-06-09 — Viewer/dashboard/browse/serving (PRs #6, #7)
**Files:** base_3d.html, _vrm_viewer.html, dashboard.html, browse.html, api.py, db.py,
main.py, requirements.txt, scripts/migrate_thumbnails_to_webp.py
**Note:** PR #6 squash-merge only captured the first (viewer) commit; the rest landed via PR #7.
**Status:** ✅ Verified on `main` (py_compile, Jinja parse, WebP encode test all pass).
