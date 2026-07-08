"""OpenAPI 3.0 specification for the 3D Asset Manager API.

Hand-written spec (no extra pip dependencies) describing every JSON endpoint
registered on ``api_bp`` (mounted at ``/api``). Served as JSON at
``/api/openapi.json`` and rendered by Swagger UI at ``/api/docs``.

Keep this in sync with ``app/api.py`` when routes change.
"""

# Allowed upload extensions are duplicated from create_app() config so the spec
# stays self-contained; update both if the list changes.
ALLOWED_EXTENSIONS = ['obj', 'fbx', 'gltf', 'glb', 'dae', '3ds', 'ply', 'stl', 'vrm', 'vrma', 'bvh']


def _model_summary_schema():
    return {
        'type': 'object',
        'properties': {
            'id': {'type': 'string', 'example': '6650f1a2b3c4d5e6f7a8b9c0'},
            'name': {'type': 'string', 'example': 'Spaceship'},
            'description': {'type': 'string', 'example': 'A low-poly spaceship.'},
            'file_format': {'type': 'string', 'enum': ALLOWED_EXTENSIONS, 'example': 'glb'},
            'file_size': {'type': 'integer', 'description': 'Size in bytes', 'example': 248320},
            'effective_file_size': {
                'type': 'integer',
                'nullable': True,
                'description': 'Rendered/deployed size in bytes; prefers the game-optimized variant when present.',
            },
            'mesh_stats': {'$ref': '#/components/schemas/MeshStats'},
            'effective_mesh_stats': {
                'allOf': [{'$ref': '#/components/schemas/MeshStats'}],
                'nullable': True,
                'description': 'Rendered/deployed mesh stats; prefers the game-optimized variant when present.',
            },
            'content_hash': {
                'type': 'string',
                'nullable': True,
                'description': 'SHA-256 digest of the original uploaded model binary.',
                'example': 'f4e2a16f2c8d0cfdcf2d8a9d3b2f0af30d3c90e2e531fdb4c8d8d55c2d5a7c0f',
            },
            'original_filename': {'type': 'string', 'example': 'spaceship.glb'},
            'is_public': {'type': 'boolean', 'example': True},
            'upload_date': {'type': 'string', 'format': 'date-time', 'nullable': True},
            'download_count': {'type': 'integer', 'example': 12},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'asset_category': {'type': 'string', 'nullable': True, 'example': 'building'},
            'asset_styles': {'type': 'array', 'items': {'type': 'string'}, 'example': ['fantasy', 'stylized']},
            'asset_types': {'type': 'array', 'items': {'type': 'string'}, 'example': ['rigged', 'animated']},
            'runtime_metadata': {'$ref': '#/components/schemas/RuntimeMetadata'},
            'conversion_status': {
                'type': 'string',
                'nullable': True,
                'enum': ['pending', 'processing', 'done', 'failed', 'skipped', None],
            },
            'has_viewable': {'type': 'boolean'},
            'has_vrma': {'type': 'boolean'},
            'has_thumbnail': {'type': 'boolean'},
            'thumbnail_url': {'type': 'string', 'nullable': True},
            'has_preview': {'type': 'boolean'},
            'preview_url': {'type': 'string', 'nullable': True},
            'view_url': {
                'type': 'string',
                'nullable': True,
                'description': 'Original/viewable model URL for preview fallback when optimized or LOD variants are missing.',
            },
            'lod_preview_fallback_url': {
                'type': 'string',
                'nullable': True,
                'description': 'Preview URL consumers can use while lod_ready is false.',
            },
            'has_game_optimized': {'type': 'boolean'},
            'game_optimized': {'$ref': '#/components/schemas/GameOptimized'},
            'asset_lod_urls': {'$ref': '#/components/schemas/AssetLodUrls'},
            'lod_variants': {
                'type': 'array',
                'description': 'Generated LOD variants currently stored under this original asset id.',
                'items': {'$ref': '#/components/schemas/LodVariant'},
            },
            'has_lod_variants': {
                'type': 'boolean',
                'description': 'True when at least one LOD variant exists. Use lod_ready for full-chain readiness.',
            },
            'lod_ready': {
                'type': 'boolean',
                'description': 'True only when LOD0, LOD1, LOD2, and LOD3 are all present.',
            },
            'lod_status': {
                'type': 'string',
                'enum': ['ready', 'partial', 'missing'],
                'description': 'Full LOD-chain state for audit and consumers that need complete LOD coverage.',
            },
            'lod_available_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
            'lod_missing_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
            'lod_summary': {'$ref': '#/components/schemas/LodSummary'},
            'has_impostor': {'type': 'boolean'},
            'impostor': {'$ref': '#/components/schemas/ImpostorVariant'},
            'ai_status': {'type': 'string', 'nullable': True, 'enum': ['pending', 'processing', 'done', 'failed', None]},
            'ai_error': {'type': 'string', 'nullable': True},
            'ai_title': {'type': 'string', 'nullable': True},
            'ai_description': {'type': 'string', 'nullable': True},
            'ai_tags': {'type': 'array', 'items': {'type': 'string'}},
            'approve_game_ready': {'type': 'boolean'},
            'approve_asset_store': {'type': 'boolean'},
            'ready_for_tellus': {'type': 'boolean'},
            'catalog_ready': {'type': 'boolean'},
            'world_ready': {'type': 'boolean'},
            'storefront_ready': {'type': 'boolean'},
            'media_capture': {
                'type': 'object',
                'properties': {
                    'needs_thumbnail': {'type': 'boolean'},
                    'needs_preview': {'type': 'boolean'},
                    'status': {'type': 'string', 'enum': ['queued', 'processing', 'captured', 'failed', 'blocked', 'idle']},
                    'attempt_count': {'type': 'integer'},
                    'last_error': {'type': 'string', 'nullable': True},
                },
            },
            'processing_state': {'type': 'object'},
            'owner': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string', 'nullable': True},
                    'username': {'type': 'string', 'example': 'jane'},
                },
            },
        },
    }


def _pagination_schema():
    return {
        'type': 'object',
        'properties': {
            'page': {'type': 'integer', 'example': 1},
            'per_page': {'type': 'integer', 'example': 20},
            'total': {'type': 'integer', 'example': 57},
            'pages': {'type': 'integer', 'example': 3},
            'has_prev': {'type': 'boolean', 'example': False},
            'has_next': {'type': 'boolean', 'example': True},
        },
    }


def _error_response(description):
    return {
        'description': description,
        'content': {
            'application/json': {
                'schema': {'$ref': '#/components/schemas/Error'},
            }
        },
    }


def get_openapi_spec(base_url=''):
    """Return the OpenAPI 3.0 spec as a dict.

    ``base_url`` is the externally-visible origin (scheme + host) used to fill
    in the ``servers`` block so "Try it out" hits the right host.
    """
    server_url = (base_url.rstrip('/') + '/api') if base_url else '/api'

    return {
        'openapi': '3.0.3',
        'info': {
            'title': '3D Asset Manager API',
            'description': (
                'REST API for browsing, uploading, viewing and downloading 3D '
                'models. Authenticated endpoints accept either the session cookie '
                'set by `/auth/login` or `Authorization: Bearer <token>` when '
                'ASSET_MANAGER_API_TOKEN/API_UPLOAD_TOKEN/TELLUS_ADMIN_API_TOKEN '
                'is configured.'
            ),
            'version': '1.0.0',
        },
        'servers': [{'url': server_url, 'description': 'API root'}],
        'tags': [
            {'name': 'System', 'description': 'Health and platform statistics'},
            {'name': 'Models', 'description': 'List, view, update and delete models'},
            {'name': 'Files', 'description': 'Download and view model binaries and thumbnails'},
            {'name': 'Upload', 'description': 'Upload new models'},
            {'name': 'Workflows', 'description': 'Conversion, AI enrichment and approval workflows'},
            {'name': 'Bundles', 'description': 'Create and download multi-asset bundles'},
        ],
        'components': {
            'securitySchemes': {
                'sessionCookie': {
                    'type': 'apiKey',
                    'in': 'cookie',
                    'name': 'session',
                    'description': 'Flask-Login session cookie obtained via /auth/login.',
                },
                'uploadApiKey': {
                    'type': 'http',
                    'scheme': 'bearer',
                    'description': 'User-owned upload API key created from the Profile page.',
                },
                'bearerAuth': {
                    'type': 'http',
                    'scheme': 'bearer',
                    'description': 'Configured API token for uploads and automation.',
                },
            },
            'schemas': {
                'Error': {
                    'type': 'object',
                    'properties': {
                        'error': {'type': 'string', 'example': 'Model not found'},
                    },
                },
                'ModelSummary': _model_summary_schema(),
                'MeshStats': {
                    'type': 'object',
                    'nullable': True,
                    'properties': {
                        'vertices': {'type': 'integer', 'example': 12000},
                        'triangles': {'type': 'integer', 'example': 24000},
                        'primitives': {'type': 'integer', 'example': 3},
                    },
                },
                'RuntimeMetadata': {
                    'type': 'object',
                    'description': (
                        'Runtime hints consumed by Tellus/Three.js when an asset is placed in-world. '
                        'A lantern-like asset can set light.enabled=true so the world spawns a real THREE.Light.'
                    ),
                    'properties': {
                        'behaviors': {
                            'type': 'array',
                            'items': {'type': 'string'},
                            'example': ['light-emitter'],
                        },
                        'light': {
                            'type': 'object',
                            'properties': {
                                'enabled': {'type': 'boolean', 'example': True},
                                'type': {
                                    'type': 'string',
                                    'enum': ['none', 'point', 'spot', 'directional', 'ambient'],
                                    'example': 'point',
                                },
                                'color': {'type': 'string', 'example': '#ffb35a'},
                                'intensity': {'type': 'number', 'example': 1.5},
                                'range': {'type': 'number', 'example': 8},
                                'cast_shadow': {'type': 'boolean', 'example': True},
                                'attach_to': {
                                    'type': 'string',
                                    'description': 'Optional GLB node name; empty means attach to the asset root.',
                                    'example': 'LanternGlow',
                                },
                                'offset': {
                                    'type': 'array',
                                    'items': {'type': 'number'},
                                    'minItems': 3,
                                    'maxItems': 3,
                                    'example': [0, 0.6, 0],
                                },
                            },
                        },
                        'animations': {
                            'type': 'array',
                            'description': 'Animation clips detected directly from GLB/GLTF animation metadata.',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'name': {'type': 'string', 'example': 'Idle'},
                                    'duration': {'type': 'number', 'example': 1.75},
                                },
                            },
                        },
                        'mesh_stats': {'$ref': '#/components/schemas/MeshStats'},
                    },
                },
                'Pagination': _pagination_schema(),
                'ModelListResponse': {
                    'type': 'object',
                    'properties': {
                        'models': {
                            'type': 'array',
                            'items': {'$ref': '#/components/schemas/ModelSummary'},
                        },
                        'pagination': {'$ref': '#/components/schemas/Pagination'},
                    },
                },
                'Stats': {
                    'type': 'object',
                    'properties': {
                        'public_models': {'type': 'integer', 'example': 42},
                        'total_users': {'type': 'integer', 'example': 8},
                        'total_downloads': {'type': 'integer', 'example': 310},
                    },
                },
                'Bundle': {
                    'type': 'object',
                    'properties': {
                        'id': {'type': 'string'},
                        'name': {'type': 'string'},
                        'description': {'type': 'string'},
                        'is_public': {'type': 'boolean'},
                        'model_ids': {'type': 'array', 'items': {'type': 'string'}},
                        'tags': {'type': 'array', 'items': {'type': 'string'}},
                        'status': {'type': 'string', 'example': 'draft'},
                        'has_file': {'type': 'boolean'},
                        'metadata': {'type': 'object'},
                    },
                },
                'GameOptimized': {
                    'type': 'object',
                    'nullable': True,
                    'description': 'The game-optimized GLB variant attached to the model, or null.',
                    'properties': {
                        'size': {'type': 'integer', 'description': 'Optimized GLB size in bytes', 'example': 248320},
                        'mesh_stats': {'$ref': '#/components/schemas/MeshStats'},
                        'runtime_cost': {'$ref': '#/components/schemas/RuntimeCost'},
                        'optimization': {'type': 'object'},
                        'status': {'type': 'string', 'example': 'ready'},
                        'settings': {'type': 'object', 'description': 'gltfpack settings + size/savings used to produce it'},
                        'updated_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'url': {'type': 'string', 'description': 'Inline GLB URL', 'example': '/api/model/abc/game-optimized'},
                        'download_url': {'type': 'string', 'example': '/api/model/abc/game-optimized?download=1'},
                    },
                },
                'AssetLodUrls': {
                    'type': 'object',
                    'description': (
                        'Stable Tellus/game backend URLs for generated runtime variants. '
                        'These URLs are deterministic for every model id; fetch them and handle 404 '
                        'when a specific variant has not been generated yet.'
                    ),
                    'properties': {
                        'game_optimized': {
                            'type': 'string',
                            'example': '/api/assets/model/abc/game-optimized',
                            'description': 'Game runtime GLB. Falls back to LOD0 when no explicit game variant exists.',
                        },
                        'lod0': {'type': 'string', 'example': '/api/assets/model/abc/lod/0'},
                        'lod1': {'type': 'string', 'example': '/api/assets/model/abc/lod/1'},
                        'lod2': {'type': 'string', 'example': '/api/assets/model/abc/lod/2'},
                        'impostor': {'type': 'string', 'example': '/api/assets/model/abc/impostor'},
                    },
                },
                'LodVariant': {
                    'type': 'object',
                    'description': 'Metadata for a generated ModelVariant(kind=lod) row.',
                    'properties': {
                        'level': {'type': 'integer', 'enum': [0, 1, 2, 3]},
                        'size': {'type': 'integer', 'description': 'Variant file size in bytes'},
                        'size_mb': {'type': 'number', 'nullable': True},
                        'vertices': {'type': 'integer', 'nullable': True},
                        'triangles': {'type': 'integer', 'nullable': True},
                        'recommended_use': {
                            'type': 'string',
                            'nullable': True,
                            'enum': ['large_fill', 'general_fill', 'feature', 'hero_only', None],
                            'description': 'Heuristic suitability bucket from the cheapest LOD metrics.',
                        },
                        'file_format': {'type': 'string', 'example': 'glb'},
                        'status': {'type': 'string', 'example': 'ready'},
                        'settings': {
                            'type': 'object',
                            'description': 'LOD generation settings and runtime cost metadata. LOD1 targets mid/fill use with target_vertices=20000; LOD2 uses the same known-good textured simplification profile for visible world placement; LOD3 is the small deformed textured proxy. The impostor variant remains the preferred true far-distance representation.',
                        },
                        'mesh_stats': {'$ref': '#/components/schemas/MeshStats'},
                        'physical': {'type': 'object', 'nullable': True},
                        'runtime_cost': {'$ref': '#/components/schemas/RuntimeCost'},
                        'url': {'type': 'string', 'example': '/api/assets/model/abc/lod/1'},
                        'download_url': {'type': 'string', 'example': '/api/assets/model/abc/lod/1?download=1'},
                        'updated_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                    },
                },
                'LodSummary': {
                    'type': 'object',
                    'description': 'Compact LOD metrics for filtering assets without downloading GLBs.',
                    'properties': {
                        'ready': {'type': 'boolean'},
                        'status': {'type': 'string', 'enum': ['ready', 'partial', 'missing']},
                        'missing_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'levels': {'type': 'array', 'items': {'$ref': '#/components/schemas/LodVariant'}},
                        'cheapest_level': {'type': 'integer', 'nullable': True, 'enum': [0, 1, 2, 3, None]},
                        'cheapest_vertices': {'type': 'integer', 'nullable': True},
                        'cheapest_triangles': {'type': 'integer', 'nullable': True},
                        'cheapest_size': {'type': 'integer', 'nullable': True},
                        'cheapest_size_mb': {'type': 'number', 'nullable': True},
                        'recommended_use': {
                            'type': 'string',
                            'nullable': True,
                            'enum': ['large_fill', 'general_fill', 'feature', 'hero_only', None],
                        },
                    },
                },
                'OptimizationJob': {
                    'type': 'object',
                    'description': (
                        'Background game optimization job. A completed game optimization job '
                        'creates the game-optimized GLB, generates LOD0/LOD1/LOD2/LOD3, and then '
                        'creates a far-field impostor billboard for the same source model.'
                    ),
                    'properties': {
                        'id': {'type': 'string'},
                        'source_model_id': {'type': 'string'},
                        'status': {'type': 'string', 'enum': ['queued', 'running', 'done', 'failed']},
                        'settings': {'type': 'object'},
                        'result': {'type': 'object'},
                        'result_model_id': {'type': 'string', 'nullable': True},
                        'variant': {'type': 'object', 'nullable': True},
                        'original_size': {'type': 'integer', 'nullable': True},
                        'optimized_size': {'type': 'integer', 'nullable': True},
                        'savings_ratio': {'type': 'number', 'nullable': True},
                        'lod_result': {'type': 'object', 'nullable': True},
                        'lod_variants': {'type': 'array', 'items': {'$ref': '#/components/schemas/LodVariant'}},
                        'lod_ready': {'type': 'boolean', 'nullable': True},
                        'lod_status': {'type': 'string', 'nullable': True, 'enum': ['ready', 'partial', 'missing', None]},
                        'lod_available_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'lod_missing_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'lod_summary': {'$ref': '#/components/schemas/LodSummary'},
                        'impostor_result': {'type': 'object', 'nullable': True},
                        'has_impostor': {'type': 'boolean', 'nullable': True},
                        'impostor': {'$ref': '#/components/schemas/ImpostorVariant'},
                        'error': {'type': 'string', 'nullable': True},
                        'created_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'updated_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'started_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'finished_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                    },
                },
                'OptimizationJobResponse': {
                    'type': 'object',
                    'properties': {
                        'success': {'type': 'boolean'},
                        'queued': {'type': 'boolean'},
                        'job': {'$ref': '#/components/schemas/OptimizationJob'},
                        'status_url': {'type': 'string', 'example': '/api/model/abc/optimize-game/job-id'},
                    },
                },
                'LodBackfillStatus': {
                    'type': 'object',
                    'description': 'Admin LOD backfill progress for generating missing LOD chains in bulk.',
                    'properties': {
                        'status': {'type': 'string', 'nullable': True, 'example': 'started'},
                        'running': {'type': 'boolean'},
                        'total': {'type': 'integer'},
                        'done': {'type': 'integer'},
                        'failed': {'type': 'integer'},
                        'skipped': {'type': 'integer'},
                        'current': {'type': 'string', 'nullable': True},
                        'started_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'finished_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'last_error': {'type': 'string', 'nullable': True},
                    },
                },
                'ImpostorVariant': {
                    'type': 'object',
                    'nullable': True,
                    'description': 'Metadata for a generated impostor variant stored under the original asset id. Octahedral atlases expose grid metadata for far-distance runtime selection.',
                    'properties': {
                        'size': {'type': 'integer', 'description': 'Variant file size in bytes'},
                        'size_mb': {'type': 'number', 'nullable': True},
                        'file_format': {'type': 'string', 'example': 'webp'},
                        'status': {'type': 'string', 'example': 'ready'},
                        'settings': {'type': 'object'},
                        'type': {'type': 'string', 'nullable': True, 'enum': ['octahedral_atlas', 'billboard', None]},
                        'width': {'type': 'integer', 'nullable': True},
                        'height': {'type': 'integer', 'nullable': True},
                        'atlas_width': {'type': 'integer', 'nullable': True},
                        'atlas_height': {'type': 'integer', 'nullable': True},
                        'grid_size_x': {'type': 'integer', 'nullable': True, 'example': 31},
                        'grid_size_y': {'type': 'integer', 'nullable': True, 'example': 31},
                        'cell_size': {'type': 'integer', 'nullable': True, 'example': 66},
                        'view_count': {'type': 'integer', 'nullable': True, 'example': 961},
                        'octahedron_type': {'type': 'string', 'nullable': True, 'example': 'hemi'},
                        'source': {'type': 'string', 'nullable': True, 'example': 'octahedral_server_render'},
                        'role': {'type': 'string', 'nullable': True, 'example': 'far/octahedral'},
                        'url': {'type': 'string', 'example': '/api/assets/model/abc/impostor'},
                        'download_url': {'type': 'string', 'example': '/api/assets/model/abc/impostor?download=1'},
                        'updated_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                    },
                },
                'RuntimeCost': {
                    'type': 'object',
                    'nullable': True,
                    'description': 'Post-optimize cost metadata for Tellus load priority, LOD and memory budgeting.',
                    'properties': {
                        'triangle_count': {'type': 'integer', 'nullable': True, 'example': 24000},
                        'vertex_count': {'type': 'integer', 'nullable': True, 'example': 12000},
                        'primitive_count': {'type': 'integer', 'nullable': True, 'example': 3},
                        'texture_count': {'type': 'integer', 'example': 4},
                        'image_count': {'type': 'integer', 'example': 4},
                        'largest_texture_bytes': {'type': 'integer', 'example': 524288},
                        'total_texture_bytes': {'type': 'integer', 'example': 1048576},
                        'geometry_buffer_bytes': {'type': 'integer', 'example': 320000},
                        'texture_vram_bytes': {'type': 'integer', 'example': 1048576},
                        'approx_vram_bytes': {'type': 'integer', 'example': 1368576},
                        'total_byte_size': {'type': 'integer', 'example': 248320},
                        'ktx2': {'type': 'boolean', 'example': True},
                        'ktx2_produced': {'type': 'boolean', 'example': True},
                        'meshopt': {'type': 'boolean', 'example': True},
                        'preset': {'type': 'string', 'example': 'balanced'},
                        'defaults_version': {'type': 'string', 'example': '2026-06-17'},
                    },
                },
                'MediaSummary': {
                    'type': 'object',
                    'description': 'All media URLs for a model in one response.',
                    'properties': {
                        'id': {'type': 'string'},
                        'name': {'type': 'string'},
                        'file_format': {'type': 'string', 'enum': ALLOWED_EXTENSIONS},
                        'conversion_status': {
                            'type': 'string', 'nullable': True,
                            'enum': ['pending', 'processing', 'done', 'failed', 'skipped', None],
                        },
                        'image': {
                            'type': 'object',
                            'description': 'Still thumbnail image.',
                            'properties': {
                                'has': {'type': 'boolean'},
                                'url': {'type': 'string', 'nullable': True},
                                'content_type': {'type': 'string', 'example': 'image/webp'},
                            },
                        },
                        'video': {
                            'type': 'object',
                            'description': 'Rotating preview video (supports HTTP Range).',
                            'properties': {
                                'has': {'type': 'boolean'},
                                'url': {'type': 'string', 'nullable': True},
                                'content_type': {'type': 'string', 'example': 'video/webm'},
                                'supports_range': {'type': 'boolean'},
                            },
                        },
                        'model': {
                            'type': 'object',
                            'description': 'The renderable / downloadable model file.',
                            'properties': {
                                'viewable': {'type': 'boolean'},
                                'view_url': {'type': 'string', 'nullable': True},
                                'download_url': {'type': 'string'},
                                'file_format': {'type': 'string'},
                            },
                        },
                        'game_optimized': {'$ref': '#/components/schemas/GameOptimized'},
                        'has_game_optimized': {'type': 'boolean'},
                        'asset_lod_urls': {'$ref': '#/components/schemas/AssetLodUrls'},
                        'lod_variants': {'type': 'array', 'items': {'$ref': '#/components/schemas/LodVariant'}},
                        'has_lod_variants': {'type': 'boolean'},
                        'lod_ready': {'type': 'boolean'},
                        'lod_status': {'type': 'string', 'enum': ['ready', 'partial', 'missing']},
                        'lod_available_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'lod_missing_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'lod_summary': {'$ref': '#/components/schemas/LodSummary'},
                        'has_impostor': {'type': 'boolean'},
                        'impostor': {'$ref': '#/components/schemas/ImpostorVariant'},
                    },
                },
                'BrowseCard': {
                    'type': 'object',
                    'description': 'Compact card payload for the browse gallery.',
                    'properties': {
                        'id': {'type': 'string'},
                        'name': {'type': 'string'},
                        'file_format': {'type': 'string'},
                        'conversion_status': {'type': 'string', 'nullable': True},
                        'download_count': {'type': 'integer'},
                        'owner_username': {'type': 'string'},
                        'tags': {'type': 'array', 'items': {'type': 'string'}},
                        'has_preview': {'type': 'boolean'},
                        'has_thumbnail': {'type': 'boolean'},
                        'preview_url': {'type': 'string', 'nullable': True},
                        'thumbnail_url': {'type': 'string', 'nullable': True},
                        'detail_url': {'type': 'string'},
                        'is_owner': {'type': 'boolean'},
                        'viewable': {'type': 'boolean'},
                        'view_url': {'type': 'string', 'nullable': True},
                        'lod_ready': {'type': 'boolean'},
                        'lod_status': {'type': 'string', 'enum': ['ready', 'partial', 'missing']},
                        'lod_available_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'lod_missing_levels': {'type': 'array', 'items': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        'lod_summary': {'$ref': '#/components/schemas/LodSummary'},
                    },
                },
                'BrowseListResponse': {
                    'type': 'object',
                    'properties': {
                        'models': {'type': 'array', 'items': {'$ref': '#/components/schemas/BrowseCard'}},
                        'page': {'type': 'integer', 'example': 1},
                        'per_page': {'type': 'integer', 'example': 24},
                        'total': {'type': 'integer', 'example': 57},
                        'pages': {'type': 'integer', 'example': 3},
                        'has_next': {'type': 'boolean'},
                    },
                },
            },
        },
        'paths': {
            '/test': {
                'get': {
                    'tags': ['System'],
                    'summary': 'Health check',
                    'description': 'Simple endpoint to verify the API is reachable.',
                    'responses': {
                        '200': {
                            'description': 'API is up',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'status': {'type': 'string', 'example': 'success'},
                                            'message': {'type': 'string', 'example': 'API is working!'},
                                            'timestamp': {'type': 'string'},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            '/stats': {
                'get': {
                    'tags': ['System'],
                    'summary': 'Platform statistics',
                    'responses': {
                        '200': {
                            'description': 'Aggregate stats',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/Stats'}
                                }
                            },
                        },
                        '500': _error_response('Failed to retrieve statistics'),
                    },
                }
            },
            '/models': {
                'get': {
                    'tags': ['Models'],
                    'summary': 'List models',
                    'description': (
                        'Returns public models by default. Pass `user_only=true` '
                        'with an authenticated session to list the current '
                        "user's models instead. Trusted service tokens can pass "
                        '`include_private=true` to search all owner inventories, '
                        'or combine `user_only=true` with `X-Asset-Username` / '
                        '`X-Asset-User-Id` to inspect one account.'
                    ),
                    'parameters': [
                        {
                            'name': 'page', 'in': 'query',
                            'schema': {'type': 'integer', 'default': 1, 'minimum': 1},
                        },
                        {
                            'name': 'per_page', 'in': 'query',
                            'description': 'Items per page (max 100).',
                            'schema': {'type': 'integer', 'default': 20, 'maximum': 100},
                        },
                        {
                            'name': 'search', 'in': 'query',
                            'description': 'Search over title, description, filename, tags, asset facets, AI metadata, and runtime metadata.',
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'user_only', 'in': 'query',
                            'description': "Restrict to the logged-in user's models.",
                            'schema': {'type': 'boolean', 'default': False},
                        },
                        {
                            'name': 'include_private', 'in': 'query',
                            'description': 'Trusted service tokens only. Include private assets across all owners.',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                        {
                            'name': 'ready_for_tellus', 'in': 'query',
                            'description': 'When true, only return assets with a thumbnail and required game-optimized variant.',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                        {
                            'name': 'X-Asset-Username', 'in': 'header',
                            'description': 'Trusted service tokens only. Resolve user_only=true as this username.',
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'X-Asset-User-Id', 'in': 'header',
                            'description': 'Trusted service tokens only. Resolve user_only=true as this user id.',
                            'schema': {'type': 'string'},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'Paginated list of models',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/ModelListResponse'}
                                }
                            },
                        },
                        '500': _error_response('Failed to retrieve models'),
                    },
                }
            },
            '/animated-models': {
                'get': {
                    'tags': ['Models'],
                    'summary': 'List animated rigged GLB/GLTF models',
                    'description': (
                        'Narrow catalog endpoint for realtime clients. Returns '
                        'loadable GLB/GLTF assets tagged as both rigged and '
                        'animated, excluding VRMA/BVH clips and animation-source '
                        'records. Trusted service tokens can pass '
                        '`include_private=true`.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'parameters': [
                        {
                            'name': 'page', 'in': 'query',
                            'schema': {'type': 'integer', 'default': 1, 'minimum': 1},
                        },
                        {
                            'name': 'per_page', 'in': 'query',
                            'description': 'Items per page (max 100).',
                            'schema': {'type': 'integer', 'default': 20, 'maximum': 100},
                        },
                        {
                            'name': 'sort', 'in': 'query',
                            'schema': {'type': 'string', 'default': 'newest'},
                        },
                        {
                            'name': 'format', 'in': 'query',
                            'description': 'Optional repeated filter. Only glb and gltf are accepted.',
                            'schema': {'type': 'array', 'items': {'type': 'string', 'enum': ['glb', 'gltf']}},
                            'style': 'form',
                            'explode': True,
                        },
                        {
                            'name': 'user_only', 'in': 'query',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                        {
                            'name': 'include_private', 'in': 'query',
                            'description': 'Trusted service tokens only. Include private assets across all owners.',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'Paginated animated model list',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/ModelListResponse'}
                                }
                            },
                        },
                        '500': _error_response('Failed to retrieve animated models'),
                    },
                }
            },
            '/user/models': {
                'get': {
                    'tags': ['Models'],
                    'summary': "List the current user's models",
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'parameters': [
                        {
                            'name': 'page', 'in': 'query',
                            'schema': {'type': 'integer', 'default': 1, 'minimum': 1},
                        },
                        {
                            'name': 'per_page', 'in': 'query',
                            'schema': {'type': 'integer', 'default': 20, 'maximum': 100},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': "The user's models, paginated",
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/ModelListResponse'}
                                }
                            },
                        },
                        '401': _error_response('Authentication required'),
                        '500': _error_response('Failed to retrieve user models'),
                    },
                }
            },
            '/vrma': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'List VRMA animation clips',
                    'description': (
                        'Lists uploaded .vrma clips plus generated VRMA clips, '
                        'including the external-client `clips` contract and the '
                        'legacy in-app `animations` shape.'
                    ),
                    'responses': {
                        '200': {
                            'description': 'Available VRMA clips',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'clips': {
                                                'type': 'array',
                                                'items': {
                                                    'type': 'object',
                                                    'properties': {
                                                        'id': {'type': 'string'},
                                                        'name': {'type': 'string'},
                                                        'downloadUrl': {'type': 'string'},
                                                        'source': {'type': 'string', 'enum': ['upload', 'generated']},
                                                    },
                                                },
                                            },
                                            'animations': {'type': 'array', 'items': {'type': 'object'}},
                                            'default_id': {'type': 'string', 'nullable': True},
                                            'default_url': {'type': 'string', 'nullable': True},
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            },
            '/vrm': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'List VRM avatars',
                    'description': (
                        'Lists visible VRM avatars, including native .vrm uploads '
                        'and derived GLB-to-VRM avatar variants.'
                    ),
                    'responses': {
                        '200': {
                            'description': 'Available VRM avatars',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'avatars': {
                                                'type': 'array',
                                                'items': {
                                                    'type': 'object',
                                                    'properties': {
                                                        'id': {'type': 'string'},
                                                        'model_id': {'type': 'string'},
                                                        'name': {'type': 'string'},
                                                        'source': {'type': 'string', 'enum': ['upload', 'generated']},
                                                        'view_url': {'type': 'string'},
                                                        'download_url': {'type': 'string'},
                                                        'thumbnail_url': {'type': 'string', 'nullable': True},
                                                        'size': {'type': 'integer'},
                                                        'optimized': {'type': 'boolean'},
                                                        'optimized_url': {'type': 'string', 'nullable': True},
                                                        'optimized_size': {'type': 'integer', 'nullable': True},
                                                    },
                                                },
                                            },
                                            'count': {'type': 'integer'},
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            },
            '/vrm-models': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'List VRM avatar models',
                    'description': (
                        'Alias for `/vrm` with the same response shape. Lists '
                        'visible native .vrm uploads and derived GLB-to-VRM '
                        'avatar variants. Trusted service tokens can pass '
                        '`include_private=true`.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'parameters': [
                        {
                            'name': 'user_only', 'in': 'query',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                        {
                            'name': 'include_private', 'in': 'query',
                            'description': 'Trusted service tokens only. Include private avatars across all owners.',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'Available VRM avatars',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'avatars': {'type': 'array', 'items': {'type': 'object'}},
                                            'count': {'type': 'integer'},
                                        },
                                    }
                                }
                            },
                        },
                    },
                }
            },
            '/model/{model_id}': {
                'parameters': [
                    {
                        'name': 'model_id', 'in': 'path', 'required': True,
                        'schema': {'type': 'string'},
                        'description': 'The model id.',
                    }
                ],
                'get': {
                    'tags': ['Models'],
                    'summary': 'Get model metadata',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {
                            'description': 'Model metadata',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'model': {'$ref': '#/components/schemas/ModelSummary'},
                                        },
                                    }
                                }
                            },
                        },
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Failed to retrieve model'),
                    },
                },
                'put': {
                    'tags': ['Models'],
                    'summary': 'Update model metadata',
                    'description': (
                        'Owner-only. Updates only the fields present in the body. '
                        'Accepts JSON or form-encoded data. PATCH behaves identically.'
                    ),
                    'security': [{'sessionCookie': []}],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'name': {'type': 'string'},
                                        'description': {'type': 'string'},
                                        'is_public': {'type': 'boolean'},
                                        'camera_orbit': {
                                            'type': 'string',
                                            'description': 'Default <model-viewer> angle, e.g. "180deg 75deg 105%". Empty resets to auto.',
                                        },
                                        'tags': {
                                            'type': 'array',
                                            'items': {'type': 'string'},
                                            'description': 'Tags as an array or comma-separated string.',
                                        },
                                        'asset_category': {'type': 'string'},
                                        'asset_styles': {'type': 'array', 'items': {'type': 'string'}},
                                        'asset_types': {'type': 'array', 'items': {'type': 'string'}},
                                        'runtime_metadata': {'$ref': '#/components/schemas/RuntimeMetadata'},
                                    },
                                }
                            }
                        },
                    },
                    'responses': {
                        '200': {
                            'description': 'Updated model',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'message': {'type': 'string'},
                                            'model': {'$ref': '#/components/schemas/ModelSummary'},
                                        },
                                    }
                                }
                            },
                        },
                        '400': _error_response('Validation error (e.g. empty name)'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Not the owner'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Update failed'),
                    },
                },
                'patch': {
                    'tags': ['Models'],
                    'summary': 'Update model metadata (alias of PUT)',
                    'security': [{'sessionCookie': []}],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'name': {'type': 'string'},
                                        'description': {'type': 'string'},
                                        'is_public': {'type': 'boolean'},
                                        'camera_orbit': {'type': 'string'},
                                        'tags': {'type': 'array', 'items': {'type': 'string'}},
                                    },
                                }
                            }
                        },
                    },
                    'responses': {
                        '200': {'description': 'Updated model'},
                        '400': _error_response('Validation error'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Not the owner'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Update failed'),
                    },
                },
                'delete': {
                    'tags': ['Models'],
                    'summary': 'Delete a model',
                    'description': 'Owner-only. Removes the model record and its stored file.',
                    'security': [{'sessionCookie': []}],
                    'responses': {
                        '200': {
                            'description': 'Deleted',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'message': {'type': 'string', 'example': 'Model deleted successfully'},
                                        },
                                    }
                                }
                            },
                        },
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Not the owner'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Delete failed'),
                    },
                },
            },
            '/model/{model_id}/to-vrm': {
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Convert a rigged GLB to a VRM avatar',
                    'description': (
                        'Owner-only. Injects VRMC_vrm humanoid metadata into a '
                        'rigged binary GLB and stores the result as the model\'s '
                        '`vrm` variant. The source must already be skinned/rigged, '
                        'for example by mesh2motion.'
                    ),
                    'security': [{'sessionCookie': []}],
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        }
                    ],
                    'responses': {
                        '200': {
                            'description': 'VRM variant created',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'variant': {'type': 'object'},
                                            'optimized': {'type': 'boolean'},
                                        },
                                    }
                                }
                            },
                        },
                        '400': _error_response('Model has no binary GLB data to convert'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Only the owner can convert this model'),
                        '404': _error_response('Model not found'),
                        '413': _error_response('Model is too large to convert'),
                        '422': _error_response('VRM conversion failed'),
                        '500': _error_response('Could not convert model to VRM'),
                    },
                }
            },
            '/model/{model_id}/vrm': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Fetch a derived VRM avatar variant',
                    'description': 'Returns the GLB-to-VRM avatar variant inline, or as an attachment with `download=1`.',
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'download', 'in': 'query',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'VRM binary',
                            'content': {
                                'model/gltf-binary': {
                                    'schema': {'type': 'string', 'format': 'binary'}
                                }
                            },
                        },
                        '304': {'description': 'Not modified'},
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model or VRM variant not found'),
                        '500': _error_response('VRM fetch failed'),
                    },
                }
            },
            '/model/{model_id}/optimize-vrm': {
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Create a rig-safe optimized VRM variant',
                    'description': (
                        'Owner-only. Optimizes the derived VRM avatar without mesh '
                        'simplification, preserving the humanoid rig for VRMA playback.'
                    ),
                    'security': [{'sessionCookie': []}],
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'texture_limit', 'in': 'query',
                            'schema': {'type': 'integer', 'default': 2048},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'Optimized VRM variant created',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'variant': {'type': 'object'},
                                            'download_url': {'type': 'string'},
                                            'source_size': {'type': 'integer'},
                                            'optimized_size': {'type': 'integer'},
                                            'savings_ratio': {'type': 'number'},
                                        },
                                    }
                                }
                            },
                        },
                        '400': _error_response('No VRM avatar to optimize'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Only the owner can optimize this avatar'),
                        '404': _error_response('Model not found'),
                        '422': _error_response('VRM optimization failed'),
                        '500': _error_response('Could not optimize VRM'),
                    },
                }
            },
            '/model/{model_id}/optimized-vrm': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Fetch the optimized VRM avatar variant',
                    'description': 'Returns the rig-safe optimized VRM variant inline, or as an attachment with `download=1`.',
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'download', 'in': 'query',
                            'schema': {'type': 'boolean', 'default': False},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'Optimized VRM binary',
                            'content': {
                                'model/gltf-binary': {
                                    'schema': {'type': 'string', 'format': 'binary'}
                                }
                            },
                        },
                        '304': {'description': 'Not modified'},
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model or optimized VRM variant not found'),
                        '500': _error_response('Optimized VRM fetch failed'),
                    },
                }
            },
            '/download/{model_id}': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Download a model file',
                    'description': (
                        'Streams the model binary as an attachment and increments '
                        'the download counter. Private models require the owner session.'
                    ),
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        }
                    ],
                    'responses': {
                        '200': {
                            'description': 'The model file',
                            'content': {
                                'application/octet-stream': {
                                    'schema': {'type': 'string', 'format': 'binary'}
                                }
                            },
                        },
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('Model or file not found'),
                        '500': _error_response('Download failed'),
                    },
                }
            },
            '/view/{model_id}': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Stream the renderable model for inline 3D viewing',
                    'description': 'Serves a derived GLB when conversion produced one, otherwise the original file.',
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        }
                    ],
                    'responses': {
                        '200': {
                            'description': 'The renderable model file (inline)',
                            'content': {
                                'application/octet-stream': {
                                    'schema': {'type': 'string', 'format': 'binary'}
                                }
                            },
                        },
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('Model or file not found'),
                        '500': _error_response('View failed'),
                    },
                }
            },
            '/model/{model_id}/media': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Media summary for a model',
                    'description': (
                        'Returns every media URL for the model in one call: the '
                        'still thumbnail image, the rotating preview video, the '
                        'renderable/downloadable model file, and the game-optimized '
                        'variant (if any) — each with a presence flag. Private '
                        'models require the owner session.'
                    ),
                    'parameters': [
                        {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
                    ],
                    'responses': {
                        '200': {
                            'description': 'Media summary',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/MediaSummary'}
                                }
                            },
                        },
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Media summary failed'),
                    },
                }
            },
            '/model/{model_id}/preview': {
                'parameters': [
                    {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}
                ],
                'get': {
                    'tags': ['Files'],
                    'summary': "Get a model's rotating preview video",
                    'description': (
                        'Streams the looping WebM preview. Supports HTTP Range '
                        'requests (206 partial content) for seeking/streaming, and '
                        'sends a strong ETag + long immutable cache. Private models '
                        'require the owner session. 404 when no preview exists.'
                    ),
                    'parameters': [
                        {
                            'name': 'Range', 'in': 'header', 'required': False,
                            'description': 'Optional byte range, e.g. "bytes=0-".',
                            'schema': {'type': 'string'},
                        }
                    ],
                    'responses': {
                        '200': {
                            'description': 'The full preview video',
                            'content': {'video/webm': {'schema': {'type': 'string', 'format': 'binary'}}},
                        },
                        '206': {
                            'description': 'Partial content (range request)',
                            'content': {'video/webm': {'schema': {'type': 'string', 'format': 'binary'}}},
                        },
                        '304': {'description': 'Not modified (ETag matched)'},
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('No preview'),
                    },
                },
                'post': {
                    'tags': ['Files'],
                    'summary': 'Upload/replace a rotating preview video',
                    'description': (
                        'Owner-only. Send the raw WebM bytes as the request body '
                        'with Content-Type video/webm. Max ~8MB. Replaces any '
                        'existing preview. Typically produced client-side by '
                        'recording the rotating viewer canvas.'
                    ),
                    'security': [{'sessionCookie': []}],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'video/webm': {'schema': {'type': 'string', 'format': 'binary'}}
                        },
                    },
                    'responses': {
                        '200': {
                            'description': 'Stored',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'preview_file_id': {'type': 'string'},
                                        },
                                    }
                                }
                            },
                        },
                        '400': _error_response('Preview missing or too large'),
                        '403': _error_response('Not the owner'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Preview upload failed'),
                    },
                },
            },
            '/model/{model_id}/game-optimized': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Serve the game-optimized GLB variant',
                    'description': (
                        'Returns the game-optimized GLB attached to the model '
                        '(created via /model/{id}/optimize-game). Inline by '
                        'default; pass `download=1` for an attachment. Supports '
                        'HTTP Range + ETag revalidation. 404 if the model has '
                        'no game-optimized variant yet.'
                    ),
                    'parameters': [
                        {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                        {
                            'name': 'download', 'in': 'query', 'required': False,
                            'description': 'Set to 1/true to receive the file as an attachment.',
                            'schema': {'type': 'boolean'},
                        },
                        {
                            'name': 'Range', 'in': 'header', 'required': False,
                            'schema': {'type': 'string'},
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'The game-optimized GLB',
                            'content': {'model/gltf-binary': {'schema': {'type': 'string', 'format': 'binary'}}},
                        },
                        '206': {'description': 'Partial content (range request)'},
                        '304': {'description': 'Not modified (ETag matched)'},
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('No game-optimized variant'),
                        '500': _error_response('Game-optimized fetch failed'),
                    },
                }
            },
            '/assets/model/{model_id}/game-optimized': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Tellus asset URL for the game-optimized GLB',
                    'description': (
                        'Stable Tellus/game backend URL for the near runtime GLB. '
                        'Serves the explicit game variant when present; otherwise falls back to '
                        'ModelVariant(kind=lod, level=0). Supports HTTP Range + ETag revalidation.'
                    ),
                    'parameters': [
                        {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                        {'name': 'Range', 'in': 'header', 'required': False, 'schema': {'type': 'string'}},
                    ],
                    'responses': {
                        '200': {
                            'description': 'The game-optimized GLB, or LOD0 GLB fallback',
                            'headers': {
                                'ETag': {'schema': {'type': 'string'}},
                                'Accept-Ranges': {'schema': {'type': 'string', 'example': 'bytes'}},
                                'Cache-Control': {'schema': {'type': 'string'}},
                            },
                            'content': {'model/gltf-binary': {'schema': {'type': 'string', 'format': 'binary'}}},
                        },
                        '206': {'description': 'Partial content (range request)'},
                        '304': {'description': 'Not modified (ETag matched)'},
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('No game-optimized or LOD0 variant'),
                    },
                }
            },
            '/assets/model/{model_id}/lod/{level}': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Tellus asset URL for generated LOD GLBs',
                    'description': (
                        'Returns ModelVariant(kind=lod, level=0/1/2) under the original asset id. '
                        'LOD 0 falls back to the game-optimized variant until explicit LOD backfill exists.'
                    ),
                    'parameters': [
                        {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                        {'name': 'level', 'in': 'path', 'required': True, 'schema': {'type': 'integer', 'enum': [0, 1, 2, 3]}},
                        {'name': 'Range', 'in': 'header', 'required': False, 'schema': {'type': 'string'}},
                    ],
                    'responses': {
                        '200': {
                            'description': 'The LOD GLB',
                            'headers': {
                                'ETag': {'schema': {'type': 'string'}},
                                'Accept-Ranges': {'schema': {'type': 'string', 'example': 'bytes'}},
                                'Cache-Control': {'schema': {'type': 'string'}},
                            },
                            'content': {'model/gltf-binary': {'schema': {'type': 'string', 'format': 'binary'}}},
                        },
                        '206': {'description': 'Partial content (range request)'},
                        '304': {'description': 'Not modified (ETag matched)'},
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('No LOD variant'),
                    },
                }
            },
            '/assets/model/{model_id}/impostor': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Tellus asset URL for generated impostor media',
                    'description': (
                        'Returns ModelVariant(kind=impostor) under the original asset id. '
                        'The file may be an image atlas or another compact runtime format.'
                    ),
                    'parameters': [
                        {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                        {'name': 'Range', 'in': 'header', 'required': False, 'schema': {'type': 'string'}},
                    ],
                    'responses': {
                        '200': {
                            'description': 'The impostor media file',
                            'headers': {
                                'ETag': {'schema': {'type': 'string'}},
                                'Accept-Ranges': {'schema': {'type': 'string', 'example': 'bytes'}},
                                'Cache-Control': {'schema': {'type': 'string'}},
                            },
                            'content': {
                                'image/webp': {'schema': {'type': 'string', 'format': 'binary'}},
                                'image/png': {'schema': {'type': 'string', 'format': 'binary'}},
                                'model/gltf-binary': {'schema': {'type': 'string', 'format': 'binary'}},
                                'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}},
                            },
                        },
                        '206': {'description': 'Partial content (range request)'},
                        '304': {'description': 'Not modified (ETag matched)'},
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('No impostor variant'),
                    },
                }
            },
            '/models/browse': {
                'get': {
                    'tags': ['Models'],
                    'summary': 'Browse gallery feed (infinite scroll)',
                    'description': (
                        'Paginated list of public models for the browse gallery. '
                        'Supports search, sort and multi-tag filtering, and returns '
                        'compact cards with media URLs and ownership flags. Distinct '
                        'from /models (different response shape).'
                    ),
                    'parameters': [
                        {'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'default': 1, 'minimum': 1}},
                        {
                            'name': 'per_page', 'in': 'query',
                            'description': 'Items per page (max 60).',
                            'schema': {'type': 'integer', 'default': 24, 'maximum': 60},
                        },
                        {'name': 'search', 'in': 'query', 'schema': {'type': 'string'}},
                        {
                            'name': 'sort', 'in': 'query',
                            'schema': {'type': 'string', 'enum': ['newest', 'oldest', 'downloads', 'name'], 'default': 'newest'},
                        },
                        {
                            'name': 'tag', 'in': 'query',
                            'description': 'Tag filter; repeat for multiple (AND-matched).',
                            'schema': {'type': 'array', 'items': {'type': 'string'}},
                            'style': 'form', 'explode': True,
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'Browse cards page',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/BrowseListResponse'}
                                }
                            },
                        },
                        '500': _error_response('Could not list models'),
                    },
                }
            },
            '/model/{model_id}/status': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Get conversion status for a model',
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        }
                    ],
                    'responses': {
                        '200': {
                            'description': 'Current conversion status',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'status': {
                                                'type': 'string',
                                                'nullable': True,
                                                'enum': ['pending', 'processing', 'done', 'failed', 'skipped', None],
                                            },
                                            'has_viewable': {'type': 'boolean'},
                                            'has_vrma': {'type': 'boolean'},
                                            'error': {'type': 'string', 'nullable': True},
                                            'has_game_optimized': {'type': 'boolean'},
                                            'game_optimized': {'$ref': '#/components/schemas/GameOptimized'},
                                        },
                                    }
                                }
                            },
                        },
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('Model not found'),
                    },
                }
            },
            '/export/{model_id}': {
                'get': {
                    'tags': ['Files'],
                    'summary': 'Export/transcode a model to another format',
                    'parameters': [
                        {
                            'name': 'model_id', 'in': 'path', 'required': True,
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'format', 'in': 'query', 'required': True,
                            'schema': {
                                'type': 'string',
                                'enum': ['glb', 'gltf', 'obj', 'stl', 'ply', 'fbx', 'dae', '3ds', 'vrma'],
                            },
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'The exported file',
                            'content': {
                                'application/octet-stream': {
                                    'schema': {'type': 'string', 'format': 'binary'}
                                }
                            },
                        },
                        '400': _error_response('Unsupported or missing format'),
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('Model or source file not found'),
                        '409': _error_response('No VRMA animation for this model'),
                        '502': _error_response('Transcode failed'),
                        '503': _error_response('Transcoding disabled on this server'),
                    },
                }
            },
            '/model/{model_id}/thumbnail': {
                'parameters': [
                    {
                        'name': 'model_id', 'in': 'path', 'required': True,
                        'schema': {'type': 'string'},
                    }
                ],
                'get': {
                    'tags': ['Files'],
                    'summary': "Get a model's thumbnail image",
                    'description': (
                        'Serves the still thumbnail (WebP for new uploads, PNG for '
                        'older ones) with a strong ETag + long immutable cache '
                        '(304 on revalidation). Private models require the owner '
                        'session. 404 when no thumbnail exists.'
                    ),
                    'responses': {
                        '200': {
                            'description': 'Thumbnail image (WebP or PNG)',
                            'content': {
                                'image/webp': {'schema': {'type': 'string', 'format': 'binary'}},
                                'image/png': {'schema': {'type': 'string', 'format': 'binary'}},
                            },
                        },
                        '304': {'description': 'Not modified (ETag matched)'},
                        '403': _error_response('Access denied (private model)'),
                        '404': _error_response('No thumbnail'),
                    },
                },
                'post': {
                    'tags': ['Files'],
                    'summary': 'Upload/replace a model thumbnail',
                    'description': (
                        'Owner-only. Send a base64 PNG (optionally as a data URL). '
                        'Max 2MB. Replaces any existing thumbnail.'
                    ),
                    'security': [{'sessionCookie': []}],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['image'],
                                    'properties': {
                                        'image': {
                                            'type': 'string',
                                            'description': 'Base64 PNG or "data:image/png;base64,..." URL.',
                                        },
                                    },
                                }
                            }
                        },
                    },
                    'responses': {
                        '200': {
                            'description': 'Stored',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'thumbnail_file_id': {'type': 'string'},
                                        },
                                    }
                                }
                            },
                        },
                        '400': _error_response('Missing/invalid/too-large image'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Not the owner'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('Thumbnail upload failed'),
                    },
                },
            },
            '/model/{model_id}/conversion': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Requeue model conversion',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {'description': 'Queued', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model not found'),
                        '503': _error_response('Conversion disabled'),
                    },
                },
            },
            '/optimization/defaults': {
                'get': {
                    'tags': ['Workflows'],
                    'summary': 'Get game optimization defaults and presets',
                    'description': (
                        'Public Tellus-facing contract for the optimizer defaults. '
                        'Use this to populate preset selectors and understand what '
                        'metadata appears on game_optimized.runtime_cost.'
                    ),
                    'responses': {
                        '200': {
                            'description': 'Current optimizer defaults and presets',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'defaults_version': {'type': 'string'},
                                            'default_preset': {'type': 'string', 'example': 'balanced'},
                                            'defaults': {'type': 'object'},
                                            'presets': {'type': 'object'},
                                            'supported': {'type': 'object'},
                                        },
                                    }
                                }
                            },
                        },
                    },
                },
            },
            '/admin/pipeline/status': {
                'get': {
                    'tags': ['Workflows'],
                    'summary': 'Get asset processing pipeline status',
                    'description': 'Admin status for the self-healing asset pipeline: optimizer reconciler, media-capture worker, and pending media queue counts.',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {'description': 'Pipeline status', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '401': _error_response('Unauthorized'),
                    },
                },
            },
            '/admin/pipeline/reconcile': {
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Run one asset processing reconciliation pass',
                    'description': 'Admin repair pass that optimizes missing GLB/GLTF game variants, generates missing LOD chains, creates missing far-field impostor billboards, requeues missing FBX/BVH conversions, queues thumbnail-ready AI enrichment, and reports media still waiting for capture.',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'parameters': [
                        {'name': 'optimize_limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1}},
                        {'name': 'lod_limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1}},
                        {'name': 'impostor_limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1}},
                        {'name': 'enrich_limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1}},
                        {'name': 'conversion_limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1}},
                    ],
                    'responses': {
                        '200': {'description': 'Reconciliation result', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '401': _error_response('Unauthorized'),
                    },
                },
            },
            '/admin/lod-backfill': {
                'get': {
                    'tags': ['Workflows'],
                    'summary': 'Get LOD backfill progress',
                    'description': 'Admin endpoint for the current bulk LOD generation state.',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {
                            'description': 'LOD backfill status',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/LodBackfillStatus'}
                                }
                            },
                        },
                        '401': _error_response('Unauthorized'),
                    },
                },
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Start LOD backfill',
                    'description': (
                        'Admin bulk repair endpoint that generates missing LOD0/LOD1/LOD2/LOD3 '
                        'variants for GLB/GLTF models and regenerates stale defaults-version LODs. '
                        'Use `force=true` to rebuild even current LOD variants, or `sync=true` for '
                        'a bounded foreground run during maintenance or tests.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'parameters': [
                        {
                            'name': 'sync', 'in': 'query',
                            'schema': {'type': 'boolean', 'default': False},
                            'description': 'Run synchronously in the request instead of spawning a background thread.',
                        },
                        {
                            'name': 'force', 'in': 'query',
                            'schema': {'type': 'boolean', 'default': False},
                            'description': 'Regenerate existing LOD variants even when they already match the current defaults version.',
                        },
                        {
                            'name': 'limit', 'in': 'query',
                            'schema': {'type': 'integer', 'minimum': 1},
                            'description': 'Maximum number of assets to process in this backfill run.',
                        },
                    ],
                    'responses': {
                        '200': {
                            'description': 'LOD backfill started, already running, or finished synchronously',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/LodBackfillStatus'}
                                }
                            },
                        },
                        '401': _error_response('Unauthorized'),
                    },
                },
            },
            '/admin/lod-backfill/status': {
                'get': {
                    'tags': ['Workflows'],
                    'summary': 'Poll LOD backfill status',
                    'description': 'Admin polling endpoint for the bulk LOD generation worker.',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {
                            'description': 'LOD backfill status',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/LodBackfillStatus'}
                                }
                            },
                        },
                        '401': _error_response('Unauthorized'),
                    },
                },
            },
            '/admin/media-capture/report': {
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Report per-asset media capture progress',
                    'description': 'Browser media-capture workers call this to persist processing, captured, failed, or blocked state for an individual model.',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['model_id', 'status'],
                                    'properties': {
                                        'model_id': {'type': 'string'},
                                        'status': {'type': 'string', 'enum': ['processing', 'captured', 'failed', 'blocked']},
                                        'kind': {'type': 'string', 'nullable': True},
                                        'capture_url': {'type': 'string', 'nullable': True},
                                        'error': {'type': 'string', 'nullable': True},
                                    },
                                },
                            },
                        },
                    },
                    'responses': {
                        '200': {'description': 'Capture state stored', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '400': _error_response('Invalid report'),
                        '401': _error_response('Unauthorized'),
                        '404': _error_response('Model not found'),
                    },
                },
            },
            '/model/{model_id}/optimize-game': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Queue game optimization, LOD generation, and impostor generation',
                    'description': (
                        'Queues a single background job that creates the game-optimized GLB and then '
                        'generates LOD0, LOD1, LOD2, and LOD3, followed by a WebP far-field impostor '
                        'for the same source asset. The impostor is an octahedral atlas when the server '
                        'render stack is available, with thumbnail billboard fallback. Select a preset, then override individual '
                        'fields when needed. Choose meshopt for the smallest file or fallback for a '
                        'self-contained file without mesh compression. The source asset is not replaced.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'preset': {
                                            'type': 'string',
                                            'default': 'balanced',
                                            'enum': ['balanced', 'preview', 'quality', 'compatibility'],
                                        },
                                        'texture_limit': {
                                            'type': 'integer',
                                            'default': 1024,
                                            'enum': [0, 1024, 2048, 4096],
                                        },
                                        'simplify_ratio': {
                                            'type': 'number',
                                            'default': 0.85,
                                            'minimum': 0.01,
                                            'maximum': 1,
                                        },
                                        'compression_mode': {
                                            'type': 'string',
                                            'default': 'meshopt',
                                            'enum': ['meshopt', 'fallback'],
                                        },
                                        'name': {'type': 'string'},
                                    },
                                }
                            }
                        }
                    },
                    'responses': {
                        '202': {
                            'description': 'Game + LOD + impostor optimization job queued',
                            'content': {
                                'application/json': {
                                    'schema': {'$ref': '#/components/schemas/OptimizationJobResponse'}
                                }
                            },
                        },
                        '400': _error_response('Unsupported format or invalid settings'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model or source file not found'),
                        '500': _error_response('Optimization could not be queued'),
                    },
                },
            },
            '/model/{model_id}/lod/rebuild': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Rebuild LOD variants for one model',
                    'description': (
                        'Synchronously rebuilds only this model\'s LOD0, LOD1, LOD2, and LOD3 variants '
                        'using the current LOD defaults. Use this to refresh stale LOD assets without '
                        'running the full bulk backfill or regenerating the game-optimized variant.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {
                            'description': 'LOD variants rebuilt',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'defaults_version': {'type': 'string'},
                                            'lod_result': {'type': 'object'},
                                            'lod_variants': {'type': 'array', 'items': {'$ref': '#/components/schemas/LodVariant'}},
                                            'lod_summary': {'$ref': '#/components/schemas/LodSummary'},
                                            'lod_ready': {'type': 'boolean'},
                                            'lod_status': {'type': 'string'},
                                        },
                                    }
                                }
                            },
                        },
                        '400': _error_response('Unsupported format'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model not found'),
                        '500': _error_response('LOD rebuild failed'),
                    },
                },
            },
            '/model/{model_id}/optimize-game/{job_id}': {
                'parameters': [
                    {'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                    {'name': 'job_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}},
                ],
                'get': {
                    'tags': ['Workflows'],
                    'summary': 'Get game optimization, LOD, and impostor job status',
                    'description': (
                        'Polls the one-step Game + LOD job. When complete, `job.lod_ready`, '
                        '`job.lod_variants`, and `job.lod_summary` describe generated LOD coverage, '
                        'sizes, vertices, triangles, and recommended use buckets. `job.impostor_result` '
                        'reports the generated far-field WebP billboard or its non-fatal error.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {
                            'description': 'Optimization job status',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'type': 'object',
                                        'properties': {
                                            'success': {'type': 'boolean'},
                                            'job': {'$ref': '#/components/schemas/OptimizationJob'},
                                        },
                                    }
                                }
                            },
                        },
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model or optimization job not found'),
                        '500': _error_response('Optimization status failed'),
                    },
                },
            },
            '/model/{model_id}/animation-source': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Attach an animated roundtrip file to the original model',
                    'description': (
                        'Uploads a GLB/GLTF returned from mesh2motion or another animation tool, '
                        'stores it as the original model rigged/animated source variant, merges '
                        'embedded animation metadata onto the original model, and can queue a fresh '
                        'game-optimized copy from that animated source.'
                    ),
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'multipart/form-data': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['file'],
                                    'properties': {
                                        'file': {'type': 'string', 'format': 'binary'},
                                        'reoptimize': {'type': 'boolean', 'default': False},
                                    },
                                },
                            },
                        },
                    },
                    'responses': {
                        '200': {'description': 'Animated source stored', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '400': _error_response('Invalid or non-animated GLB/GLTF'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model not found'),
                        '413': _error_response('Animated source is too large'),
                    },
                },
            },
            '/model/{model_id}/ai/autotag': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'AI autotag and describe a model',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'overwrite': {'type': 'boolean', 'default': True},
                                        'include_title': {'type': 'boolean', 'default': True},
                                        'include_description': {'type': 'boolean', 'default': True},
                                        'async': {
                                            'type': 'boolean',
                                            'default': False,
                                            'description': 'Queue enrichment and return immediately with ai_status=pending.',
                                        },
                                        'context': {'type': 'object'},
                                    },
                                }
                            }
                        }
                    },
                    'responses': {
                        '200': {'description': 'Enriched', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '202': {'description': 'Enrichment queued', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model not found'),
                        '502': _error_response('AI enrichment failed'),
                    },
                },
            },
            '/model/{model_id}/approval': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'patch': {
                    'tags': ['Workflows'],
                    'summary': 'Approve model for game-ready and asset-store workflows',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'approve_game_ready': {'type': 'boolean'},
                                        'approve_asset_store': {'type': 'boolean'},
                                        'approval_notes': {'type': 'string'},
                                    },
                                }
                            }
                        }
                    },
                    'responses': {
                        '200': {'description': 'Updated', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model not found'),
                    },
                },
                'put': {
                    'tags': ['Workflows'],
                    'summary': 'Approve model for game-ready and asset-store workflows',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {'200': {'description': 'Updated'}},
                },
            },
            '/bundles': {
                'get': {
                    'tags': ['Bundles'],
                    'summary': 'List bundles',
                    'responses': {'200': {'description': 'Bundles'}},
                },
                'post': {
                    'tags': ['Bundles'],
                    'summary': 'Create a bundle from model ids',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['model_ids'],
                                    'properties': {
                                        'model_ids': {'type': 'array', 'items': {'type': 'string'}},
                                        'name': {'type': 'string'},
                                        'description': {'type': 'string'},
                                        'tags': {'type': 'array', 'items': {'type': 'string'}},
                                        'is_public': {'type': 'boolean'},
                                        'status': {'type': 'string', 'default': 'draft'},
                                        'create_zip': {'type': 'boolean', 'default': True},
                                    },
                                }
                            }
                        },
                    },
                    'responses': {
                        '201': {'description': 'Bundle created', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '400': _error_response('Invalid request'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                    },
                },
            },
            '/bundles/{bundle_id}': {
                'parameters': [{'name': 'bundle_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'get': {'tags': ['Bundles'], 'summary': 'Get bundle details', 'responses': {'200': {'description': 'Bundle'}}},
            },
            '/bundles/{bundle_id}/download': {
                'parameters': [{'name': 'bundle_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'get': {'tags': ['Bundles'], 'summary': 'Download bundle zip', 'responses': {'200': {'description': 'Bundle zip'}}},
            },
            '/upload': {
                'post': {
                    'tags': ['Upload'],
                    'summary': 'Upload one or more 3D models',
                    'description': (
                        'Multipart form upload. Allowed formats: '
                        + ', '.join(ALLOWED_EXTENSIONS)
                        + '. The size limit (MAX_UPLOAD_MB) is enforced **per '
                        'file**.\n\n'
                        '**Recommended:** send one file per request. To upload a '
                        'folder or many files, issue one request per file — this '
                        'keeps each upload under the per-file limit and gives '
                        'per-file progress and error reporting. (The endpoint also '
                        'accepts several repeated `file` fields in a single '
                        'request, but then the whole request must fit under the '
                        'server body cap.)\n\n'
                        '**Naming:** if `name` is provided (single-file request '
                        'only), it names the model. If `name` is omitted, or '
                        'multiple files are sent in one request, each model is '
                        'named from its own filename. `name` is therefore optional '
                        '— a per-file batch (one request each, no name) auto-names '
                        'from filenames.\n\n'
                        '**Response shape:** a single-file request returns '
                        '`{success, message, model}`; a multi-file request returns '
                        '`{success, message, uploaded[], errors[]}`. Duplicate '
                        'model binaries are rejected by SHA-256 content hash.\n\n'
                        'Trusted service tokens can upload on behalf of a specific '
                        'account with `X-Asset-Username` or `X-Asset-User-Id`. '
                        '`TELLUS_ADMIN_API_TOKEN` can also default ownership through '
                        '`TELLUS_ADMIN_USERNAME` / `TELLUS_ADMIN_USER_ID`, and '
                        'admin-token uploads are tagged with `tellus`. Assets '
                        'referenced later in Tellus world state are automatically '
                        'tagged `tellus-world-<world-id>`; a world id may also '
                        'be supplied at upload time to stamp that tag immediately.'
                    ),
                    'security': [{'sessionCookie': []}, {'uploadApiKey': []}, {'bearerAuth': []}],
                    'parameters': [
                        {
                            'name': 'X-Asset-Username', 'in': 'header',
                            'description': 'Trusted service tokens only. Own this upload as the named user.',
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'X-Asset-User-Id', 'in': 'header',
                            'description': 'Trusted service tokens only. Own this upload as the given user id.',
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'X-Tellus-World-Id', 'in': 'header',
                            'description': 'Optional Tellus world id. Admin-token uploads receive a normalized tellus-world-<world-id> tag.',
                            'schema': {'type': 'string'},
                        },
                    ],
                    'requestBody': {
                        'required': True,
                        'content': {
                            'multipart/form-data': {
                                'schema': {
                                    'type': 'object',
                                    'required': ['file'],
                                    'properties': {
                                        'file': {
                                            'type': 'array',
                                            'items': {'type': 'string', 'format': 'binary'},
                                            'description': 'One or more model files. Repeat the field for multiple files.',
                                        },
                                        'name': {
                                            'type': 'string',
                                            'description': 'Optional. Names the model on a single-file request; ignored when multiple files are sent. When omitted, the model is named from the filename.',
                                        },
                                        'description': {'type': 'string'},
                                        'is_public': {
                                            'type': 'string',
                                            'enum': ['true', 'false'],
                                            'description': 'Send "true" to make the model(s) public. Applies to all files.',
                                        },
                                        'tags': {
                                            'type': 'string',
                                            'description': 'Comma-separated tags. Applies to all files.',
                                        },
                                        'runtime_metadata': {
                                            'type': 'string',
                                            'description': 'Optional JSON runtime metadata. See RuntimeMetadata schema.',
                                        },
                                        'worldId': {
                                            'type': 'string',
                                            'description': 'Optional Tellus world id. Also accepted as world_id, tellusWorldId, or tellus_world_id.',
                                        },
                                    },
                                }
                            }
                        },
                    },
                    'responses': {
                        '201': {
                            'description': 'Created (one or more models)',
                            'content': {
                                'application/json': {
                                    'schema': {
                                        'oneOf': [
                                            {
                                                'type': 'object',
                                                'description': 'Single-file response',
                                                'properties': {
                                                    'success': {'type': 'boolean'},
                                                    'message': {'type': 'string'},
                                                    'model': {'$ref': '#/components/schemas/ModelSummary'},
                                                },
                                            },
                                            {
                                                'type': 'object',
                                                'description': 'Multi-file response',
                                                'properties': {
                                                    'success': {'type': 'boolean'},
                                                    'message': {'type': 'string'},
                                                    'uploaded': {
                                                        'type': 'array',
                                                        'items': {'$ref': '#/components/schemas/ModelSummary'},
                                                    },
                                                    'errors': {
                                                        'type': 'array',
                                                        'items': {
                                                            'type': 'object',
                                                            'properties': {
                                                                'filename': {'type': 'string'},
                                                                'error': {'type': 'string'},
                                                            },
                                                        },
                                                    },
                                                },
                                            },
                                        ]
                                    }
                                }
                            },
                        },
                        '400': _error_response('Missing file/name, bad type, too large, or all files failed'),
                        '401': _error_response('Authentication required'),
                        '409': _error_response('Duplicate model binary'),
                        '500': _error_response('Upload failed'),
                    },
                }
            },
        },
    }
