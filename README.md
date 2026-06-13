# 3D Asset Manager

A Flask-based 3D model management platform with Postgres-backed assets and
world-state persistence for Tellus/EvoFlow-style worlds.

## Technology Stack

- Backend: Python Flask
- Database: Postgres via SQLAlchemy
- File storage: MinIO/S3-compatible object storage
- Frontend: HTML5, Tailwind CSS, JavaScript
- 3D viewer: Google Model-Viewer.js
- Deployment: Coolify or another Flask-capable container host

## Environment

Copy `.env.example` to `.env` and configure:

```bash
POSTGRES_PASSWORD=long-random-postgres-password
MINIO_ROOT_USER=asset-manager
MINIO_ROOT_PASSWORD=long-random-minio-password
S3_BUCKET=asset-manager
TELLUS_PERSISTENCE_API_TOKEN=long-random-shared-secret
SECRET_KEY=long-random-flask-secret
```

When running with `docker-compose.yml`, `DATABASE_URL` is built automatically
from `POSTGRES_PASSWORD` and the internal `postgres` service name. For an
external database, set `DATABASE_URL` directly instead.

If `DATABASE_URL` is omitted in development, the app falls back to local SQLite
at `asset_manager_dev.sqlite3`.

## Coolify Docker Compose

The repo includes `docker-compose.yml` with:

- `app`: the Flask/Gunicorn asset manager
- `postgres`: Postgres 16 with a persistent `postgres_data` volume
- `minio`: S3-compatible object storage with a persistent `minio_data` volume
- `migrate-mongo`: optional one-shot migration profile

For Coolify, set at least:

```bash
POSTGRES_PASSWORD=long-random-postgres-password
MINIO_ROOT_USER=asset-manager
MINIO_ROOT_PASSWORD=long-random-minio-password
S3_BUCKET=asset-manager
SECRET_KEY=long-random-flask-secret
TELLUS_PERSISTENCE_API_TOKEN=long-random-shared-secret
MAX_UPLOAD_MB=100
```

The app stores model binaries, thumbnails, previews, generated assets, and
future optimized/compressed derivatives in MinIO. Postgres stores the metadata,
ownership, visibility, world snapshots, and MinIO object keys.

## Upload API Keys

Logged-in users can create upload API keys from Profile. Keys are shown once,
stored only as SHA-256 hashes, and can be revoked from the same page. Use them
for scripts, Tellus, or other asset tooling:

```bash
curl -X POST "https://your-asset-manager.example.com/api/upload" \
  -H "Authorization: Bearer tam_your-api-key" \
  -F "file=@model.glb" \
  -F "name=Golden Apple Tree" \
  -F "is_public=true" \
  -F "tags=tellus,generated"
```

Tellus should store one of these generated keys in its generation backend as
`TELLUS_ASSET_STORE_UPLOAD_TOKEN`. The asset manager resolves that key to the
owning user, so generated assets persist in that user's inventory and the shared
asset library.

For trusted automation that needs to operate across accounts, use a configured
service token (`ASSET_MANAGER_API_TOKEN`, `API_UPLOAD_TOKEN`,
`TELLUS_PERSISTENCE_API_TOKEN`, or `TELLUS_ADMIN_API_TOKEN`) and set
`X-Asset-Username` or `X-Asset-User-Id` on upload/search/world persistence
requests. For example, Tellus can upload as `rsafier` with:

```bash
curl -X POST "https://your-asset-manager.example.com/api/upload" \
  -H "Authorization: Bearer your-service-token" \
  -H "X-Asset-Username: rsafier" \
  -F "file=@generated.glb" \
  -F "name=Generated Prop" \
  -F "tags=tellus,generated"
```

Service-token searches can use
`/api/models?include_private=true&search=instant%20mesh` to search public and
private asset metadata across all owners, or `/api/models?user_only=true` with
`X-Asset-Username` to inspect one account.

In-world generators such as Instant Mesh should use `TELLUS_ADMIN_API_TOKEN`.
Set `TELLUS_ADMIN_USERNAME` or `TELLUS_ADMIN_USER_ID` to the asset-manager
account that should own default in-world generations. If a generation should
belong to a specific player account, Tellus can still override the owner per
request with `X-Asset-Username` or `X-Asset-User-Id`. Uploads made with the
Tellus admin token automatically receive `tellus`, `generated`, and
`in-world-generation` tags plus the `generated` asset type so they register in
asset-store and Tellus search even when titles differ.

## AI Metadata Enrichment

The asset manager can generate catalog titles, descriptions, and tags from model
metadata and stored preview thumbnails. For Hyades Vision Services, use the A2A
JSON-RPC endpoint with the `holo` vision model:

Generated metadata is shaped for Fab-style 3D listings: titles stay under the
80-character listing limit, descriptions use buyer-facing product copy, and tags
focus on discoverability rather than file formats, generator names, or pipeline
provenance.

```bash
AI_AUTOTAG_PROVIDER=hyades
AI_AUTOTAG_API_KEY=your-hyades-api-key
AI_AUTOTAG_BASE_URL=https://hyades.gnostr.cloud/a2a
AI_AUTOTAG_MODEL=holo
AI_AUTOTAG_TRANSPORT=a2a
AI_AUTOTAG_ON_UPLOAD=1
AI_AUTOTAG_USE_VISION=1
```

Without an API key, `/api/model/:id/ai/autotag` falls back to deterministic
filename/metadata tags for local development and tests. Dashboard rows include a
robot action that triggers the same enrichment endpoint and updates the visible
title, description, and tags.

Hyades also exposes an OpenAI-compatible `/v1` surface for chat/image/audio
models. If you want to use that path instead of A2A, set:

```bash
AI_AUTOTAG_BASE_URL=https://hyades.gnostr.cloud/v1
AI_AUTOTAG_TRANSPORT=openai
```

Z.AI remains supported for text-only Coding Plan enrichment:

```bash
AI_AUTOTAG_PROVIDER=zai
AI_AUTOTAG_BASE_URL=https://api.z.ai/api/coding/paas/v4
AI_AUTOTAG_MODEL=glm-5.1
AI_AUTOTAG_USE_VISION=0
```

Z.AI Vision MCP is separate from the Flask app's HTTP enrichment call. For
Claude Code or another MCP-capable client, configure the local MCP server:

```bash
claude mcp add -s user zai-mcp-server --env Z_AI_API_KEY=your-zai-api-key Z_AI_MODE=ZAI -- npx -y "@z_ai/mcp-server"
```

The Vision MCP server requires Node.js 22 or newer. Use it from the coding
client by referencing local image/video paths; the Flask enrichment endpoint can
also include stored thumbnails when the selected OpenAI-compatible model accepts
image content.

## Runtime Metadata for Tellus

Asset API responses include a `runtime_metadata` object for in-world behavior
hints. Tellus can use this when it places models from the OpenAPI/tool surface.
For example, lantern-like assets may be enriched or edited to include:

```json
{
  "behaviors": ["light-emitter"],
  "light": {
    "enabled": true,
    "type": "point",
    "color": "#ffb35a",
    "intensity": 1.5,
    "range": 8,
    "cast_shadow": true,
    "attach_to": "",
    "offset": [0, 0.6, 0]
  }
}
```

When `runtime_metadata.light.enabled` is true, the Tellus Three.js loader should
attach a matching `THREE.PointLight`, `THREE.SpotLight`, or other supported light
to the named `attach_to` node when present, otherwise to the model root with the
given offset. The asset manager treats this as metadata only; the world runtime
owns the actual light creation.

## Tellus Persistence

Tellus should keep using Cloudflare Durable Objects for live WebSocket
coordination and multiplayer room state. Configure the Durable Object to save
durable snapshots to this API:

```bash
TELLUS_PERSISTENCE_API_BASE=https://your-asset-manager.example.com
TELLUS_PERSISTENCE_API_TOKEN=the-same-long-random-shared-secret
```

The Flask API exposes:

```text
GET /api/tellus/worlds
GET /api/tellus/worlds/:worldId/state
PUT /api/tellus/worlds/:worldId/state
PATCH /api/tellus/worlds/:worldId
```

Worlds support `is_public`, ownership, a `source` field, and a JSON snapshot.
EvoFlow Worlds can be added as another `source` later.

## Mongo Migration

Set the old `MONGODB_URI` and new `DATABASE_URL`, then run:

```bash
python scripts/migrate_mongo_to_postgres.py
```

With Docker Compose, set `MONGODB_URI` to the old Mongo service connection
string and run:

```bash
docker compose --profile migration run --rm migrate-mongo
```

The script migrates users, model metadata, old GridFS binaries into MinIO, and
existing Tellus world snapshots if the Mongo collection exists.
