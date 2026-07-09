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
