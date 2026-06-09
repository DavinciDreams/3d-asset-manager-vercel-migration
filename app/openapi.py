"""OpenAPI 3.0 specification for the 3D Asset Manager API.

Hand-written spec (no extra pip dependencies) describing every JSON endpoint
registered on ``api_bp`` (mounted at ``/api``). Served as JSON at
``/api/openapi.json`` and rendered by Swagger UI at ``/api/docs``.

Keep this in sync with ``app/api.py`` when routes change.
"""

# Allowed upload extensions are duplicated from create_app() config so the spec
# stays self-contained; update both if the list changes.
ALLOWED_EXTENSIONS = ['obj', 'fbx', 'gltf', 'glb', 'dae', '3ds', 'ply', 'stl', 'vrm', 'vrma']


def _model_summary_schema():
    return {
        'type': 'object',
        'properties': {
            'id': {'type': 'string', 'example': '6650f1a2b3c4d5e6f7a8b9c0'},
            'name': {'type': 'string', 'example': 'Spaceship'},
            'description': {'type': 'string', 'example': 'A low-poly spaceship.'},
            'file_format': {'type': 'string', 'enum': ALLOWED_EXTENSIONS, 'example': 'glb'},
            'file_size': {'type': 'integer', 'description': 'Size in bytes', 'example': 248320},
            'original_filename': {'type': 'string', 'example': 'spaceship.glb'},
            'is_public': {'type': 'boolean', 'example': True},
            'upload_date': {'type': 'string', 'format': 'date-time', 'nullable': True},
            'download_count': {'type': 'integer', 'example': 12},
            'tags': {'type': 'array', 'items': {'type': 'string'}},
            'conversion_status': {
                'type': 'string',
                'nullable': True,
                'enum': ['pending', 'processing', 'done', 'failed', 'skipped', None],
            },
            'has_viewable': {'type': 'boolean'},
            'has_vrma': {'type': 'boolean'},
            'ai_status': {'type': 'string', 'nullable': True, 'enum': ['done', 'failed', None]},
            'ai_description': {'type': 'string', 'nullable': True},
            'ai_tags': {'type': 'array', 'items': {'type': 'string'}},
            'approve_game_ready': {'type': 'boolean'},
            'approve_asset_store': {'type': 'boolean'},
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
                'ASSET_MANAGER_API_TOKEN/API_UPLOAD_TOKEN is configured.'
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
                        'status': {'type': 'string', 'example': 'ready'},
                        'settings': {'type': 'object', 'description': 'gltfpack settings + size/savings used to produce it'},
                        'updated_at': {'type': 'string', 'format': 'date-time', 'nullable': True},
                        'url': {'type': 'string', 'description': 'Inline GLB URL', 'example': '/api/model/abc/game-optimized'},
                        'download_url': {'type': 'string', 'example': '/api/model/abc/game-optimized?download=1'},
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
                        "user's models instead."
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
                            'description': 'Full-text search over name and description.',
                            'schema': {'type': 'string'},
                        },
                        {
                            'name': 'user_only', 'in': 'query',
                            'description': "Restrict to the logged-in user's models.",
                            'schema': {'type': 'boolean', 'default': False},
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
            '/model/{model_id}': {
                'parameters': [
                    {
                        'name': 'model_id', 'in': 'path', 'required': True,
                        'schema': {'type': 'string'},
                        'description': 'The model id.',
                    }
                ],
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
                        'HTTP Range + ETag + immutable cache. 404 if the model has '
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
            '/model/{model_id}/optimize-game': {
                'parameters': [{'name': 'model_id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
                'post': {
                    'tags': ['Workflows'],
                    'summary': 'Queue a game-optimized GLB copy',
                    'description': 'Queues gltfpack optimization in the background. Choose meshopt for the smallest file or fallback for a self-contained file without mesh compression. The source asset is not replaced.',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'requestBody': {
                        'content': {
                            'application/json': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'texture_limit': {
                                            'type': 'integer',
                                            'default': 1024,
                                            'enum': [0, 1024, 2048, 4096],
                                        },
                                        'simplify_ratio': {
                                            'type': 'number',
                                            'default': 0.75,
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
                        '202': {'description': 'Optimization job queued', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '400': _error_response('Unsupported format or invalid settings'),
                        '401': _error_response('Authentication required'),
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model or source file not found'),
                        '500': _error_response('Optimization could not be queued'),
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
                    'summary': 'Get game optimization job status',
                    'security': [{'sessionCookie': []}, {'bearerAuth': []}],
                    'responses': {
                        '200': {'description': 'Optimization job status', 'content': {'application/json': {'schema': {'type': 'object'}}}},
                        '403': _error_response('Access denied'),
                        '404': _error_response('Model or optimization job not found'),
                        '500': _error_response('Optimization status failed'),
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
                                        'include_description': {'type': 'boolean', 'default': True},
                                        'context': {'type': 'object'},
                                    },
                                }
                            }
                        }
                    },
                    'responses': {
                        '200': {'description': 'Enriched', 'content': {'application/json': {'schema': {'type': 'object'}}}},
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
                        '`{success, message, uploaded[], errors[]}`.'
                    ),
                    'security': [{'sessionCookie': []}, {'uploadApiKey': []}],
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
                        '500': _error_response('Upload failed'),
                    },
                }
            },
        },
    }
