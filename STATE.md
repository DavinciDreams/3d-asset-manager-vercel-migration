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

### 2026-06-09 — "Fix Eyes" blinker eyeballs (client-side bake)
**Why:** Image-to-3D pipeline produces character models with holes/black voids in
the eye sockets (reconstruction can't resolve dark recessed regions). Feature lets
the owner drop in eyeballs to cover the holes and bake them into a downloadable GLB.

**Key decision — baking happens CLIENT-SIDE in the browser, not server-side.**
The source GLBs are `EXT_meshopt_compression` + `KHR_mesh_quantization` + `EXT_texture_webp`
(gltfpack `-cc` output, emitted by the pipeline itself — original == optimized file).
**trimesh CANNOT read these** (IndexError on compressed buffers). The viewer ALREADY
decodes them (MeshoptDecoder/KTX2). So eyes are added in Three.js and exported with
`GLTFExporter`; the server just stores the uploaded bytes. No trimesh/gltfpack/Blender,
no new Python deps.

**Eye node structure (3 nested nodes per eye — decouples all concerns):**
`wrapper` (position + SIZE, axis-aligned) → `blinkNode` (blink scale.y, axis-aligned,
so squash is always world-vertical regardless of aim) → `mesh` (eyeball, carries the
per-eye LOOK rotation). Each held in a `FixedEyes` group. This layering is why size,
blink, and gaze never fight each other.

**Files changed:**
- `base_3d.html` — `GLTFExporter` import; `buildEyeballMesh()` (sphere, sclera/iris/
  pupil via VERTEX COLORS, no texture; iris cap small (0.28) so white shows at sides);
  `api.eyes` editor: add/select/drag(mirror)/setSizeScale/setSpacing/setVertical/
  setDepth/setColors/setLook(yaw,pitch,both)/flipFacing/previewBlink/exportGLB,
  plus getters (baseRadius, center, mirror, selectedYaw/Pitch). `eyeMixer` for live
  blink wired into `animate()`; pauses model auto-rotate while editing; eye cleanup
  in `dispose()`.
  - Blink = baked `THREE.AnimationClip('EyeBlink')` squashing the blinkNode scale.y;
    keyframes use scale 1 (independent of size). Autoplays in any glTF viewer.
  - Mirror mode (default on): dragging one eye mirrors the other across model X
    center → stays level/parallel/equal-depth. Per-eye yaw/pitch stored in
    `wrap.userData` so gaze can be corrected per eye when symmetry is off.
- `model_detail.html` — owner-only "Fix Eyes" button; FLOATING overlay panel
  (fixed top-right, own scroll, close ✕) with: Editing L/R, Symmetry toggle, sliders
  (size/spacing/height/depth), iris+sclera pickers, Flip facing + yaw/pitch aim,
  blink toggle, Undo, Bake/Cancel. Pointer-drag wiring; "Fixed Eyes" entry in variant
  toggle + Export menu; `revealFixedEyes()`; `switchVariant()` handles 3 variants;
  `loadDetailModel()` srcUrl handles `fixed_eyes`. Sliders/symmetry reset on Add Eyes.
  - Undo: editor keeps a 50-deep history of eye-state snapshots (pos/size/per-eye
    aim), pushed before each gesture (drag, slider, flip). Button + Ctrl/Cmd+Z.
- `api.py` — `POST /api/model/<id>/fixed-eyes` (`@login_required`, owner-only,
  validates GLB magic bytes, 200MB cap, FILE_STORE.put + `ModelVariant.upsert(id,
  'fixed_eyes',...)`, deletes old blob); `GET .../fixed-eyes` (mirror of
  get_game_optimized: Range + ETag + ?download=1). Variant kind = `'fixed_eyes'`.
- `main.py` — model_detail passes `fixed_eyes_variant`.
- `wsgi.py` — local `__main__` dev run now enables `TEMPLATES_AUTO_RELOAD` + debug +
  reloader (never runs under gunicorn) so template edits hot-reload. NOTE: Flask
  caches Jinja in-memory per process; if templates seem stale, restart the dev server.

**Verification:**
- `py_compile` (api/main/wsgi) OK; `node --check` on viewer module + detail scripts OK.
- App boots (sqlite fallback); both routes register; `url_for` resolves; owner render
  shows full panel, anon doesn't; anon POST → 302; GET missing → 404.
- ✅ **LIVE in-browser bake CONFIRMED** by the user on the fairy model (uploaded via
  the real /api/upload path): eyes placed, blink works, baked GLB saved as the
  fixed_eyes variant, viewer swapped to it. GLTFExporter round-trip on a
  meshopt/quantized/WebP source works.

**Follow-up (same PR) — variant chaining + preview priority:**
- `_run_game_optimizer` now PREFERS the `fixed_eyes` variant as its source when
  one exists (reads variant bytes; falls back to `model.get_viewable_data()`), so
  the game-optimized asset folds in the eyes + blink. Records `source_is_fixed_eyes`
  in the variant settings/metadata/result. gltfpack preserves the blink clip.
- Preview source priority across galleries (index/browse/dashboard rows):
  **game-optimized → fixed-eyes → original** (game variant is smallest AND now has
  the eyes). `main.py` sets `model.has_fixed_eyes` alongside `has_game_optimized`
  via a second batched `model_ids_with_kind('fixed_eyes', ids)` query in all three
  list paths + `_enrich_dashboard_models`.
- Detail UI: optimize panel shows a "using your fixed-eyes version" note when a
  fixed-eyes variant exists (revealed live after a bake too); optimize success
  message appends "(includes your fixed eyes & blink)"; bake success nudges
  re-optimizing if a (now-stale, eyeless) game variant already existed.
- Verified: py_compile + node --check + Jinja-compile of the 3 gallery templates;
  detail renders with the note; `url_for('api.get_fixed_eyes')` resolves.

**Status:** ✅ Complete & live-verified (core); follow-up statically verified.

### 2026-06-09 — Viewer/dashboard/browse/serving (PRs #6, #7)
**Files:** base_3d.html, _vrm_viewer.html, dashboard.html, browse.html, api.py, db.py,
main.py, requirements.txt, scripts/migrate_thumbnails_to_webp.py
**Note:** PR #6 squash-merge only captured the first (viewer) commit; the rest landed via PR #7.
**Status:** ✅ Verified on `main` (py_compile, Jinja parse, WebP encode test all pass).
