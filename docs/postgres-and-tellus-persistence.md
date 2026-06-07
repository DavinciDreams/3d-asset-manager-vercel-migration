# Postgres and Tellus Persistence

The asset manager now uses Postgres as the durable catalog for users, asset
metadata, object-storage keys, and persistent worlds. MinIO stores the heavy
asset binaries and derived artifacts. Tellus can keep using its Cloudflare
Durable Object for live WebSocket coordination while saving snapshots here as
the long-lived source of truth.

## Runtime Roles

- Cloudflare Durable Object: live multiplayer room, WebSockets, transient
  presence, immediate terrain/object coordination.
- Postgres asset manager API: users, public/private asset metadata,
  public/private worlds, world snapshots, generated object state, queued
  generation jobs, and MinIO object keys.
- MinIO object storage: model binaries, thumbnails, previews, generated assets,
  mesh-optimization outputs, and compressed derivatives.
- EvoFlow Worlds: can be imported later as another world `source` value, with
  its genome/snapshot stored in the same `world_states.state` JSON column.

## Environment

Set these on the Flask/API deployment:

```bash
POSTGRES_PASSWORD=long-random-postgres-password
DATABASE_URL=postgresql://user:password@host:5432/3d_asset_manager
MINIO_ROOT_USER=asset-manager
MINIO_ROOT_PASSWORD=long-random-minio-password
S3_BUCKET=asset-manager
TELLUS_PERSISTENCE_API_TOKEN=long-random-shared-secret
SECRET_KEY=long-random-flask-secret
```

When using the included `docker-compose.yml`, Coolify only needs
`POSTGRES_PASSWORD` plus the MinIO credentials; the app service builds
`DATABASE_URL` and `S3_ENDPOINT_URL` from the internal compose service names:

```text
postgresql://asset_manager:${POSTGRES_PASSWORD}@postgres:5432/3d_asset_manager
http://minio:9000
```

Set these on the Tellus Cloudflare Worker:

```bash
TELLUS_PERSISTENCE_API_BASE=https://your-asset-manager.example.com
TELLUS_PERSISTENCE_API_TOKEN=the-same-long-random-shared-secret
```

## World API

Tellus already calls:

```text
GET /api/tellus/worlds/:worldId/state
PUT /api/tellus/worlds/:worldId/state
```

The asset manager also exposes:

```text
GET /api/tellus/worlds?per_page=24&search=forest
GET /api/tellus/worlds?user_only=true
PATCH /api/tellus/worlds/:worldId
```

World records carry `is_public`, `owner_id`, `source`, and a full JSON state
payload. The `main` Tellus world can remain public, while private worlds can be
owned by logged-in asset-manager users.

## Mongo Migration

Install dependencies, set both database URLs, then run:

```bash
python scripts/migrate_mongo_to_postgres.py
```

Or run the one-shot compose migration profile:

```bash
docker compose --profile migration run --rm migrate-mongo
```

The script migrates:

- `users`
- `models`
- old GridFS binaries into MinIO, with object metadata in `asset_files`
- `tellus_world_states`, when present, into `world_states`
