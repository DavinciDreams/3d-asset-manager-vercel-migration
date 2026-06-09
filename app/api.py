from datetime import datetime
from flask import Blueprint, jsonify, request, current_app, make_response, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException
from sqlalchemy import select, update
from app.db import asset_files, optimization_jobs
from app.models import ApiKey, AssetBundle, Model3D, ModelVariant, User, WorldState
from app.openapi import get_openapi_spec
import hmac
import io
import json
import os
import struct
import threading
import uuid
import zipfile

api_bp = Blueprint('api', __name__)

MIME_TYPES = {
    'glb': 'model/gltf-binary',
    'gltf': 'application/json',
    'obj': 'text/plain',
    'fbx': 'application/octet-stream',
    'dae': 'application/xml',
    '3ds': 'application/octet-stream',
    'ply': 'application/octet-stream',
    'stl': 'application/octet-stream',
    'vrm': 'model/gltf-binary',
    'vrma': 'application/octet-stream',
}


def _mime_for(fmt):
    return MIME_TYPES.get((fmt or '').lower(), 'application/octet-stream')


_GLB_MAGIC = b'glTF'
_GLB_JSON_CHUNK = 0x4E4F534A


def _json_chunk_bytes(payload):
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    padding = (-len(raw)) % 4
    return raw + (b' ' * padding)


def _force_meshopt_required_for_external_fallback(glb_bytes):
    """Repair old gltfpack -cf GLBs that reference a missing *.fallback.bin.

    gltfpack -cf writes meshopt-compressed data into the GLB and an external
    fallback buffer next to it. Since this app stores one file per model, older
    uploads are missing that fallback .bin. Modern loaders can use the embedded
    meshopt data when EXT_meshopt_compression is required, so we rewrite only
    the JSON chunk to require meshopt and leave binary chunks intact.
    """
    if not glb_bytes or glb_bytes[:4] != _GLB_MAGIC or len(glb_bytes) < 20:
        return glb_bytes
    try:
        magic, version, declared_length = struct.unpack_from('<4sII', glb_bytes, 0)
        if magic != _GLB_MAGIC or version != 2 or declared_length > len(glb_bytes):
            return glb_bytes

        offset = 12
        json_start = json_end = None
        chunks = []
        while offset + 8 <= declared_length:
            chunk_length, chunk_type = struct.unpack_from('<II', glb_bytes, offset)
            data_start = offset + 8
            data_end = data_start + chunk_length
            if data_end > declared_length:
                return glb_bytes
            chunks.append((offset, chunk_length, chunk_type, data_start, data_end))
            if chunk_type == _GLB_JSON_CHUNK and json_start is None:
                json_start, json_end = data_start, data_end
            offset = data_end

        if json_start is None:
            return glb_bytes

        gltf = json.loads(glb_bytes[json_start:json_end].decode('utf-8').rstrip(' \t\r\n\0'))
        used = set(gltf.get('extensionsUsed') or [])
        required = set(gltf.get('extensionsRequired') or [])
        if 'EXT_meshopt_compression' not in used or 'EXT_meshopt_compression' in required:
            return glb_bytes
        external_fallback = any(
            (buffer.get('uri') or '').endswith('.fallback.bin')
            and (buffer.get('extensions') or {}).get('EXT_meshopt_compression', {}).get('fallback') is True
            for buffer in gltf.get('buffers') or []
        )
        if not external_fallback:
            return glb_bytes

        required.add('EXT_meshopt_compression')
        gltf['extensionsRequired'] = [name for name in gltf.get('extensionsUsed', []) if name in required]
        json_bytes = _json_chunk_bytes(gltf)

        rebuilt = bytearray()
        rebuilt.extend(glb_bytes[:12])
        for _chunk_offset, chunk_length, chunk_type, data_start, data_end in chunks:
            if chunk_type == _GLB_JSON_CHUNK and data_start == json_start:
                rebuilt.extend(struct.pack('<II', len(json_bytes), chunk_type))
                rebuilt.extend(json_bytes)
            else:
                rebuilt.extend(struct.pack('<II', chunk_length, chunk_type))
                rebuilt.extend(glb_bytes[data_start:data_end])
        struct.pack_into('<I', rebuilt, 8, len(rebuilt))
        return bytes(rebuilt)
    except Exception as error:
        print(f"GLB meshopt fallback repair warning: {error}")
        return glb_bytes


def _can_access_model(model):
    if model.is_public:
        return True
    return current_user.is_authenticated and model.user_id == current_user.id


def _can_access_model_as(model, principal=None, service=False):
    if service or _can_access_model(model):
        return True
    return bool(principal and model.user_id == principal.id)


def _authorized_service_token():
    return _bearer_token_valid()


def _configured_bearer_tokens():
    tokens = [
        os.environ.get('ASSET_MANAGER_API_TOKEN'),
        os.environ.get('API_UPLOAD_TOKEN'),
        os.environ.get('TELLUS_PERSISTENCE_API_TOKEN'),
    ]
    return [token.strip() for token in tokens if token and token.strip()]


def _bearer_token_valid():
    header = request.headers.get('Authorization', '')
    return any(hmac.compare_digest(header, f'Bearer {token}') for token in _configured_bearer_tokens())


def _bearer_token():
    header = request.headers.get('Authorization', '').strip()
    prefix = 'Bearer '
    if not header.startswith(prefix):
        return ''
    return header[len(prefix):].strip()


def _upload_actor_user():
    """Return the user that should own an upload request, or (None, message)."""
    user, service = _api_principal()
    if user:
        return user, None
    if service:
        return None, None
    return None, 'Authentication required'


def _api_principal(required_scope='upload'):
    if current_user.is_authenticated:
        return current_user, False
    api_key = ApiKey.verify_token(_bearer_token(), required_scope='upload')
    if api_key:
        user = User.get_by_id(api_key.user_id)
        return user, False
    if not _bearer_token_valid():
        return None, False
    user_id = (
        request.headers.get('X-Asset-User-Id')
        or request.headers.get('X-User-Id')
        or os.environ.get('API_UPLOAD_USER_ID')
        or os.environ.get('ASSET_MANAGER_DEFAULT_USER_ID')
    )
    user = User.get_by_id(user_id) if user_id else None
    if not user:
        username = os.environ.get('API_UPLOAD_USERNAME') or os.environ.get('ASSET_MANAGER_DEFAULT_USERNAME')
        user = User.get_by_username(username) if username else None
    return user, True


def _require_api_principal():
    user, service = _api_principal()
    if user or service:
        return user, service, None
    return None, False, (jsonify({'error': 'Authentication required'}), 401)


def _can_write_model(model):
    user, service = _api_principal()
    if service:
        return True
    return bool(user and model.user_id == user.id)


def _can_read_world(world):
    return (
        world.is_public
        or _authorized_service_token()
        or (current_user.is_authenticated and world.owner_id == current_user.id)
    )


def _can_write_world(world=None):
    return (
        _authorized_service_token()
        or (
            current_user.is_authenticated
            and (world is None or world.owner_id in (None, current_user.id))
        )
    )


@api_bp.app_errorhandler(413)
def _request_too_large(error):
    """Return clean JSON for oversized requests on API routes.

    Flask aborts with 413 (RequestEntityTooLarge) before the view runs when a
    request body exceeds MAX_CONTENT_LENGTH. Without this, API clients would get
    an HTML error page and response.json() would throw. Only API routes get the
    JSON body; other routes keep Flask's default handling.
    """
    if request.path.startswith('/api/'):
        try:
            limit_mb = current_app.config['MAX_FILE_BYTES'] // (1024 * 1024)
        except Exception:
            limit_mb = None
        msg = 'File too large.'
        if limit_mb:
            msg = f'File too large. Maximum size is {limit_mb}MB per file.'
        return jsonify({'error': msg}), 413
    return error


@api_bp.route('/openapi.json')
def openapi_spec():
    """Serve the OpenAPI 3.0 spec as JSON."""
    # request.url_root is e.g. "https://host/" -> strip the trailing slash so
    # the spec's server URL points at this deployment's real origin.
    base_url = request.url_root.rstrip('/')
    return jsonify(get_openapi_spec(base_url=base_url))


@api_bp.route('/docs')
def swagger_ui():
    """Render Swagger UI (assets loaded from CDN) against /api/openapi.json."""
    spec_url = url_for('api.openapi_spec')
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>3D Asset Manager API – Swagger UI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
  <style>body {{ margin: 0; }}</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js" crossorigin></script>
  <script>
    window.onload = function () {{
      window.ui = SwaggerUIBundle({{
        url: "{spec_url}",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis],
      }});
    }};
  </script>
</body>
</html>"""
    return make_response(html, 200, {'Content-Type': 'text/html; charset=utf-8'})


@api_bp.route('/test')
def test_api():
    """Simple test endpoint to verify API is working"""
    return jsonify({
        'status': 'success',
        'message': 'API is working!',
        'timestamp': str(Model3D().upload_date)
    })

@api_bp.route('/models')
def list_models():
    """List models with pagination and search"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)  # Max 100 per page
        search = request.args.get('search', '').strip()
        user_only = request.args.get('user_only', 'false').lower() == 'true'
        
        principal, service = _api_principal()
        if user_only and principal:
            # Get user's models
            models, total = Model3D.get_user_models(principal.id, page=page, per_page=per_page)
        elif user_only and service:
            return jsonify({'error': 'API token is valid, but no API upload user is configured.'}), 409
        else:
            # Get public models
            models, total = Model3D.get_public_models(page=page, per_page=per_page, search=search if search else None)
        
        # Convert models to JSON-serializable format
        models_data = []
        for model in models:
            owner = User.get_by_id(model.user_id)
            models_data.append({
                'id': model.id,
                'name': model.name,
                'description': model.description,
                'file_format': model.file_format,
                'file_size': model.file_size,
                'original_filename': model.original_filename,
                'is_public': model.is_public,
                'upload_date': model.upload_date.isoformat() if model.upload_date else None,
                'download_count': model.download_count,
                'conversion_status': model.conversion_status,
                'has_viewable': bool(model.viewable_file_id),
                'has_vrma': bool(model.vrma_file_id),
                'tags': model.tags,
                'ai_status': model.ai_status,
                'ai_description': model.ai_description,
                'ai_tags': model.ai_tags,
                'approve_game_ready': model.approve_game_ready,
                'approve_asset_store': model.approve_asset_store,
                'owner': {
                    'id': owner.id if owner else None,
                    'username': owner.username if owner else 'Unknown'
                }
            })
        
        # Calculate pagination info
        total_pages = (total + per_page - 1) // per_page
        
        return jsonify({
            'models': models_data,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': total_pages,
                'has_prev': page > 1,
                'has_next': page < total_pages
            }
        })
        
    except Exception as e:
        print(f"API list models error: {e}")
        return jsonify({'error': 'Failed to retrieve models'}), 500

@api_bp.route('/download/<model_id>')
def download_model(model_id):
    """Download model file"""
    try:
        model = Model3D.get_by_id(model_id)
        
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403
        
        # Get file data from the database-backed file store.
        file_data = model.get_file_data()
        
        if not file_data:
            return jsonify({'error': 'File not found'}), 404
        
        # Increment download counter
        model.increment_download_count()
        
        # Create response
        response = make_response(file_data)
        response.headers['Content-Type'] = _mime_for(model.file_format)
        response.headers['Content-Disposition'] = f'attachment; filename="{model.original_filename}"'
        response.headers['Content-Length'] = str(len(file_data))
        
        return response
        
    except Exception as e:
        print(f"API download error: {e}")
        return jsonify({'error': 'Download failed'}), 500

@api_bp.route('/view/<model_id>')
def view_model(model_id):
    """Serve the renderable model file for inline 3D viewing."""
    try:
        model = Model3D.get_by_id(model_id)
        
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        file_data, view_format = model.get_viewable_data()
        
        if not file_data:
            return jsonify({'error': 'File not found'}), 404
        if (view_format or '').lower() in ('glb', 'vrm'):
            file_data = _force_meshopt_required_for_external_fallback(file_data)

        # Create response for viewing (not download)
        response = make_response(file_data)
        response.headers['Content-Type'] = _mime_for(view_format)
        response.headers['Content-Length'] = str(len(file_data))
        response.headers['Cache-Control'] = 'public, max-age=3600'  # Cache for 1 hour
        
        return response
        
    except Exception as e:
        print(f"API view error: {e}")
        return jsonify({'error': 'View failed'}), 500


@api_bp.route('/model/<model_id>/game-optimized')
def get_game_optimized(model_id):
    """Serve the game-optimized GLB variant attached to a model.

    Inline by default (for the detail-page viewer); pass ?download=1 for an
    attachment. Honors HTTP Range and uses the immutable variant file id as a
    strong ETag. 404 if the model has no game-optimized variant yet."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'game')
        if not variant or not variant.file_id:
            return jsonify({'error': 'No game-optimized variant'}), 404

        file_id = variant.file_id
        etag = f'"game-{file_id}"'
        cache_control = 'public, max-age=31536000, immutable'
        as_download = request.args.get('download') in ('1', 'true', 'yes')
        content_type = _mime_for('glb')
        download_name = f'{_safe_stem(model)}-game.glb'

        if request.if_none_match and etag in request.if_none_match:
            resp = make_response('', 304)
            resp.headers['ETag'] = etag
            resp.headers['Cache-Control'] = cache_control
            resp.headers['Accept-Ranges'] = 'bytes'
            return resp

        fs = current_app.config['FILE_STORE']
        range_header = request.headers.get('Range')

        if not as_download and range_header and range_header.startswith('bytes=') and hasattr(fs, 'get_range'):
            spec = range_header.split('=', 1)[1].split(',')[0].strip()
            start_s, _, end_s = spec.partition('-')
            try:
                start = int(start_s) if start_s else 0
                provisional_end = int(end_s) if end_s else None
                probe_end = provisional_end if provisional_end is not None else start
                _, total, _ = fs.get_range(file_id, start, probe_end)
                if total <= 0:
                    raise ValueError('empty')
                end = provisional_end if provisional_end is not None else total - 1
                end = min(end, total - 1)
                if start > end or start >= total:
                    resp = make_response('', 416)
                    resp.headers['Content-Range'] = f'bytes */{total}'
                    resp.headers['Accept-Ranges'] = 'bytes'
                    return resp
                chunk, total, _ = fs.get_range(file_id, start, end)
                resp = make_response(chunk, 206)
                resp.headers['Content-Type'] = content_type
                resp.headers['Content-Length'] = str(len(chunk))
                resp.headers['Content-Range'] = f'bytes {start}-{end}/{total}'
                resp.headers['Accept-Ranges'] = 'bytes'
                resp.headers['ETag'] = etag
                resp.headers['Cache-Control'] = cache_control
                return resp
            except Exception as e:
                print(f"Game-optimized range fetch fell back to full body: {e}")

        data = variant.read_data()
        if data is None:
            return jsonify({'error': 'Variant file not found'}), 404
        data = _force_meshopt_required_for_external_fallback(data)

        response = make_response(data)
        response.headers['Content-Type'] = content_type
        response.headers['Content-Length'] = str(len(data))
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['ETag'] = etag
        response.headers['Cache-Control'] = cache_control
        if as_download:
            response.headers['Content-Disposition'] = f'attachment; filename="{download_name}"'
        return response

    except Exception as e:
        print(f"API game-optimized fetch error: {e}")
        return jsonify({'error': 'Game-optimized fetch failed'}), 500


@api_bp.route('/model/<model_id>/status')
def conversion_status(model_id):
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403
        return jsonify({
            'status': model.conversion_status,
            'has_viewable': bool(model.viewable_file_id),
            'has_vrma': bool(model.vrma_file_id),
            'error': model.conversion_error,
            **_game_optimized_fields(model),
        })
    except Exception as e:
        print(f"API status error: {e}")
        return jsonify({'error': 'Status lookup failed'}), 500


_EXPORT_MESH_FORMATS = {'glb', 'gltf', 'obj', 'stl', 'ply', 'fbx', 'dae', '3ds'}


def _safe_stem(model):
    base = model.name or model.original_filename or 'model'
    stem = base.rsplit('.', 1)[0] if '.' in base else base
    keep = ''.join(c if c.isalnum() or c in ' -_' else '_' for c in stem).strip()
    return keep or 'model'


def _download_bytes(data, filename, mimetype):
    response = make_response(data)
    response.headers['Content-Type'] = mimetype
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.headers['Content-Length'] = str(len(data))
    return response


def _cached_export_file_id(model_id, fmt):
    engine = current_app.config["DB_ENGINE"]
    with engine.begin() as conn:
        rows = conn.execute(select(asset_files.c.id, asset_files.c.metadata)).all()
    for row in rows:
        metadata = row.metadata or {}
        if metadata.get('export_for') == str(model_id) and metadata.get('export_format') == fmt:
            return row.id
    return None


@api_bp.route('/export/<model_id>')
def export_model(model_id):
    import shutil
    import tempfile
    from app.conversion import assimp_export, tool_paths

    try:
        fmt = (request.args.get('format') or '').lower().strip()
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        if fmt == 'vrma':
            data = model.get_vrma_data()
            if not data:
                return jsonify({'error': 'No VRMA animation for this model.'}), 409
            return _download_bytes(data, f'{_safe_stem(model)}.vrma', _mime_for('vrma'))

        if fmt not in _EXPORT_MESH_FORMATS:
            return jsonify({'error': f'Unsupported export format: {fmt or "(none)"}'}), 400

        fs = current_app.config['FILE_STORE']
        cached_id = _cached_export_file_id(model.id, fmt)
        if cached_id:
            return _download_bytes(fs.get(cached_id).read(), f'{_safe_stem(model)}.{fmt}', _mime_for(fmt))

        src_bytes, src_fmt = model.get_viewable_data()
        if not src_bytes:
            return jsonify({'error': 'Source file not found'}), 404

        if fmt == (src_fmt or '').lower():
            return _download_bytes(src_bytes, f'{_safe_stem(model)}.{fmt}', _mime_for(fmt))

        if not current_app.config.get('ENABLE_CONVERSION', True):
            return jsonify({'error': 'Transcoding is disabled on this server.'}), 503

        workdir = tempfile.mkdtemp(prefix='export_')
        try:
            in_path = os.path.join(workdir, f'src.{src_fmt or "glb"}')
            out_path = os.path.join(workdir, f'out.{fmt}')
            with open(in_path, 'wb') as f:
                f.write(src_bytes)
            try:
                assimp_export(tool_paths(current_app)['assimp'], in_path, out_path, timeout=60)
            except Exception as e:
                print(f"Export transcode failed: {e}")
                return jsonify({'error': f'Could not export to {fmt}.'}), 502
            with open(out_path, 'rb') as f:
                out_bytes = f.read()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        try:
            fs.put(out_bytes, filename=f'export_{model.id}.{fmt}',
                   content_type=_mime_for(fmt),
                   metadata={'export_for': model.id, 'export_format': fmt})
        except Exception as e:
            print(f"Export cache warning: {e}")

        return _download_bytes(out_bytes, f'{_safe_stem(model)}.{fmt}', _mime_for(fmt))
    except Exception as e:
        print(f"API export error: {e}")
        return jsonify({'error': 'Export failed'}), 500

@api_bp.route('/model/<model_id>', methods=['PUT', 'PATCH'])
def update_model(model_id):
    """Update a model's metadata (name, description, visibility)."""
    try:
        model = Model3D.get_by_id(model_id)

        if not model:
            return jsonify({'error': 'Model not found'}), 404

        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403

        # Accept either JSON or form-encoded payloads
        data = request.get_json(silent=True) or request.form

        # Only update fields that were actually provided
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'error': 'Name cannot be empty.'}), 400
            model.name = name

        if 'description' in data:
            model.description = (data.get('description') or '').strip()

        if 'is_public' in data:
            raw = data.get('is_public')
            # Normalize bool from JSON (true/false) or form ('true'/'on'/'1')
            if isinstance(raw, bool):
                model.is_public = raw
            else:
                model.is_public = str(raw).lower() in ('true', 'on', '1', 'yes')

        if 'camera_orbit' in data:
            # Default <model-viewer> view angle, e.g. "180deg 75deg 105%".
            # Empty string resets to automatic framing (stored as None).
            orbit = (data.get('camera_orbit') or '').strip()
            model.camera_orbit = orbit or None

        if 'tags' in data:
            model.tags = Model3D.normalize_tags(data.get('tags'))

        if 'default_animation' in data:
            # Embedded-clip name to auto-play; empty clears it.
            clip = (data.get('default_animation') or '').strip()
            model.default_animation = clip or None

        if 'default_vrma_id' in data:
            # VRMA asset id to auto-apply on a VRM; empty clears it.
            vid = (data.get('default_vrma_id') or '').strip()
            model.default_vrma_id = vid or None

        model.save()

        return jsonify({
            'success': True,
            'message': 'Model updated successfully.',
            'model': {
                'id': model.id,
                'name': model.name,
                'description': model.description,
                'is_public': model.is_public,
                'camera_orbit': model.camera_orbit,
                'tags': model.tags,
                'default_animation': model.default_animation,
                'default_vrma_id': model.default_vrma_id,
            }
        })

    except Exception as e:
        print(f"API update error: {e}")
        return jsonify({'error': 'Update failed. Please try again.'}), 500


def _encode_thumbnail_webp(png_bytes):
    """Transcode captured PNG thumbnail bytes to WebP once, at upload time.

    Returns (bytes, content_type, filename_ext). Falls back to the original PNG
    if Pillow is unavailable or the image can't be decoded, so a missing
    optional dependency never breaks thumbnail upload."""
    try:
        from PIL import Image
    except Exception:
        return png_bytes, 'image/png', 'png'
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            # Flatten any alpha onto white (previews already render on white) so
            # the WebP isn't unexpectedly transparent, then encode.
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')
            out = io.BytesIO()
            img.save(out, format='WEBP', quality=82, method=4)
            return out.getvalue(), 'image/webp', 'webp'
    except Exception as e:
        print(f"Thumbnail WebP encode failed, storing PNG: {e}")
        return png_bytes, 'image/png', 'png'


@api_bp.route('/model/<model_id>/thumbnail', methods=['POST'])
def upload_thumbnail(model_id):
    """Store a client-captured thumbnail for a model (owner only).

    Accepts JSON {"image": "data:image/png;base64,...."} or a raw base64
    string. The PNG is transcoded to WebP once here and stored as WebP;
    replaces any existing thumbnail.
    """
    import base64

    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403

        data = request.get_json(silent=True) or {}
        image = data.get('image', '')
        if not image:
            return jsonify({'error': 'No image provided'}), 400

        # Strip a data-URL prefix if present
        if ',' in image and image.strip().lower().startswith('data:'):
            image = image.split(',', 1)[1]

        try:
            png_bytes = base64.b64decode(image)
        except Exception:
            return jsonify({'error': 'Invalid image data'}), 400

        # Sanity cap: thumbnails should be small (< 2MB)
        if not png_bytes or len(png_bytes) > 2 * 1024 * 1024:
            return jsonify({'error': 'Thumbnail missing or too large'}), 400

        fs = current_app.config['FILE_STORE']

        # Remove the previous thumbnail, if any
        if model.thumbnail_file_id:
            try:
                fs.delete(model.thumbnail_file_id)
            except Exception as e:
                print(f"Thumbnail cleanup warning: {e}")

        thumb_bytes, thumb_ct, thumb_ext = _encode_thumbnail_webp(png_bytes)
        new_id = fs.put(
            thumb_bytes,
            filename=f"thumb_{model_id}.{thumb_ext}",
            content_type=thumb_ct,
            metadata={'model_id': model_id, 'kind': 'thumbnail'}
        )
        model.thumbnail_file_id = str(new_id)
        model.save()

        return jsonify({'success': True, 'thumbnail_file_id': model.thumbnail_file_id})

    except Exception as e:
        print(f"API thumbnail upload error: {e}")
        return jsonify({'error': 'Thumbnail upload failed'}), 500


@api_bp.route('/model/<model_id>/thumbnail', methods=['GET'])
def get_thumbnail(model_id):
    """Serve a model's thumbnail (WebP for new uploads, PNG for older ones).
    404 if none (frontend shows a fallback)."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model or not model.thumbnail_file_id:
            return jsonify({'error': 'No thumbnail'}), 404

        # Respect privacy: private models' thumbnails are owner-only
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        # The thumbnail file id is immutable for a given image (regenerating the
        # default view creates a NEW file id), so it is a safe, strong ETag and
        # lets us cache aggressively while still busting on regeneration.
        etag = f'"thumb-{model.thumbnail_file_id}"'
        if request.if_none_match and etag in request.if_none_match:
            resp = make_response('', 304)
            resp.headers['ETag'] = etag
            resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            return resp

        fs = current_app.config['FILE_STORE']
        grid_out = fs.get(model.thumbnail_file_id)
        img_bytes = grid_out.read()

        response = make_response(img_bytes)
        response.headers['Content-Type'] = getattr(grid_out, 'content_type', None) or 'image/webp'
        response.headers['Content-Length'] = str(len(img_bytes))
        response.headers['ETag'] = etag
        # Immutable: a changed thumbnail has a different file id (and ETag), so
        # browsers can cache for a long time and revalidate cheaply via ETag.
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    except Exception as e:
        print(f"API thumbnail fetch error: {e}")
        return jsonify({'error': 'Thumbnail fetch failed'}), 404


@api_bp.route('/vrma')
def list_vrma():
    """List VRMA animation assets available to apply on a VRM avatar:
    the current user's own VRMA assets plus all public ones."""
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        items = []
        for model in Model3D.list_vrma_for_user(user_id):
            items.append({
                'id': model.id,
                'name': model.name or 'Untitled',
                'view_url': url_for('api.view_model', model_id=model.id),
                'source': 'upload',
            })
        for model in Model3D.list_generated_vrma_for_user(user_id):
            items.append({
                'id': model.id + ':vrma',
                'name': (model.name or 'Untitled') + ' (animation)',
                'view_url': url_for('api.export_model', model_id=model.id) + '?format=vrma',
                'source': 'generated',
            })
        return jsonify({'animations': items})
    except Exception as e:
        print(f"API list vrma error: {e}")
        return jsonify({'animations': []})


@api_bp.route('/model/<model_id>/preview', methods=['POST'])
def upload_preview(model_id):
    """Store a client-captured looping preview video (WebM) for a model
    (owner only). Sent as raw bytes (Content-Type: video/webm)."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403

        video_bytes = request.get_data()
        # Cap preview size (~8MB) so a stray long recording can't bloat storage.
        if not video_bytes or len(video_bytes) > 8 * 1024 * 1024:
            return jsonify({'error': 'Preview missing or too large'}), 400

        fs = current_app.config['FILE_STORE']

        # Remove the previous preview, if any
        if model.preview_file_id:
            try:
                fs.delete(model.preview_file_id)
            except Exception as e:
                print(f"Preview cleanup warning: {e}")

        content_type = request.content_type or 'video/webm'
        new_id = fs.put(
            video_bytes,
            filename=f"preview_{model_id}.webm",
            content_type=content_type,
            metadata={'model_id': model_id, 'kind': 'preview'}
        )
        model.preview_file_id = str(new_id)
        model.save()

        return jsonify({'success': True, 'preview_file_id': model.preview_file_id})

    except Exception as e:
        print(f"API preview upload error: {e}")
        return jsonify({'error': 'Preview upload failed'}), 500


@api_bp.route('/model/<model_id>/preview', methods=['GET'])
def get_preview(model_id):
    """Serve a model's looping preview video. Supports HTTP Range requests so
    browsers can stream/seek without downloading the whole clip. 404 if none."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model or not model.preview_file_id:
            return jsonify({'error': 'No preview'}), 404

        if not model.is_public:
            if not current_user.is_authenticated or model.user_id != current_user.id:
                return jsonify({'error': 'Access denied'}), 403

        file_id = model.preview_file_id
        etag = f'"preview-{file_id}"'
        cache_control = 'public, max-age=31536000, immutable'

        # Cheap revalidation: a new preview gets a new file id (and ETag).
        if request.if_none_match and etag in request.if_none_match:
            resp = make_response('', 304)
            resp.headers['ETag'] = etag
            resp.headers['Cache-Control'] = cache_control
            resp.headers['Accept-Ranges'] = 'bytes'
            return resp

        fs = current_app.config['FILE_STORE']
        range_header = request.headers.get('Range')

        if range_header and range_header.startswith('bytes=') and hasattr(fs, 'get_range'):
            # Parse a single "bytes=start-end" range (end optional).
            spec = range_header.split('=', 1)[1].split(',')[0].strip()
            start_s, _, end_s = spec.partition('-')
            try:
                # Need the total size first; get_range returns it.
                start = int(start_s) if start_s else 0
                provisional_end = int(end_s) if end_s else None
                # Fetch a probe range to learn the total, then clamp.
                probe_end = provisional_end if provisional_end is not None else start
                _, total, content_type = fs.get_range(file_id, start, probe_end)
                if total <= 0:
                    raise ValueError('empty')
                end = provisional_end if provisional_end is not None else total - 1
                end = min(end, total - 1)
                if start > end or start >= total:
                    resp = make_response('', 416)
                    resp.headers['Content-Range'] = f'bytes */{total}'
                    resp.headers['Accept-Ranges'] = 'bytes'
                    return resp
                chunk, total, content_type = fs.get_range(file_id, start, end)
                resp = make_response(chunk, 206)
                resp.headers['Content-Type'] = content_type or 'video/webm'
                resp.headers['Content-Length'] = str(len(chunk))
                resp.headers['Content-Range'] = f'bytes {start}-{end}/{total}'
                resp.headers['Accept-Ranges'] = 'bytes'
                resp.headers['ETag'] = etag
                resp.headers['Cache-Control'] = cache_control
                return resp
            except Exception as e:
                print(f"Preview range fetch fell back to full body: {e}")
                # Fall through to full-body response below.

        grid_out = fs.get(file_id)
        video_bytes = grid_out.read()

        response = make_response(video_bytes)
        response.headers['Content-Type'] = getattr(grid_out, 'content_type', None) or 'video/webm'
        response.headers['Content-Length'] = str(len(video_bytes))
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['ETag'] = etag
        response.headers['Cache-Control'] = cache_control
        return response

    except Exception as e:
        print(f"API preview fetch error: {e}")
        return jsonify({'error': 'Preview fetch failed'}), 404


def _serialize_browse_card(model):
    """Compact payload the browse gallery card needs (lazy-loaded client-side)."""
    return {
        'id': model.id,
        'name': model.name or 'Untitled',
        'file_format': model.file_format,
        'conversion_status': model.conversion_status,
        'download_count': model.download_count,
        'owner_username': getattr(model, 'owner_username', None) or 'Unknown',
        'tags': model.tags or [],
        'has_preview': bool(model.preview_file_id),
        'has_thumbnail': bool(model.thumbnail_file_id),
        'preview_url': url_for('api.get_preview', model_id=model.id) if model.preview_file_id else None,
        'thumbnail_url': url_for('api.get_thumbnail', model_id=model.id) if model.thumbnail_file_id else None,
        'detail_url': url_for('main.model_detail', model_id=model.id),
    }


@api_bp.route('/models/browse', methods=['GET'])
def list_public_models():
    """Paginated JSON list of public models for the browse gallery's infinite
    scroll. Mirrors the /browse query params (search, sort, tag, page).

    NOTE: distinct path from the public `/api/models` endpoint (list_models),
    which returns a different shape and lacks tag/sort filtering. Don't merge
    the two routes -- Flask would let the first-registered one shadow this."""
    try:
        search = (request.args.get('search') or '').strip()
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 24, type=int)
        per_page = max(1, min(per_page, 60))
        sort = request.args.get('sort', 'newest')
        tags = Model3D.normalize_tags(request.args.getlist('tag'))

        models_list, total = Model3D.get_public_models(
            page=page, per_page=per_page,
            search=search or None, sort=sort,
            tag=tags or None)

        for model in models_list:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'

        pages = (total + per_page - 1) // per_page if per_page else 0
        return jsonify({
            'models': [_serialize_browse_card(m) for m in models_list],
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': pages,
            'has_next': page < pages,
        })
    except Exception as e:
        print(f"API list public models error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Could not list models', 'models': [], 'has_next': False}), 500


@api_bp.route('/model/<model_id>', methods=['DELETE'])
def delete_model(model_id):
    """Delete a model"""
    try:
        model = Model3D.get_by_id(model_id)
        
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403
        
        # Delete model and file
        model.delete()
        
        return jsonify({'message': 'Model deleted successfully'})
        
    except Exception as e:
        print(f"API delete error: {e}")
        return jsonify({'error': 'Delete failed'}), 500

@api_bp.route('/stats')
def get_stats():
    """Get platform statistics"""
    try:
        stats = Model3D.get_stats()
        return jsonify(stats)
        
    except Exception as e:
        print(f"API stats error: {e}")
        return jsonify({'error': 'Failed to retrieve statistics'}), 500


@api_bp.route('/tellus/worlds')
def list_tellus_worlds():
    """List public worlds, or the current user's accessible worlds."""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        search = request.args.get('search', '').strip()
        user_only = request.args.get('user_only', 'false').lower() == 'true'
        user_id = current_user.id if current_user.is_authenticated else None
        service_token = _authorized_service_token()

        worlds, total = WorldState.list_worlds(
            page=page,
            per_page=per_page,
            search=search if search else None,
            user_id=user_id,
            public_only=False if service_token else not user_only,
        )
        total_pages = (total + per_page - 1) // per_page
        return jsonify({
            'worlds': [world.to_api(include_state=False) for world in worlds],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': total_pages,
                'has_prev': page > 1,
                'has_next': page < total_pages,
            },
        })
    except Exception as e:
        print(f"Tellus list worlds error: {e}")
        return jsonify({'error': 'Failed to retrieve worlds'}), 500


@api_bp.route('/tellus/worlds/<world_id>/state', methods=['GET'])
def get_tellus_world_state(world_id):
    try:
        world = WorldState.get(world_id)
        if not world:
            return jsonify({'error': 'World not found'}), 404
        if not _can_read_world(world):
            return jsonify({'error': 'Access denied'}), 403
        return jsonify(world.to_api(include_state=True))
    except Exception as e:
        print(f"Tellus get world error: {e}")
        return jsonify({'error': 'Failed to retrieve world'}), 500


@api_bp.route('/tellus/worlds/<world_id>/state', methods=['PUT'])
def put_tellus_world_state(world_id):
    try:
        existing = WorldState.get(world_id)
        if not _can_write_world(existing):
            return jsonify({'error': 'Access denied'}), 403

        payload = request.get_json(silent=True) or {}
        if payload.get('worldId') not in (None, world_id):
            return jsonify({'error': 'worldId mismatch'}), 400

        owner_id = current_user.id if current_user.is_authenticated else None
        world = WorldState.upsert(world_id, payload, owner_id=owner_id)
        return jsonify(world.to_api(include_state=True))
    except Exception as e:
        print(f"Tellus save world error: {e}")
        return jsonify({'error': 'Failed to save world'}), 500


@api_bp.route('/tellus/worlds/<world_id>', methods=['PATCH'])
def patch_tellus_world_metadata(world_id):
    try:
        world = WorldState.get(world_id)
        if not world:
            return jsonify({'error': 'World not found'}), 404
        if not _can_write_world(world):
            return jsonify({'error': 'Access denied'}), 403

        payload = request.get_json(silent=True) or {}
        world = world.patch_metadata(payload)
        return jsonify(world.to_api(include_state=False))
    except Exception as e:
        print(f"Tellus patch world error: {e}")
        return jsonify({'error': 'Failed to update world'}), 500

@api_bp.route('/user/models')
def get_user_models():
    """Get current user's models"""
    try:
        principal, service, auth_error = _require_api_principal()
        if auth_error:
            return auth_error
        if service and not principal:
            return jsonify({'error': 'API token is valid, but no API upload user is configured.'}), 409
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        
        models, total = Model3D.get_user_models(principal.id, page=page, per_page=per_page)
        
        models_data = []
        for model in models:
            models_data.append({
                'id': model.id,
                'name': model.name,
                'description': model.description,
                'file_format': model.file_format,
                'file_size': model.file_size,
                'original_filename': model.original_filename,
                'is_public': model.is_public,
                'upload_date': model.upload_date.isoformat() if model.upload_date else None,
                'download_count': model.download_count,
                'conversion_status': model.conversion_status,
                'has_viewable': bool(model.viewable_file_id),
                'has_vrma': bool(model.vrma_file_id),
                'tags': model.tags,
                'ai_status': model.ai_status,
                'ai_description': model.ai_description,
                'ai_tags': model.ai_tags,
                'approve_game_ready': model.approve_game_ready,
                'approve_asset_store': model.approve_asset_store,
            })
        
        total_pages = (total + per_page - 1) // per_page
        
        return jsonify({
            'models': models_data,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': total_pages,
                'has_prev': page > 1,
                'has_next': page < total_pages
            }
        })
        
    except Exception as e:
        print(f"API user models error: {e}")
        return jsonify({'error': 'Failed to retrieve user models'}), 500

def _name_from_filename(original_filename):
    """Derive a human-friendly model name from a filename.

    Strips the directory part (folder uploads send paths like "robot/arm.glb"),
    drops the extension, and turns separators into spaces.
    e.g. "robot/walk_cycle.glb" -> "walk cycle".
    """
    base = original_filename.replace('\\', '/').split('/')[-1]
    stem = base.rsplit('.', 1)[0] if '.' in base else base
    cleaned = stem.replace('_', ' ').replace('-', ' ').strip()
    return cleaned or base


def _store_one_upload(file, base_name, description, is_public, tags, allowed_extensions, fs, max_bytes, owner_id=None):
    """Validate and persist a single uploaded file as a Model3D.

    Returns (model, None) on success or (None, error_message) on failure.
    """
    from werkzeug.utils import secure_filename

    if not file or file.filename == '':
        return None, 'Empty file.'

    filename = secure_filename(file.filename)
    file_extension = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''

    if file_extension not in allowed_extensions:
        return None, f'File type not supported (.{file_extension or "none"}).'

    file_content = file.read()
    file_size = len(file_content)

    if file_size == 0:
        return None, 'File is empty.'
    if file_size > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        return None, f'File too large. Maximum size is {limit_mb}MB.'

    # Per-file name: when a shared base name is given AND multiple files are
    # involved, the caller passes base_name="" so each model is named from its
    # own filename. A single-file upload keeps the typed name.
    model_name = base_name or _name_from_filename(file.filename)

    gridfs_file_id = fs.put(
        file_content,
        filename=filename,
        content_type=file.content_type,
        metadata={
            'original_filename': file.filename,
            'uploaded_by': owner_id,
            'upload_date': Model3D().upload_date
        }
    )

    model = Model3D(
        name=model_name,
        description=description,
        file_format=file_extension,
        file_size=file_size,
        original_filename=file.filename,
        user_id=owner_id,
        is_public=is_public,
        gridfs_file_id=str(gridfs_file_id),
        tags=tags
    )
    from app.conversion import enqueue
    enqueue(model, enabled=current_app.config.get('ENABLE_CONVERSION', True))
    model.save()
    return model, None


def _serialize_model(model):
    return {
        'id': model.id,
        'name': model.name,
        'description': model.description,
        'file_format': model.file_format,
        'file_size': model.file_size,
        'original_filename': model.original_filename,
        'is_public': model.is_public,
        'upload_date': model.upload_date.isoformat() if model.upload_date else None,
        'conversion_status': model.conversion_status,
        'has_viewable': bool(model.viewable_file_id),
        'has_vrma': bool(model.vrma_file_id),
        'tags': model.tags,
        'ai_status': model.ai_status,
        'ai_description': model.ai_description,
        'ai_tags': model.ai_tags,
        'approve_game_ready': model.approve_game_ready,
        'approve_asset_store': model.approve_asset_store,
        **_game_optimized_fields(model),
    }


def _game_optimized_fields(model):
    """Summary of the model's game-optimized variant (if any) for serialization."""
    variant = ModelVariant.get(model.id, 'game')
    if not variant or not variant.file_id:
        return {'has_game_optimized': False, 'game_optimized': None}
    return {
        'has_game_optimized': True,
        'game_optimized': {
            'size': variant.size,
            'settings': variant.settings,
            'status': variant.status,
            'updated_at': variant.updated_at.isoformat() if variant.updated_at else None,
            'url': url_for('api.get_game_optimized', model_id=model.id),
            'download_url': url_for('api.get_game_optimized', model_id=model.id, download=1),
        },
    }


def _payload():
    return request.get_json(silent=True) or request.form or {}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in ('true', 'on', '1', 'yes')


def _merge_tags(*tag_lists):
    merged = []
    for tags in tag_lists:
        for tag in Model3D.normalize_tags(tags):
            if tag not in merged:
                merged.append(tag)
    return merged


def _optimize_game_int(data, key, default, allowed=None):
    raw = data.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{key} must be an integer.')
    if allowed is not None and value not in allowed:
        allowed_values = ', '.join(str(item) for item in allowed)
        raise ValueError(f'{key} must be one of: {allowed_values}.')
    return value


def _optimize_game_float(data, key, default):
    raw = data.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{key} must be a number.')
    if value <= 0 or value > 1:
        raise ValueError(f'{key} must be greater than 0 and at most 1.')
    return value


def _optimization_job_to_api(row):
    if not row:
        return None
    result = row.result or {}
    return {
        'id': row.id,
        'source_model_id': row.source_model_id,
        'status': row.status,
        'settings': row.settings or {},
        'result': result,
        'result_model_id': row.result_model_id,
        # The optimized GLB is now a variant on the source model, not a copy.
        'variant': result.get('variant'),
        'original_size': result.get('original_size'),
        'optimized_size': result.get('optimized_size'),
        'savings_ratio': result.get('savings_ratio'),
        'error': row.error,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        'started_at': row.started_at.isoformat() if row.started_at else None,
        'finished_at': row.finished_at.isoformat() if row.finished_at else None,
    }


def _get_optimization_job(job_id):
    engine = current_app.config['DB_ENGINE']
    with engine.begin() as conn:
        return conn.execute(
            select(optimization_jobs).where(optimization_jobs.c.id == str(job_id))
        ).mappings().first()


def _patch_optimization_job(app, job_id, **fields):
    fields['updated_at'] = datetime.utcnow()
    with app.config['DB_ENGINE'].begin() as conn:
        conn.execute(
            update(optimization_jobs)
            .where(optimization_jobs.c.id == str(job_id))
            .values(**fields)
        )


def _run_game_optimizer(model, owner_id, settings):
    import shutil
    import subprocess
    import tempfile

    gltfpack_bin = shutil.which('gltfpack')
    if not gltfpack_bin:
        raise RuntimeError('Game optimization is unavailable because gltfpack is not installed.')

    texture_limit = settings['texture_limit']
    simplify_ratio = settings['simplify_ratio']
    compression_mode = settings['compression_mode']
    texture_limit_applied = bool(texture_limit)

    src_bytes, src_fmt = model.get_viewable_data()
    src_fmt = (src_fmt or model.file_format or '').lower()
    if not src_bytes:
        raise FileNotFoundError('Source file not found')
    if src_fmt not in ('glb', 'gltf'):
        raise ValueError('Game optimization currently supports GLB/GLTF assets.')

    workdir = tempfile.mkdtemp(prefix='game_optimize_')
    try:
        in_path = os.path.join(workdir, f'input.{src_fmt}')
        out_path = os.path.join(workdir, 'game.glb')
        report_path = os.path.join(workdir, 'report.json')
        with open(in_path, 'wb') as f:
            f.write(src_bytes)

        cmd = [
            gltfpack_bin,
            '-i', in_path,
            '-o', out_path,
            '-si', f'{simplify_ratio:g}',
            '-r', report_path,
        ]
        if compression_mode == 'meshopt':
            cmd.append('-cc')
        if texture_limit:
            cmd.extend(['-tc', '-tl', str(texture_limit)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            msg = (result.stderr or result.stdout or 'gltfpack failed.').strip()
            raise RuntimeError(msg[-1000:] or 'gltfpack failed.')

        with open(out_path, 'rb') as f:
            out_bytes = f.read()

        report = {}
        if os.path.exists(report_path):
            try:
                with open(report_path, 'r', encoding='utf-8') as f:
                    report = json.load(f)
            except Exception as e:
                print(f"Could not read gltfpack report: {e}")

        fs = current_app.config['FILE_STORE']
        optimized_filename = f'{_safe_stem(model)}-game.glb'
        metadata = {
            'kind': 'game_optimized',
            'source_model_id': model.id,
            'source_format': src_fmt,
            'source_size': len(src_bytes),
            'optimized_size': len(out_bytes),
            'texture_limit': texture_limit if texture_limit_applied else None,
            'requested_texture_limit': texture_limit,
            'simplify_ratio': simplify_ratio,
            'gltfpack': {
                'mode': compression_mode,
                'texture_compression': texture_limit_applied,
                'texture_note': 'Texture cap applied with KTX2/Basis compression.' if texture_limit_applied else '',
                'report': report,
            },
        }
        file_id = fs.put(
            out_bytes,
            filename=optimized_filename,
            content_type=_mime_for('glb'),
            metadata=metadata,
        )

        original_size = len(src_bytes)
        optimized_size = len(out_bytes)
        savings_ratio = 0 if original_size <= 0 else 1 - (optimized_size / original_size)
        texture_note = 'KTX2/Basis' if texture_limit_applied else 'unchanged'

        # Attach the optimized GLB to the SOURCE model as a 'game' variant
        # (no separate Model3D). Re-optimizing replaces the existing variant;
        # the old blob is removed once the pointer is swapped.
        variant_settings = {
            'texture_limit': texture_limit,
            'simplify_ratio': simplify_ratio,
            'compression_mode': compression_mode,
            'texture_compression': texture_note,
            'original_size': original_size,
            'optimized_size': optimized_size,
            'savings_ratio': savings_ratio,
            'report': report,
        }
        variant, old_file_id = ModelVariant.upsert(
            model.id, 'game', str(file_id),
            file_format='glb', size=optimized_size,
            settings=variant_settings, status='ready',
        )
        if old_file_id and old_file_id != str(file_id):
            try:
                fs.delete(old_file_id)
            except Exception as e:
                print(f"Old game-optimized blob {old_file_id} not deleted: {e}")

        return {
            'success': True,
            'source_model_id': model.id,
            'variant': variant.to_api() if variant else None,
            'original_size': original_size,
            'optimized_size': optimized_size,
            'savings_ratio': savings_ratio,
            'settings': {
                'texture_limit': texture_limit,
                'simplify_ratio': simplify_ratio,
                'compression': 'gltfpack -cc' if compression_mode == 'meshopt' else 'gltfpack without mesh compression',
                'texture_compression': texture_note,
            },
            'report': report,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _process_game_optimization_job(app, job_id):
    with app.app_context():
        try:
            job = _get_optimization_job(job_id)
            if not job:
                return
            _patch_optimization_job(app, job_id, status='running', started_at=datetime.utcnow(), error=None)
            model = Model3D.get_by_id(job.source_model_id)
            if not model:
                raise FileNotFoundError('Model not found')
            result = _run_game_optimizer(model, job.owner_id or model.user_id, job.settings or {})
            _patch_optimization_job(
                app,
                job_id,
                status='done',
                result=result,
                # The optimized GLB now lives on the source model as a variant,
                # so the job resolves back to the source model itself.
                result_model_id=result.get('source_model_id'),
                finished_at=datetime.utcnow(),
                error=None,
            )
        except Exception as e:
            msg = str(e)[:1000] or 'Game optimization failed'
            print(f"Game optimization job {job_id} failed: {msg}", flush=True)
            _patch_optimization_job(
                app,
                job_id,
                status='failed',
                error=msg,
                finished_at=datetime.utcnow(),
            )


def _start_game_optimization_thread(app, job_id):
    thread = threading.Thread(
        target=_process_game_optimization_job,
        args=(app, job_id),
        name=f'game-optimizer-{job_id[:8]}',
        daemon=True,
    )
    thread.start()


@api_bp.route('/model/<model_id>/optimize-game', methods=['POST'])
def optimize_model_for_game(model_id):
    """Queue a game-optimized GLB copy without replacing the source asset."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404

        principal, service = _api_principal()
        if not (principal or service):
            return jsonify({'error': 'Authentication required'}), 401
        if not _can_access_model_as(model, principal, service):
            return jsonify({'error': 'Access denied'}), 403

        data = _payload()
        try:
            settings = {
                'texture_limit': _optimize_game_int(data, 'texture_limit', 1024, allowed=(0, 1024, 2048, 4096)),
                'simplify_ratio': _optimize_game_float(data, 'simplify_ratio', 0.75),
                'compression_mode': (data.get('compression_mode') or 'meshopt').strip().lower(),
            }
            if settings['compression_mode'] not in ('meshopt', 'fallback'):
                return jsonify({'error': 'compression_mode must be meshopt or fallback.'}), 400
            if data.get('name'):
                settings['name'] = str(data.get('name')).strip()
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        owner_id = principal.id if principal else model.user_id
        job_id = str(uuid.uuid4())
        now = datetime.utcnow()
        with current_app.config['DB_ENGINE'].begin() as conn:
            conn.execute(optimization_jobs.insert().values(
                id=job_id,
                source_model_id=model.id,
                owner_id=owner_id,
                status='queued',
                settings=settings,
                result={},
                result_model_id=None,
                error=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                finished_at=None,
            ))

        _start_game_optimization_thread(current_app._get_current_object(), job_id)
        job = _get_optimization_job(job_id)
        return jsonify({
            'success': True,
            'queued': True,
            'job': _optimization_job_to_api(job),
            'status_url': url_for('api.game_optimization_status', model_id=model.id, job_id=job_id),
        }), 202
    except Exception as e:
        print(f"API game optimization enqueue error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Game optimization could not be queued'}), 500


@api_bp.route('/model/<model_id>/optimize-game/<job_id>', methods=['GET'])
def game_optimization_status(model_id, job_id):
    try:
        job = _get_optimization_job(job_id)
        if not job or str(job.source_model_id) != str(model_id):
            return jsonify({'error': 'Optimization job not found'}), 404
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        principal, service = _api_principal()
        if not (_can_access_model_as(model, principal, service) or (principal and job.owner_id == principal.id)):
            return jsonify({'error': 'Access denied'}), 403
        return jsonify({'success': True, 'job': _optimization_job_to_api(job)})
    except Exception as e:
        print(f"API game optimization status error: {e}")
        return jsonify({'error': 'Optimization status failed'}), 500


@api_bp.route('/upload', methods=['POST'])
def upload_model():
    """Upload one or more 3D models.

    Accepts one or many files under the ``file`` form field. The web client
    sends one file per request (so the size limit is enforced per file); the
    endpoint also still accepts several repeated ``file`` fields in one request.
    Each file becomes its own model.

    Naming: if a ``name`` field is provided, it names the model — but only when
    a *single* file is in the request (a shared name can't apply to many files).
    When ``name`` is empty, or when multiple files are sent, each model is named
    from its own filename. This means a multi-file upload (one request per file,
    no name) does not require a name.
    """
    try:
        principal, _service, auth_error = _require_api_principal()
        if auth_error:
            return auth_error
        owner_id = principal.id if principal else None
        description = request.form.get('description', '').strip()
        is_public = request.form.get('is_public') == 'true'
        tags = Model3D.normalize_tags(request.form.get('tags', ''))

        # Collect all uploaded files (supports repeated 'file' fields).
        files = [f for f in request.files.getlist('file') if f and f.filename]
        if not files:
            return jsonify({'error': 'Please select a file to upload.'}), 400

        typed_name = request.form.get('name', '').strip()
        single = len(files) == 1

        # Use the typed name only for a single-file request; otherwise auto-name
        # each file from its own filename. An empty name is NOT an error — we
        # fall back to the filename. (The web UI still asks for a name on
        # single-file uploads for nicer UX, but the API no longer requires it,
        # so per-file batch uploads — one request each, no name — work.)
        base_name = typed_name if single else ''

        allowed_extensions = current_app.config['ALLOWED_EXTENSIONS']
        # Per-file limit (not the request-body cap), so each file is judged on
        # its own size regardless of how many are sent.
        max_bytes = current_app.config['MAX_FILE_BYTES']
        fs = current_app.config['FILE_STORE']

        uploaded, errors = [], []
        for file in files:
            model, err = _store_one_upload(
                file, base_name, description, is_public, tags,
                allowed_extensions, fs, max_bytes, owner_id=owner_id
            )
            if model:
                uploaded.append(model)
            else:
                errors.append({'filename': file.filename, 'error': err})

        # Single-file path: preserve the original response shape exactly.
        if single:
            if uploaded:
                model = uploaded[0]
                return jsonify({
                    'success': True,
                    'message': f'Model "{model.name}" uploaded successfully!',
                    'model': _serialize_model(model)
                }), 201
            return jsonify({'error': errors[0]['error']}), 400

        # Multi-file path: report per-file outcomes.
        status = 201 if uploaded else 400
        return jsonify({
            'success': bool(uploaded),
            'message': f'{len(uploaded)} of {len(files)} file(s) uploaded successfully.',
            'uploaded': [_serialize_model(m) for m in uploaded],
            'errors': errors,
        }), status

    except HTTPException:
        # Let Flask's error handlers run (e.g. the 413 handler returns clean
        # JSON when the request body exceeds MAX_CONTENT_LENGTH). Reading
        # request.form/files above can raise RequestEntityTooLarge.
        raise
    except Exception as e:
        print(f"API upload error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Upload failed. Please try again.'}), 500


@api_bp.route('/model/<model_id>/conversion', methods=['POST'])
def enqueue_conversion(model_id):
    """Requeue a model for converter processing."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403
        if not current_app.config.get('ENABLE_CONVERSION', True):
            return jsonify({'error': 'Conversion is disabled on this server.'}), 503
        from app.conversion import enqueue
        enqueue(model, enabled=True)
        model.conversion_error = None
        model.conversion_claimed_at = None
        model.save()
        return jsonify({'success': True, 'model': _serialize_model(model)})
    except Exception as e:
        print(f"API conversion enqueue error: {e}")
        return jsonify({'error': 'Conversion enqueue failed'}), 500


@api_bp.route('/model/<model_id>/ai/autotag', methods=['POST'])
def autotag_model(model_id):
    """Generate tags and a store-ready description for a model."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403

        data = _payload()
        overwrite = _as_bool(data.get('overwrite', True))
        include_description = _as_bool(data.get('include_description', True))

        try:
            from app.ai_enrichment import enrich_model
            enriched = enrich_model(model, extra_context=data.get('context') or {})
            model.ai_status = 'done'
            model.ai_error = None
        except Exception as e:
            enriched = {}
            model.ai_status = 'failed'
            model.ai_error = str(e)[:500]
            model.save()
            return jsonify({'error': 'AI enrichment failed', 'detail': model.ai_error}), 502

        model.ai_tags = Model3D.normalize_tags(enriched.get('tags', []))
        model.ai_description = enriched.get('description') or None
        model.ai_metadata = {
            'summary': enriched.get('summary'),
            'categories': enriched.get('categories', []),
            'quality_notes': enriched.get('quality_notes', []),
            'provider': enriched.get('provider'),
            'model': enriched.get('model'),
            'response_id': enriched.get('response_id'),
            'updated_at': datetime.utcnow().isoformat(),
        }
        if overwrite:
            model.tags = _merge_tags(model.ai_tags)
            if include_description and model.ai_description:
                model.description = model.ai_description
        else:
            model.tags = _merge_tags(model.tags, model.ai_tags)
            if include_description and not model.description and model.ai_description:
                model.description = model.ai_description
        model.save()
        return jsonify({'success': True, 'model': _serialize_model(model), 'enrichment': model.ai_metadata})
    except Exception as e:
        print(f"API autotag error: {e}")
        return jsonify({'error': 'AI enrichment failed'}), 500


@api_bp.route('/model/<model_id>/approval', methods=['PUT', 'PATCH'])
def update_approval(model_id):
    """Set game-ready and asset-store approval flags."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403
        data = _payload()
        if 'approve_game_ready' in data:
            model.approve_game_ready = _as_bool(data.get('approve_game_ready'))
        if 'approve_asset_store' in data:
            model.approve_asset_store = _as_bool(data.get('approve_asset_store'))
        if 'approval_notes' in data:
            model.approval_notes = (data.get('approval_notes') or '').strip() or None
        model.approval_updated_at = datetime.utcnow()
        model.tags = _merge_tags(
            model.tags,
            ['game-ready'] if model.approve_game_ready else [],
            ['asset-store'] if model.approve_asset_store else [],
        )
        model.save()
        return jsonify({'success': True, 'model': _serialize_model(model)})
    except Exception as e:
        print(f"API approval error: {e}")
        return jsonify({'error': 'Approval update failed'}), 500


def _build_bundle_zip(bundle, models_):
    fs = current_app.config['FILE_STORE']
    out = io.BytesIO()
    manifest = bundle.to_api(include_models=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))
        readme = f"# {bundle.name}\n\n{bundle.description}\n\nAssets: {len(models_)}\n"
        zf.writestr('README.md', readme)
        for model in models_:
            data, fmt = model.get_viewable_data()
            if not data:
                continue
            filename = model.original_filename or f"{_safe_stem(model)}.{fmt or model.file_format or 'bin'}"
            safe_name = filename.replace('\\', '/').split('/')[-1]
            zf.writestr(f"assets/{model.id}_{safe_name}", data)
    return out.getvalue()


@api_bp.route('/bundles', methods=['GET'])
def list_bundles():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        user_only = request.args.get('user_only', 'false').lower() == 'true'
        principal, service = _api_principal()
        bundles_, total = AssetBundle.list_for_user(
            user_id=principal.id if principal else None,
            page=page,
            per_page=per_page,
            public_only=not (user_only or service),
        )
        total_pages = (total + per_page - 1) // per_page
        return jsonify({
            'bundles': [bundle.to_api(include_models=False) for bundle in bundles_],
            'pagination': {
                'page': page, 'per_page': per_page, 'total': total,
                'pages': total_pages, 'has_prev': page > 1, 'has_next': page < total_pages,
            },
        })
    except Exception as e:
        print(f"API list bundles error: {e}")
        return jsonify({'error': 'Failed to retrieve bundles'}), 500


@api_bp.route('/bundles', methods=['POST'])
def create_bundle():
    try:
        principal, service, auth_error = _require_api_principal()
        if auth_error:
            return auth_error
        data = request.get_json(silent=True) or {}
        model_ids = [str(mid) for mid in data.get('model_ids', []) if mid]
        if not model_ids:
            return jsonify({'error': 'model_ids is required.'}), 400
        models_ = []
        for model_id in model_ids:
            model = Model3D.get_by_id(model_id)
            if not model:
                return jsonify({'error': f'Model not found: {model_id}'}), 404
            if not (service or _can_access_model(model) or (principal and model.user_id == principal.id)):
                return jsonify({'error': f'Access denied for model: {model_id}'}), 403
            models_.append(model)

        name = (data.get('name') or '').strip()
        if not name:
            name = f"{models_[0].name} Bundle" if models_ else "Asset Bundle"
        tags = _merge_tags(data.get('tags', []), *(model.tags for model in models_))
        bundle = AssetBundle(
            name=name,
            description=(data.get('description') or '').strip(),
            owner_id=principal.id if principal else None,
            is_public=_as_bool(data.get('is_public', False)),
            model_ids=model_ids,
            tags=tags,
            status=data.get('status') or 'draft',
            metadata={
                'approve_game_ready': all(model.approve_game_ready for model in models_),
                'approve_asset_store': all(model.approve_asset_store for model in models_),
                'created_by_api': True,
            },
        ).save()

        if _as_bool(data.get('create_zip', True)):
            zip_bytes = _build_bundle_zip(bundle, models_)
            file_id = current_app.config['FILE_STORE'].put(
                zip_bytes,
                filename=f"bundle_{bundle.id}.zip",
                content_type='application/zip',
                metadata={'bundle_id': bundle.id, 'kind': 'bundle'},
            )
            bundle.file_id = str(file_id)
            bundle.save()

        return jsonify({'success': True, 'bundle': bundle.to_api(include_models=True)}), 201
    except Exception as e:
        print(f"API create bundle error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Bundle creation failed'}), 500


@api_bp.route('/bundles/<bundle_id>', methods=['GET'])
def get_bundle(bundle_id):
    try:
        bundle = AssetBundle.get(bundle_id)
        if not bundle:
            return jsonify({'error': 'Bundle not found'}), 404
        principal, service = _api_principal()
        if not (bundle.is_public or service or (principal and bundle.owner_id == principal.id)):
            return jsonify({'error': 'Access denied'}), 403
        return jsonify({'bundle': bundle.to_api(include_models=True)})
    except Exception as e:
        print(f"API get bundle error: {e}")
        return jsonify({'error': 'Failed to retrieve bundle'}), 500


@api_bp.route('/bundles/<bundle_id>/download', methods=['GET'])
def download_bundle(bundle_id):
    try:
        bundle = AssetBundle.get(bundle_id)
        if not bundle:
            return jsonify({'error': 'Bundle not found'}), 404
        principal, service = _api_principal()
        if not (bundle.is_public or service or (principal and bundle.owner_id == principal.id)):
            return jsonify({'error': 'Access denied'}), 403
        if not bundle.file_id:
            return jsonify({'error': 'Bundle zip has not been created.'}), 404
        data = current_app.config['FILE_STORE'].get(bundle.file_id).read()
        return _download_bytes(data, f'{_safe_stem(bundle)}.zip', 'application/zip')
    except Exception as e:
        print(f"API download bundle error: {e}")
        return jsonify({'error': 'Bundle download failed'}), 500
