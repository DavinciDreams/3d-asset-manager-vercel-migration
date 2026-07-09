# Production Pipeline Safety

The background asset reconciler can perform catalog-wide work: game optimization,
LOD generation, impostor generation, media repair, conversion requeueing, and AI
enrichment. Keep heavyweight jobs opt-in in production.

Recommended safe defaults:

```env
PIPELINE_RECONCILER_WORKER=0
AUTO_LOD_OPTIMIZE=0
AUTO_IMPOSTOR_GENERATE=0
PIPELINE_LOD_LIMIT=1
PIPELINE_IMPOSTOR_LIMIT=1
```

Only enable `PIPELINE_RECONCILER_WORKER=1` when intentionally running background
maintenance. If enabling LOD or impostor work, set container memory limits in the
deployment platform first and start with single-job limits.

Avoid bumping `LOD_OPTIMIZE_DEFAULTS_VERSION` in the same deploy that changes a
heavy LOD implementation. A version bump makes existing variants stale and can
cause the reconciler to rebuild many assets. Roll out new LOD modes behind an
explicit flag or manual rebuild endpoint, test one asset, then widen.

Manual single-asset palette and impostor rebuilds are the safe path:

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
