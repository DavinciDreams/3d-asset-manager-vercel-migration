# Production Pipeline Safety

The background asset reconciler can perform catalog-wide work: game optimization,
LOD generation, impostor generation, media repair, conversion requeueing, and AI
enrichment. LOD generation is safe to leave enabled with a one-asset pass limit:
all Gunicorn processes share one PostgreSQL advisory claim, so only one asset can
generate LODs at a time.

Recommended safe defaults:

```env
PIPELINE_RECONCILER_WORKER=1
AUTO_LOD_OPTIMIZE=1
AUTO_IMPOSTOR_GENERATE=0
PIPELINE_LOD_LIMIT=1
PIPELINE_IMPOSTOR_LIMIT=1
```

Keep impostor generation opt-in and retain the deployment platform's container
memory limit. `PIPELINE_LOD_LIMIT=1` controls how many stale candidates a pass
selects; the database claim is the cross-process concurrency authority.

LOD staleness is versioned per level. Changing the LOD1 profile queues only LOD1;
changing the far palette profiles queues only LOD2/LOD3. Do not remove the
per-level `profile_version` values from `LOD_OPTIMIZE_LEVELS`.

Manual single-asset palette and impostor rebuilds remain useful for inspection:

```bash
curl -X POST "$BASE_URL/api/model/$MODEL_ID/lod/rebuild" \
  -H "Authorization: Bearer $ASSET_MANAGER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_palette": true}'

curl -X POST "$BASE_URL/api/model/$MODEL_ID/impostor/rebuild" \
  -H "Authorization: Bearer $ASSET_MANAGER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"size": 512}'
```

The source-palette LOD path is bounded by env vars:

```env
SOURCE_PALETTE_MAX_SOURCE_MB=96
SOURCE_PALETTE_CLUSTER_SAMPLES=50000
SOURCE_PALETTE_SOURCE_SAMPLES=12000
SOURCE_PALETTE_MAX_TARGET_FACES=60000
SOURCE_PALETTE_TRANSFER_CHUNK=256
```

Raise these only after watching memory for a representative asset.

The dashboard's **Backfill stale LODs** action is stale-only. `force=true` remains
available to API administrators for deliberate full regeneration, but the
dashboard does not use it.
