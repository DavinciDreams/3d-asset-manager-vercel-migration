from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, current_app, make_response, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException
from sqlalchemy import cast, func, or_, select, String, true, update
from app.db import asset_files, model_variants, models as model_rows, optimization_jobs
from app.models import ApiKey, AssetBundle, Model3D, ModelVariant, User, WorldState
from app.openapi import get_openapi_spec
from app.permissions import asset_admin_configured, can_manage_model, is_asset_admin_user
from pathlib import Path
from werkzeug.datastructures import FileStorage
import base64
import hashlib
import hmac
import io
import json
import os
import re
import struct
import threading
import uuid
import zipfile

api_bp = Blueprint('api', __name__)
AI_ENRICHMENT_WORKER = None
AI_ENRICHMENT_KICK_THREAD = None
AI_ENRICHMENT_KICK_LOCK = threading.Lock()
PIPELINE_RECONCILER_WORKER = None
PIPELINE_RECONCILER_LOCK = threading.Lock()

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
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
}


def _mime_for(fmt):
    return MIME_TYPES.get((fmt or '').lower(), 'application/octet-stream')


_GLB_MAGIC = b'glTF'
_GLB_JSON_CHUNK = 0x4E4F534A
_GLB_BIN_CHUNK = 0x004E4942


def _json_chunk_bytes(payload):
    raw = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    padding = (-len(raw)) % 4
    return raw + (b' ' * padding)


def _rewrite_glb_json(glb_bytes, rewrite):
    if not glb_bytes or glb_bytes[:4] != _GLB_MAGIC or len(glb_bytes) < 20:
        raise ValueError('Expected binary GLB bytes.')
    magic, version, declared_length = struct.unpack_from('<4sII', glb_bytes, 0)
    if magic != _GLB_MAGIC or version != 2 or declared_length > len(glb_bytes):
        raise ValueError('Invalid GLB header.')

    offset = 12
    chunks = []
    json_index = None
    while offset + 8 <= declared_length:
        chunk_length, chunk_type = struct.unpack_from('<II', glb_bytes, offset)
        data_start = offset + 8
        data_end = data_start + chunk_length
        if data_end > declared_length:
            raise ValueError('Invalid GLB chunk length.')
        chunk_data = glb_bytes[data_start:data_end]
        if chunk_type == _GLB_JSON_CHUNK and json_index is None:
            json_index = len(chunks)
        chunks.append((chunk_type, chunk_data))
        offset = data_end
    if json_index is None:
        raise ValueError('GLB has no JSON chunk.')

    gltf = json.loads(chunks[json_index][1].decode('utf-8').rstrip(' \t\r\n\0'))
    rewritten = rewrite(gltf)
    json_bytes = _json_chunk_bytes(rewritten if rewritten is not None else gltf)
    chunks[json_index] = (_GLB_JSON_CHUNK, json_bytes)

    rebuilt = bytearray(struct.pack('<4sII', _GLB_MAGIC, 2, 0))
    for chunk_type, chunk_data in chunks:
        padding = b'\0' * ((-len(chunk_data)) % 4) if chunk_type == _GLB_BIN_CHUNK else b' ' * ((-len(chunk_data)) % 4)
        padded = chunk_data + padding
        rebuilt.extend(struct.pack('<II', len(padded), chunk_type))
        rebuilt.extend(padded)
    struct.pack_into('<I', rebuilt, 8, len(rebuilt))
    return bytes(rebuilt)


def _flatten_lod_glb_materials(glb_bytes, *, color=None):
    flat_color = color or [0.30, 0.42, 0.20, 1.0]

    def rewrite(gltf):
        used = [ext for ext in (gltf.get('extensionsUsed') or []) if ext not in {'KHR_texture_transform', 'KHR_materials_unlit'}]
        required = [ext for ext in (gltf.get('extensionsRequired') or []) if ext in used]
        if used:
            gltf['extensionsUsed'] = used
        else:
            gltf.pop('extensionsUsed', None)
        if required:
            gltf['extensionsRequired'] = required
        else:
            gltf.pop('extensionsRequired', None)

        gltf.pop('images', None)
        gltf.pop('textures', None)
        gltf.pop('samplers', None)
        gltf['materials'] = [
            {
                'name': 'LOD flat silhouette',
                'pbrMetallicRoughness': {
                    'baseColorFactor': flat_color,
                    'roughnessFactor': 0.95,
                    'metallicFactor': 0.0,
                },
            }
        ]
        for mesh in gltf.get('meshes') or []:
            if not isinstance(mesh, dict):
                continue
            for primitive in mesh.get('primitives') or []:
                if isinstance(primitive, dict):
                    primitive['material'] = 0
        return gltf

    return _rewrite_glb_json(glb_bytes, rewrite)


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


def _gltf_container_json_from_bytes(data):
    if not data:
        return None
    try:
        if data[:4] == _GLB_MAGIC:
            if len(data) < 20:
                return None
            magic, version, declared_length = struct.unpack_from('<4sII', data, 0)
            if magic != _GLB_MAGIC or version != 2 or declared_length > len(data):
                return None
            offset = 12
            while offset + 8 <= declared_length:
                chunk_length, chunk_type = struct.unpack_from('<II', data, offset)
                data_start = offset + 8
                data_end = data_start + chunk_length
                if data_end > declared_length:
                    return None
                if chunk_type == _GLB_JSON_CHUNK:
                    return json.loads(data[data_start:data_end].decode('utf-8').rstrip(' \t\r\n\0'))
                offset = data_end
            return None
        return json.loads(data.decode('utf-8', errors='ignore'))
    except Exception as error:
        print(f"GLTF metadata parse warning: {error}")
        return None


def _gltf_node_names_from_bytes(data):
    gltf = _gltf_container_json_from_bytes(data)
    if not isinstance(gltf, dict):
        return set()
    return {
        node.get('name')
        for node in (gltf.get('nodes') or [])
        if isinstance(node, dict) and node.get('name')
    }


def _glb_is_meshopt_compressed(glb_bytes):
    """True if the GLB already uses EXT_meshopt_compression (i.e. it is already
    gltfpack/meshopt output). Such files are effectively already game-optimized,
    so we register them as the variant instead of re-running gltfpack (which
    fails on legacy -cf GLBs that reference a missing external fallback .bin)."""
    if not glb_bytes or glb_bytes[:4] != _GLB_MAGIC or len(glb_bytes) < 20:
        return False
    try:
        magic, version, declared_length = struct.unpack_from('<4sII', glb_bytes, 0)
        if magic != _GLB_MAGIC or version != 2 or declared_length > len(glb_bytes):
            return False
        offset = 12
        while offset + 8 <= declared_length:
            chunk_length, chunk_type = struct.unpack_from('<II', glb_bytes, offset)
            data_start = offset + 8
            data_end = data_start + chunk_length
            if data_end > declared_length:
                return False
            if chunk_type == _GLB_JSON_CHUNK:
                gltf = json.loads(glb_bytes[data_start:data_end].decode('utf-8').rstrip(' \t\r\n\0'))
                used = set(gltf.get('extensionsUsed') or [])
                return 'EXT_meshopt_compression' in used
            offset = data_end
    except Exception as e:
        print(f"meshopt detection warning: {e}")
    return False


def _gltf_uses_extension(file_content, file_extension, extension_name):
    gltf = _gltf_json_from_bytes(file_content, file_extension)
    if not isinstance(gltf, dict):
        return False
    used = set(gltf.get('extensionsUsed') or [])
    required = set(gltf.get('extensionsRequired') or [])
    if extension_name in used or extension_name in required:
        return True

    def contains_extension(value):
        if isinstance(value, dict):
            extensions = value.get('extensions')
            if isinstance(extensions, dict) and extension_name in extensions:
                return True
            return any(contains_extension(child) for child in value.values())
        if isinstance(value, list):
            return any(contains_extension(child) for child in value)
        return False

    return contains_extension(gltf)


def _gltf_json_from_bytes(file_content, file_extension):
    fmt = (file_extension or '').lower()
    try:
        if fmt == 'glb':
            if not file_content or file_content[:4] != _GLB_MAGIC or len(file_content) < 20:
                return None
            magic, version, declared_length = struct.unpack_from('<4sII', file_content, 0)
            if magic != _GLB_MAGIC or version != 2 or declared_length > len(file_content):
                return None
            offset = 12
            while offset + 8 <= declared_length:
                chunk_length, chunk_type = struct.unpack_from('<II', file_content, offset)
                data_start = offset + 8
                data_end = data_start + chunk_length
                if data_end > declared_length:
                    return None
                if chunk_type == _GLB_JSON_CHUNK:
                    return json.loads(file_content[data_start:data_end].decode('utf-8').rstrip(' \t\r\n\0'))
                offset = data_end
        if fmt == 'gltf':
            return json.loads(file_content.decode('utf-8', errors='ignore'))
    except Exception as error:
        print(f"GLTF metadata parse warning: {error}")
    return None


def _gltf_runtime_cost_metadata(file_content, file_extension, runtime=None, file_size=None):
    """Post-optimization cost hints for Tellus loaders and LOD decisions."""
    gltf = _gltf_json_from_bytes(file_content, file_extension)
    if not isinstance(gltf, dict):
        return {}

    runtime = runtime if isinstance(runtime, dict) else {}
    mesh_stats = runtime.get('mesh_stats') if isinstance(runtime.get('mesh_stats'), dict) else _gltf_mesh_stats(gltf)
    buffer_views = gltf.get('bufferViews') if isinstance(gltf.get('bufferViews'), list) else []
    accessors = gltf.get('accessors') if isinstance(gltf.get('accessors'), list) else []
    images = gltf.get('images') if isinstance(gltf.get('images'), list) else []
    textures = gltf.get('textures') if isinstance(gltf.get('textures'), list) else []
    extensions_used = set(gltf.get('extensionsUsed') or [])
    extensions_required = set(gltf.get('extensionsRequired') or [])

    def _buffer_view_size(index):
        try:
            view = buffer_views[int(index)]
        except (TypeError, ValueError, IndexError):
            return 0
        if not isinstance(view, dict):
            return 0
        try:
            return max(0, int(view.get('byteLength') or 0))
        except (TypeError, ValueError):
            return 0

    geometry_views = set()
    for accessor in accessors:
        if isinstance(accessor, dict) and accessor.get('bufferView') is not None:
            try:
                geometry_views.add(int(accessor.get('bufferView')))
            except (TypeError, ValueError):
                continue
    geometry_buffer_bytes = sum(_buffer_view_size(index) for index in geometry_views)

    image_bytes = []
    ktx2 = 'KHR_texture_basisu' in extensions_used or 'KHR_texture_basisu' in extensions_required
    for image in images:
        if not isinstance(image, dict):
            continue
        mime_type = str(image.get('mimeType') or '').lower()
        uri = str(image.get('uri') or '')
        if 'ktx2' in mime_type or uri.lower().endswith('.ktx2'):
            ktx2 = True
        byte_size = _buffer_view_size(image.get('bufferView'))
        if not byte_size and uri.startswith('data:'):
            try:
                _header, encoded = uri.split(',', 1)
                byte_size = len(base64.b64decode(encoded))
            except Exception:
                byte_size = 0
        image_bytes.append(byte_size)
    for texture in textures:
        if isinstance(texture, dict) and isinstance(texture.get('extensions'), dict):
            if 'KHR_texture_basisu' in texture['extensions']:
                ktx2 = True

    total_texture_bytes = sum(image_bytes)
    largest_texture_bytes = max(image_bytes) if image_bytes else 0
    texture_vram_bytes = total_texture_bytes if ktx2 else total_texture_bytes * 4
    approx_vram_bytes = geometry_buffer_bytes + texture_vram_bytes
    meshopt = 'EXT_meshopt_compression' in extensions_used or 'EXT_meshopt_compression' in extensions_required

    stats = {
        'triangle_count': mesh_stats.get('triangles') if isinstance(mesh_stats, dict) else None,
        'vertex_count': mesh_stats.get('vertices') if isinstance(mesh_stats, dict) else None,
        'primitive_count': mesh_stats.get('primitives') if isinstance(mesh_stats, dict) else None,
        'texture_count': len(textures) or len(images),
        'image_count': len(images),
        'largest_texture_bytes': largest_texture_bytes,
        'total_texture_bytes': total_texture_bytes,
        'geometry_buffer_bytes': geometry_buffer_bytes,
        'texture_vram_bytes': texture_vram_bytes,
        'approx_vram_bytes': approx_vram_bytes,
        'total_byte_size': int(file_size if file_size is not None else len(file_content or b'')),
        'ktx2': bool(ktx2),
        'meshopt': bool(meshopt),
    }
    return {key: value for key, value in stats.items() if value is not None}


def _file_derived_metadata(file_content, file_extension):
    gltf = _gltf_json_from_bytes(file_content, file_extension)
    if not isinstance(gltf, dict):
        return [], {}

    asset_types = []
    runtime = {}
    mesh_stats = _gltf_mesh_stats(gltf)
    if mesh_stats:
        runtime['mesh_stats'] = mesh_stats
    physical = _gltf_physical_metadata(gltf)
    if physical:
        runtime['physical'] = physical
    skins = gltf.get('skins') if isinstance(gltf.get('skins'), list) else []
    nodes = gltf.get('nodes') if isinstance(gltf.get('nodes'), list) else []
    has_skinned_mesh = any(isinstance(node, dict) and node.get('skin') is not None for node in nodes)
    has_joint_list = any(isinstance(skin, dict) and skin.get('joints') for skin in skins)
    if has_skinned_mesh or has_joint_list:
        asset_types.append('rigged')

    animations = gltf.get('animations') if isinstance(gltf.get('animations'), list) else []
    animation_items = []
    for index, animation in enumerate(animations):
        if not isinstance(animation, dict):
            continue
        name = str(animation.get('name') or f'animation-{index + 1}').strip()
        if not name:
            name = f'animation-{index + 1}'
        item = {'name': name}
        duration = _gltf_animation_duration(gltf, animation)
        if duration is not None:
            item['duration'] = duration
        animation_items.append(item)
    if animation_items:
        asset_types.append('animated')
        runtime['animations'] = animation_items

    return asset_types, runtime


def _validate_uploaded_gltf_bytes(data, file_format='glb'):
    fmt = (file_format or 'glb').lower()
    if fmt not in {'glb', 'gltf'}:
        return False
    gltf = _gltf_json_from_bytes(data, fmt)
    return isinstance(gltf, dict) and isinstance(gltf.get('asset'), dict)


def _gltf_mesh_stats(gltf):
    accessors = gltf.get('accessors') if isinstance(gltf.get('accessors'), list) else []
    meshes = gltf.get('meshes') if isinstance(gltf.get('meshes'), list) else []
    vertex_count = 0
    triangle_count = 0
    primitive_count = 0

    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        for primitive in mesh.get('primitives') or []:
            if not isinstance(primitive, dict):
                continue
            primitive_count += 1
            attributes = primitive.get('attributes') if isinstance(primitive.get('attributes'), dict) else {}
            position_index = attributes.get('POSITION')
            position_count = _gltf_accessor_count(accessors, position_index)
            vertex_count += position_count

            mode = primitive.get('mode', 4)
            if mode == 4:
                index_count = _gltf_accessor_count(accessors, primitive.get('indices'))
                triangle_count += (index_count or position_count) // 3

    stats = {}
    if vertex_count:
        stats['vertices'] = vertex_count
    if triangle_count:
        stats['triangles'] = triangle_count
    if primitive_count:
        stats['primitives'] = primitive_count
    return stats


def _gltf_accessor_count(accessors, index):
    if not isinstance(index, int) or index < 0 or index >= len(accessors):
        return 0
    accessor = accessors[index]
    if not isinstance(accessor, dict):
        return 0
    try:
        return max(0, int(accessor.get('count') or 0))
    except (TypeError, ValueError):
        return 0


def _gltf_physical_metadata(gltf):
    accessors = gltf.get('accessors') if isinstance(gltf.get('accessors'), list) else []
    meshes = gltf.get('meshes') if isinstance(gltf.get('meshes'), list) else []
    mins = [None, None, None]
    maxs = [None, None, None]

    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        for primitive in mesh.get('primitives') or []:
            if not isinstance(primitive, dict):
                continue
            attributes = primitive.get('attributes') if isinstance(primitive.get('attributes'), dict) else {}
            position_index = attributes.get('POSITION')
            if not isinstance(position_index, int) or position_index < 0 or position_index >= len(accessors):
                continue
            accessor = accessors[position_index]
            if not isinstance(accessor, dict):
                continue
            raw_min = accessor.get('min')
            raw_max = accessor.get('max')
            if not (isinstance(raw_min, list) and isinstance(raw_max, list) and len(raw_min) >= 3 and len(raw_max) >= 3):
                continue
            try:
                acc_min = [float(raw_min[i]) for i in range(3)]
                acc_max = [float(raw_max[i]) for i in range(3)]
            except (TypeError, ValueError):
                continue
            for i in range(3):
                mins[i] = acc_min[i] if mins[i] is None else min(mins[i], acc_min[i])
                maxs[i] = acc_max[i] if maxs[i] is None else max(maxs[i], acc_max[i])

    if any(value is None for value in mins) or any(value is None for value in maxs):
        return {}
    min_v = [float(v) for v in mins]
    max_v = [float(v) for v in maxs]
    size = [max(0.0, max_v[i] - min_v[i]) for i in range(3)]
    center = [(min_v[i] + max_v[i]) / 2 for i in range(3)]
    radius = (sum((axis / 2) ** 2 for axis in size)) ** 0.5
    height = size[1]
    physical = {
        'min': min_v,
        'max': max_v,
        'size': size,
        'center': center,
        'width': size[0],
        'height': height,
        'depth': size[2],
        'radius': radius,
    }
    if height > 0:
        # Scale factor to normalize the asset to 1 world unit tall. Tellus can
        # multiply this by category/avatar targets without trusting generator units.
        physical['suggested_scale'] = 1.0 / height
    return physical


def _gltf_animation_duration(gltf, animation):
    accessors = gltf.get('accessors') if isinstance(gltf.get('accessors'), list) else []
    max_time = None
    for sampler in animation.get('samplers') or []:
        if not isinstance(sampler, dict):
            continue
        input_index = sampler.get('input')
        if not isinstance(input_index, int) or input_index < 0 or input_index >= len(accessors):
            continue
        accessor = accessors[input_index]
        if not isinstance(accessor, dict):
            continue
        values = accessor.get('max')
        if isinstance(values, list) and values:
            try:
                time_value = float(values[0])
            except (TypeError, ValueError):
                continue
            max_time = time_value if max_time is None else max(max_time, time_value)
    return round(max_time, 3) if max_time is not None else None


def _can_access_model(model):
    if model.is_public:
        return True
    if _authorized_service_token():
        return True
    if current_user.is_authenticated and is_asset_admin_user(current_user):
        return True
    return current_user.is_authenticated and model.user_id == current_user.id


def _can_access_model_as(model, principal=None, service=False):
    if service or _can_access_model(model):
        return True
    if principal and is_asset_admin_user(principal):
        return True
    return bool(principal and model.user_id == principal.id)


def _authorized_service_token():
    return _bearer_token_valid()


def _configured_bearer_tokens():
    tokens = [
        os.environ.get('ASSET_MANAGER_API_TOKEN'),
        os.environ.get('API_UPLOAD_TOKEN'),
        os.environ.get('TELLUS_PERSISTENCE_API_TOKEN'),
        os.environ.get('TELLUS_ADMIN_API_TOKEN'),
    ]
    return [token.strip() for token in tokens if token and token.strip()]


def _bearer_token_valid():
    header = request.headers.get('Authorization', '')
    return any(hmac.compare_digest(header, f'Bearer {token}') for token in _configured_bearer_tokens())


def _tellus_admin_token_valid():
    token = (os.environ.get('TELLUS_ADMIN_API_TOKEN') or '').strip()
    if not token:
        return False
    return hmac.compare_digest(request.headers.get('Authorization', ''), f'Bearer {token}')


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


def _service_target_user():
    """Resolve the user a trusted service token wants to act as."""
    user_id = (
        request.headers.get('X-Asset-User-Id')
        or request.headers.get('X-User-Id')
        or (_tellus_admin_token_valid() and os.environ.get('TELLUS_ADMIN_USER_ID'))
        or os.environ.get('API_UPLOAD_USER_ID')
        or os.environ.get('ASSET_MANAGER_DEFAULT_USER_ID')
    )
    user = User.get_by_id(user_id) if user_id else None
    if user:
        return user

    username = (
        request.headers.get('X-Asset-Username')
        or request.headers.get('X-Username')
        or (_tellus_admin_token_valid() and os.environ.get('TELLUS_ADMIN_USERNAME'))
        or os.environ.get('API_UPLOAD_USERNAME')
        or os.environ.get('ASSET_MANAGER_DEFAULT_USERNAME')
    )
    return User.get_by_username(username) if username else None


def _with_generation_defaults(tags, asset_types):
    if not _tellus_admin_token_valid():
        return tags, asset_types
    world_tag = _tellus_world_tag(_tellus_world_id())
    tags = Model3D.normalize_tags([*(tags or []), 'tellus', *([world_tag] if world_tag else [])])
    return tags, asset_types


def _tellus_world_id():
    value = (
        request.form.get('worldId')
        or request.form.get('world_id')
        or request.form.get('tellusWorldId')
        or request.form.get('tellus_world_id')
        or request.headers.get('X-Tellus-World-Id')
        or request.headers.get('X-World-Id')
    )
    return str(value or '').strip()


def _tellus_world_tag(world_id):
    cleaned = ''.join(c if c.isalnum() else '-' for c in str(world_id or '').strip().lower())
    cleaned = '-'.join(part for part in cleaned.split('-') if part)
    return f'tellus-world-{cleaned}' if cleaned else None


_WORLD_ASSET_ID_KEYS = {
    'assetid', 'asset_id', 'assetstoreid', 'asset_store_id',
    'modelid', 'model_id', 'model', 'asset',
}


def _iter_world_asset_ids(value):
    if isinstance(value, dict):
        for key, child in value.items():
            key_normalized = str(key or '').replace('-', '_').lower()
            if key_normalized in _WORLD_ASSET_ID_KEYS and isinstance(child, str):
                yield child
            yield from _iter_world_asset_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_world_asset_ids(child)


def _tag_assets_for_tellus_world(world_id, state):
    world_tag = _tellus_world_tag(world_id)
    if not world_tag:
        return
    seen = set()
    for asset_id in _iter_world_asset_ids(state):
        asset_id = str(asset_id or '').strip()
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        model = Model3D.get_by_id(asset_id)
        if not model:
            continue
        updated_tags = _merge_tags(model.tags, ['tellus', world_tag])
        if updated_tags != (model.tags or []):
            model.tags = updated_tags
            model.save()


def _api_principal(required_scope='upload'):
    if current_user.is_authenticated:
        return current_user, False
    api_key = ApiKey.verify_token(_bearer_token(), required_scope=required_scope)
    if api_key:
        user = User.get_by_id(api_key.user_id)
        return user, False
    if not _bearer_token_valid():
        return None, False
    return _service_target_user(), True


def _require_api_principal():
    user, service = _api_principal()
    if user or service:
        return user, service, None
    return None, False, (jsonify({'error': 'Authentication required'}), 401)


def _can_write_model(model):
    user, service = _api_principal()
    if service:
        return True
    return can_manage_model(user, model)


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


@api_bp.route('/me')
def current_api_user():
    """Return the logged-in browser user's API/admin state for diagnostics."""
    if not current_user.is_authenticated:
        return jsonify({
            'authenticated': False,
            'is_asset_admin': False,
        })
    return jsonify({
        'authenticated': True,
        'id': current_user.id,
        'username': current_user.username,
        'email': current_user.email,
        'is_asset_admin': is_asset_admin_user(current_user),
        'asset_admin_configured': asset_admin_configured(),
    })


@api_bp.route('/models')
def list_models():
    """List models with pagination and search"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)  # Max 100 per page
        search = request.args.get('search', '').strip()
        user_only = request.args.get('user_only', 'false').lower() == 'true'
        include_private = request.args.get('include_private', 'false').lower() == 'true'
        category = request.args.get('category')
        styles = Model3D.normalize_tags(request.args.getlist('style'))
        asset_types = Model3D.normalize_tags(request.args.getlist('type'))
        asset_kinds = Model3D.normalize_tags(request.args.getlist('asset'))
        ready_for_tellus = request.args.get('ready_for_tellus', 'false').lower() in {'1', 'true', 'yes'}
        
        principal, service = _api_principal()
        if user_only and principal:
            # Get user's models
            models, total = Model3D.get_user_models(
                principal.id, page=page, per_page=per_page,
                category=category, style=styles or None, asset_type=asset_types or None,
                asset_kind=asset_kinds or None)
        elif user_only and service:
            return jsonify({'error': 'API token is valid, but no API upload user is configured.'}), 409
        elif include_private and service:
            models, total = Model3D.list_models(
                page=page, per_page=per_page, search=search if search else None,
                category=category, style=styles or None, asset_type=asset_types or None,
                public_only=False, asset_kind=asset_kinds or None)
        else:
            # Get public models
            models, total = Model3D.get_public_models(
                page=page, per_page=per_page, search=search if search else None,
                category=category, style=styles or None, asset_type=asset_types or None,
                exclude_animation_carriers=True, asset_kind=asset_kinds or None)

        if ready_for_tellus:
            models = _filter_ready_for_tellus(models)
            total = len(models)
        
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
                'asset_category': model.asset_category,
                'asset_styles': model.asset_styles,
                'asset_types': model.asset_types,
                'runtime_metadata': model.runtime_metadata,
                'ai_status': model.ai_status,
                'ai_title': (model.ai_metadata or {}).get('title'),
                'ai_description': model.ai_description,
                'ai_tags': model.ai_tags,
                'approve_game_ready': model.approve_game_ready,
                'approve_asset_store': model.approve_asset_store,
                **_media_presence_fields(model),
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


def _animated_model_payload(model):
    owner = User.get_by_id(model.user_id)
    fmt = (model.file_format or '').lower()
    viewable = bool(model.viewable_file_id) or fmt in ('glb', 'gltf')
    game_variant = ModelVariant.get(model.id, 'game') if viewable else None
    fixed_variant = ModelVariant.get(model.id, 'fixed_eyes') if viewable else None
    game_uses_fixed = bool(
        game_variant
        and isinstance(game_variant.settings, dict)
        and game_variant.settings.get('source_is_fixed_eyes')
    )
    current_game = bool(game_variant and game_variant.file_id and (not fixed_variant or game_uses_fixed))
    if current_game:
        view_url = url_for('api.get_game_optimized', model_id=model.id)
    elif fixed_variant and fixed_variant.file_id:
        view_url = url_for('api.get_fixed_eyes', model_id=model.id)
    elif viewable:
        view_url = url_for('api.view_model', model_id=model.id) + '?viewer=2'
    else:
        view_url = None

    payload = {
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
        'has_viewable': viewable,
        'has_vrma': bool(model.vrma_file_id),
        'tags': model.tags or [],
        'asset_category': model.asset_category,
        'asset_styles': model.asset_styles or [],
        'asset_types': model.asset_types or [],
        'runtime_metadata': model.runtime_metadata or {},
        'default_animation': model.default_animation or None,
        'camera_orbit': model.camera_orbit or None,
        'view_url': view_url,
        'download_url': url_for('api.download_model', model_id=model.id),
        'detail_url': url_for('main.model_detail', model_id=model.id),
        'owner': {
            'id': owner.id if owner else None,
            'username': owner.username if owner else 'Unknown',
        },
        'has_game_optimized': current_game,
        'has_fixed_eyes': bool(fixed_variant and fixed_variant.file_id),
        'game_uses_fixed': game_uses_fixed,
        **_media_presence_fields(model),
    }
    if fixed_variant and fixed_variant.file_id:
        payload['fixed_eyes_url'] = url_for('api.get_fixed_eyes', model_id=model.id)
    if current_game:
        payload['game_optimized_url'] = url_for('api.get_game_optimized', model_id=model.id)
    return payload


@api_bp.route('/animated-models')
def list_animated_models():
    """List loadable GLB/GLTF models that include both a rig and animations."""
    try:
        page = max(request.args.get('page', 1, type=int), 1)
        per_page = min(max(request.args.get('per_page', 20, type=int), 1), 100)
        sort = request.args.get('sort', 'newest')
        user_only = request.args.get('user_only', 'false').lower() == 'true'
        include_private = request.args.get('include_private', 'false').lower() == 'true'
        formats = Model3D.normalize_tags(request.args.getlist('format'))

        principal, service = _api_principal()
        if user_only and principal:
            models, total = Model3D.list_animated_models(
                page=page, per_page=per_page, sort=sort, public_only=False,
                owner_id=principal.id, formats=formats or None)
        elif user_only and service:
            return jsonify({'error': 'API token is valid, but no API upload user is configured.'}), 409
        elif include_private and service:
            models, total = Model3D.list_animated_models(
                page=page, per_page=per_page, sort=sort, public_only=False,
                formats=formats or None)
        else:
            models, total = Model3D.list_animated_models(
                page=page, per_page=per_page, sort=sort, public_only=True,
                formats=formats or None)

        total_pages = (total + per_page - 1) // per_page
        return jsonify({
            'models': [_animated_model_payload(model) for model in models],
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total': total,
                'pages': total_pages,
                'has_prev': page > 1,
                'has_next': page < total_pages,
            },
            'filters': {
                'asset_types': ['rigged', 'animated'],
                'formats': [
                    fmt for fmt in (formats or ['glb', 'gltf'])
                    if fmt in ('glb', 'gltf')
                ] or ['glb', 'gltf'],
            },
        })
    except Exception as e:
        print(f"API animated models error: {e}")
        return jsonify({'error': 'Failed to retrieve animated models'}), 500


@api_bp.route('/download/<model_id>')
def download_model(model_id):
    """Download model file"""
    try:
        model = Model3D.get_by_id_or_alias(model_id)

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
        model = Model3D.get_by_id_or_alias(model_id)

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
        model = Model3D.get_by_id_or_alias(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'game')
        if not variant or not variant.file_id:
            return jsonify({'error': 'No game-optimized variant'}), 404

        return _serve_variant_file(model, variant, etag_prefix='game', filename_suffix='game')

    except Exception as e:
        print(f"API game-optimized fetch error: {e}")
        return jsonify({'error': 'Game-optimized fetch failed'}), 500


@api_bp.route('/assets/model/<model_id>/game-optimized')
def get_asset_game_optimized(model_id):
    """Tellus-compatible game asset URL.

    Prefer the explicit game variant. If a manual/admin LOD backfill created
    LOD0 before the older game variant exists, serve LOD0 here so Tellus'
    stable game-optimized URL still resolves under the original asset id.
    """
    try:
        model = Model3D.get_by_id_or_alias(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'game')
        etag_prefix = 'game'
        filename_suffix = 'game'
        if not variant or not variant.file_id:
            variant = ModelVariant.get(model.id, 'lod', level=0)
            etag_prefix = 'lod-0'
            filename_suffix = 'lod-0'
        if not variant or not variant.file_id:
            return jsonify({'error': 'No game-optimized variant'}), 404

        return _serve_variant_file(model, variant, etag_prefix=etag_prefix, filename_suffix=filename_suffix)

    except Exception as e:
        print(f"API asset game-optimized fetch error: {e}")
        return jsonify({'error': 'Game-optimized fetch failed'}), 500


@api_bp.route('/assets/model/<model_id>/lod/<int:level>')
def get_asset_lod(model_id, level):
    """Serve generated LOD variants for Tellus.

    LOD variants are stored under the source asset id as ModelVariant(kind='lod',
    level=N). While the backfill pipeline is still catching up, LOD 0 falls back
    to the existing game-optimized variant because it is the current runtime GLB.
    """
    if level not in {0, 1, 2, 3}:
        return jsonify({'error': 'LOD level must be 0, 1, 2, or 3'}), 404
    try:
        model = Model3D.get_by_id_or_alias(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'lod', level=level)
        etag_prefix = f'lod-{level}'
        filename_suffix = f'lod-{level}'
        if (not variant or not variant.file_id) and level == 0:
            variant = ModelVariant.get(model.id, 'game')
            etag_prefix = 'game'
            filename_suffix = 'game'
        if not variant or not variant.file_id:
            return jsonify({'error': f'No LOD {level} variant'}), 404

        return _serve_variant_file(model, variant, etag_prefix=etag_prefix, filename_suffix=filename_suffix)
    except Exception as e:
        print(f"API LOD fetch error: {e}")
        return jsonify({'error': 'LOD fetch failed'}), 500


@api_bp.route('/assets/model/<model_id>/impostor')
def get_asset_impostor(model_id):
    """Serve a generated impostor variant for Tellus.

    The impostor may be an image atlas or another compact runtime file; its
    stored variant file_format controls the response content type.
    """
    try:
        model = Model3D.get_by_id_or_alias(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'impostor')
        if not variant or not variant.file_id:
            return jsonify({'error': 'No impostor variant'}), 404
        return _serve_variant_file(model, variant, etag_prefix='impostor', filename_suffix='impostor')
    except Exception as e:
        print(f"API impostor fetch error: {e}")
        return jsonify({'error': 'Impostor fetch failed'}), 500


def _serve_variant_file(model, variant, *, etag_prefix, filename_suffix):
    file_id = variant.file_id
    fmt = (variant.file_format or 'glb').lower()
    etag = f'"{etag_prefix}-{file_id}"'
    cache_control = 'public, max-age=31536000, immutable'
    as_download = request.args.get('download') in ('1', 'true', 'yes')
    content_type = _mime_for(fmt)
    download_name = f'{_safe_stem(model)}-{filename_suffix}.{fmt}'

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
            print(f"{etag_prefix} range fetch fell back to full body: {e}")

    data = variant.read_data()
    if data is None:
        return jsonify({'error': 'Variant file not found'}), 404
    if fmt in {'glb', 'vrm'}:
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


# Hard cap on an uploaded baked GLB. Eyeballs add a few hundred KB at most; the
# original model dominates the size, so cap generously above any real asset.
FIXED_EYES_MAX_BYTES = 200 * 1024 * 1024


def _delete_file_quietly(file_id, label):
    if not file_id:
        return
    try:
        current_app.config['FILE_STORE'].delete(file_id)
    except Exception as e:
        print(f"{label} cleanup warning: {e}")


def _delete_model_variant(model, kind):
    variant = ModelVariant.get(model.id, kind)
    file_id = variant.file_id if variant else None
    if variant:
        ModelVariant.delete_for(model.id, kind)
    _delete_file_quietly(file_id, f"Old {kind} variant")
    return bool(variant)


def _invalidate_captured_media(model):
    had_media = bool(model.thumbnail_file_id or model.preview_file_id)
    _delete_file_quietly(model.thumbnail_file_id, 'Thumbnail')
    _delete_file_quietly(model.preview_file_id, 'Preview')
    if had_media:
        model.thumbnail_file_id = None
        model.preview_file_id = None
        model.save()
    return had_media


@api_bp.route('/model/<model_id>/fixed-eyes', methods=['POST'])
@login_required
def post_fixed_eyes(model_id):
    """Store an owner-baked GLB (original model + blinker eyeballs, optionally a
    blink animation clip) as the model's 'fixed_eyes' variant. The GLB is built
    client-side by the viewer's GLTFExporter, so the server just validates and
    stores the bytes -- no mesh library needed."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not (current_user.is_authenticated and model.user_id == current_user.id):
            return jsonify({'error': 'Only the owner can fix a model\'s eyes.'}), 403

        upload = request.files.get('file')
        if upload is None:
            return jsonify({'error': 'No file uploaded.'}), 400
        data = upload.read()
        if not data:
            return jsonify({'error': 'Uploaded file is empty.'}), 400
        if len(data) > FIXED_EYES_MAX_BYTES:
            return jsonify({'error': 'Baked model is too large.'}), 413
        # Validate it's actually a binary glTF (magic "glTF").
        if data[:4] != b'glTF':
            return jsonify({'error': 'Uploaded file is not a valid GLB.'}), 400

        blink = request.form.get('blink') in ('1', 'true', 'yes')

        fs = current_app.config['FILE_STORE']
        filename = f'{_safe_stem(model)}-fixed-eyes.glb'
        file_id = fs.put(
            data,
            filename=filename,
            content_type=_mime_for('glb'),
            metadata={
                'kind': 'fixed_eyes',
                'source_model_id': model.id,
                'has_blink': blink,
                'size': len(data),
            },
        )
        variant, old_file_id = ModelVariant.upsert(
            model.id, 'fixed_eyes', str(file_id),
            file_format='glb', size=len(data),
            settings={'has_blink': blink}, status='ready',
        )
        if old_file_id and old_file_id != str(file_id):
            _delete_file_quietly(old_file_id, 'Old fixed-eyes blob')

        # The fixed GLB is now the canonical preview source until a fresh game
        # optimized variant is rebuilt from it. Clear stale media and stale game
        # output immediately so browse/detail never show the pre-fix asset.
        had_game = _delete_model_variant(model, 'game')
        invalidated_media = _invalidate_captured_media(model)

        # Re-run game optimization so the preferred 'game' variant includes the
        # baked eyes. _run_game_optimizer uses the fixed-eyes GLB as its source;
        # force=True replaces any existing (eyeless) game variant. Previews
        # prefer 'game', so once it finishes the small + fixed-eyes file is used.
        import shutil
        _maybe_autostart_game_optimization(model, force=True)
        reoptimizing = bool(shutil.which('gltfpack'))

        return jsonify({
            'success': True,
            'variant': variant.to_api() if variant else None,
            'reoptimizing': reoptimizing,
            'replaced_game_variant': had_game,
            'invalidated_media': invalidated_media,
            'recapture_url': url_for('main.model_detail', model_id=model.id, capture=1),
        })
    except Exception as e:
        print(f"API fixed-eyes upload error: {e}")
        return jsonify({'error': 'Could not save fixed-eyes model.'}), 500


@api_bp.route('/model/<model_id>/fixed-eyes', methods=['GET'])
def get_fixed_eyes(model_id):
    """Serve the fixed-eyes GLB variant. Inline by default (detail-page viewer);
    ?download=1 for an attachment. Mirrors get_game_optimized (Range + ETag)."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'fixed_eyes')
        if not variant or not variant.file_id:
            return jsonify({'error': 'No fixed-eyes variant'}), 404

        file_id = variant.file_id
        etag = f'"fixedeyes-{file_id}"'
        cache_control = 'public, max-age=31536000, immutable'
        as_download = request.args.get('download') in ('1', 'true', 'yes')
        content_type = _mime_for('glb')
        download_name = f'{_safe_stem(model)}-fixed-eyes.glb'

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
                print(f"Fixed-eyes range fetch fell back to full body: {e}")

        data = variant.read_data()
        if data is None:
            return jsonify({'error': 'Variant file not found'}), 404

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
        print(f"API fixed-eyes fetch error: {e}")
        return jsonify({'error': 'Fixed-eyes fetch failed'}), 500


# A rigged GLB can be large; cap generously above any real avatar.
TO_VRM_MAX_BYTES = 200 * 1024 * 1024


class VrmConversionError(Exception):
    """Raised by _convert_glb_bytes_to_vrm with an HTTP status hint."""
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _decompress_meshopt_glb(data, workdir):
    """Strip EXT_meshopt_compression so downstream tools (glb2vrm) can read the
    BIN chunk. Best-effort: returns the original bytes if gltfpack is missing or
    the repack fails. Does NOT undo -si decimation -- callers that need a full
    skeleton must start from an un-decimated source, not just decompress."""
    import shutil
    import subprocess
    if not _glb_is_meshopt_compressed(data):
        return data
    gltfpack_bin = shutil.which('gltfpack')
    if not gltfpack_bin:
        return data
    in_path = os.path.join(workdir, 'meshopt-in.glb')
    out_path = os.path.join(workdir, 'meshopt-out.glb')
    with open(in_path, 'wb') as f:
        f.write(data)
    try:
        result = subprocess.run(
            [gltfpack_bin, '-i', in_path, '-o', out_path, '-kn', '-km'],
            capture_output=True, text=True, timeout=180, check=False,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            with open(out_path, 'rb') as f:
                return f.read()
    except Exception as e:
        print(f"meshopt decompress failed (using original): {str(e)[:200]}")
    return data


def _convert_glb_bytes_to_vrm(model, data, author=None):
    """Convert rigged GLB bytes into a VRM, store it as the model's 'vrm'
    variant, and auto-produce the rig-safe optimized variant. Returns
    (vrm_variant, optimized_bool). Raises VrmConversionError (with .status) on a
    user-actionable failure. Shared by the to-vrm route and the rig route."""
    import shutil
    import tempfile
    from app.conversion import glb_to_vrm, tool_paths

    if not data or data[:4] != b'glTF':
        raise VrmConversionError(
            'VRM conversion needs a binary GLB. Rig the model first, then try again.', 400)
    if len(data) > TO_VRM_MAX_BYTES:
        raise VrmConversionError('Model is too large to convert.', 413)

    paths = tool_paths(current_app)
    workdir = tempfile.mkdtemp(prefix='to_vrm_')
    try:
        in_path = os.path.join(workdir, 'input.glb')
        out_path = os.path.join(workdir, 'avatar.vrm')
        # Safety net: a meshopt-compressed GLB would hide its BIN chunk from
        # glb2vrm. Decompress first. (The rigger already prefers an un-decimated
        # source; this guards any other caller.)
        data = _decompress_meshopt_glb(data, workdir)
        with open(in_path, 'wb') as f:
            f.write(data)
        try:
            glb_to_vrm(
                paths['node'], paths['fbx2vrma_dir'], in_path, out_path,
                name=(model.name or None), author=author,
            )
        except RuntimeError as e:
            # glb2vrm exits non-zero with a human-readable reason (e.g. not
            # rigged / required bones unmapped). Surface it as 422.
            raise VrmConversionError(f'VRM conversion failed: {e}', 422)
        with open(out_path, 'rb') as f:
            vrm_bytes = f.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not vrm_bytes or vrm_bytes[:4] != b'glTF':
        raise VrmConversionError('Converter produced an invalid VRM.', 500)

    fs = current_app.config['FILE_STORE']
    file_id = fs.put(
        vrm_bytes,
        filename=f'{_safe_stem(model)}.vrm',
        content_type=_mime_for('vrm'),
        metadata={'kind': 'vrm', 'source_model_id': model.id, 'size': len(vrm_bytes)},
    )
    variant, old_file_id = ModelVariant.upsert(
        model.id, 'vrm', str(file_id),
        file_format='vrm', size=len(vrm_bytes), status='ready',
    )
    model.tags = _merge_tags(model.tags, ['avatar', 'vrm'])
    model.asset_types = _merge_tags(model.asset_types, ['avatar', 'vrm'])
    model.save()
    if old_file_id and old_file_id != str(file_id):
        try:
            fs.delete(old_file_id)
        except Exception as e:
            print(f"Old VRM blob {old_file_id} not deleted: {e}")

    # The raw VRM has just been replaced. Do not leave a stale optimized VRM
    # around if the new optimization fails; the old optimized avatar may have
    # been generated from the bad rig the user is trying to replace.
    _delete_model_variant_file(model.id, 'vrm_optimized')

    # Auto-produce the rig-safe optimized avatar too. Best-effort.
    optimized = False
    try:
        opt_variant, _ = _optimize_vrm_variant(model)
        optimized = bool(opt_variant and opt_variant.file_id)
    except Exception as e:
        print(f"Auto VRM optimization skipped for {model.id}: {e}")

    return variant, optimized


def _delete_model_variant_file(model_id, kind):
    variant = ModelVariant.get(model_id, kind)
    if not variant:
        return False
    file_id = variant.file_id
    ModelVariant.delete_for(model_id, kind)
    if file_id:
        try:
            current_app.config['FILE_STORE'].delete(file_id)
        except Exception as e:
            print(f"Variant blob {file_id} for {kind} not deleted: {e}")
    return True


@api_bp.route('/model/<model_id>/to-vrm', methods=['POST'])
@login_required
def post_to_vrm(model_id):
    """Convert this model's rigged GLB into a VRM avatar by injecting the
    VRMC_vrm humanoid extension (via tools/glb2vrm-converter.js), and store it as
    the model's 'vrm' variant. Prefers the in-app 'rigged' variant when present
    (so the Rig Avatar / Make VRM buttons chain), else the viewable GLB. The
    mesh/skeleton are preserved; only VRM humanoid metadata is added."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Only the owner can convert this model to VRM.'}), 403

        # Prefer the rigged variant (from the in-app rigger) as the source.
        rigged = ModelVariant.get(model.id, 'rigged')
        if rigged and rigged.file_id:
            data, fmt = rigged.read_data(), 'glb'
        else:
            data, fmt = model.get_viewable_data()
        if data is None:
            return jsonify({'error': 'Model has no GLB data to convert.'}), 400
        if (fmt or '').lower() not in ('glb', 'gltf') or data[:4] != b'glTF':
            return jsonify({
                'error': 'VRM conversion needs a binary GLB. Rig the model first '
                         '(use Rig Avatar) and try again.'
            }), 400

        author = current_user.username if current_user.is_authenticated else None
        try:
            variant, optimized = _convert_glb_bytes_to_vrm(model, data, author)
        except VrmConversionError as e:
            return jsonify({'error': str(e)}), e.status

        return jsonify({
            'success': True,
            'variant': variant.to_api() if variant else None,
            'optimized': optimized,
        })
    except Exception as e:
        print(f"API to-vrm error: {e}")
        return jsonify({'error': 'Could not convert model to VRM.'}), 500


@api_bp.route('/model/<model_id>/rig', methods=['POST'])
@login_required
def post_rig(model_id):
    """Store an owner-rigged GLB (skeleton + skin baked client-side by the Rig
    Avatar editor) as the model's 'rigged' variant. Optionally (to_vrm=1) also
    convert it to a VRM avatar. Mirrors post_fixed_eyes: the server just validates
    and stores the bytes; the mesh work happened in the browser."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Only the owner can rig this model.'}), 403

        upload = request.files.get('file')
        if upload is None:
            return jsonify({'error': 'No file uploaded.'}), 400
        data = upload.read()
        if not data:
            return jsonify({'error': 'Uploaded file is empty.'}), 400
        if len(data) > FIXED_EYES_MAX_BYTES:
            return jsonify({'error': 'Rigged model is too large.'}), 413
        if data[:4] != b'glTF':
            return jsonify({'error': 'Uploaded file is not a valid GLB.'}), 400

        markers = request.form.get('markers')  # optional JSON, stored for re-edit
        settings = {}
        if markers:
            try:
                settings['markers'] = json.loads(markers)
            except Exception:
                pass

        fs = current_app.config['FILE_STORE']
        file_id = fs.put(
            data,
            filename=f'{_safe_stem(model)}-rigged.glb',
            content_type=_mime_for('glb'),
            metadata={'kind': 'rigged', 'source_model_id': model.id, 'size': len(data)},
        )
        variant, old_file_id = ModelVariant.upsert(
            model.id, 'rigged', str(file_id),
            file_format='glb', size=len(data), settings=settings, status='ready',
        )
        if old_file_id and old_file_id != str(file_id):
            try:
                fs.delete(old_file_id)
            except Exception as e:
                print(f"Old rigged blob {old_file_id} not deleted: {e}")

        # Optional chain to VRM from the freshly-rigged bytes.
        vrm = None
        if request.form.get('to_vrm') in ('1', 'true', 'yes'):
            try:
                author = current_user.username if current_user.is_authenticated else None
                vrm_variant, _ = _convert_glb_bytes_to_vrm(model, data, author)
                vrm = vrm_variant.to_api() if vrm_variant else None
            except VrmConversionError as e:
                # Rig succeeded; VRM step failed — report rig success + a note.
                return jsonify({
                    'success': True,
                    'variant': variant.to_api() if variant else None,
                    'vrm': None,
                    'vrm_error': str(e),
                })

        return jsonify({
            'success': True,
            'variant': variant.to_api() if variant else None,
            'vrm': vrm,
        })
    except Exception as e:
        print(f"API rig error: {e}")
        return jsonify({'error': 'Could not save rigged model.'}), 500


@api_bp.route('/model/<model_id>/animation-source', methods=['POST'])
def post_animation_source(model_id):
    """Attach a returned animated GLB/GLTF to the original model.

    This is the mesh2motion roundtrip path: the edited file stays a variant of
    the original asset, its embedded animation metadata is merged onto the
    original model, and future game optimization uses this animated source.
    """
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403

        upload = request.files.get('file')
        if upload is None or not upload.filename:
            return jsonify({'error': 'No file uploaded.'}), 400
        filename = upload.filename.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
        file_format = filename.rsplit('.', 1)[1].lower() if '.' in filename else 'glb'
        if file_format not in {'glb', 'gltf'}:
            return jsonify({'error': 'Upload the animated roundtrip as GLB or GLTF.'}), 400

        data = upload.read()
        if not data:
            return jsonify({'error': 'Uploaded file is empty.'}), 400
        if len(data) > FIXED_EYES_MAX_BYTES:
            return jsonify({'error': 'Animated source is too large.'}), 413
        if not _validate_uploaded_gltf_bytes(data, file_format):
            return jsonify({'error': 'Uploaded file is not a valid GLB/GLTF asset.'}), 400

        derived_asset_types, derived_runtime = _file_derived_metadata(data, file_format)
        if 'animated' not in derived_asset_types:
            return jsonify({'error': 'Uploaded file does not contain embedded animation clips.'}), 400

        fs = current_app.config['FILE_STORE']
        file_id = fs.put(
            data,
            filename=f'{_safe_stem(model)}-animated.{file_format}',
            content_type=_mime_for(file_format),
            metadata={
                'kind': 'animation_source',
                'source_model_id': model.id,
                'original_filename': filename,
                'size': len(data),
            },
        )
        settings = {
            'source': 'animation_roundtrip',
            'original_filename': filename,
            'runtime_metadata': derived_runtime,
        }
        variant, old_file_id = ModelVariant.upsert(
            model.id, 'rigged', str(file_id),
            file_format=file_format, size=len(data), settings=settings, status='ready',
        )
        if old_file_id and old_file_id != str(file_id):
            try:
                fs.delete(old_file_id)
            except Exception as e:
                print(f"Old animated source blob {old_file_id} not deleted: {e}")

        model.asset_types = _merge_tags(model.asset_types, derived_asset_types)
        model.runtime_metadata = _merge_runtime_metadata(model.runtime_metadata, derived_runtime)
        model.ai_status = None
        model.ai_error = None
        if isinstance(model.ai_metadata, dict):
            metadata = dict(model.ai_metadata)
            metadata.pop('animatedModel', None)
            metadata.pop('animationClips', None)
            model.ai_metadata = metadata
        model.save()

        reoptimize = _as_bool(request.form.get('reoptimize') or request.args.get('reoptimize'))
        job = None
        status_url = None
        if reoptimize:
            job_id = _enqueue_game_optimization(model.id, model.user_id, dict(GAME_OPTIMIZE_DEFAULTS))
            job = _get_optimization_job(job_id)
            status_url = url_for('api.game_optimization_status', model_id=model.id, job_id=job_id)

        return jsonify({
            'success': True,
            'variant': variant.to_api() if variant else None,
            'model': _serialize_model(model),
            'animations': (model.runtime_metadata or {}).get('animations') or [],
            'reoptimizing': reoptimize,
            'queued': bool(job),
            'job': _optimization_job_to_api(job) if job else None,
            'status_url': status_url,
        })
    except Exception as e:
        print(f"API animation-source error: {e}")
        return jsonify({'error': 'Could not save animated source.'}), 500


def _decode_data_url_image(value):
    value = str(value or '').strip()
    if not value:
        return None, None
    if not value.startswith('data:') or ';base64,' not in value:
        raise ValueError('Expected a base64 data URL image.')
    header, encoded = value.split(';base64,', 1)
    content_type = header[5:].strip().lower() or 'image/png'
    if content_type not in {'image/png', 'image/jpeg', 'image/jpg', 'image/webp'}:
        raise ValueError('Unsupported marker suggestion image type.')
    data = base64.b64decode(encoded, validate=True)
    if len(data) > int(os.environ.get('AI_AUTORIG_MAX_IMAGE_BYTES', str(2 * 1024 * 1024))):
        raise ValueError('Marker suggestion image is too large.')
    return data, ('image/jpeg' if content_type == 'image/jpg' else content_type)


@api_bp.route('/model/<model_id>/rig/suggest-markers', methods=['POST'])
@login_required
def suggest_rig_markers(model_id):
    """Use the configured vision provider to draft autorig marker positions.

    The client may pass image_data_url from the current front/profile rig view.
    If omitted, the saved thumbnail is used as a front-view fallback.
    """
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_write_model(model):
            return jsonify({'error': 'Access denied'}), 403

        data = _payload()
        view = (data.get('view') or 'front').strip().lower()
        if view not in {'front', 'profile'}:
            view = 'front'
        image_bytes = image_mime = None
        if data.get('image_data_url'):
            try:
                image_bytes, image_mime = _decode_data_url_image(data.get('image_data_url'))
            except ValueError as e:
                return jsonify({'error': 'Invalid image', 'detail': str(e)}), 400
        elif not model.thumbnail_file_id:
            missing_thumbnail = _thumbnail_required_error(model)
            return jsonify({'error': 'Thumbnail required', 'detail': missing_thumbnail}), 409

        try:
            from app.ai_enrichment import suggest_autorig_markers
            suggestions = suggest_autorig_markers(
                model,
                image_bytes=image_bytes,
                image_mime=image_mime or 'image/webp',
                view=view,
            )
        except Exception as e:
            detail = str(e)[:500]
            print(f"API rig marker suggestion error for model {model.id}: {detail}", flush=True)
            return jsonify({'error': 'AI marker suggestion failed', 'detail': detail}), 502
        if not suggestions:
            return jsonify({'error': 'AI marker suggestion unavailable'}), 503
        return jsonify({'success': True, 'suggestions': suggestions})
    except Exception as e:
        print(f"API rig marker suggestion route error: {e}")
        return jsonify({'error': 'AI marker suggestion failed'}), 500


@api_bp.route('/model/<model_id>/rigged', methods=['GET'])
def get_rigged(model_id):
    """Serve the rigged GLB variant. Inline by default; ?download=1 attachment."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403

        variant = ModelVariant.get(model.id, 'rigged')
        if not variant or not variant.file_id:
            return jsonify({'error': 'No rigged variant'}), 404

        data = variant.read_data()
        if data is None:
            return jsonify({'error': 'Variant file not found'}), 404

        etag = f'"rigged-{variant.file_id}"'
        cache_control = 'public, max-age=31536000, immutable'
        if request.if_none_match and etag in request.if_none_match:
            resp = make_response('', 304)
            resp.headers['ETag'] = etag
            resp.headers['Cache-Control'] = cache_control
            return resp

        response = make_response(data)
        response.headers['Content-Type'] = _mime_for('glb')
        response.headers['Content-Length'] = str(len(data))
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['ETag'] = etag
        response.headers['Cache-Control'] = cache_control
        if request.args.get('download') in ('1', 'true', 'yes'):
            response.headers['Content-Disposition'] = (
                f'attachment; filename="{_safe_stem(model)}-rigged.glb"'
            )
        return response
    except Exception as e:
        print(f"API get-rigged error: {e}")
        return jsonify({'error': 'Rigged fetch failed'}), 500


def _serve_vrm_variant(model_id, kind, etag_prefix, filename_suffix):
    """Shared serving for VRM variants (raw + optimized): ETag + immutable
    cache, inline by default, ?download=1 for an attachment."""
    model = Model3D.get_by_id_or_alias(model_id)
    if not model:
        return jsonify({'error': 'Model not found'}), 404
    if not _can_access_model(model):
        return jsonify({'error': 'Access denied'}), 403

    variant = ModelVariant.get(model.id, kind)
    if not variant or not variant.file_id:
        return jsonify({'error': f'No {kind} variant'}), 404

    etag = f'"{etag_prefix}-{variant.file_id}"'
    cache_control = 'public, max-age=31536000, immutable'
    if request.if_none_match and etag in request.if_none_match:
        resp = make_response('', 304)
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = cache_control
        return resp

    data = variant.read_data()
    if data is None:
        return jsonify({'error': 'Variant file not found'}), 404

    response = make_response(data)
    response.headers['Content-Type'] = _mime_for('vrm')
    response.headers['Content-Length'] = str(len(data))
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = cache_control
    if request.args.get('download') in ('1', 'true', 'yes'):
        response.headers['Content-Disposition'] = (
            f'attachment; filename="{_safe_stem(model)}{filename_suffix}.vrm"'
        )
    return response


@api_bp.route('/model/<model_id>/vrm', methods=['GET'])
def get_vrm_variant(model_id):
    """Serve the converted VRM variant. Inline by default; ?download=1 for an
    attachment. Mirrors get_fixed_eyes (ETag + immutable cache)."""
    try:
        return _serve_vrm_variant(model_id, 'vrm', 'vrm', '')
    except Exception as e:
        print(f"API get-vrm error: {e}")
        return jsonify({'error': 'VRM fetch failed'}), 500


@api_bp.route('/model/<model_id>/optimized-vrm', methods=['GET'])
def get_optimized_vrm(model_id):
    """Serve the rig-safe optimized VRM avatar variant."""
    try:
        return _serve_vrm_variant(model_id, 'vrm_optimized', 'vrmopt', '-optimized')
    except Exception as e:
        print(f"API get-optimized-vrm error: {e}")
        return jsonify({'error': 'Optimized VRM fetch failed'}), 500


@api_bp.route('/model/<model_id>/optimize-vrm', methods=['POST'])
@login_required
def post_optimize_vrm(model_id):
    """Produce a rig-safe optimized version of the model's VRM avatar (owner
    only). Meshopt + texture compression with the skeleton preserved -- no mesh
    simplification, so the humanoid rig and VRMA retargeting still work."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not (current_user.is_authenticated and model.user_id == current_user.id):
            return jsonify({'error': 'Only the owner can optimize this avatar.'}), 403
        if not ModelVariant.get(model.id, 'vrm'):
            return jsonify({'error': 'This model has no VRM avatar to optimize yet.'}), 400

        try:
            texture_limit = int(request.args.get('texture_limit', 2048))
        except (TypeError, ValueError):
            texture_limit = 2048

        try:
            opt_variant, info = _optimize_vrm_variant(model, texture_limit=texture_limit)
        except RuntimeError as e:
            return jsonify({'error': f'VRM optimization failed: {e}'}), 422

        return jsonify({
            'success': True,
            'variant': opt_variant.to_api() if opt_variant else None,
            'download_url': url_for('api.get_optimized_vrm', model_id=model.id) + '?download=1',
            **info,
        })
    except Exception as e:
        print(f"API optimize-vrm error: {e}")
        return jsonify({'error': 'Could not optimize VRM.'}), 500


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


def _media_summary(model):
    """All media URLs + presence flags for a model in one place: still image
    (thumbnail), rotating preview video, the renderable model file, and the
    game-optimized variant. URLs are only included when the asset exists."""
    fmt = (model.file_format or '').lower()
    viewable = bool(model.viewable_file_id) or fmt in ('glb', 'gltf', 'vrm')
    view_format = (model.viewable_format or 'glb') if model.viewable_file_id else model.file_format
    go = _game_optimized_fields(model)
    return {
        'id': model.id,
        'name': model.name,
        'file_format': model.file_format,
        'conversion_status': model.conversion_status,
        'image': {
            'has': bool(model.thumbnail_file_id),
            'url': url_for('api.get_thumbnail', model_id=model.id) if model.thumbnail_file_id else None,
            'content_type': 'image/webp',
        },
        'video': {
            'has': bool(model.preview_file_id),
            'url': url_for('api.get_preview', model_id=model.id) if model.preview_file_id else None,
            'content_type': 'video/webm',
            'supports_range': True,
        },
        'model': {
            'viewable': viewable,
            'view_url': url_for('api.view_model', model_id=model.id) if viewable else None,
            'download_url': url_for('api.download_model', model_id=model.id),
            'file_format': model.file_format,
            'view_format': view_format if viewable else None,
        },
        'game_optimized': go['game_optimized'],
        'has_game_optimized': go['has_game_optimized'],
        **_asset_lod_url_fields(model),
    }


@api_bp.route('/model/<model_id>/media')
def get_media_summary(model_id):
    """One call returning every media URL for a model: thumbnail image,
    rotating preview video, the renderable model file, and the game-optimized
    variant (if any), each with a presence flag."""
    try:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        if not _can_access_model(model):
            return jsonify({'error': 'Access denied'}), 403
        return jsonify(_media_summary(model))
    except Exception as e:
        print(f"API media summary error: {e}")
        return jsonify({'error': 'Media summary failed'}), 500


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


def _assimp_unfriendly_gltf(src_bytes, src_fmt):
    if (src_fmt or '').lower() not in ('glb', 'gltf'):
        return False
    gltf = _gltf_json_from_bytes(src_bytes, (src_fmt or '').lower())
    if not isinstance(gltf, dict):
        return False
    used = set(gltf.get('extensionsUsed') or [])
    required = set(gltf.get('extensionsRequired') or [])
    return bool((used | required) & {'EXT_meshopt_compression', 'KHR_mesh_quantization'})


def _assimp_export_source_path(src_bytes, src_fmt, workdir):
    """Write a temporary source file for Assimp.

    Assimp still fails on many gltfpack/meshopt GLBs. When the source declares
    those extensions, first repack it through gltfpack without mesh compression
    so FBX/OBJ/STL/etc. export has a compatible input.
    """
    import shutil
    import subprocess

    src_fmt = (src_fmt or 'glb').lower()
    in_path = os.path.join(workdir, f'src.{src_fmt}')
    with open(in_path, 'wb') as f:
        f.write(src_bytes)

    if src_fmt not in ('glb', 'gltf') or not _assimp_unfriendly_gltf(src_bytes, src_fmt):
        return in_path

    gltfpack_bin = shutil.which('gltfpack')
    if not gltfpack_bin:
        raise RuntimeError('gltfpack is required to prepare this optimized GLB for export.')

    out_path = os.path.join(workdir, 'assimp-source.glb')
    result = subprocess.run(
        [
            gltfpack_bin,
            '-i', in_path,
            '-o', out_path,
            '-kn',
            '-km',
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or 'gltfpack failed.').strip()
        raise RuntimeError(msg[-1000:] or 'gltfpack failed.')
    if not os.path.exists(out_path):
        raise RuntimeError('gltfpack produced no export source.')
    return out_path


@api_bp.route('/export/<model_id>')
def export_model(model_id):
    import shutil
    import tempfile
    from app.conversion import assimp_export, tool_paths

    try:
        fmt = (request.args.get('format') or '').lower().strip()
        model = Model3D.get_by_id_or_alias(model_id)
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

        # If the caller asks for the model's native uploaded format (notably
        # FBX), return the original bytes instead of re-exporting the converted
        # preview GLB back through Assimp. The preview/export source may be a
        # derived GLB, but "format=fbx" on an FBX upload should preserve the
        # creator/player's original FBX.
        original_fmt = (model.file_format or '').lower()
        if fmt == original_fmt:
            original = model.get_file_data()
            if not original:
                return jsonify({'error': 'Original file not found'}), 404
            filename = model.original_filename or f'{_safe_stem(model)}.{fmt}'
            return _download_bytes(original, filename, _mime_for(fmt))

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
            out_path = os.path.join(workdir, f'out.{fmt}')
            try:
                in_path = _assimp_export_source_path(src_bytes, src_fmt, workdir)
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

@api_bp.route('/model/<model_id>', methods=['GET'])
def get_model(model_id):
    """Return a single model summary, including async job status."""
    try:
        model = Model3D.get_by_id_or_alias(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        user, service = _api_principal()
        if not _can_access_model_as(model, principal=user, service=service):
            return jsonify({'error': 'Access denied'}), 403
        return jsonify({'success': True, 'model': _serialize_model(model)})
    except Exception as e:
        print(f"API get model error: {e}")
        return jsonify({'error': 'Failed to retrieve model'}), 500


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
        if 'asset_category' in data:
            model.asset_category = Model3D.normalize_category(data.get('asset_category'))
        if 'asset_styles' in data:
            model.asset_styles = Model3D.normalize_tags(data.get('asset_styles'))
        if 'asset_types' in data:
            model.asset_types = Model3D.normalize_tags(data.get('asset_types'))
        if 'runtime_metadata' in data:
            model.runtime_metadata = Model3D.normalize_runtime_metadata(data.get('runtime_metadata'))

        if 'default_animation' in data:
            # Embedded-clip name to auto-play; empty clears it.
            clip = (data.get('default_animation') or '').strip()
            model.default_animation = clip or None

        if 'default_vrma_id' in data:
            # VRMA asset id to auto-apply on a VRM. The literal 'none' is an
            # EXPLICIT "no animation (T-pose)" choice and is preserved so the
            # global default (hip-hop dance) does NOT override it. Empty/null
            # means "never set" -> the global default applies.
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
                'asset_category': model.asset_category,
                'asset_styles': model.asset_styles,
                'asset_types': model.asset_types,
                'runtime_metadata': model.runtime_metadata,
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


def _encode_impostor_webp(image_bytes, *, size=512):
    """Encode a square far-field billboard texture as WebP."""
    try:
        from PIL import Image, ImageOps
    except Exception as e:
        raise RuntimeError('Pillow is required to generate impostor WebP textures.') from e

    with Image.open(io.BytesIO(image_bytes)) as img:
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert('RGB')
        target_size = max(128, min(int(size or 512), 2048))
        img = ImageOps.contain(img, (target_size, target_size))
        canvas = Image.new('RGB', (target_size, target_size), (255, 255, 255))
        x = (target_size - img.width) // 2
        y = (target_size - img.height) // 2
        canvas.paste(img, (x, y))
        out = io.BytesIO()
        canvas.save(out, format='WEBP', quality=78, method=4)
        return out.getvalue(), target_size, target_size


def _encode_impostor_atlas_webp(image_bytes):
    """Encode an atlas image as WebP while preserving alpha."""
    try:
        from PIL import Image
    except Exception as e:
        raise RuntimeError('Pillow is required to generate impostor WebP textures.') from e

    with Image.open(io.BytesIO(image_bytes)) as img:
        img = img.convert('RGBA')
        out = io.BytesIO()
        img.save(out, format='WEBP', quality=82, method=4)
        return out.getvalue(), img.width, img.height


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

        _, enrichment_queued = _store_thumbnail_png(
            model, png_bytes, kind='thumbnail', source='thumbnail_capture')

        return jsonify({
            'success': True,
            'thumbnail_file_id': model.thumbnail_file_id,
            'ai_enrichment_queued': enrichment_queued,
        })

    except Exception as e:
        print(f"API thumbnail upload error: {e}")
        return jsonify({'error': 'Thumbnail upload failed'}), 500


def _store_thumbnail_png(model, png_bytes, *, kind='thumbnail', source='server_render'):
    """Store rendered/captured PNG bytes as the model's thumbnail.

    Shared write path: transcode to WebP, swap the file, set thumbnail_file_id +
    media_capture='captured', and kick AI enrichment. Used by the browser upload
    route and the server-side renderer alike."""
    if not png_bytes:
        raise ValueError('empty thumbnail bytes')
    fs = current_app.config['FILE_STORE']
    if model.thumbnail_file_id:
        try:
            fs.delete(model.thumbnail_file_id)
        except Exception as e:
            print(f"Thumbnail cleanup warning: {e}")
    thumb_bytes, thumb_ct, thumb_ext = _encode_thumbnail_webp(png_bytes)
    new_id = fs.put(
        thumb_bytes,
        filename=f"thumb_{model.id}.{thumb_ext}",
        content_type=thumb_ct,
        metadata={'model_id': model.id, 'kind': 'thumbnail'},
    )
    model.thumbnail_file_id = str(new_id)
    _set_media_capture_state(model, status='captured', kind=kind)
    model.save()
    enrichment_queued = _maybe_enqueue_autotag_after_thumbnail(model, context={'source': source})
    return model.thumbnail_file_id, enrichment_queued


def _mark_media_capture(model, status, error=None):
    """Persist a media_capture status, swallowing save errors (best-effort)."""
    try:
        _set_media_capture_state(model, status=status, kind='server_render', error=error)
        model.save()
    except Exception as e:
        print(f"Could not set media_capture={status} for {model.id}: {str(e)[:200]}")


def _server_render_thumbnail(model, *, size=1024):
    """Render a model's viewable GLB to a thumbnail server-side (no browser).

    Returns True on success. Best-effort: returns False (and logs) on any failure
    so a single bad asset never breaks a batch. Always leaves an honest
    media_capture status so a poller (Tellus) is never stuck on a stale
    'processing': 'captured' on success, 'failed' on a render error (queue stops
    retrying), 'blocked' when the asset simply isn't renderable here."""
    from app import render as render_mod

    if not render_mod.render_available():
        return False
    fmt = (model.file_format or '').lower()
    # Only mesh formats render here; vrm is glTF under the hood so it's fine too.
    if fmt in ('vrma', 'bvh'):
        _mark_media_capture(model, 'blocked', error='not a renderable mesh format')
        return False
    try:
        data, view_fmt = model.get_viewable_data()
    except Exception as e:
        print(f"Render: could not read viewable for {model.id}: {e}")
        _mark_media_capture(model, 'blocked', error='no viewable mesh available')
        return False
    if not data or data[:4] != b'glTF':
        _mark_media_capture(model, 'blocked', error='no usable GLB bytes')
        return False
    try:
        png = render_mod.render_glb_to_png(
            data,
            file_type=(view_fmt or 'glb').lower() if (view_fmt or 'glb').lower() in ('glb', 'gltf') else 'glb',
            size=size,
            decompress=lambda b: _decompress_meshopt_glb_bytes(b),
        )
        _store_thumbnail_png(model, png, kind='thumbnail', source='server_render')
        return True
    except render_mod.RenderError as e:
        print(f"Render failed for {model.id}: {e}")
        _mark_media_capture(model, 'failed', error=f'render failed: {str(e)[:200]}')
        return False
    except Exception as e:
        print(f"Render unexpected error for {model.id}: {e}")
        _mark_media_capture(model, 'failed', error=f'render error: {str(e)[:200]}')
        return False


def _decompress_meshopt_glb_bytes(data):
    """Wrapper that gives the renderer a temp workdir for meshopt decompression."""
    import tempfile
    workdir = tempfile.mkdtemp(prefix='render_')
    try:
        return _decompress_meshopt_glb(data, workdir)
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def _model_render_ready(model):
    """True when a model can be rendered to a thumbnail right now: a renderable
    format with usable bytes (native glb/gltf/vrm, or a converted FBX/etc. that
    already has its viewable GLB)."""
    fmt = (model.file_format or '').lower()
    if fmt in ('vrma', 'bvh'):
        return False
    if fmt in ('glb', 'gltf', 'vrm'):
        return True
    return bool(model.viewable_file_id)


def _maybe_render_thumbnail_async(model):
    """Render this model's thumbnail in the background so a fresh upload/conversion
    shows an image immediately, without waiting for the reconciler. Best-effort:
    skipped if the model already has a thumbnail, isn't renderable yet, or the
    render stack is unavailable. Never blocks the caller (HTTP upload response)."""
    if os.environ.get('SERVER_RENDER_THUMBNAILS', '1').lower() in {'0', 'false', 'no', 'off'}:
        return False
    if not model or model.thumbnail_file_id or not _model_render_ready(model):
        return False
    from app import render as render_mod
    if not render_mod.render_available():
        return False

    app = current_app._get_current_object()
    model_id = model.id

    # Mark 'processing' synchronously BEFORE the thread starts so the very first
    # status poll (Tellus, world UI) sees a render in flight and waits, rather
    # than seeing a derived 'queued'/'idle' and assuming nothing is happening.
    # If the render thread dies, the pipeline stuck-sweep resets this after its
    # timeout, so 'processing' is never permanently sticky.
    try:
        _set_media_capture_state(model, status='processing', kind='server_render')
        model.save()
    except Exception as e:
        print(f"Could not mark render processing for {model_id}: {str(e)[:200]}")

    def _run():
        with app.app_context():
            try:
                fresh = Model3D.get_by_id(model_id)
                if fresh and not fresh.thumbnail_file_id:
                    _server_render_thumbnail(fresh)
            except Exception as e:
                print(f"Async thumbnail render failed for {model_id}: {str(e)[:200]}", flush=True)

    threading.Thread(target=_run, name=f'render-thumb-{model_id[:8]}', daemon=True).start()
    return True


@api_bp.route('/model/<model_id>/thumbnail', methods=['GET'])
def get_thumbnail(model_id):
    """Serve a model's thumbnail (WebP for new uploads, PNG for older ones).
    404 if none (frontend shows a fallback)."""
    try:
        model = Model3D.get_by_id_or_alias(model_id)
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


def _pick_default_vrma(items):
    """Choose the VRMA that VRMs auto-play when they have no per-model default.
    Matched by name so it works without hardcoding an asset id; configurable via
    DEFAULT_VRMA_NAME (default: a hip-hop dance clip). Returns the item or None."""
    if not items:
        return None
    raw = os.environ.get('DEFAULT_VRMA_NAME', 'hiphop,hip hop,hip-hop,dance')
    needles = [n.strip().lower() for n in raw.split(',') if n.strip()]
    for needle in needles:
        for item in items:
            if needle in (item.get('name') or '').lower():
                return item
    return None


def _animation_clip_name(model):
    runtime = model.runtime_metadata or {}
    for clip in runtime.get('animations') or []:
        if isinstance(clip, dict) and str(clip.get('name') or '').strip():
            return str(clip.get('name')).strip()
        if isinstance(clip, str) and clip.strip():
            return clip.strip()
    name = model.name or 'Untitled'
    for suffix in (' Humanoid Animation Clip', ' animation', ' Animation'):
        if name.lower().endswith(suffix.lower()):
            return name[:-len(suffix)].strip() or name
    return name


def _animation_registry_metadata(model):
    metadata = model.ai_metadata if isinstance(model.ai_metadata, dict) else {}
    animation = metadata.get('animation') if isinstance(metadata.get('animation'), dict) else {}
    runtime = model.runtime_metadata if isinstance(model.runtime_metadata, dict) else {}
    duration = animation.get('duration')
    if duration is None:
        for clip in runtime.get('animations') or []:
            if not isinstance(clip, dict) or clip.get('duration') is None:
                continue
            try:
                duration = round(max(0, float(clip.get('duration'))), 3)
                break
            except (TypeError, ValueError):
                continue
    return {
        'intent': animation.get('intent'),
        'intents': animation.get('intents') or ([animation.get('intent')] if animation.get('intent') else []),
        'actorKind': animation.get('actorKind'),
        'skeletonProfile': animation.get('skeletonProfile'),
        'category': animation.get('category'),
        'bodyType': animation.get('bodyType'),
        'tags': animation.get('tags') or metadata.get('tags') or model.ai_tags or model.tags or [],
        'loop': animation.get('loop'),
        'duration': duration,
        'durationSeconds': animation.get('durationSeconds', duration),
        'transitionIn': animation.get('transitionIn'),
        'transitionOut': animation.get('transitionOut'),
        'energy': animation.get('energy'),
        'locomotion': animation.get('locomotion'),
        'rootMotion': animation.get('rootMotion'),
        'speedMetersPerSecond': animation.get('speedMetersPerSecond'),
        'direction': animation.get('direction'),
        'gait': animation.get('gait'),
        'transition': animation.get('transition') or {'from': [], 'to': []},
        'aliases': animation.get('aliases') or [],
        'quality': animation.get('quality') or {'score': None, 'issues': []},
        'searchText': animation.get('searchText'),
        'requiresMount': animation.get('requiresMount'),
    }


def _vrma_item(model, *, clip_id, view_url, download_url, source):
    registry = _animation_registry_metadata(model)
    return {
        'id': clip_id,
        'name': _animation_clip_name(model),
        'view_url': view_url,
        'download_url': download_url,
        'source': source,
        'model_id': model.id,
        'description': model.description or model.ai_description or '',
        'tags': model.tags or [],
        'ai_tags': model.ai_tags or [],
        'ai_status': model.ai_status,
        'animation': registry,
        'intent': registry.get('intent'),
        'intents': registry.get('intents'),
        'actorKind': registry.get('actorKind'),
        'skeletonProfile': registry.get('skeletonProfile'),
        'category': registry.get('category'),
        'bodyType': registry.get('bodyType'),
        'loop': registry.get('loop'),
        'duration': registry.get('duration'),
        'durationSeconds': registry.get('durationSeconds'),
        'transitionIn': registry.get('transitionIn'),
        'transitionOut': registry.get('transitionOut'),
        'energy': registry.get('energy'),
        'locomotion': registry.get('locomotion'),
        'rootMotion': registry.get('rootMotion'),
        'speedMetersPerSecond': registry.get('speedMetersPerSecond'),
        'direction': registry.get('direction'),
        'gait': registry.get('gait'),
        'transition': registry.get('transition'),
        'aliases': registry.get('aliases'),
        'quality': registry.get('quality'),
        'searchText': registry.get('searchText'),
        'requiresMount': registry.get('requiresMount'),
    }


@api_bp.route('/vrma')
def list_vrma():
    """List VRMA animation assets available to apply on a VRM avatar:
    the current user's own VRMA assets plus all public ones. Also flags a
    default animation (hip-hop dance by name) that VRMs without their own saved
    default will auto-play."""
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        items = []
        for model in Model3D.list_vrma_for_user(user_id):
            view_url = url_for('api.view_model', model_id=model.id)
            items.append(_vrma_item(
                model,
                clip_id=model.id,
                view_url=view_url,
                download_url=url_for('api.download_model', model_id=model.id),
                source='upload',
            ))
        seen_clip_model_ids = {it['model_id'] for it in items}
        for model in Model3D.list_generated_vrma_for_user(user_id):
            # A native .vrma that also carries a vrma_file_id would otherwise be
            # listed twice (once per list); one clip per underlying model.
            if model.id in seen_clip_model_ids:
                continue
            seen_clip_model_ids.add(model.id)
            vrma_url = url_for('api.export_model', model_id=model.id) + '?format=vrma'
            # The generated VRMA is served via export; expose an explicit
            # download URL so API consumers can fetch the .vrma file.
            items.append(_vrma_item(
                model,
                clip_id=model.id + ':vrma',
                view_url=vrma_url,
                download_url=vrma_url,
                source='generated',
            ))
        default = _pick_default_vrma(items)
        for item in items:
            item['is_default'] = bool(default and item['id'] == default['id'])

        # `clips` is the external-client contract: a flat list keyed by name and
        # loaded by URL, with a stable `id` for a static fallback map. Kept
        # alongside `animations` (the in-app viewer reads `animations`/view_url).
        clips = [{
            'id': it['id'],
            'name': it['name'],
            'downloadUrl': it.get('download_url') or it['view_url'],
            'source': it['source'],
            'intent': it.get('intent'),
            'intents': it.get('intents') or [],
            'tags': it.get('animation', {}).get('tags') or it.get('tags') or [],
            'actorKind': it.get('actorKind'),
            'skeletonProfile': it.get('skeletonProfile'),
            'category': it.get('category'),
            'bodyType': it.get('bodyType'),
            'loop': it.get('loop'),
            'duration': it.get('duration'),
            'durationSeconds': it.get('durationSeconds'),
            'transitionIn': it.get('transitionIn'),
            'transitionOut': it.get('transitionOut'),
            'energy': it.get('energy'),
            'locomotion': it.get('locomotion'),
            'rootMotion': it.get('rootMotion'),
            'speedMetersPerSecond': it.get('speedMetersPerSecond'),
            'direction': it.get('direction'),
            'gait': it.get('gait'),
            'transition': it.get('transition'),
            'aliases': it.get('aliases') or [],
            'quality': it.get('quality') or {'score': None, 'issues': []},
            'searchText': it.get('searchText'),
            'requiresMount': it.get('requiresMount'),
        } for it in items]

        return jsonify({
            'clips': clips,
            'animations': items,
            'registry': clips,
            'default_id': default['id'] if default else None,
            'default_url': default['view_url'] if default else None,
        })
    except Exception as e:
        print(f"API list vrma error: {e}")
        return jsonify({'clips': [], 'animations': [], 'default_id': None, 'default_url': None})


@api_bp.route('/vrm')
@api_bp.route('/vrm-models')
def list_vrm():
    """List VRM avatar assets visible to the caller: models uploaded as native
    .vrm, plus models that have a derived VRM avatar (a 'vrm' variant from
    glb2vrm). Each item carries fetch + download URLs so an external client can
    load or save the avatar. The counterpart to GET /api/vrma."""
    try:
        user_only = request.args.get('user_only', 'false').lower() == 'true'
        include_private = request.args.get('include_private', 'false').lower() == 'true'
        principal, service = _api_principal()
        if user_only and service and not principal:
            return jsonify({'error': 'API token is valid, but no API upload user is configured.'}), 409
        user_id = principal.id if principal else None
        include_all_private = bool(include_private and service)
        owner_only = bool(user_only and principal)
        items = []
        seen = set()

        # Native .vrm uploads: served via the standard view/download routes.
        for model in Model3D.list_vrm_for_user(user_id, include_private=include_all_private):
            if owner_only and model.user_id != user_id:
                continue
            if model.id in seen:
                continue
            seen.add(model.id)
            items.append({
                'id': model.id,
                'model_id': model.id,
                'name': model.name or 'Untitled',
                'source': 'upload',
                'view_url': url_for('api.view_model', model_id=model.id),
                'download_url': url_for('api.download_model', model_id=model.id),
                'thumbnail_url': (url_for('api.get_thumbnail', model_id=model.id)
                                  if model.thumbnail_file_id else None),
                'size': model.file_size or 0,
            })

        # Derived VRM variants (e.g. a rigged GLB converted via glb2vrm).
        for model in Model3D.list_with_vrm_variant_for_user(user_id, include_private=include_all_private):
            if owner_only and model.user_id != user_id:
                continue
            if model.id in seen:
                continue
            seen.add(model.id)
            variant = ModelVariant.get(model.id, 'vrm')
            if not variant or not variant.file_id:
                continue
            opt = ModelVariant.get(model.id, 'vrm_optimized')
            items.append({
                'id': model.id + ':vrm',
                'model_id': model.id,
                'name': (model.name or 'Untitled') + ' (avatar)',
                'source': 'generated',
                'view_url': url_for('api.get_vrm_variant', model_id=model.id),
                'download_url': url_for('api.get_vrm_variant', model_id=model.id) + '?download=1',
                'thumbnail_url': (url_for('api.get_thumbnail', model_id=model.id)
                                  if model.thumbnail_file_id else None),
                'size': variant.size or 0,
                'optimized': bool(opt and opt.file_id),
                'optimized_url': (url_for('api.get_optimized_vrm', model_id=model.id)
                                  if opt and opt.file_id else None),
                'optimized_size': (opt.size if opt and opt.file_id else None),
            })

        return jsonify({'avatars': items, 'count': len(items)})
    except Exception as e:
        print(f"API list vrm error: {e}")
        return jsonify({'avatars': [], 'count': 0})


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
        _set_media_capture_state(model, status='captured', kind='preview')
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
        model = Model3D.get_by_id_or_alias(model_id)
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
    is_owner = can_manage_model(current_user, model) if current_user.is_authenticated else False
    # Live preview is possible for renderable mesh formats (the Three.js viewer
    # handles GLB/GLTF incl. Draco/meshopt). VRM/VRMA use other viewers, so we
    # leave those to their thumbnail/icon on browse.
    vrm_variant = ModelVariant.get(model.id, 'vrm')
    optimized_vrm_variant = ModelVariant.get(model.id, 'vrm_optimized')
    viewable = bool(model.viewable_file_id) or (model.file_format or '').lower() in ('glb', 'gltf')
    # Preview source priority: fixed-sourced game variant -> fixed-eyes/mouth ->
    # original. A stale game variant from before a bake must not hide the fixed
    # GLB while a fresh optimization is queued/running.
    game_variant = ModelVariant.get(model.id, 'game') if viewable else None
    fixed_variant = ModelVariant.get(model.id, 'fixed_eyes') if viewable else None
    game_uses_fixed = bool(
        game_variant
        and isinstance(game_variant.settings, dict)
        and game_variant.settings.get('source_is_fixed_eyes')
    )
    current_game = bool(game_variant and game_variant.file_id and (not fixed_variant or game_uses_fixed))
    if current_game:
        view_url = url_for('api.get_game_optimized', model_id=model.id)
    elif fixed_variant and fixed_variant.file_id:
        view_url = url_for('api.get_fixed_eyes', model_id=model.id)
    elif viewable:
        view_url = url_for('api.view_model', model_id=model.id) + '?viewer=2'
    else:
        view_url = None
    processing_state = _model_processing_state(model)
    lod_fields = _asset_lod_url_fields(model)
    return {
        'id': model.id,
        'name': model.name or 'Untitled',
        'file_format': model.file_format,
        'conversion_status': model.conversion_status,
        'download_count': model.download_count,
        'owner_username': getattr(model, 'owner_username', None) or 'Unknown',
        'tags': model.tags or [],
        'asset_category': model.asset_category,
        'asset_styles': model.asset_styles or [],
        'asset_types': model.asset_types or [],
        'runtime_metadata': model.runtime_metadata or {},
        'is_animated': bool(model.has_embedded_animations()),
        'has_preview': bool(model.preview_file_id),
        'has_thumbnail': bool(model.thumbnail_file_id),
        'preview_url': url_for('api.get_preview', model_id=model.id) if model.preview_file_id else None,
        'thumbnail_url': url_for('api.get_thumbnail', model_id=model.id) if model.thumbnail_file_id else None,
        'media_capture': {
            'needs_thumbnail': not bool(model.thumbnail_file_id),
            'needs_preview': not bool(model.preview_file_id),
            **_media_capture_state_for_model(model),
        },
        'processing_state': processing_state,
        'ready_for_tellus': processing_state['ready_for_tellus'],
        'catalog_ready': processing_state['catalog_ready'],
        'world_ready': processing_state['world_ready'],
        'storefront_ready': processing_state['storefront_ready'],
        'detail_url': url_for('main.model_detail', model_id=model.id),
        # For browse live-3D fallback when there's no cached preview yet.
        'is_owner': bool(is_owner),
        'viewable': viewable,
        'view_url': view_url,
        'has_game_optimized': current_game,
        'has_fixed_eyes': bool(fixed_variant and fixed_variant.file_id),
        'has_vrm_variant': bool(vrm_variant and vrm_variant.file_id),
        'has_optimized_vrm_variant': bool(optimized_vrm_variant and optimized_vrm_variant.file_id),
        'game_uses_fixed': game_uses_fixed,
        'lod_ready': lod_fields['lod_ready'],
        'lod_status': lod_fields['lod_status'],
        'lod_available_levels': lod_fields['lod_available_levels'],
        'lod_missing_levels': lod_fields['lod_missing_levels'],
        'lod_summary': lod_fields['lod_summary'],
        'camera_orbit': model.camera_orbit or None,
        'default_animation': model.default_animation or None,
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
        category = request.args.get('category')
        styles = Model3D.normalize_tags(request.args.getlist('style'))
        asset_types = Model3D.normalize_tags(request.args.getlist('type'))
        asset_kinds = Model3D.normalize_tags(request.args.getlist('asset'))
        ready_for_tellus = request.args.get('ready_for_tellus', 'false').lower() in {'1', 'true', 'yes'}

        models_list, total = Model3D.get_public_models(
            page=page, per_page=per_page,
            search=search or None, sort=sort,
            tag=tags or None, category=category, style=styles or None,
            asset_type=asset_types or None,
            asset_kind=asset_kinds or None,
            exclude_formats=['vrma', 'bvh'],
            exclude_animation_carriers=True)

        for model in models_list:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'

        if ready_for_tellus:
            models_list = _filter_ready_for_tellus(models_list)
            total = len(models_list)

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

        principal, service = _api_principal()
        owner_id = principal.id if principal and (service or current_user.is_authenticated) else None
        world = WorldState.upsert(world_id, payload, owner_id=owner_id)
        _tag_assets_for_tellus_world(world_id, world.state)
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
        
        category = request.args.get('category')
        styles = Model3D.normalize_tags(request.args.getlist('style'))
        asset_types = Model3D.normalize_tags(request.args.getlist('type'))
        models, total = Model3D.get_user_models(
            principal.id, page=page, per_page=per_page,
            category=category, style=styles or None, asset_type=asset_types or None)
        
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
                'asset_category': model.asset_category,
                'asset_styles': model.asset_styles,
                'asset_types': model.asset_types,
                'runtime_metadata': model.runtime_metadata,
                'ai_status': model.ai_status,
                'ai_title': (model.ai_metadata or {}).get('title'),
                'ai_description': model.ai_description,
                'ai_tags': model.ai_tags,
                'approve_game_ready': model.approve_game_ready,
                'approve_asset_store': model.approve_asset_store,
                **_media_presence_fields(model),
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


def _store_one_upload(file, base_name, description, is_public, tags, allowed_extensions, fs, max_bytes,
                      owner_id=None, asset_category=None, asset_styles=None, asset_types=None,
                      runtime_metadata=None, world_id=None):
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
    content_hash = hashlib.sha256(file_content).hexdigest()
    generation_id = _upload_generation_id()
    duplicate = Model3D.get_by_content_hash(content_hash)
    if duplicate:
        if duplicate.is_public or duplicate.user_id == owner_id:
            return None, f'Duplicate model already exists: {duplicate.name} ({duplicate.id}).'
        return None, 'Duplicate model already exists in the asset library.'

    derived_asset_types, derived_runtime_metadata = _file_derived_metadata(file_content, file_extension)
    asset_types = _merge_tags(_clean_asset_types(asset_types), derived_asset_types)
    runtime_metadata = _merge_runtime_metadata(runtime_metadata, derived_runtime_metadata)
    if file_extension == 'vrm':
        tags = _merge_tags(tags, ['avatar', 'vrm'])
        asset_types = _merge_tags(asset_types, ['avatar', 'vrm'])

    # Per-file name: when a shared base name is given AND multiple files are
    # involved, the caller passes base_name="" so each model is named from its
    # own filename. A single-file upload keeps the typed name.
    model_name = base_name or _name_from_filename(file.filename)
    is_legacy_pixal3d_direct = _is_legacy_pixal3d_direct_payload(
        file.filename,
        model_name,
        description,
        tags,
        world_id,
    )
    if is_legacy_pixal3d_direct and _block_legacy_pixal3d_uploads_enabled():
        return None, 'Pixal3D direct uploads are disabled; use the Tellus world upload path.'
    superseded_ids = []
    existing_generation = _find_existing_generation_upload(generation_id, owner_id)
    if existing_generation:
        if world_id and _is_legacy_pixal3d_direct_model(existing_generation):
            try:
                # Alias the deleted id -> the new model (recorded after save) so a
                # Tellus reference to the old generation still resolves.
                superseded_ids.append(existing_generation.id)
                existing_generation.delete()
            except Exception as e:
                print(f"Failed to replace legacy Pixal3D generation duplicate {existing_generation.id}: {e}")
                return None, f'Duplicate generation already exists: {existing_generation.name} ({existing_generation.id}).'
        else:
            return None, f'Duplicate generation already exists: {existing_generation.name} ({existing_generation.id}).'
    stats = _mesh_stats(runtime_metadata)
    if stats and is_legacy_pixal3d_direct:
        existing = _find_recent_authoritative_tellus_duplicate(stats, owner_id)
        if existing:
            return None, f'Duplicate generation already exists: {existing.name} ({existing.id}).'

    runtime_metadata = _merge_runtime_metadata(
        runtime_metadata,
        {'upload': _upload_provenance(world_id, content_hash=content_hash, generation_id=generation_id)},
    )

    gridfs_file_id = fs.put(
        file_content,
        filename=filename,
        content_type=file.content_type,
        metadata={
            'original_filename': file.filename,
            'uploaded_by': owner_id,
            'upload_date': Model3D().upload_date,
            'content_hash': content_hash,
        }
    )

    model = Model3D(
        name=model_name,
        description=description,
        file_format=file_extension,
        file_size=file_size,
        content_hash=content_hash,
        original_filename=file.filename,
        user_id=owner_id,
        is_public=is_public,
        gridfs_file_id=str(gridfs_file_id),
        tags=tags,
        asset_category=asset_category,
        asset_styles=asset_styles,
        asset_types=asset_types,
        runtime_metadata=runtime_metadata,
    )
    from app.conversion import enqueue
    enqueue(model, enabled=current_app.config.get('ENABLE_CONVERSION', True))
    model.save()
    _maybe_create_vrm_avatar_variant(model, file_content)
    # Auto-generate a game-optimized variant for GLB/GLTF uploads so every
    # asset gets a small, performant browse preview/download by default.
    _maybe_autostart_game_optimization(model)
    # Render a thumbnail immediately for natively-viewable uploads (glb/gltf/vrm)
    # so the browse grid shows an image without waiting for the reconciler.
    # Converted formats (fbx/obj) get theirs when conversion finishes.
    _maybe_render_thumbnail_async(model)
    _maybe_autotag_on_upload(model, context={'source': 'api_upload'})
    deleted_duplicates = _delete_recent_legacy_pixal3d_duplicates(model)
    if deleted_duplicates:
        print(
            f"Deleted {len(deleted_duplicates)} recent legacy Pixal3D duplicate(s) "
            f"for Tellus upload {model.id}: {deleted_duplicates}",
            flush=True,
        )
    # Alias every id we just superseded -> this surviving model, so a Tellus
    # world that stored a now-deleted id keeps resolving to the replacement.
    for old_id in list(superseded_ids) + list(deleted_duplicates):
        Model3D.record_alias(old_id, model.id, reason='generation_replace')
    return model, None


def _serialize_model(model):
    vrm_variant = ModelVariant.get(model.id, 'vrm')
    optimized_vrm_variant = ModelVariant.get(model.id, 'vrm_optimized')
    return {
        'id': model.id,
        'name': model.name,
        'description': model.description,
        'file_format': model.file_format,
        'file_size': model.file_size,
        'content_hash': model.content_hash,
        'original_filename': model.original_filename,
        'is_public': model.is_public,
        'upload_date': model.upload_date.isoformat() if model.upload_date else None,
        'conversion_status': model.conversion_status,
        'has_viewable': bool(model.viewable_file_id),
        'has_vrma': bool(model.vrma_file_id),
        'has_vrm_variant': bool(vrm_variant and vrm_variant.file_id),
        'has_optimized_vrm_variant': bool(optimized_vrm_variant and optimized_vrm_variant.file_id),
        'tags': model.tags,
        'asset_category': model.asset_category,
        'asset_styles': model.asset_styles,
        'asset_types': model.asset_types,
        'runtime_metadata': model.runtime_metadata,
        'ai_status': model.ai_status,
        'ai_error': model.ai_error,
        'ai_title': (model.ai_metadata or {}).get('title'),
        'ai_description': model.ai_description,
        'ai_tags': model.ai_tags,
        'approve_game_ready': model.approve_game_ready,
        'approve_asset_store': model.approve_asset_store,
        **_media_presence_fields(model),
    }


def _media_presence_fields(model):
    game = _game_optimized_fields(model)
    processing_state = _model_processing_state(model, game)
    capture_state = _media_capture_state_for_model(model)
    viewable = bool(model.viewable_file_id) or (model.file_format or '').lower() in ('glb', 'gltf')
    original_view_url = url_for('api.view_model', model_id=model.id) + '?viewer=2' if viewable else None
    return {
        'has_thumbnail': bool(model.thumbnail_file_id),
        'thumbnail_url': url_for('api.get_thumbnail', model_id=model.id) if model.thumbnail_file_id else None,
        'has_preview': bool(model.preview_file_id),
        'preview_url': url_for('api.get_preview', model_id=model.id) if model.preview_file_id else None,
        'view_url': original_view_url,
        'lod_preview_fallback_url': original_view_url,
        'mesh_stats': _model_mesh_stats(model),
        'physical_metadata': _model_physical_metadata(model),
        'effective_file_size': _effective_file_size(model, game),
        'effective_mesh_stats': _effective_mesh_stats(model, game),
        'effective_physical_metadata': _effective_physical_metadata(model, game),
        'detail_url': url_for('main.model_detail', model_id=model.id, capture=1),
        'media_capture': {
            'needs_thumbnail': not bool(model.thumbnail_file_id),
            'needs_preview': not bool(model.preview_file_id),
            'thumbnail_file_id': model.thumbnail_file_id,
            'preview_file_id': model.preview_file_id,
            'capture_url': url_for('main.model_detail', model_id=model.id, capture=1),
            **capture_state,
        },
        'processing_state': processing_state,
        'ready_for_tellus': processing_state['ready_for_tellus'],
        'catalog_ready': processing_state['catalog_ready'],
        'world_ready': processing_state['world_ready'],
        'storefront_ready': processing_state['storefront_ready'],
        **game,
        **_asset_lod_url_fields(model),
    }


def _model_processing_state(model, game_fields=None):
    fmt = (model.file_format or '').lower()
    game_fields = game_fields or _game_optimized_fields(model)
    game = game_fields.get('game_optimized') if isinstance(game_fields, dict) else None
    needs_game = fmt in ('glb', 'gltf') and not bool(game)
    needs_thumbnail = not bool(model.thumbnail_file_id)
    needs_preview = not bool(model.preview_file_id)
    needs_ai = _ai_enrichment_needs_visual_retry(model)
    media_ready = _media_capture_ready(model)
    blocked_by = []
    if needs_thumbnail:
        blocked_by.append('thumbnail')
    if needs_game:
        blocked_by.append('game_optimized')
    if not media_ready and (needs_thumbnail or needs_preview):
        blocked_by.append('media_capture_not_ready')
    if needs_ai and not model.thumbnail_file_id:
        blocked_by.append('ai_waiting_for_thumbnail')
    capture_state = _media_capture_state_for_model(model)
    ready_for_tellus = not needs_thumbnail and not needs_game
    catalog_ready = ready_for_tellus and not needs_ai
    return {
        'needs_thumbnail': needs_thumbnail,
        'needs_preview': needs_preview,
        'needs_game_optimized': needs_game,
        'needs_ai_enrichment': needs_ai,
        'media_capture_ready': media_ready,
        'media_capture_status': capture_state.get('status'),
        'ready_for_tellus': ready_for_tellus,
        'catalog_ready': catalog_ready,
        'world_ready': ready_for_tellus,
        'storefront_ready': catalog_ready,
        'blocked_by': blocked_by,
        'queue': {
            'media': needs_thumbnail or needs_preview,
            'optimization': needs_game,
            'enrichment': needs_ai,
        },
    }


def _filter_ready_for_tellus(models):
    return [
        model for model in models
        if _model_processing_state(model).get('ready_for_tellus')
    ]


def _media_capture_state_for_model(model):
    state = Model3D.normalize_media_capture(getattr(model, 'media_capture', None))
    status = state.get('status')
    if not status:
        if model.thumbnail_file_id and model.preview_file_id:
            status = 'captured'
        elif _media_capture_ready(model) and (not model.thumbnail_file_id or not model.preview_file_id):
            status = 'queued'
        elif not _media_capture_ready(model) and (not model.thumbnail_file_id or not model.preview_file_id):
            status = 'blocked'
        else:
            status = 'idle'
    return {
        'status': status,
        'attempt_count': int(state.get('attempt_count') or 0),
        'last_error': state.get('last_error'),
        'last_kind': state.get('last_kind'),
        'last_attempt_at': state.get('last_attempt_at'),
        'last_success_at': state.get('last_success_at'),
        'last_failed_at': state.get('last_failed_at'),
        'last_capture_url': state.get('last_capture_url'),
    }


def _set_media_capture_state(model, *, status, kind=None, error=None, capture_url=None):
    current = Model3D.normalize_media_capture(getattr(model, 'media_capture', None))
    now = datetime.utcnow().isoformat()
    attempts = int(current.get('attempt_count') or 0)
    if status in {'processing', 'failed', 'captured'}:
        attempts += 1
        current['attempt_count'] = attempts
        current['last_attempt_at'] = now
    current['status'] = status
    if kind:
        current['last_kind'] = str(kind)[:80]
    if capture_url:
        current['last_capture_url'] = str(capture_url)[:500]
    if status == 'captured':
        current['last_success_at'] = now
        current.pop('last_error', None)
    elif status == 'failed':
        current['last_failed_at'] = now
        current['last_error'] = str(error or 'media capture failed')[:500]
    elif error:
        current['last_error'] = str(error)[:500]
    model.media_capture = Model3D.normalize_media_capture(current)
    return model.media_capture


def _media_capture_ready(model):
    """Return True when the detail page can render this model for capture."""
    fmt = (model.file_format or '').lower()
    if fmt in ('vrma', 'bvh'):
        return False
    if fmt in ('glb', 'gltf', 'vrm'):
        return True
    return bool(model.viewable_file_id)


def _media_capture_suppressed(state):
    """Whether a queued item should be skipped this cycle.

    Stops the forever-retry loop: a job that has exhausted its attempts
    ('failed'/'blocked') or is inside its exponential backoff window is held
    back from the worker queue. The admin recapture path bypasses this by
    building items with recapture=True (which forces status fresh).
    """
    status = (state or {}).get('status')
    if status in {'failed', 'blocked'}:
        return True
    backoff_until = (state or {}).get('backoff_until')
    if backoff_until:
        try:
            if datetime.fromisoformat(backoff_until) > datetime.utcnow():
                return True
        except (TypeError, ValueError):
            return False
    return False


def _maybe_create_vrm_avatar_variant(model, file_content):
    if os.environ.get('AUTO_VRM_FROM_HUMANOID_GLB', '1').lower() in ('0', 'false', 'no', 'off'):
        return False
    fmt = (model.file_format or '').lower()
    if fmt not in ('glb', 'gltf'):
        return False
    if ModelVariant.get(model.id, 'vrm'):
        return False
    try:
        from app.conversion import looks_humanoid
        if not looks_humanoid(_gltf_node_names_from_bytes(file_content)):
            return False
        author = None
        if model.user_id:
            owner = User.get_by_id(model.user_id)
            author = owner.username if owner else None
        variant, _optimized = _convert_glb_bytes_to_vrm(model, file_content, author)
        return bool(variant and variant.file_id)
    except Exception as error:
        print(f"Auto VRM avatar generation skipped for {model.id}: {error}", flush=True)
        return False


def _media_capture_queue_item(model, *, force_capture=False):
    owner = User.get_by_id(model.user_id)
    needs_thumbnail = force_capture or not bool(model.thumbnail_file_id)
    needs_preview = force_capture or not bool(model.preview_file_id)
    needs_enrichment = model.ai_status not in ('done', 'processing', 'pending')
    capture_state = _media_capture_state_for_model(model)
    return {
        'id': model.id,
        'name': model.name,
        'file_format': model.file_format,
        'is_public': model.is_public,
        'owner': {
            'id': owner.id if owner else None,
            'username': owner.username if owner else 'Unknown',
        },
        'upload_date': model.upload_date.isoformat() if model.upload_date else None,
        'conversion_status': model.conversion_status,
        'has_viewable': bool(model.viewable_file_id),
        'has_thumbnail': bool(model.thumbnail_file_id),
        'thumbnail_file_id': model.thumbnail_file_id,
        'has_preview': bool(model.preview_file_id),
        'preview_file_id': model.preview_file_id,
        'needs_thumbnail': needs_thumbnail,
        'needs_preview': needs_preview,
        'force_capture': force_capture,
        'needs_enrichment': needs_enrichment,
        'capture_ready': _media_capture_ready(model),
        'capture_status': capture_state.get('status'),
        'capture_attempt_count': capture_state.get('attempt_count', 0),
        'capture_last_error': capture_state.get('last_error'),
        'capture_last_attempt_at': capture_state.get('last_attempt_at'),
        'capture_last_success_at': capture_state.get('last_success_at'),
        'capture_last_failed_at': capture_state.get('last_failed_at'),
        'detail_url': url_for('main.model_detail', model_id=model.id, capture=1),
        'capture_url': (
            url_for('main.model_detail', model_id=model.id, capture=1, regen=1)
            if force_capture else url_for('main.model_detail', model_id=model.id, capture=1)
        ),
    }


def _animation_capture_ready():
    with current_app.config['DB_ENGINE'].begin() as conn:
        native = conn.execute(
            select(model_rows.c.id)
            .where(model_rows.c.file_format == 'vrm')
            .limit(1)
        ).first()
        if native:
            return True
        derived = conn.execute(
            select(model_variants.c.model_id)
            .where(
                model_variants.c.kind.in_(['vrm_optimized', 'vrm']),
                model_variants.c.file_id.is_not(None),
            )
            .limit(1)
        ).first()
    return bool(derived)


def _animation_media_capture_queue_item(model, *, generated=False, capture_ready=True, force_capture=False):
    owner = User.get_by_id(model.user_id)
    clip_id = (model.id + ':vrma') if generated else model.id
    needs_thumbnail = force_capture or not bool(model.thumbnail_file_id)
    needs_preview = force_capture or not bool(model.preview_file_id)
    needs_enrichment = _ai_enrichment_needs_visual_retry(model)
    capture_state = _media_capture_state_for_model(model)
    return {
        'id': model.id,
        'clip_id': clip_id,
        'name': (model.name or 'Untitled') + (' animation' if generated else ''),
        'file_format': 'vrma',
        'is_public': model.is_public,
        'owner': {
            'id': owner.id if owner else None,
            'username': owner.username if owner else 'Unknown',
        },
        'upload_date': model.upload_date.isoformat() if model.upload_date else None,
        'conversion_status': model.conversion_status,
        'has_viewable': True,
        'has_thumbnail': bool(model.thumbnail_file_id),
        'thumbnail_file_id': model.thumbnail_file_id,
        'has_preview': bool(model.preview_file_id),
        'preview_file_id': model.preview_file_id,
        'needs_thumbnail': needs_thumbnail,
        'needs_preview': needs_preview,
        'force_capture': force_capture,
        'needs_enrichment': needs_enrichment,
        'capture_ready': capture_ready,
        'capture_status': capture_state.get('status'),
        'capture_attempt_count': capture_state.get('attempt_count', 0),
        'capture_last_error': capture_state.get('last_error'),
        'capture_last_attempt_at': capture_state.get('last_attempt_at'),
        'capture_last_success_at': capture_state.get('last_success_at'),
        'capture_last_failed_at': capture_state.get('last_failed_at'),
        'detail_url': url_for('main.animations'),
        'capture_url': url_for('main.animations', capture_clip=clip_id),
        'capture_mode': 'animation',
    }


def _animation_media_capture_queue_items(limit, *, include_not_ready=False, recapture=False):
    capture_ready = _animation_capture_ready()
    missing = true() if recapture else or_(
        model_rows.c.thumbnail_file_id.is_(None),
        model_rows.c.preview_file_id.is_(None),
        model_rows.c.ai_status.is_(None),
        model_rows.c.ai_status == '',
        model_rows.c.ai_status == 'failed',
        # Include done rows so Python can detect legacy/generic enrichment that
        # should be retried with a captured animation frame.
        model_rows.c.ai_status == 'done',
    )
    with current_app.config['DB_ENGINE'].begin() as conn:
        rows = conn.execute(
            select(model_rows)
            .where(
                missing,
                or_(model_rows.c.file_format == 'vrma', model_rows.c.vrma_file_id.is_not(None)),
            )
            .order_by(model_rows.c.upload_date.desc())
            .limit(limit * 3)
        ).mappings().all()

    items = []
    skipped_not_ready = 0
    for row in rows:
        model = Model3D.from_doc(row)
        generated = (model.file_format or '').lower() != 'vrma'
        item = _animation_media_capture_queue_item(
            model,
            generated=generated,
            capture_ready=capture_ready,
            force_capture=recapture,
        )
        if not (item['needs_thumbnail'] or item['needs_preview'] or item['needs_enrichment']):
            continue
        if not item['capture_ready'] and not include_not_ready:
            skipped_not_ready += 1
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items, skipped_not_ready


def _game_optimized_fields(model):
    """Summary of the model's game-optimized variant (if any) for serialization."""
    variant = ModelVariant.get(model.id, 'game')
    if not variant or not variant.file_id:
        return {'has_game_optimized': False, 'game_optimized': None}
    runtime_cost = _variant_runtime_cost_metadata(variant)
    return {
        'has_game_optimized': True,
        'game_optimized': {
            'size': variant.size,
            'settings': variant.settings,
            'mesh_stats': _variant_mesh_stats(variant),
            'physical': _variant_physical_metadata(variant),
            'runtime_cost': runtime_cost,
            'optimization': {
                'preset': (variant.settings or {}).get('preset'),
                'defaults_version': (variant.settings or {}).get('defaults_version'),
                'texture_compression': (variant.settings or {}).get('texture_compression'),
                'ktx2_produced': runtime_cost.get('ktx2_produced') if isinstance(runtime_cost, dict) else None,
            },
            'status': variant.status,
            'updated_at': variant.updated_at.isoformat() if variant.updated_at else None,
            'url': url_for('api.get_game_optimized', model_id=model.id),
            'download_url': url_for('api.get_game_optimized', model_id=model.id, download=1),
        },
    }


def _asset_lod_url_fields(model):
    expected_levels = [int(config['level']) for config in LOD_OPTIMIZE_LEVELS]
    lod_variants = []
    available_levels = []
    for level in expected_levels:
        variant = ModelVariant.get(model.id, 'lod', level=level)
        if not variant or not variant.file_id:
            continue
        available_levels.append(level)
        runtime_cost = _variant_runtime_cost_metadata(variant)
        mesh_stats = _variant_mesh_stats(variant)
        lod_variants.append({
            'level': level,
            'size': variant.size,
            'size_mb': round((variant.size or 0) / 1024 / 1024, 3) if variant.size else None,
            'vertices': _lod_metric_value(mesh_stats, runtime_cost, 'vertices', 'vertex_count'),
            'triangles': _lod_metric_value(mesh_stats, runtime_cost, 'triangles', 'triangle_count'),
            'file_format': variant.file_format,
            'status': variant.status,
            'settings': variant.settings or {},
            'mesh_stats': mesh_stats,
            'physical': _variant_physical_metadata(variant),
            'runtime_cost': runtime_cost,
            'url': url_for('api.get_asset_lod', model_id=model.id, level=level),
            'download_url': url_for('api.get_asset_lod', model_id=model.id, level=level, download=1),
            'updated_at': variant.updated_at.isoformat() if variant.updated_at else None,
        })
        lod_variants[-1]['recommended_use'] = _lod_recommended_use(
            lod_variants[-1]['vertices'],
            lod_variants[-1]['size'],
        )
    missing_levels = [level for level in expected_levels if level not in available_levels]
    lod_ready = not missing_levels
    impostor = ModelVariant.get(model.id, 'impostor')
    impostor_payload = None
    if impostor and impostor.file_id:
        settings = impostor.settings or {}
        impostor_payload = {
            'size': impostor.size,
            'size_mb': round((impostor.size or 0) / 1024 / 1024, 3) if impostor.size else None,
            'file_format': impostor.file_format,
            'status': impostor.status,
            'settings': settings,
            'type': settings.get('type'),
            'width': settings.get('width'),
            'height': settings.get('height'),
            'atlas_width': settings.get('atlas_width'),
            'atlas_height': settings.get('atlas_height'),
            'grid_size_x': settings.get('grid_size_x'),
            'grid_size_y': settings.get('grid_size_y'),
            'cell_size': settings.get('cell_size'),
            'view_count': settings.get('view_count'),
            'octahedron_type': settings.get('octahedron_type'),
            'source': settings.get('source'),
            'role': settings.get('role'),
            'url': url_for('api.get_asset_impostor', model_id=model.id),
            'download_url': url_for('api.get_asset_impostor', model_id=model.id, download=1),
            'updated_at': impostor.updated_at.isoformat() if impostor.updated_at else None,
        }
    asset_lod_urls = {
        'game_optimized': url_for('api.get_asset_game_optimized', model_id=model.id),
        'impostor': url_for('api.get_asset_impostor', model_id=model.id),
    }
    for level in expected_levels:
        asset_lod_urls[f'lod{level}'] = url_for('api.get_asset_lod', model_id=model.id, level=level)
    return {
        'asset_lod_urls': asset_lod_urls,
        'lod_variants': lod_variants,
        'has_lod_variants': bool(lod_variants),
        'lod_ready': lod_ready,
        'lod_status': 'ready' if lod_ready else ('partial' if lod_variants else 'missing'),
        'lod_available_levels': available_levels,
        'lod_missing_levels': missing_levels,
        'lod_summary': _lod_summary_payload(lod_variants, lod_ready, missing_levels),
        'has_impostor': bool(impostor and impostor.file_id),
        'impostor': impostor_payload,
    }


def _lod_metric_value(mesh_stats, runtime_cost, mesh_key, runtime_key):
    value = None
    if isinstance(mesh_stats, dict):
        value = mesh_stats.get(mesh_key)
    if value is None and isinstance(runtime_cost, dict):
        value = runtime_cost.get(runtime_key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _lod_recommended_use(vertices, size):
    """Heuristic bucket for quick asset triage. Counts stay exposed for overrides."""
    if vertices is None and size is None:
        return None
    vertices = vertices if vertices is not None else 10**9
    size = size if size is not None else 10**12
    if vertices <= 10000 and size <= 512 * 1024:
        return 'large_fill'
    if vertices <= 40000 and size <= 1536 * 1024:
        return 'general_fill'
    if vertices <= 100000 and size <= 4 * 1024 * 1024:
        return 'feature'
    return 'hero_only'


def _lod_summary_payload(lod_variants, lod_ready, missing_levels):
    levels = []
    for lod in lod_variants or []:
        size = lod.get('size')
        vertices = lod.get('vertices')
        triangles = lod.get('triangles')
        levels.append({
            'level': lod.get('level'),
            'size': size,
            'size_mb': lod.get('size_mb'),
            'vertices': vertices,
            'triangles': triangles,
            'recommended_use': _lod_recommended_use(vertices, size),
        })
    cheapest = None
    if levels:
        cheapest = min(
            levels,
            key=lambda item: (
                item.get('vertices') if item.get('vertices') is not None else 10**9,
                item.get('size') if item.get('size') is not None else 10**12,
            ),
        )
    return {
        'ready': bool(lod_ready),
        'status': 'ready' if lod_ready else ('partial' if levels else 'missing'),
        'missing_levels': missing_levels,
        'levels': levels,
        'cheapest_level': cheapest.get('level') if cheapest else None,
        'cheapest_vertices': cheapest.get('vertices') if cheapest else None,
        'cheapest_triangles': cheapest.get('triangles') if cheapest else None,
        'cheapest_size': cheapest.get('size') if cheapest else None,
        'cheapest_size_mb': cheapest.get('size_mb') if cheapest else None,
        'recommended_use': cheapest.get('recommended_use') if cheapest else None,
    }


def _model_mesh_stats(model):
    metadata = model.runtime_metadata or {}
    stats = metadata.get('mesh_stats') if isinstance(metadata, dict) else None
    return stats if isinstance(stats, dict) and stats else None


def _model_physical_metadata(model):
    metadata = model.runtime_metadata or {}
    physical = metadata.get('physical') if isinstance(metadata, dict) else None
    return physical if isinstance(physical, dict) and physical else None


def _variant_mesh_stats(variant):
    settings = variant.settings or {}
    stats = settings.get('mesh_stats') if isinstance(settings, dict) else None
    if isinstance(stats, dict) and stats:
        return stats
    data = variant.read_data()
    if not data:
        return None
    _asset_types, runtime = _file_derived_metadata(data, variant.file_format or 'glb')
    stats = runtime.get('mesh_stats') if isinstance(runtime, dict) else None
    return stats if isinstance(stats, dict) and stats else None


def _variant_physical_metadata(variant):
    settings = variant.settings or {}
    physical = settings.get('physical') if isinstance(settings, dict) else None
    if isinstance(physical, dict) and physical:
        return physical
    data = variant.read_data()
    if not data:
        return None
    _asset_types, runtime = _file_derived_metadata(data, variant.file_format or 'glb')
    physical = runtime.get('physical') if isinstance(runtime, dict) else None
    return physical if isinstance(physical, dict) and physical else None


def _variant_runtime_cost_metadata(variant):
    settings = variant.settings or {}
    stats = settings.get('runtime_cost') if isinstance(settings, dict) else None
    if isinstance(stats, dict) and stats:
        return stats
    data = variant.read_data()
    if not data:
        return None
    _asset_types, runtime = _file_derived_metadata(data, variant.file_format or 'glb')
    stats = _gltf_runtime_cost_metadata(data, variant.file_format or 'glb', runtime, variant.size)
    return stats if isinstance(stats, dict) and stats else None


def _effective_file_size(model, game_fields=None):
    game_fields = game_fields or _game_optimized_fields(model)
    game = game_fields.get('game_optimized') if isinstance(game_fields, dict) else None
    if isinstance(game, dict) and game.get('size'):
        return game.get('size')
    return model.file_size


def _effective_mesh_stats(model, game_fields=None):
    game_fields = game_fields or _game_optimized_fields(model)
    game = game_fields.get('game_optimized') if isinstance(game_fields, dict) else None
    stats = game.get('mesh_stats') if isinstance(game, dict) else None
    if isinstance(stats, dict) and stats:
        return stats
    return _model_mesh_stats(model)


def _effective_physical_metadata(model, game_fields=None):
    game_fields = game_fields or _game_optimized_fields(model)
    game = game_fields.get('game_optimized') if isinstance(game_fields, dict) else None
    physical = game.get('physical') if isinstance(game, dict) else None
    if isinstance(physical, dict) and physical:
        return physical
    return _model_physical_metadata(model)


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


def _merge_runtime_metadata(base, extra):
    merged = dict(base or {})
    extra = extra or {}
    if not isinstance(extra, dict):
        return Model3D.normalize_runtime_metadata(merged)
    for key, value in extra.items():
        if key == 'animations' and isinstance(value, list):
            existing = merged.get('animations') if isinstance(merged.get('animations'), list) else []
            seen = {
                str(item.get('name') or '').strip().lower()
                for item in existing
                if isinstance(item, dict)
            }
            additions = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name') or '').strip()
                if not name or name.lower() in seen:
                    continue
                seen.add(name.lower())
                additions.append(item)
            if existing or additions:
                merged['animations'] = [*existing, *additions]
        elif key not in merged or merged.get(key) in (None, {}, [], ''):
            merged[key] = value
    return Model3D.normalize_runtime_metadata(merged)


def _clean_asset_types(asset_types):
    return [
        value for value in Model3D.normalize_tags(asset_types or [])
        if value not in {'static', 'static-mesh', 'generated'}
    ]


def _mesh_stats(runtime_metadata):
    if not isinstance(runtime_metadata, dict):
        return None
    stats = runtime_metadata.get('mesh_stats')
    return stats if isinstance(stats, dict) and stats else None


def _mesh_stats_match(left, right):
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        left_triangles = int(left.get('triangles') or 0)
        right_triangles = int(right.get('triangles') or 0)
        left_vertices = int(left.get('vertices') or 0)
        right_vertices = int(right.get('vertices') or 0)
        left_primitives = int(left.get('primitives') or 0)
        right_primitives = int(right.get('primitives') or 0)
    except (TypeError, ValueError):
        return False
    if left_triangles <= 0 or right_triangles <= 0 or left_vertices <= 0 or right_vertices <= 0:
        return False
    if left_primitives and right_primitives and left_primitives != right_primitives:
        return False
    return abs(left_triangles - right_triangles) <= 2 and abs(left_vertices - right_vertices) <= 8


def _has_tellus_world_tag(tags):
    return any(str(tag or '').lower().startswith('tellus-world-') for tag in (tags or []))


def _is_legacy_pixal3d_direct_payload(filename, model_name, description, tags, world_id):
    if world_id:
        return False
    values = ' '.join([
        str(filename or ''),
        str(model_name or ''),
        str(description or ''),
        ' '.join(str(tag or '') for tag in (tags or [])),
    ]).lower()
    return (
        'pixal3d' in values
        and (
            'image-to-3d' in values
            or 'generated by pixal3d' in values
            or 'pixal3d-hyades-' in values
            or 'pixal3d hyades-' in values
        )
    )


def _block_legacy_pixal3d_uploads_enabled():
    return os.environ.get('BLOCK_LEGACY_PIXAL3D_UPLOADS', '1').lower() not in ('0', 'false', 'no', 'off')


def _is_legacy_pixal3d_direct_model(model):
    if not model or _has_tellus_world_tag(model.tags):
        return False
    return _is_legacy_pixal3d_direct_payload(
        model.original_filename,
        model.name,
        model.description,
        model.tags,
        world_id=None,
    )


def _is_authoritative_tellus_world_model(model):
    tags = [str(tag or '').lower() for tag in (model.tags or [])]
    return bool(model and 'tellus' in tags and _has_tellus_world_tag(tags))


def _recent_owner_models(owner_id, minutes=15, limit=100):
    try:
        models_list, _total = Model3D.list_models(
            page=1,
            per_page=limit,
            sort='newest',
            public_only=False,
            owner_id=owner_id,
        )
        cutoff = datetime.utcnow() - timedelta(minutes=minutes)
        return [
            model for model in models_list
            if model.upload_date and model.upload_date >= cutoff
        ]
    except Exception as e:
        print(f"Recent upload lookup failed: {e}")
        return []


def _owner_models(owner_id, limit=1000):
    try:
        models_list, _total = Model3D.list_models(
            page=1,
            per_page=limit,
            sort='newest',
            public_only=False,
            owner_id=owner_id,
        )
        return models_list
    except Exception as e:
        print(f"Owner upload lookup failed: {e}")
        return []


def _find_recent_matching_model(stats, owner_id, predicate, exclude_id=None):
    for candidate in _recent_owner_models(owner_id):
        if exclude_id and candidate.id == exclude_id:
            continue
        if not predicate(candidate):
            continue
        if _mesh_stats_match(stats, _mesh_stats(candidate.runtime_metadata)):
            return candidate
    return None


def _find_recent_authoritative_tellus_duplicate(stats, owner_id):
    return _find_recent_matching_model(stats, owner_id, _is_authoritative_tellus_world_model)


def _delete_recent_legacy_pixal3d_duplicates(authoritative_model):
    stats = _mesh_stats(authoritative_model.runtime_metadata)
    if not stats or not _is_authoritative_tellus_world_model(authoritative_model):
        return []
    deleted = []
    for candidate in list(_recent_owner_models(authoritative_model.user_id)):
        if candidate.id == authoritative_model.id:
            continue
        if not _is_legacy_pixal3d_direct_model(candidate):
            continue
        if not _mesh_stats_match(stats, _mesh_stats(candidate.runtime_metadata)):
            continue
        try:
            candidate.delete()
            deleted.append(candidate.id)
        except Exception as e:
            print(f"Failed to delete legacy Pixal3D duplicate {candidate.id}: {e}")
    return deleted


def _upload_generation_id():
    value = (
        request.form.get('generationId')
        or request.form.get('generation_id')
        or request.form.get('sourceGenerationId')
        or request.form.get('source_generation_id')
        or request.headers.get('X-Generation-Id')
        or request.headers.get('X-Asset-Generation-Id')
        or request.headers.get('X-Source-Generation-Id')
    )
    return str(value or '').strip()


def _find_existing_generation_upload(generation_id, owner_id):
    if not generation_id:
        return None
    for model in _owner_models(owner_id):
        metadata = model.runtime_metadata or {}
        upload = metadata.get('upload') if isinstance(metadata, dict) else None
        if not isinstance(upload, dict):
            continue
        if str(upload.get('generation_id') or '').strip() == generation_id:
            return model
    return None


def _upload_provenance(world_id, content_hash=None, generation_id=None):
    source = (
        request.form.get('source')
        or request.form.get('upload_source')
        or request.headers.get('X-Upload-Source')
        or request.headers.get('X-Upload-Origin')
        or request.headers.get('X-Asset-Source')
    )
    return {
        'source': source or ('tellus-world' if world_id else 'api-upload'),
        'world_id': world_id or '',
        'asset_username': request.headers.get('X-Asset-Username') or request.headers.get('X-Username') or '',
        'asset_user_id': request.headers.get('X-Asset-User-Id') or request.headers.get('X-User-Id') or '',
        'user_agent': request.headers.get('User-Agent') or '',
        'content_hash': content_hash or '',
        'generation_id': generation_id or '',
    }


_PROVENANCE_TAGS = {'tellus'}
_STRUCTURAL_ASSET_TYPES = {'rigged', 'animated'}
_AVATAR_ASSET_TYPES = {'avatar', 'vrm', 'humanoid'}


def _preserved_provenance_tags(existing):
    return [
        value for value in Model3D.normalize_tags(existing or [])
        if value in _PROVENANCE_TAGS or value.startswith('tellus-world-')
    ]


def _preserved_structural_asset_types(existing):
    return [
        value for value in Model3D.normalize_tags(existing or [])
        if value in _STRUCTURAL_ASSET_TYPES or value in _AVATAR_ASSET_TYPES
    ]


def _model_has_vrm_variant(model):
    try:
        variant = ModelVariant.get(model.id, 'vrm')
        return bool(variant and variant.file_id)
    except Exception:
        return False


def _model_is_avatar_like(model):
    tags = {str(tag or '').strip().lower() for tag in (model.tags or [])}
    asset_types = {str(tag or '').strip().lower() for tag in (model.asset_types or [])}
    return (
        (model.file_format or '').lower() == 'vrm'
        or bool((tags | asset_types) & {'avatar', 'vrm'})
        or _model_has_vrm_variant(model)
    )


def _model_is_animation_like(model):
    try:
        return bool(model and model.is_animation_carrier())
    except Exception:
        if not model:
            return False
        return (model.file_format or '').lower() in {'vrma', 'bvh'} or bool(model.vrma_file_id)


def _model_has_embedded_animation_clips(model):
    runtime = model.runtime_metadata if isinstance(getattr(model, 'runtime_metadata', None), dict) else {}
    return bool(runtime.get('animations'))


def _model_should_enrich_embedded_animations(model):
    if not model or _model_is_animation_like(model) or _model_is_avatar_like(model):
        return False
    if not _model_has_embedded_animation_clips(model):
        return False
    text = ' '.join([
        str(model.name or ''),
        str(model.original_filename or ''),
        str(model.asset_category or ''),
        ' '.join(str(tag) for tag in (model.tags or [])),
        ' '.join(str(kind) for kind in (model.asset_types or [])),
    ]).lower()
    # Humanoids are expected to flow through the VRM/avatar path. Embedded GLB
    # animation metadata is for animals, mounts, vehicles, and objects.
    if any(token in text for token in ('avatar', 'vrm', 'humanoid', 'mixamo', 'person', 'human')):
        return False
    return True


def _preserved_structural_runtime_metadata(existing):
    existing = existing or {}
    if not isinstance(existing, dict):
        return {}
    return {
        key: existing[key]
        for key in ('animations', 'mesh_stats', 'physical')
        if existing.get(key)
    }


def _run_ai_enrichment(model, data=None):
    data = data or {}
    overwrite = _as_bool(data.get('overwrite', True))
    include_title = _as_bool(data.get('include_title', True))
    include_description = _as_bool(data.get('include_description', True))

    is_animation = _model_is_animation_like(model)
    from app.ai_enrichment import enrich_animation_clip, enrich_embedded_model_animations, enrich_model, _generic_description, _generic_title
    enriched = (
        enrich_animation_clip(model, extra_context=data.get('context') or {})
        if is_animation else enrich_model(model, extra_context=data.get('context') or {})
    )
    embedded_animation = (
        enrich_embedded_model_animations(model, extra_context=data.get('context') or {})
        if not is_animation and _model_should_enrich_embedded_animations(model) else None
    )

    model.ai_status = 'done'
    model.ai_error = None
    model.ai_tags = Model3D.normalize_tags(enriched.get('tags', []))
    model.ai_description = enriched.get('description') or None
    model.ai_metadata = {
        'title': enriched.get('title'),
        'asset_category': enriched.get('asset_category'),
        'asset_styles': enriched.get('asset_styles', []),
        'asset_types': enriched.get('asset_types', []),
        'runtime_metadata': {},
        'summary': enriched.get('summary'),
        'categories': enriched.get('categories', []),
        'quality_notes': enriched.get('quality_notes', []),
        'animation': enriched.get('animation'),
        'animatedModel': embedded_animation,
        'animationClips': (embedded_animation or {}).get('animationClips', []),
        'provider': enriched.get('provider'),
        'base_url': enriched.get('base_url'),
        'model': enriched.get('model'),
        'response_id': enriched.get('response_id'),
        'vision_fallback': enriched.get('vision_fallback', False),
        'vision_mcp': enriched.get('vision_mcp', False),
        'vision_mcp_attempted': enriched.get('vision_mcp_attempted', False),
        'vision_mcp_analysis': enriched.get('vision_mcp_analysis'),
        'vision_mcp_error': enriched.get('vision_mcp_error'),
        'vision_frame': enriched.get('vision_frame', False),
        'preview_video_available': enriched.get('preview_video_available', False),
        'updated_at': datetime.utcnow().isoformat(),
    }
    avatar_like = _model_is_avatar_like(model)
    if overwrite:
        model.tags = _merge_tags(_preserved_provenance_tags(model.tags), model.ai_tags)
        if is_animation:
            model.asset_category = 'animation'
        elif avatar_like:
            model.tags = _merge_tags(model.tags, ['avatar', 'vrm'])
            model.asset_category = 'person'
        else:
            model.asset_category = enriched.get('asset_category') or model.asset_category
        model.asset_styles = Model3D.normalize_tags(enriched.get('asset_styles', []))
        blocked_types = {'static', 'static-mesh', *_STRUCTURAL_ASSET_TYPES, 'light-emitter', 'emissive', 'glowing', 'vrm', 'optimized'}
        if is_animation:
            blocked_types = blocked_types - {'animated'}
        ai_asset_types = [
            value for value in Model3D.normalize_tags(enriched.get('asset_types', []))
            if value not in blocked_types
        ]
        model.asset_types = _merge_tags(_preserved_structural_asset_types(model.asset_types), ai_asset_types)
        if is_animation:
            model.asset_types = _merge_tags(model.asset_types, ['avatar-animation'])
        if avatar_like:
            model.asset_types = _merge_tags(model.asset_types, ['avatar', 'vrm', 'humanoid'])
        model.runtime_metadata = Model3D.normalize_runtime_metadata(model.runtime_metadata)
        if include_title and enriched.get('title'):
            model.name = enriched['title']
        if include_description and model.ai_description:
            model.description = model.ai_description
    else:
        model.tags = _merge_tags(model.tags, model.ai_tags)
        if is_animation:
            model.asset_category = 'animation'
        elif avatar_like:
            model.tags = _merge_tags(model.tags, ['avatar', 'vrm'])
            model.asset_category = 'person'
        elif enriched.get('asset_category') and not model.asset_category:
            model.asset_category = enriched.get('asset_category')
        model.asset_styles = _merge_tags(model.asset_styles, enriched.get('asset_styles', []))
        ai_asset_types = [
            value for value in Model3D.normalize_tags(enriched.get('asset_types', []))
            if value not in {'static', 'static-mesh', *_STRUCTURAL_ASSET_TYPES, 'light-emitter', 'emissive', 'glowing', 'vrm', 'optimized'}
        ]
        model.asset_types = _merge_tags(model.asset_types, ai_asset_types)
        if is_animation:
            model.asset_types = _merge_tags(model.asset_types, ['avatar-animation'])
        if avatar_like:
            model.asset_types = _merge_tags(model.asset_types, ['avatar', 'vrm', 'humanoid'])
        if include_title and (not model.name or _generic_title(model.name)) and enriched.get('title'):
            model.name = enriched['title']
        if include_description and (not model.description or _generic_description(model.description)) and model.ai_description:
            model.description = model.ai_description
    model.save()
    return enriched


def _thumbnail_required_error(model):
    if _model_is_animation_like(model):
        return None
    if model and model.thumbnail_file_id:
        return None
    return 'AI enrichment requires a saved thumbnail. Capture/upload a thumbnail before enriching metadata.'


def _ai_enrichment_needs_visual_retry(model):
    if not model:
        return False
    if model.ai_status in ('pending', 'processing'):
        return False
    if model.ai_status in (None, '', 'failed'):
        return True
    metadata = model.ai_metadata if isinstance(model.ai_metadata, dict) else {}
    if metadata.get('vision_fallback') or metadata.get('vision_mcp_error'):
        return True
    if model.ai_error and 'thumbnail' in str(model.ai_error).lower():
        return True
    if model.ai_status == 'done':
        if _model_should_enrich_embedded_animations(model):
            metadata = model.ai_metadata if isinstance(model.ai_metadata, dict) else {}
            if not metadata.get('animatedModel') or not metadata.get('animationClips'):
                return True
        try:
            from app.ai_enrichment import _generic_description, _generic_title
            return (
                _generic_title(model.name)
                or _generic_description(model.description)
                or _generic_description(model.ai_description)
            )
        except Exception:
            return False
    return False


def _maybe_enqueue_autotag_after_thumbnail(model, context=None):
    if not _as_bool(os.environ.get('AI_AUTOTAG_ON_UPLOAD', '0')):
        return False
    if not model or not model.thumbnail_file_id:
        return False
    if not _ai_enrichment_needs_visual_retry(model):
        return False
    _enqueue_ai_enrichment(model, {
        'overwrite': os.environ.get('AI_AUTOTAG_OVERWRITE_ON_UPLOAD', '1'),
        'include_title': os.environ.get('AI_AUTOTAG_INCLUDE_TITLE', '1'),
        'include_description': os.environ.get('AI_AUTOTAG_INCLUDE_DESCRIPTION', '1'),
        'context': context or {},
    })
    _kick_ai_enrichment_worker(current_app._get_current_object())
    return True


def _run_ai_enrichment_worker(app, model_id, data):
    with app.app_context():
        model = Model3D.get_by_id(model_id)
        if not model:
            return
        try:
            _run_ai_enrichment(model, data)
        except Exception as e:
            model.ai_status = 'failed'
            model.ai_error = str(e)[:500]
            model.save()
            print(f"API autotag background error for model {model.id}: {model.ai_error}", flush=True)


def _enqueue_ai_enrichment(model, data):
    data = dict(data or {})
    data.pop('async', None)
    metadata = dict(model.ai_metadata or {})
    metadata['_job'] = {
        'data': data,
        'queued_at': datetime.utcnow().isoformat(),
    }
    model.ai_status = 'pending'
    model.ai_error = None
    model.ai_metadata = metadata
    model.save()


def _parse_ai_job_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _ai_job_is_claimable(status, job, stale_before):
    if not job:
        return False
    if status == 'pending':
        return True
    if status != 'processing':
        return False
    claimed_at = _parse_ai_job_datetime(job.get('claimed_at'))
    return claimed_at is None or claimed_at < stale_before


def _claim_ai_enrichment_job(app):
    now = datetime.utcnow()
    stale_minutes = int(os.environ.get('AI_AUTOTAG_STALE_MINUTES', '15'))
    stale_before = now - timedelta(minutes=stale_minutes)
    with app.config['DB_ENGINE'].begin() as conn:
        rows = conn.execute(
            select(model_rows.c.id, model_rows.c.ai_status, model_rows.c.ai_metadata)
            .where(model_rows.c.ai_status.in_(('pending', 'processing')))
            .order_by(model_rows.c.upload_date.asc())
            .limit(25)
        ).mappings().all()
        row = None
        metadata = {}
        job = {}
        for candidate in rows:
            candidate_metadata = dict(candidate.ai_metadata or {})
            candidate_job = dict(candidate_metadata.get('_job') or {})
            if _ai_job_is_claimable(candidate.ai_status, candidate_job, stale_before):
                row = candidate
                metadata = candidate_metadata
                job = candidate_job
                break
        if row is None:
            return None
        job['claimed_at'] = now.isoformat()
        metadata['_job'] = job
        updated = conn.execute(
            update(model_rows)
            .where(model_rows.c.id == row.id)
            .where(model_rows.c.ai_status == row.ai_status)
            .values(ai_status='processing', ai_error=None, ai_metadata=metadata)
        )
        if updated.rowcount != 1:
            return None
        return {'model_id': row.id, 'data': job.get('data') or {}}


def _process_ai_enrichment_claim(app, claim):
    with app.app_context():
        model = Model3D.get_by_id(claim['model_id'])
        if not model:
            return
        try:
            _run_ai_enrichment(model, claim.get('data') or {})
        except Exception as e:
            model.ai_status = 'failed'
            model.ai_error = str(e)[:500]
            metadata = dict(model.ai_metadata or {})
            job = dict(metadata.get('_job') or {})
            job['failed_at'] = datetime.utcnow().isoformat()
            metadata['_job'] = job
            model.ai_metadata = metadata
            model.save()
            print(f"AI enrichment worker error for model {model.id}: {model.ai_error}", flush=True)


def _drain_ai_enrichment_once(app):
    processed = 0
    while True:
        claim = _claim_ai_enrichment_job(app)
        if not claim:
            break
        _process_ai_enrichment_claim(app, claim)
        processed += 1
    return processed


def _kick_ai_enrichment_worker(app):
    if os.environ.get('AI_AUTOTAG_KICK_ON_REQUEST', '1').lower() in {'0', 'false', 'no', 'off'}:
        return

    def run_once():
        try:
            _drain_ai_enrichment_once(app)
        except Exception as e:
            print(f"AI enrichment kick error: {type(e).__name__}: {e}", flush=True)

    global AI_ENRICHMENT_KICK_THREAD
    with AI_ENRICHMENT_KICK_LOCK:
        if AI_ENRICHMENT_KICK_THREAD and AI_ENRICHMENT_KICK_THREAD.is_alive():
            return
        AI_ENRICHMENT_KICK_THREAD = threading.Thread(
            target=run_once,
            name='ai-enrichment-kick',
            daemon=True,
        )
        AI_ENRICHMENT_KICK_THREAD.start()


class AIEnrichmentWorker:
    def __init__(self, app, poll_interval=2.0):
        self.app = app
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name='ai-enrichment-worker', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                _drain_ai_enrichment_once(self.app)
            except Exception as e:
                print(f"AI enrichment worker loop error: {e}", flush=True)
            self._stop.wait(self.poll_interval)


def start_ai_enrichment_worker(app):
    global AI_ENRICHMENT_WORKER
    if AI_ENRICHMENT_WORKER is None:
        AI_ENRICHMENT_WORKER = AIEnrichmentWorker(app)
        AI_ENRICHMENT_WORKER.start()
    return AI_ENRICHMENT_WORKER


def _maybe_autotag_on_upload(model, context=None):
    if not _as_bool(os.environ.get('AI_AUTOTAG_ON_UPLOAD', '0')):
        return
    missing_thumbnail = _thumbnail_required_error(model)
    if missing_thumbnail:
        model.ai_status = None
        model.ai_error = missing_thumbnail
        model.save()
        return
    try:
        _run_ai_enrichment(model, {
            'overwrite': os.environ.get('AI_AUTOTAG_OVERWRITE_ON_UPLOAD', '1'),
            'include_title': os.environ.get('AI_AUTOTAG_INCLUDE_TITLE', '1'),
            'include_description': os.environ.get('AI_AUTOTAG_INCLUDE_DESCRIPTION', '1'),
            'context': context or {},
        })
    except Exception as e:
        model.ai_status = 'failed'
        model.ai_error = str(e)[:500]
        model.save()


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
    lod_fields = {}
    if row.source_model_id:
        model = Model3D.get_by_id(row.source_model_id)
        if model:
            lod_fields = _asset_lod_url_fields(model)
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
        'lod_result': result.get('lod_result'),
        'lod_variants': lod_fields.get('lod_variants') or result.get('lod_variants') or [],
        'lod_ready': lod_fields.get('lod_ready', result.get('lod_ready')),
        'lod_status': lod_fields.get('lod_status', result.get('lod_status')),
        'lod_available_levels': lod_fields.get('lod_available_levels') or result.get('lod_available_levels') or [],
        'lod_missing_levels': lod_fields.get('lod_missing_levels') or result.get('lod_missing_levels') or [],
        'lod_summary': lod_fields.get('lod_summary') or result.get('lod_summary'),
        'impostor_result': result.get('impostor_result'),
        'has_impostor': lod_fields.get('has_impostor'),
        'impostor': lod_fields.get('impostor'),
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


def _optimize_vrm_variant(model, texture_limit=2048):
    """Rig-safe optimization of the model's VRM avatar.

    A VRM is a GLB, but the game optimizer's mesh simplification (-si) would
    destroy the skeleton + skin weights that the VRMC_vrm humanoid map and VRMA
    retargeting depend on. So this uses a RIG-PRESERVING gltfpack profile:
    meshopt geometry compression (-cc) + optional texture compression
    (-tc/-tl, KTX2/Basis), and -kn to keep all named nodes (skeleton intact),
    with NO -si. gltfpack drops the (unknown-to-it) VRMC_vrm extension but keeps
    the named mixamorig:* skeleton, so we RE-INJECT the VRM humanoid metadata via
    glb2vrm afterwards. The result is a smaller, still-rigged VRM.

    Stores the result as a 'vrm_optimized' variant. Returns (variant, info) or
    raises. Best-effort: callers in the worker treat failure as non-fatal.
    """
    import shutil
    import subprocess
    import tempfile
    from app.conversion import glb_to_vrm, tool_paths

    variant = ModelVariant.get(model.id, 'vrm')
    if not variant or not variant.file_id:
        raise FileNotFoundError('No VRM variant to optimize.')
    src_bytes = variant.read_data()
    if not src_bytes:
        raise FileNotFoundError('VRM variant file not found.')
    if src_bytes[:4] != b'glTF':
        raise ValueError('VRM variant is not a binary glTF.')

    fs = current_app.config['FILE_STORE']

    # Already meshopt-compressed? Then it's effectively optimized; register as-is.
    if _glb_is_meshopt_compressed(src_bytes):
        out_bytes = src_bytes
        report = {'already_optimized': True}
    else:
        gltfpack_bin = shutil.which('gltfpack')
        if not gltfpack_bin:
            raise RuntimeError('VRM optimization is unavailable because gltfpack is not installed.')
        workdir = tempfile.mkdtemp(prefix='vrm_optimize_')
        try:
            in_path = os.path.join(workdir, 'input.glb')
            packed_path = os.path.join(workdir, 'packed.glb')
            out_path = os.path.join(workdir, 'avatar-opt.vrm')
            report_path = os.path.join(workdir, 'report.json')
            with open(in_path, 'wb') as f:
                f.write(src_bytes)
            cmd = [
                gltfpack_bin,
                '-i', in_path,
                '-o', packed_path,
                '-cc',   # meshopt geometry compression
                '-kn',   # keep named nodes -> skeleton survives for the rig
                '-km',   # keep named materials (preserves VRM material refs)
                '-r', report_path,
            ]
            if texture_limit:
                cmd.extend(['-tc', '-tl', str(texture_limit)])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode != 0:
                msg = (result.stderr or result.stdout or 'gltfpack failed.').strip()
                raise RuntimeError(msg[-1000:] or 'gltfpack failed.')

            # gltfpack stripped VRMC_vrm but kept the mixamorig:* skeleton.
            # Re-inject the VRM humanoid extension over the compressed file.
            paths = tool_paths(current_app)
            glb_to_vrm(
                paths['node'], paths['fbx2vrma_dir'], packed_path, out_path,
                name=(model.name or None),
            )
            with open(out_path, 'rb') as f:
                out_bytes = f.read()
            report = {}
            if os.path.exists(report_path):
                try:
                    with open(report_path, 'r', encoding='utf-8') as f:
                        report = json.load(f)
                except Exception as e:
                    print(f"Could not read VRM gltfpack report: {e}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # Sanity: the final file must carry the VRM extension (re-injected above),
    # else the rig didn't survive and we must NOT store it.
    if b'VRMC_vrm' not in out_bytes:
        raise RuntimeError('Optimized output lost the VRMC_vrm extension; refusing to store.')

    original_size = len(src_bytes)
    optimized_size = len(out_bytes)
    savings = 0 if original_size <= 0 else 1 - (optimized_size / original_size)

    file_id = fs.put(
        out_bytes,
        filename=f'{_safe_stem(model)}-avatar-opt.vrm',
        content_type=_mime_for('vrm'),
        metadata={
            'kind': 'vrm_optimized',
            'source_model_id': model.id,
            'source_size': original_size,
            'optimized_size': optimized_size,
            'texture_limit': texture_limit,
        },
    )
    opt_variant, old_file_id = ModelVariant.upsert(
        model.id, 'vrm_optimized', str(file_id),
        file_format='vrm', size=optimized_size,
        settings={'source_size': original_size, 'savings_ratio': round(savings, 4),
                  'texture_limit': texture_limit},
        status='ready',
    )
    if old_file_id and old_file_id != str(file_id):
        try:
            fs.delete(old_file_id)
        except Exception as e:
            print(f"Old vrm_optimized blob {old_file_id} not deleted: {e}")

    return opt_variant, {
        'source_size': original_size,
        'optimized_size': optimized_size,
        'savings_ratio': round(savings, 4),
    }


def _run_game_optimizer(model, owner_id, settings):
    import shutil
    import subprocess
    import tempfile

    # gltfpack is required only to RUN an optimization; the already-optimized
    # short-circuit below (registering an existing meshopt GLB as the variant)
    # does not need it, so we check inside the gltfpack branch instead.
    gltfpack_bin = shutil.which('gltfpack')

    settings = _normalize_game_optimization_settings(settings)
    texture_limit = settings['texture_limit']
    simplify_ratio = settings['simplify_ratio']
    compression_mode = settings['compression_mode']
    texture_limit_applied = bool(texture_limit)

    # Prefer a mesh2motion/rigged roundtrip as the source when it exists so
    # re-optimization keeps the returned animation clips attached to the
    # original asset. Fall back to fixed-eyes, then the normal viewable data.
    src_bytes = None
    src_fmt = None
    source_variant_kind = 'original'
    used_rigged = False
    used_fixed_eyes = False
    rigged_variant = ModelVariant.get(model.id, 'rigged')
    if rigged_variant and rigged_variant.file_id:
        data = rigged_variant.read_data()
        if data:
            src_bytes = data
            src_fmt = (rigged_variant.file_format or 'glb').lower()
            source_variant_kind = 'rigged'
            used_rigged = True
    fixed_variant = ModelVariant.get(model.id, 'fixed_eyes')
    if src_bytes is None and fixed_variant and fixed_variant.file_id:
        data = fixed_variant.read_data()
        if data:
            src_bytes = data
            src_fmt = (fixed_variant.file_format or 'glb').lower()
            source_variant_kind = 'fixed_eyes'
            used_fixed_eyes = True
    if src_bytes is None:
        src_bytes, src_fmt = model.get_viewable_data()
        src_fmt = (src_fmt or model.file_format or '').lower()
    if not src_bytes:
        raise FileNotFoundError('Source file not found')
    if src_fmt not in ('glb', 'gltf'):
        raise ValueError('Game optimization currently supports GLB/GLTF assets.')

    # Repair legacy gltfpack -cf GLBs that reference a missing external
    # *.fallback.bin (otherwise gltfpack reports "resource not found").
    if src_fmt == 'glb':
        src_bytes = _force_meshopt_required_for_external_fallback(src_bytes)

    workdir = tempfile.mkdtemp(prefix='game_optimize_')
    try:
        report = {}
        source_prepare = {'draco_decompressed': False}

        # If the source is ALREADY a meshopt/gltfpack GLB, it is effectively
        # already game-optimized. Re-running gltfpack on it is redundant and
        # fails on the missing fallback buffer -- so register the existing file
        # as the variant (the optimized preview + size still show up).
        if src_fmt == 'glb' and _glb_is_meshopt_compressed(src_bytes):
            out_bytes = src_bytes
            report = {'already_optimized': True}
        else:
            if not gltfpack_bin:
                raise RuntimeError('Game optimization is unavailable because gltfpack is not installed.')
            in_path, source_prepare = _prepare_lod_input_path(src_bytes, src_fmt, workdir)
            out_path = os.path.join(workdir, 'game.glb')
            report_path = os.path.join(workdir, 'report.json')

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
            'source_variant_kind': source_variant_kind,
            'source_prepare': source_prepare,
            'source_is_rigged': used_rigged,
            'source_is_fixed_eyes': used_fixed_eyes,
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
        source_runtime = _file_derived_metadata(src_bytes, src_fmt)[1]
        optimized_runtime = _file_derived_metadata(out_bytes, 'glb')[1]
        source_mesh_stats = source_runtime.get('mesh_stats')
        optimized_mesh_stats = optimized_runtime.get('mesh_stats')
        source_physical = source_runtime.get('physical')
        optimized_physical = optimized_runtime.get('physical')
        source_runtime_cost = _gltf_runtime_cost_metadata(src_bytes, src_fmt, source_runtime, original_size)
        runtime_cost = _gltf_runtime_cost_metadata(out_bytes, 'glb', optimized_runtime, optimized_size)
        if runtime_cost:
            runtime_cost['ktx2_produced'] = bool(
                runtime_cost.get('ktx2')
                and not (source_runtime_cost or {}).get('ktx2')
            )
            runtime_cost['preset'] = settings.get('preset')
            runtime_cost['defaults_version'] = settings.get('defaults_version')
        # gltfpack + meshopt/KTX2 can leave accessor min/max values in a
        # quantized integer domain when inspected statically. That is useful for
        # low-level diagnostics but wrong for world/avatar scale. The optimized
        # variant represents the same asset, so keep source-space physical
        # bounds as the public/runtime physical metadata.
        variant_physical = source_physical or optimized_physical

        # Attach the optimized GLB to the SOURCE model as a 'game' variant
        # (no separate Model3D). Re-optimizing replaces the existing variant;
        # the old blob is removed once the pointer is swapped.
        variant_settings = {
            'preset': settings.get('preset'),
            'defaults_version': settings.get('defaults_version'),
            'texture_limit': texture_limit,
            'simplify_ratio': simplify_ratio,
            'compression_mode': compression_mode,
            'texture_compression': texture_note,
            'original_size': original_size,
            'optimized_size': optimized_size,
            'savings_ratio': savings_ratio,
            'source_mesh_stats': source_mesh_stats,
            'mesh_stats': optimized_mesh_stats,
            'source_physical': source_physical,
            'optimized_physical_raw': optimized_physical,
            'physical': variant_physical,
            'source_runtime_cost': source_runtime_cost,
            'runtime_cost': runtime_cost,
            'source_variant_kind': source_variant_kind,
            'source_prepare': source_prepare,
            'source_is_rigged': used_rigged,
            'source_is_fixed_eyes': used_fixed_eyes,
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
            'source_mesh_stats': source_mesh_stats,
            'optimized_mesh_stats': optimized_mesh_stats,
            'source_variant_kind': source_variant_kind,
            'source_is_rigged': used_rigged,
            'source_is_fixed_eyes': used_fixed_eyes,
            'settings': {
                'texture_limit': texture_limit,
                'simplify_ratio': simplify_ratio,
                'preset': settings.get('preset'),
                'defaults_version': settings.get('defaults_version'),
                'compression': 'gltfpack -cc' if compression_mode == 'meshopt' else 'gltfpack without mesh compression',
                'texture_compression': texture_note,
            },
            'runtime_cost': runtime_cost,
            'source_prepare': source_prepare,
            'report': report,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _optimizer_source_bytes(model):
    """Return the best static GLB/GLTF source for derived mesh variants."""
    src_bytes = None
    src_fmt = None
    source_variant_kind = 'original'
    fixed_variant = ModelVariant.get(model.id, 'fixed_eyes')
    if fixed_variant and fixed_variant.file_id:
        data = fixed_variant.read_data()
        if data:
            src_bytes = data
            src_fmt = (fixed_variant.file_format or 'glb').lower()
            source_variant_kind = 'fixed_eyes'
    if src_bytes is None:
        src_bytes, src_fmt = model.get_viewable_data()
        src_fmt = (src_fmt or model.file_format or '').lower()
    if not src_bytes:
        raise FileNotFoundError('Source file not found')
    if src_fmt not in ('glb', 'gltf'):
        raise ValueError('LOD generation currently supports GLB/GLTF assets.')
    if src_fmt == 'glb':
        src_bytes = _force_meshopt_required_for_external_fallback(src_bytes)
    return src_bytes, src_fmt, source_variant_kind


def _gltf_transform_cli_path():
    tools_dir = Path(os.environ.get('FBX2VRMA_DIR') or 'tools')
    if not tools_dir.is_absolute():
        tools_dir = Path(current_app.root_path).parent / tools_dir
    return tools_dir / 'node_modules' / '@gltf-transform' / 'cli' / 'bin' / 'cli.js'


def _meshopt_decode_script_path():
    tools_dir = Path(os.environ.get('FBX2VRMA_DIR') or 'tools')
    if not tools_dir.is_absolute():
        tools_dir = Path(current_app.root_path).parent / tools_dir
    return tools_dir / 'decode-meshopt-glb.mjs'


def _decode_meshopt_input_path(src_bytes, workdir):
    """Decode EXT_meshopt_compression to a self-contained GLB for gltfpack.

    Some gltfpack outputs keep an external fallback buffer URI even when the
    compressed stream is embedded in the GLB. The app stores only one file, so
    that fallback is missing and gltfpack reports "resource not found". The
    helper strips the fallback metadata, decodes meshopt, and writes plain GLB.
    """
    import shutil
    import subprocess

    node_bin = os.environ.get('NODE_BIN') or shutil.which('node')
    script_path = _meshopt_decode_script_path()
    if not node_bin or not script_path.exists():
        raise RuntimeError(
            'Source uses meshopt compression; install Node and tools/decode-meshopt-glb.mjs '
            'dependencies so LOD generation can decode it.'
        )

    in_path = os.path.join(workdir, 'input-meshopt.glb')
    out_path = os.path.join(workdir, 'input-demeshopt.glb')
    with open(in_path, 'wb') as f:
        f.write(src_bytes)
    result = subprocess.run(
        [node_bin, str(script_path), in_path, out_path],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0 or not os.path.exists(out_path):
        msg = (result.stderr or result.stdout or 'meshopt decode failed.').strip()
        raise RuntimeError(f'Meshopt decode failed before LOD generation: {msg[-1000:]}')
    return out_path


def _prepare_lod_input_path(src_bytes, src_fmt, workdir):
    """Write an optimizer input file, removing meshopt/Draco compression if necessary."""
    import shutil
    import subprocess

    in_path = os.path.join(workdir, f'input.{src_fmt}')
    with open(in_path, 'wb') as f:
        f.write(src_bytes)

    source_prepare = {
        'meshopt_decompressed': False,
        'draco_decompressed': False,
    }
    if src_fmt == 'glb' and _gltf_uses_extension(src_bytes, src_fmt, 'EXT_meshopt_compression'):
        in_path = _decode_meshopt_input_path(src_bytes, workdir)
        with open(in_path, 'rb') as f:
            src_bytes = f.read()
        src_fmt = 'glb'
        source_prepare.update({
            'meshopt_decompressed': True,
            'meshopt_decoder': 'tools/decode-meshopt-glb.mjs',
        })

    uses_draco = _gltf_uses_extension(src_bytes, src_fmt, 'KHR_draco_mesh_compression')
    if not uses_draco:
        return in_path, source_prepare

    node_bin = os.environ.get('NODE_BIN') or shutil.which('node')
    cli_path = _gltf_transform_cli_path()
    if not node_bin or not cli_path.exists():
        raise RuntimeError(
            'Source uses Draco mesh compression; install Node and @gltf-transform/cli '
            'in tools/package.json so LOD generation can decode it.'
        )

    out_path = os.path.join(workdir, 'input-dedraco.glb')
    result = subprocess.run(
        [node_bin, str(cli_path), 'copy', in_path, out_path],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0 or not os.path.exists(out_path):
        msg = (result.stderr or result.stdout or 'gltf-transform copy failed.').strip()
        raise RuntimeError(f'Draco decode failed before LOD generation: {msg[-1000:]}')
    source_prepare.update({
        'draco_decompressed': True,
        'draco_decoder': '@gltf-transform/cli copy',
    })
    return out_path, source_prepare


def _run_lod_optimizer(model, owner_id=None, levels=None):
    """Generate Tellus-facing LOD GLBs under the original asset id.

    Borrowed design choice from Hyperscape's LOD package: each level is
    simplified from the original source, never cascaded from the previous LOD.
    """
    import shutil
    import subprocess
    import tempfile

    if _is_rigged_or_avatar(model):
        return {
            'success': False,
            'skipped': True,
            'reason': 'rigged_or_avatar',
            'source_model_id': model.id,
        }

    gltfpack_bin = shutil.which('gltfpack')
    if not gltfpack_bin:
        raise RuntimeError('LOD generation is unavailable because gltfpack is not installed.')

    levels = levels or LOD_OPTIMIZE_LEVELS
    src_bytes, src_fmt, source_variant_kind = _optimizer_source_bytes(model)
    source_runtime = _file_derived_metadata(src_bytes, src_fmt)[1]
    source_mesh_stats = source_runtime.get('mesh_stats')
    source_physical = source_runtime.get('physical')
    fs = current_app.config['FILE_STORE']
    original_size = len(src_bytes)
    generated = []
    workdir = tempfile.mkdtemp(prefix='lod_optimize_')
    try:
        in_path, source_prepare = _prepare_lod_input_path(src_bytes, src_fmt, workdir)

        for config in levels:
            level = int(config['level'])
            texture_limit = int(config.get('texture_limit') or 0)
            simplify_ratio = float(config.get('simplify_ratio') or 1)
            simplification_error = config.get('simplification_error')
            compression_mode = str(config.get('compression_mode') or 'meshopt').lower()
            flat_material = bool(config.get('flat_material'))
            flat_material_color = config.get('flat_material_color') or [0.30, 0.42, 0.20, 1.0]
            out_path = os.path.join(workdir, f'lod{level}.glb')
            report_path = os.path.join(workdir, f'lod{level}.json')
            cmd = [
                gltfpack_bin,
                '-i', in_path,
                '-o', out_path,
                '-si', f'{simplify_ratio:g}',
                '-r', report_path,
            ]
            if simplification_error is not None:
                cmd.extend(['-se', f'{float(simplification_error):g}'])
            if config.get('permissive'):
                cmd.append('-sp')
            if config.get('aggressive'):
                cmd.append('-sa')
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
                raise RuntimeError(f'LOD {level}: {msg[-1000:] or "gltfpack failed."}')

            if flat_material:
                with open(out_path, 'rb') as f:
                    simplified_bytes = f.read()
                flat_input_path = os.path.join(workdir, f'lod{level}-flat-input.glb')
                with open(flat_input_path, 'wb') as f:
                    f.write(_flatten_lod_glb_materials(simplified_bytes, color=flat_material_color))
                flat_report_path = os.path.join(workdir, f'lod{level}-flat.json')
                flat_cmd = [
                    gltfpack_bin,
                    '-i', flat_input_path,
                    '-o', out_path,
                    '-r', flat_report_path,
                ]
                if compression_mode == 'meshopt':
                    flat_cmd.append('-cc')
                flat_result = subprocess.run(
                    flat_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if flat_result.returncode != 0:
                    msg = (flat_result.stderr or flat_result.stdout or 'gltfpack failed.').strip()
                    raise RuntimeError(f'LOD {level} flat material repack: {msg[-1000:] or "gltfpack failed."}')
                report_path = flat_report_path

            with open(out_path, 'rb') as f:
                out_bytes = f.read()
            report = {}
            if os.path.exists(report_path):
                try:
                    with open(report_path, 'r', encoding='utf-8') as f:
                        report = json.load(f)
                except Exception as e:
                    print(f"Could not read LOD {level} gltfpack report: {e}")

            runtime = _file_derived_metadata(out_bytes, 'glb')[1]
            optimized_size = len(out_bytes)
            savings_ratio = 0 if original_size <= 0 else 1 - (optimized_size / original_size)
            metadata = {
                'kind': 'lod',
                'level': level,
                'source_model_id': model.id,
                'source_format': src_fmt,
                'source_size': original_size,
                'source_variant_kind': source_variant_kind,
                'source_prepare': source_prepare,
                'optimized_size': optimized_size,
                'texture_limit': texture_limit or None,
                'simplify_ratio': simplify_ratio,
                'simplification_error': simplification_error,
                'aggressive': bool(config.get('aggressive')),
                'permissive': bool(config.get('permissive')),
                'flat_material': flat_material,
                'flat_material_color': flat_material_color if flat_material else None,
                'flat_material_stage': 'post_simplification' if flat_material else None,
                'target_vertices': config.get('target_vertices'),
                'compression_mode': compression_mode,
                'defaults_version': LOD_OPTIMIZE_DEFAULTS_VERSION,
                'role': config.get('role'),
                'gltfpack': {
                    'mode': compression_mode,
                    'texture_compression': bool(texture_limit),
                    'report': report,
                },
            }
            file_id = fs.put(
                out_bytes,
                filename=f'{_safe_stem(model)}-lod{level}.glb',
                content_type=_mime_for('glb'),
                metadata=metadata,
            )
            variant_settings = {
                'defaults_version': LOD_OPTIMIZE_DEFAULTS_VERSION,
                'level': level,
                'role': config.get('role'),
                'texture_limit': texture_limit,
                'simplify_ratio': simplify_ratio,
                'simplification_error': simplification_error,
                'aggressive': bool(config.get('aggressive')),
                'permissive': bool(config.get('permissive')),
                'flat_material': flat_material,
                'flat_material_color': flat_material_color if flat_material else None,
                'flat_material_stage': 'post_simplification' if flat_material else None,
                'target_vertices': config.get('target_vertices'),
                'compression_mode': compression_mode,
                'original_size': original_size,
                'optimized_size': optimized_size,
                'savings_ratio': savings_ratio,
                'source_mesh_stats': source_mesh_stats,
                'mesh_stats': runtime.get('mesh_stats'),
                'source_physical': source_physical,
                'optimized_physical_raw': runtime.get('physical'),
                'physical': source_physical or runtime.get('physical'),
                'source_variant_kind': source_variant_kind,
                'source_prepare': source_prepare,
                'source_runtime_cost': _gltf_runtime_cost_metadata(src_bytes, src_fmt, source_runtime, original_size),
                'runtime_cost': _gltf_runtime_cost_metadata(out_bytes, 'glb', runtime, optimized_size),
                'report': report,
            }
            variant, old_file_id = ModelVariant.upsert(
                model.id, 'lod', str(file_id),
                level=level, file_format='glb', size=optimized_size,
                settings=variant_settings, status='ready',
            )
            if old_file_id and old_file_id != str(file_id):
                try:
                    fs.delete(old_file_id)
                except Exception as e:
                    print(f"Old LOD {level} blob {old_file_id} not deleted: {e}")
            generated.append({
                'level': level,
                'variant': variant.to_api() if variant else None,
                'optimized_size': optimized_size,
                'savings_ratio': savings_ratio,
                'mesh_stats': runtime.get('mesh_stats'),
            })

        return {
            'success': True,
            'source_model_id': model.id,
            'source_variant_kind': source_variant_kind,
            'original_size': original_size,
            'levels': generated,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _impostor_source_image_bytes(model, *, render_size=512):
    fs = current_app.config['FILE_STORE']
    if model.thumbnail_file_id:
        try:
            return fs.get(model.thumbnail_file_id).read(), 'thumbnail'
        except Exception as e:
            print(f"Impostor: could not read thumbnail for {model.id}: {e}")

    from app import render as render_mod
    if not render_mod.render_available():
        raise RuntimeError('Server render stack unavailable and no thumbnail exists.')
    try:
        data, view_fmt = model.get_viewable_data()
    except Exception as e:
        raise RuntimeError('No viewable mesh available for impostor generation.') from e
    if not data or data[:4] != b'glTF':
        raise RuntimeError('No usable GLB bytes for impostor generation.')
    png = render_mod.render_glb_to_png(
        data,
        file_type=(view_fmt or 'glb').lower() if (view_fmt or 'glb').lower() in ('glb', 'gltf') else 'glb',
        size=render_size,
        decompress=lambda b: _decompress_meshopt_glb_bytes(b),
    )
    return png, 'server_render'


def _octahedral_impostor_source_bytes(model, *, atlas_size=2048, grid_size=31):
    from app import render as render_mod
    if not render_mod.render_available():
        raise RuntimeError('Server render stack unavailable.')
    try:
        data, view_fmt = model.get_viewable_data()
    except Exception as e:
        raise RuntimeError('No viewable mesh available for octahedral impostor generation.') from e
    if not data or data[:4] != b'glTF':
        raise RuntimeError('No usable GLB bytes for octahedral impostor generation.')
    png, bake_metadata = render_mod.render_glb_to_octahedral_atlas(
        data,
        file_type=(view_fmt or 'glb').lower() if (view_fmt or 'glb').lower() in ('glb', 'gltf') else 'glb',
        atlas_size=atlas_size,
        grid_size=grid_size,
        oct_type='hemi',
        decompress=lambda b: _decompress_meshopt_glb_bytes(b),
    )
    return png, 'octahedral_server_render', bake_metadata


def _run_impostor_generator(model, owner_id=None, *, size=512):
    """Generate a far-field impostor texture under ModelVariant(kind=impostor)."""
    if os.environ.get('AUTO_IMPOSTOR_GENERATE', '1').lower() in {'0', 'false', 'no', 'off'}:
        return {
            'success': False,
            'skipped': True,
            'reason': 'disabled',
            'source_model_id': model.id,
        }
    if _is_rigged_or_avatar(model):
        return {
            'success': False,
            'skipped': True,
            'reason': 'rigged_or_avatar',
            'source_model_id': model.id,
        }

    impostor_type = 'octahedral_atlas'
    bake_metadata = {}
    try:
        image_bytes, source, bake_metadata = _octahedral_impostor_source_bytes(model)
        impostor_bytes, width, height = _encode_impostor_atlas_webp(image_bytes)
    except Exception as e:
        print(f"Octahedral impostor generation failed for {model.id}, falling back to billboard: {e}")
        impostor_type = 'billboard'
        image_bytes, source = _impostor_source_image_bytes(model, render_size=size)
        impostor_bytes, width, height = _encode_impostor_webp(image_bytes, size=size)
    fs = current_app.config['FILE_STORE']
    metadata = {
        'kind': 'impostor',
        'type': impostor_type,
        'source_model_id': model.id,
        'source': source,
        'width': width,
        'height': height,
        'format': 'webp',
        'role': 'far/octahedral' if impostor_type == 'octahedral_atlas' else 'far/billboard',
        **bake_metadata,
        'generated_at': datetime.utcnow().isoformat(),
    }
    file_id = fs.put(
        impostor_bytes,
        filename=f'{_safe_stem(model)}-{"octahedral-impostor" if impostor_type == "octahedral_atlas" else "impostor"}.webp',
        content_type='image/webp',
        metadata=metadata,
    )
    settings = {
        'defaults_version': LOD_OPTIMIZE_DEFAULTS_VERSION,
        'type': impostor_type,
        'source': source,
        'width': width,
        'height': height,
        'role': metadata['role'],
        'runtime_use': 'far_octahedral_impostor' if impostor_type == 'octahedral_atlas' else 'far_impostor',
        'file_format': 'webp',
        'source_model_id': model.id,
        **bake_metadata,
    }
    variant, old_file_id = ModelVariant.upsert(
        model.id, 'impostor', str(file_id),
        file_format='webp', size=len(impostor_bytes),
        settings=settings, status='ready',
    )
    if old_file_id and old_file_id != str(file_id):
        try:
            fs.delete(old_file_id)
        except Exception as e:
            print(f"Old impostor blob {old_file_id} not deleted: {e}")
    return {
        'success': True,
        'source_model_id': model.id,
        'type': impostor_type,
        'source': source,
        'size': len(impostor_bytes),
        'width': width,
        'height': height,
        **bake_metadata,
        'variant': variant.to_api() if variant else None,
    }


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
            lod_result = _run_lod_optimizer(model, job.owner_id or model.user_id)
            result['lod_result'] = lod_result
            try:
                result['impostor_result'] = _run_impostor_generator(model, job.owner_id or model.user_id)
            except Exception as e:
                result['impostor_result'] = {
                    'success': False,
                    'source_model_id': model.id,
                    'error': str(e)[:1000],
                }
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


GAME_OPTIMIZE_DEFAULTS = {
    'texture_limit': 1024,
    # 0.85 keeps more geometry than 0.75 and looked great in practice while
    # still being small with meshopt ("smallest") compression.
    'simplify_ratio': 0.85,
    'compression_mode': 'meshopt',
}
GAME_OPTIMIZE_DEFAULT_PRESET = 'balanced'
GAME_OPTIMIZE_DEFAULTS_VERSION = '2026-06-17'
GAME_OPTIMIZE_PRESETS = {
    'balanced': dict(GAME_OPTIMIZE_DEFAULTS),
    'preview': {
        'texture_limit': 1024,
        'simplify_ratio': 0.75,
        'compression_mode': 'meshopt',
    },
    'quality': {
        'texture_limit': 2048,
        'simplify_ratio': 0.95,
        'compression_mode': 'meshopt',
    },
    'compatibility': {
        'texture_limit': 1024,
        'simplify_ratio': 0.85,
        'compression_mode': 'fallback',
    },
}

LOD_OPTIMIZE_DEFAULTS_VERSION = '2026-07-07-lod2-texture-preserve'
LOD_OPTIMIZE_LEVELS = [
    {
        'level': 0,
        'texture_limit': 1024,
        'simplify_ratio': 0.85,
        'compression_mode': 'meshopt',
        'role': 'near/game',
    },
    {
        'level': 1,
        'texture_limit': 512,
        'simplify_ratio': 0.18,
        'simplification_error': 0.03,
        'aggressive': True,
        'permissive': True,
        'target_vertices': 20000,
        'compression_mode': 'meshopt',
        'role': 'mid/fill',
    },
    {
        'level': 2,
        # Keep LOD2 as the aggressively simplified far-fill mesh, but preserve
        # enough atlas detail for leaf/prop assets where the silhouette survives
        # and the texture becomes the visible failure mode.
        'texture_limit': 512,
        'simplify_ratio': 0.08,
        'simplification_error': 0.04,
        'aggressive': True,
        'permissive': True,
        'target_vertices': 10000,
        'compression_mode': 'meshopt',
        'role': 'far/large-fill',
    },
    {
        'level': 3,
        'texture_limit': 0,
        'simplify_ratio': 0.015,
        'simplification_error': 0.08,
        'aggressive': True,
        'permissive': True,
        'flat_material': True,
        'flat_material_color': [0.30, 0.42, 0.20, 1.0],
        'target_vertices': 500,
        'compression_mode': 'meshopt',
        'role': 'ultra-far/flat-silhouette',
    },
]


def _game_optimization_defaults_payload():
    return {
        'defaults_version': GAME_OPTIMIZE_DEFAULTS_VERSION,
        'default_preset': GAME_OPTIMIZE_DEFAULT_PRESET,
        'defaults': dict(GAME_OPTIMIZE_DEFAULTS),
        'presets': {key: dict(value) for key, value in GAME_OPTIMIZE_PRESETS.items()},
        'supported': {
            'texture_limits': [0, 1024, 2048, 4096],
            'compression_modes': ['meshopt', 'fallback'],
            'mesh_compression': 'EXT_meshopt_compression via gltfpack -cc when compression_mode=meshopt',
            'texture_compression': 'KTX2/Basis via gltfpack -tc when texture_limit is non-zero',
            'endpoint': '/api/model/{model_id}/game-optimized',
        },
    }


def _normalize_game_optimization_settings(data):
    data = data or {}
    preset = str(data.get('preset') or GAME_OPTIMIZE_DEFAULT_PRESET).strip().lower()
    if preset in ('default', ''):
        preset = GAME_OPTIMIZE_DEFAULT_PRESET
    if preset not in GAME_OPTIMIZE_PRESETS:
        allowed = ', '.join(sorted(GAME_OPTIMIZE_PRESETS))
        raise ValueError(f'preset must be one of: {allowed}.')

    base = dict(GAME_OPTIMIZE_PRESETS[preset])
    settings = {
        'preset': preset,
        'texture_limit': _optimize_game_int(data, 'texture_limit', base['texture_limit'], allowed=(0, 1024, 2048, 4096)),
        'simplify_ratio': _optimize_game_float(data, 'simplify_ratio', base['simplify_ratio']),
        'compression_mode': (data.get('compression_mode') or base['compression_mode']).strip().lower(),
        'defaults_version': GAME_OPTIMIZE_DEFAULTS_VERSION,
    }
    if settings['compression_mode'] not in ('meshopt', 'fallback'):
        raise ValueError('compression_mode must be meshopt or fallback.')
    if data.get('name'):
        settings['name'] = str(data.get('name')).strip()
    return settings


def _enqueue_game_optimization(model_id, owner_id, settings):
    """Create a queued optimization job and start its worker thread. Returns the
    job id. Shared by the explicit endpoint and the optimize-on-upload path."""
    job_id = str(uuid.uuid4())
    now = datetime.utcnow()
    with current_app.config['DB_ENGINE'].begin() as conn:
        conn.execute(optimization_jobs.insert().values(
            id=job_id,
            source_model_id=str(model_id),
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
    return job_id


def _is_rigged_or_avatar(model):
    """True when a GLB carries a rig/avatar that mesh-simplification would break.

    Game optimization runs `gltfpack -si` (aggressive mesh decimation) which
    destroys a skeleton. Such assets must take the rig-safe `_optimize_vrm_variant`
    path instead. Conservative on purpose: require a real VRM variant OR an
    explicit avatar/vrm tag, so a plain static prop is never denied its -si pass.
    """
    try:
        if ModelVariant.get(model.id, 'vrm'):
            return True
    except Exception:
        pass
    tags = {str(t or '').strip().lower() for t in (model.tags or [])}
    types = {str(t or '').strip().lower() for t in (model.asset_types or [])}
    return bool((tags | types) & {'avatar', 'vrm'})


def _route_avatar_optimization(model):
    """Run the rig-safe optimizer for an avatar/rigged GLB (best-effort)."""
    if not ModelVariant.get(model.id, 'vrm'):
        return  # a rigged GLB with no VRM variant: skip rather than risk the rig
    try:
        _optimize_vrm_variant(model)
    except Exception as e:
        print(f"Rig-safe VRM optimize skipped for {model.id}: {str(e)[:200]}")


def _maybe_autostart_game_optimization(model, force=False):
    """Auto-queue a game-optimized variant.

    On upload (force=False): only for GLB/GLTF, and skipped if a 'game' variant
    already exists, so every new mesh upload gets a small preview without manual
    action. After baking fixed eyes (force=True): re-run even if a 'game'
    variant exists -- the optimizer prefers the fixed-eyes GLB as its source, so
    the refreshed 'game' variant includes the eyes (and previews prefer 'game').
    Best-effort; failures are swallowed so they never block the response."""
    try:
        import shutil
        if os.environ.get('AUTO_GAME_OPTIMIZE', '1').lower() in ('0', 'false', 'no', 'off'):
            return
        # When forcing (fixed-eyes re-optimize) the source is the baked GLB, so
        # the original format doesn't matter; otherwise only mesh formats apply.
        if not force and (model.file_format or '').lower() not in ('glb', 'gltf'):
            return
        if not shutil.which('gltfpack'):
            return
        # Never run -si mesh decimation on a rigged/avatar GLB -- it would wreck
        # the skeleton. Route those to the rig-safe optimizer instead.
        if _is_rigged_or_avatar(model):
            _route_avatar_optimization(model)
            return
        # On upload, don't double-optimize an existing variant. When forcing we
        # WANT to replace it (e.g. eyeless -> with eyes); upsert handles the swap.
        if not force and ModelVariant.get(model.id, 'game'):
            return
        _enqueue_game_optimization(model.id, model.user_id, dict(GAME_OPTIMIZE_DEFAULTS))
    except Exception as e:
        print(f"Auto game-optimize enqueue skipped: {e}")


# --- Admin: backfill game-optimized variants for all GLB/GLTF models --------
# Single shared background job (one at a time across the whole app). Progress is
# kept in memory so a status endpoint / page can poll it.
_BACKFILL_LOCK = threading.Lock()
_backfill_state = {
    'running': False,
    'total': 0,
    'done': 0,
    'failed': 0,
    'skipped': 0,
    'current': None,
    'started_at': None,
    'finished_at': None,
    'last_error': None,
}
_CONVERSION_BACKFILL_LOCK = threading.Lock()
_conversion_backfill_state = {
    'running': False,
    'total': 0,
    'queued': 0,
    'skipped': 0,
    'failed': 0,
    'current': None,
    'started_at': None,
    'finished_at': None,
    'last_error': None,
}
_LOD_BACKFILL_LOCK = threading.Lock()
_lod_backfill_state = {
    'running': False,
    'total': 0,
    'done': 0,
    'failed': 0,
    'skipped': 0,
    'current': None,
    'started_at': None,
    'finished_at': None,
    'last_error': None,
}
_FBX_AVATAR_IMPORT_LOCK = threading.Lock()
_fbx_avatar_import_state = {
    'running': False,
    'total': 0,
    'imported': 0,
    'skipped': 0,
    'failed': 0,
    'current': None,
    'started_at': None,
    'finished_at': None,
    'last_error': None,
    'source': None,
    'pattern': None,
    'tag': 'robot',
    'limit': None,
}
_MEDIA_CAPTURE_LOCK = threading.Lock()
_media_capture_state = {
    'last_seen': None,
    'last_status': None,
    'last_error': None,
    'last_count': None,
    'last_captured': None,
    'last_kind': None,
}
_PIPELINE_RECONCILE_LOCK = threading.Lock()
_pipeline_reconcile_state = {
    'running': False,
    'last_seen': None,
    'last_status': None,
    'last_error': None,
    'last_result': None,
    'last_duration_seconds': None,
}


def _admin_token_ok():
    """Admin actions accept a dedicated ADMIN_TASK_TOKEN (preferred) or any
    configured service token, via Authorization: Bearer or a ?token= query
    param (so it can be triggered straight from a browser URL)."""
    admin_token = os.environ.get('ADMIN_TASK_TOKEN')
    valid = _configured_bearer_tokens()
    if admin_token:
        valid = [admin_token] + valid
    if not valid:
        return False
    provided = _bearer_token() or (request.args.get('token') or '').strip()
    if not provided:
        return False
    return any(hmac.compare_digest(provided, t) for t in valid)


def _admin_or_asset_admin_session_ok():
    return _admin_token_ok() or (
        current_user.is_authenticated and is_asset_admin_user(current_user)
    )


def _fbx_avatar_source_paths(source=None):
    configured = source or os.environ.get('FBX_AVATAR_IMPORT_SOURCE') or os.environ.get('FBX_AVATAR_IMPORT_SOURCES')
    if configured:
        parts = [part.strip() for part in re.split(r'[;\n]', configured) if part.strip()]
        return [Path(part) for part in parts]
    return [Path(r'Z:\3d\assets\legacy\3d\fbx')]


def _clean_fbx_avatar_title(path):
    stem = path.stem
    stem = re.sub(r'_?Meshy_AI_?', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'_?biped(?:\s*\(\d+\))?_?', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'_?Character_output$', ' ', stem, flags=re.IGNORECASE)
    stem = re.sub(r'_?texture_fbx(?:_\d+)?$', ' ', stem, flags=re.IGNORECASE)
    stem = stem.replace('_', ' ').replace('-', ' ')
    stem = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', stem)
    stem = re.sub(r'\s+', ' ', stem).strip() or 'Legacy Character'
    title = stem.title()
    if 'Avatar' not in title:
        title = f'{title} Avatar'
    return title[:80].strip()


def _scan_fbx_avatar_files(sources, pattern):
    paths = []
    missing = []
    seen = set()
    for source in sources:
        if not source.exists():
            missing.append(str(source))
            continue
        candidates = source.rglob(pattern) if source.is_dir() else [source]
        for path in candidates:
            if not path.is_file() or path.suffix.lower() != '.fbx':
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            paths.append(path)
    return paths, missing


def _fbx_avatar_import_duplicate(path):
    with current_app.config['DB_ENGINE'].begin() as conn:
        row = conn.execute(
            select(model_rows.c.id, model_rows.c.name)
            .where(model_rows.c.original_filename == path.name)
            .limit(1)
        ).mappings().first()
    return row


def _run_fbx_avatar_import(app, *, owner_id=None, source=None, pattern='*Character_output.fbx', tag='robot', limit=None):
    with app.app_context():
        try:
            sources = _fbx_avatar_source_paths(source)
            paths, missing = _scan_fbx_avatar_files(sources, pattern)
            if limit:
                paths = paths[:int(limit)]
            with _FBX_AVATAR_IMPORT_LOCK:
                _fbx_avatar_import_state['total'] = len(paths)
                if missing:
                    _fbx_avatar_import_state['last_error'] = f"Missing source: {', '.join(missing)[:220]}"

            allowed_extensions = app.config['ALLOWED_EXTENSIONS']
            max_bytes = app.config['MAX_FILE_BYTES']
            fs = app.config['FILE_STORE']
            base_tags = ['avatar', 'humanoid', 'rigged']
            if tag:
                base_tags.append(tag)

            for path in paths:
                with _FBX_AVATAR_IMPORT_LOCK:
                    _fbx_avatar_import_state['current'] = path.name
                try:
                    duplicate = _fbx_avatar_import_duplicate(path)
                    if duplicate:
                        with _FBX_AVATAR_IMPORT_LOCK:
                            _fbx_avatar_import_state['skipped'] += 1
                            _fbx_avatar_import_state['last_error'] = (
                                f"{path.name}: already imported as {duplicate.name or duplicate.id}"
                            )
                        continue
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    runtime_metadata = {
                        'rig': {'type': 'humanoid', 'source': 'mixamo-compatible-fbx'},
                        'upload': {
                            'source': 'fbx-avatar-import',
                            'content_hash': digest,
                            'original_path': str(path),
                        },
                    }
                    file = FileStorage(
                        stream=io.BytesIO(data),
                        filename=path.name,
                        content_type='application/octet-stream',
                    )
                    with app.test_request_context('/api/admin/fbx-avatar-import', method='POST'):
                        model, error = _store_one_upload(
                            file,
                            _clean_fbx_avatar_title(path),
                            (
                                'Rigged humanoid avatar FBX imported from the legacy character library. '
                                'The asset store generates viewable GLB and VRM variants during conversion.'
                            ),
                            True,
                            Model3D.normalize_tags(base_tags),
                            allowed_extensions,
                            fs,
                            max_bytes,
                            owner_id=owner_id,
                            asset_category=Model3D.normalize_category('person'),
                            asset_styles=Model3D.normalize_tags(['stylized']),
                            asset_types=Model3D.normalize_tags(['avatar', 'humanoid', 'rigged', 'fbx']),
                            runtime_metadata=runtime_metadata,
                        )
                    with _FBX_AVATAR_IMPORT_LOCK:
                        if model:
                            _fbx_avatar_import_state['imported'] += 1
                        elif error and 'duplicate' in error.lower():
                            _fbx_avatar_import_state['skipped'] += 1
                        else:
                            _fbx_avatar_import_state['failed'] += 1
                            _fbx_avatar_import_state['last_error'] = f"{path.name}: {error}"
                except Exception as e:
                    print(f"FBX avatar import failed for {path}: {e}", flush=True)
                    with _FBX_AVATAR_IMPORT_LOCK:
                        _fbx_avatar_import_state['failed'] += 1
                        _fbx_avatar_import_state['last_error'] = f"{path.name}: {str(e)[:200]}"
        except Exception as e:
            print(f"FBX avatar import runner crashed: {e}", flush=True)
            with _FBX_AVATAR_IMPORT_LOCK:
                _fbx_avatar_import_state['last_error'] = str(e)[:300]
        finally:
            with _FBX_AVATAR_IMPORT_LOCK:
                _fbx_avatar_import_state['running'] = False
                _fbx_avatar_import_state['current'] = None
                _fbx_avatar_import_state['finished_at'] = datetime.utcnow().isoformat()


@api_bp.route('/admin/fbx-avatar-import', methods=['POST'])
def admin_fbx_avatar_import():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    source = (request.args.get('source') or '').strip() or None
    pattern = (request.args.get('pattern') or '*Character_output.fbx').strip() or '*Character_output.fbx'
    tag = (request.args.get('tag') or 'robot').strip().lower()
    sync = request.args.get('sync', 'false').lower() in {'1', 'true', 'yes'}
    limit = request.args.get('limit', type=int)
    if limit is not None:
        limit = max(1, min(limit, 500))
    owner_id = current_user.id if current_user.is_authenticated else None
    sources = _fbx_avatar_source_paths(source)
    with _FBX_AVATAR_IMPORT_LOCK:
        if _fbx_avatar_import_state['running']:
            return jsonify({'status': 'already_running', **_fbx_avatar_import_state})
        _fbx_avatar_import_state.update({
            'running': True,
            'total': 0,
            'imported': 0,
            'skipped': 0,
            'failed': 0,
            'current': None,
            'started_at': datetime.utcnow().isoformat(),
            'finished_at': None,
            'last_error': None,
            'source': ';'.join(str(path) for path in sources),
            'pattern': pattern,
            'tag': tag,
            'limit': limit,
            'sync': sync,
        })
    if sync:
        _run_fbx_avatar_import(
            current_app._get_current_object(),
            owner_id=owner_id,
            source=source,
            pattern=pattern,
            tag=tag,
            limit=limit,
        )
        with _FBX_AVATAR_IMPORT_LOCK:
            return jsonify({'status': 'finished', **_fbx_avatar_import_state})
    thread = threading.Thread(
        target=_run_fbx_avatar_import,
        args=(current_app._get_current_object(),),
        kwargs={'owner_id': owner_id, 'source': source, 'pattern': pattern, 'tag': tag, 'limit': limit},
        name='fbx-avatar-import',
        daemon=True,
    )
    thread.start()
    return jsonify({'status': 'started', **_fbx_avatar_import_state})


@api_bp.route('/admin/fbx-avatar-import/status', methods=['GET'])
def admin_fbx_avatar_import_status():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    with _FBX_AVATAR_IMPORT_LOCK:
        return jsonify(dict(_fbx_avatar_import_state))


def _media_capture_queue_snapshot(limit=50, kind='all', include_not_ready=False, recapture=False,
                                  exclude_suppressed=False):
    limit = max(1, min(int(limit or 50), 200))
    kind = (kind or 'all').strip().lower()
    if kind not in {'all', 'models', 'animations'}:
        kind = 'all'
    items = []
    skipped_not_ready = 0

    if kind in {'all', 'models'}:
        missing = true() if recapture else or_(
            model_rows.c.thumbnail_file_id.is_(None),
            model_rows.c.preview_file_id.is_(None),
        )
        with current_app.config['DB_ENGINE'].begin() as conn:
            rows = conn.execute(
                select(model_rows)
                .where(
                    missing,
                    model_rows.c.file_format.not_in(['vrma', 'bvh']),
                    or_(
                        model_rows.c.vrma_file_id.is_(None),
                        model_rows.c.viewable_file_id.is_not(None),
                    ),
                )
                .order_by(model_rows.c.upload_date.desc())
                .limit(limit * 3)
            ).mappings().all()

        for row in rows:
            model = Model3D.from_doc(row)
            if model.is_animation_carrier():
                continue
            if exclude_suppressed and not recapture and _media_capture_suppressed(
                Model3D.normalize_media_capture(getattr(model, 'media_capture', None))
            ):
                continue
            item = _media_capture_queue_item(model, force_capture=recapture)
            if not item['capture_ready'] and not include_not_ready:
                skipped_not_ready += 1
                continue
            items.append(item)
            if len(items) >= limit:
                break

    if kind in {'all', 'animations'} and len(items) < limit:
        animation_items, animation_skipped = _animation_media_capture_queue_items(
            limit - len(items),
            include_not_ready=include_not_ready,
            recapture=recapture,
        )
        seen_ids = {item.get('id') for item in items}
        items.extend(item for item in animation_items if item.get('id') not in seen_ids)
        skipped_not_ready += animation_skipped

    return {
        'success': True,
        'kind': kind,
        'recapture': recapture,
        'count': len(items),
        'ready_count': len([item for item in items if item.get('capture_ready')]),
        'not_ready_count': len([item for item in items if not item.get('capture_ready')]),
        'skipped_not_ready': skipped_not_ready,
        'models': items,
    }


def _media_capture_worker_status():
    with _MEDIA_CAPTURE_LOCK:
        state = dict(_media_capture_state)
    last_seen = state.get('last_seen')
    active = False
    if last_seen:
        try:
            seen_at = datetime.fromisoformat(last_seen)
            active = (datetime.utcnow() - seen_at).total_seconds() <= 180
        except Exception:
            active = False
    state['active'] = active
    return state


@api_bp.route('/admin/media-capture/queue', methods=['GET'])
def admin_media_capture_queue():
    """List renderable models that still need thumbnail/video capture.

    The capture itself is browser-side WebGL work, so this endpoint feeds a
    trusted browser runner. It is intentionally token/admin gated because it can
    expose private model IDs and owner names.
    """
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401

    data = _media_capture_queue_snapshot(
        limit=request.args.get('limit', 50, type=int),
        kind=request.args.get('kind') or 'all',
        include_not_ready=request.args.get('include_not_ready', 'false').lower() == 'true',
        recapture=request.args.get('recapture', 'false').lower() in {'1', 'true', 'yes'},
        # The worker shouldn't re-attempt jobs that have failed out or are inside
        # their backoff window; the /status dashboard still shows them.
        exclude_suppressed=True,
    )
    return jsonify(data)


@api_bp.route('/admin/media-capture/status', methods=['GET'])
def admin_media_capture_status():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    queue = _media_capture_queue_snapshot(
        limit=request.args.get('limit', 50, type=int),
        kind=request.args.get('kind') or 'all',
        include_not_ready=request.args.get('include_not_ready', 'true').lower() == 'true',
        recapture=request.args.get('recapture', 'false').lower() in {'1', 'true', 'yes'},
    )
    return jsonify({
        **queue,
        'worker': _media_capture_worker_status(),
    })


@api_bp.route('/admin/media-capture/heartbeat', methods=['POST'])
def admin_media_capture_heartbeat():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    with _MEDIA_CAPTURE_LOCK:
        _media_capture_state.update({
            'last_seen': datetime.utcnow().isoformat(),
            'last_status': str(payload.get('status') or 'running')[:80],
            'last_error': (str(payload.get('error'))[:300] if payload.get('error') else None),
            'last_count': payload.get('count'),
            'last_captured': payload.get('captured'),
            'last_kind': str(payload.get('kind') or '')[:40] or None,
        })
        state = dict(_media_capture_state)
    state['active'] = True
    return jsonify({'success': True, 'worker': state})


@api_bp.route('/admin/media-capture/report', methods=['POST'])
def admin_media_capture_report():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    model_id = str(payload.get('model_id') or payload.get('id') or '').strip()
    if not model_id:
        return jsonify({'error': 'model_id is required'}), 400
    model = Model3D.get_by_id(model_id)
    if not model:
        return jsonify({'error': 'Model not found'}), 404
    status = str(payload.get('status') or '').strip().lower()
    if status not in {'processing', 'captured', 'failed', 'blocked'}:
        return jsonify({'error': 'status must be processing, captured, failed, or blocked'}), 400
    capture_url = payload.get('capture_url') or payload.get('url')
    state = _set_media_capture_state(
        model,
        status=status,
        kind=payload.get('kind') or payload.get('capture_mode') or 'models',
        error=payload.get('error'),
        capture_url=capture_url,
    )
    model.save()
    return jsonify({
        'success': True,
        'model_id': model.id,
        'media_capture': _media_capture_state_for_model(model),
        'processing_state': _model_processing_state(model),
    })


def _pipeline_reconciler_status():
    state = dict(_pipeline_reconcile_state)
    last_seen = state.get('last_seen')
    active = False
    if last_seen:
        try:
            seen_at = datetime.fromisoformat(last_seen)
            interval = int(os.environ.get('PIPELINE_RECONCILE_INTERVAL', '120'))
            active = (datetime.utcnow() - seen_at).total_seconds() <= max(180, interval * 2)
        except Exception:
            active = False
    state['active'] = active
    return state


def _queue_thumbnail_ready_enrichment(limit=25):
    if os.environ.get('AUTO_ENRICH_AFTER_THUMBNAIL', '1').lower() in {'0', 'false', 'no', 'off'}:
        return 0
    limit = max(1, min(int(limit or 25), 200))
    queued = 0
    with current_app.config['DB_ENGINE'].begin() as conn:
        rows = conn.execute(
            select(model_rows)
            .where(model_rows.c.thumbnail_file_id.is_not(None))
            .where(or_(
                model_rows.c.ai_status.is_(None),
                model_rows.c.ai_status == '',
                model_rows.c.ai_status == 'failed',
                model_rows.c.ai_status == 'done',
            ))
            .order_by(model_rows.c.upload_date.asc())
            .limit(limit * 3)
        ).mappings().all()
    for row in rows:
        if queued >= limit:
            break
        model = Model3D.from_doc(row)
        if _maybe_enqueue_autotag_after_thumbnail(model, context={'source': 'pipeline_reconciler'}):
            queued += 1
    if queued:
        _kick_ai_enrichment_worker(current_app._get_current_object())
    return queued


def _optimize_missing_game_variants(limit=5):
    if os.environ.get('AUTO_GAME_OPTIMIZE', '1').lower() in {'0', 'false', 'no', 'off'}:
        return {'optimized': 0, 'failed': 0, 'skipped': 0}
    import shutil
    if not shutil.which('gltfpack'):
        return {'optimized': 0, 'failed': 0, 'skipped': 0, 'error': 'gltfpack is not installed'}
    ids = Model3D.optimizable_ids()
    have = ModelVariant.model_ids_with_kind('game', ids)
    todo = [mid for mid in ids if mid not in have]
    limit = max(1, min(int(limit or 5), 50))
    result = {'optimized': 0, 'failed': 0, 'skipped': max(0, len(ids) - len(todo)), 'remaining': max(0, len(todo) - limit)}
    for mid in todo[:limit]:
        model = Model3D.get_by_id(mid)
        if not model:
            result['failed'] += 1
            continue
        # Rigged/avatar GLBs must not be -si decimated; route to the rig-safe
        # optimizer (when they have a VRM variant) and count as skipped here.
        if _is_rigged_or_avatar(model):
            _route_avatar_optimization(model)
            result['skipped'] += 1
            continue
        try:
            _run_game_optimizer(model, model.user_id, dict(GAME_OPTIMIZE_DEFAULTS))
            result['optimized'] += 1
        except Exception as e:
            print(f"Pipeline optimize failed for {mid}: {str(e)[:200]}", flush=True)
            result['failed'] += 1
            result['last_error'] = f"{model.name or mid}: {str(e)[:200]}"
    return result


def _lod_variant_current(model, config):
    variant = ModelVariant.get(model.id, 'lod', level=int(config['level']))
    if not variant or not variant.file_id:
        return False
    settings = variant.settings or {}
    return settings.get('defaults_version') == LOD_OPTIMIZE_DEFAULTS_VERSION


def _lod_variants_complete(model):
    return all(_lod_variant_current(model, config) for config in LOD_OPTIMIZE_LEVELS)


def _optimize_missing_lod_variants(limit=2, *, force=False):
    if os.environ.get('AUTO_LOD_OPTIMIZE', '1').lower() in {'0', 'false', 'no', 'off'}:
        return {'optimized': 0, 'failed': 0, 'skipped': 0}
    import shutil
    if not shutil.which('gltfpack'):
        return {'optimized': 0, 'failed': 0, 'skipped': 0, 'error': 'gltfpack is not installed'}
    ids = Model3D.optimizable_ids()
    limit = max(1, min(int(limit or 2), 25))
    result = {'optimized': 0, 'failed': 0, 'skipped': 0, 'remaining': 0}
    todo = []
    for mid in ids:
        model = Model3D.get_by_id(mid)
        if not model:
            result['failed'] += 1
            continue
        if _is_rigged_or_avatar(model):
            result['skipped'] += 1
            continue
        if not force and _lod_variants_complete(model):
            result['skipped'] += 1
            continue
        todo.append(model)
    result['remaining'] = max(0, len(todo) - limit)
    for model in todo[:limit]:
        try:
            _run_lod_optimizer(model, model.user_id)
            result['optimized'] += 1
        except Exception as e:
            print(f"Pipeline LOD optimize failed for {model.id}: {str(e)[:200]}", flush=True)
            result['failed'] += 1
            result['last_error'] = f"{model.name or model.id}: {str(e)[:200]}"
    return result


def _impostor_variant_complete(model):
    variant = ModelVariant.get(model.id, 'impostor')
    return bool(variant and variant.file_id)


def _generate_missing_impostor_variants(limit=5):
    if os.environ.get('AUTO_IMPOSTOR_GENERATE', '1').lower() in {'0', 'false', 'no', 'off'}:
        return {'generated': 0, 'failed': 0, 'skipped': 0, 'disabled': True}
    limit = max(1, min(int(limit or 5), 50))
    result = {'generated': 0, 'failed': 0, 'skipped': 0, 'remaining': 0}
    todo = []
    for mid in Model3D.optimizable_ids():
        model = Model3D.get_by_id(mid)
        if not model:
            result['failed'] += 1
            continue
        if _is_rigged_or_avatar(model):
            result['skipped'] += 1
            continue
        if _impostor_variant_complete(model):
            result['skipped'] += 1
            continue
        todo.append(model)
    result['remaining'] = max(0, len(todo) - limit)
    for model in todo[:limit]:
        try:
            _run_impostor_generator(model, model.user_id)
            result['generated'] += 1
        except Exception as e:
            print(f"Pipeline impostor generation failed for {model.id}: {str(e)[:200]}", flush=True)
            result['failed'] += 1
            result['last_error'] = f"{model.name or model.id}: {str(e)[:200]}"
    return result


def _requeue_missing_conversions(limit=25):
    if not current_app.config.get('ENABLE_CONVERSION', True):
        return 0
    from app.conversion import enqueue
    limit = max(1, min(int(limit or 25), 200))
    with current_app.config['DB_ENGINE'].begin() as conn:
        rows = conn.execute(
            select(model_rows)
            .where(model_rows.c.file_format.in_(['fbx', 'bvh']))
            .order_by(model_rows.c.upload_date.asc())
            .limit(limit * 3)
        ).mappings().all()
    queued = 0
    for row in rows:
        if queued >= limit:
            break
        model = Model3D.from_doc(row)
        if not _conversion_backfill_candidate(model, force=False):
            continue
        try:
            enqueue(model, enabled=True)
            queued += 1
        except Exception as e:
            print(f"Pipeline conversion enqueue failed for {model.id}: {e}", flush=True)
    return queued


def _sweep_stuck_media_capture(stuck_minutes=None, max_attempts=None, limit=200):
    """Recover media-capture jobs wedged in 'processing'.

    A worker that dies mid-capture (or a page that never paints despite the
    worker's own timeout) can leave media_capture.status='processing' forever.
    This sweep reclaims those: jobs that have exhausted their attempts are
    marked 'failed' (so the queue stops serving them); the rest are reset to
    'queued' with an exponential backoff window so they retry later without
    hot-looping. Runs inside the pipeline reconciler.
    """
    stuck_minutes = int(stuck_minutes if stuck_minutes is not None
                        else os.environ.get('MEDIA_CAPTURE_STUCK_MINUTES', '15'))
    max_attempts = int(max_attempts if max_attempts is not None
                       else os.environ.get('MEDIA_CAPTURE_MAX_ATTEMPTS', '5'))
    cutoff = datetime.utcnow() - timedelta(minutes=max(1, stuck_minutes))
    result = {'reset': 0, 'failed': 0}

    # Coarse text prefilter (spacing-agnostic across Postgres JSONB / SQLite
    # text); the authoritative status check happens per-row in Python below.
    status_text = func.lower(func.coalesce(cast(model_rows.c.media_capture, String), ''))
    with current_app.config['DB_ENGINE'].begin() as conn:
        rows = conn.execute(
            select(model_rows)
            .where(status_text.like('%processing%'))
            .limit(max(1, int(limit)))
        ).mappings().all()

    for row in rows:
        model = Model3D.from_doc(row)
        state = Model3D.normalize_media_capture(getattr(model, 'media_capture', None))
        if state.get('status') != 'processing':
            continue
        last_attempt = state.get('last_attempt_at')
        if last_attempt:
            try:
                if datetime.fromisoformat(last_attempt) > cutoff:
                    continue  # still within the grace window
            except (TypeError, ValueError):
                pass
        attempts = int(state.get('attempt_count') or 0)
        if attempts >= max_attempts:
            _set_media_capture_state(model, status='failed', error='exceeded max capture attempts')
            model.save()
            result['failed'] += 1
        else:
            # Reset to 'queued' WITHOUT bumping attempt_count (the bump already
            # happened when it entered 'processing'); add an exponential backoff.
            backoff_seconds = min(3600, 30 * (2 ** attempts))
            state['status'] = 'queued'
            state['backoff_until'] = (datetime.utcnow() + timedelta(seconds=backoff_seconds)).isoformat()
            model.media_capture = Model3D.normalize_media_capture(state)
            model.save()
            result['reset'] += 1
    return result


def _render_missing_thumbnails(limit=10):
    """Server-side render thumbnails for renderable models that still lack one.

    This is the reliable replacement for the headless-browser capture: no auth,
    no WebGL-in-Chromium, no MediaRecorder. Picks GLB/GLTF/VRM models with a
    viewable file, no thumbnail, and not already failed/backed-off, and renders
    each to a PNG via app.render. Best-effort and bounded per cycle."""
    from app import render as render_mod
    if os.environ.get('SERVER_RENDER_THUMBNAILS', '1').lower() in {'0', 'false', 'no', 'off'}:
        return {'rendered': 0, 'failed': 0, 'skipped': 0, 'disabled': True}
    if not render_mod.render_available():
        return {'rendered': 0, 'failed': 0, 'skipped': 0, 'unavailable': True}

    limit = max(1, min(int(limit or 10), 100))
    result = {'rendered': 0, 'failed': 0, 'skipped': 0}
    with current_app.config['DB_ENGINE'].begin() as conn:
        rows = conn.execute(
            select(model_rows)
            .where(
                model_rows.c.thumbnail_file_id.is_(None),
                model_rows.c.file_format.not_in(['vrma', 'bvh']),
                or_(
                    model_rows.c.viewable_file_id.is_not(None),
                    model_rows.c.file_format.in_(['glb', 'gltf', 'vrm']),
                ),
            )
            .order_by(model_rows.c.upload_date.desc())
            .limit(limit * 3)
        ).mappings().all()

    for row in rows:
        if result['rendered'] + result['failed'] >= limit:
            break
        model = Model3D.from_doc(row)
        if model.is_animation_carrier():
            result['skipped'] += 1
            continue
        state = Model3D.normalize_media_capture(getattr(model, 'media_capture', None))
        if _media_capture_suppressed(state):
            result['skipped'] += 1
            continue
        if _server_render_thumbnail(model):
            result['rendered'] += 1
        else:
            result['failed'] += 1
    return result


def _reconcile_asset_pipeline_once(app, *, optimize_limit=None, lod_limit=None, impostor_limit=None, enrich_limit=None, conversion_limit=None):
    with app.app_context():
        started = datetime.utcnow()
        if not _PIPELINE_RECONCILE_LOCK.acquire(blocking=False):
            return {'success': False, 'status': 'already_running'}
        try:
            _pipeline_reconcile_state.update({
                'running': True,
                'last_seen': started.isoformat(),
                'last_status': 'running',
                'last_error': None,
            })
            optimize = _optimize_missing_game_variants(
                optimize_limit or int(os.environ.get('PIPELINE_OPTIMIZE_LIMIT', '3'))
            )
            lod = _optimize_missing_lod_variants(
                lod_limit if lod_limit is not None else int(os.environ.get('PIPELINE_LOD_LIMIT', '2'))
            )
            impostors = _generate_missing_impostor_variants(
                impostor_limit if impostor_limit is not None else int(os.environ.get('PIPELINE_IMPOSTOR_LIMIT', '5'))
            )
            conversions = _requeue_missing_conversions(
                conversion_limit or int(os.environ.get('PIPELINE_CONVERSION_LIMIT', '20'))
            )
            enrichment = _queue_thumbnail_ready_enrichment(
                enrich_limit or int(os.environ.get('PIPELINE_ENRICH_LIMIT', '25'))
            )
            media_sweep = _sweep_stuck_media_capture()
            thumbnails = _render_missing_thumbnails(
                int(os.environ.get('PIPELINE_RENDER_LIMIT', '10'))
            )
            media = _media_capture_queue_snapshot(limit=50, kind='all', include_not_ready=True)
            result = {
                'success': True,
                'status': 'ok',
                'optimized': optimize,
                'lod_optimized': lod,
                'impostors': impostors,
                'conversion_queued': conversions,
                'enrichment_queued': enrichment,
                'thumbnail_render': thumbnails,
                'thumbnails_rendered': thumbnails.get('rendered', 0),
                'thumbnails_failed': thumbnails.get('failed', 0),
                'media_queue': {
                    'count': media.get('count', 0),
                    'ready_count': media.get('ready_count', 0),
                    'not_ready_count': media.get('not_ready_count', 0),
                },
                'media_stuck_reset': media_sweep.get('reset', 0),
                'media_failed': media_sweep.get('failed', 0),
            }
            duration = round((datetime.utcnow() - started).total_seconds(), 3)
            _pipeline_reconcile_state.update({
                'running': False,
                'last_seen': datetime.utcnow().isoformat(),
                'last_status': 'ok',
                'last_result': result,
                'last_duration_seconds': duration,
            })
            return result
        except Exception as e:
            msg = str(e)[:500]
            _pipeline_reconcile_state.update({
                'running': False,
                'last_seen': datetime.utcnow().isoformat(),
                'last_status': 'failed',
                'last_error': msg,
            })
            print(f"Pipeline reconciler failed: {msg}", flush=True)
            return {'success': False, 'status': 'failed', 'error': msg}
        finally:
            if _PIPELINE_RECONCILE_LOCK.locked():
                _PIPELINE_RECONCILE_LOCK.release()


class PipelineReconcilerWorker:
    def __init__(self, app, poll_interval=120):
        self.app = app
        self.poll_interval = max(15, int(poll_interval or 120))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name='asset-pipeline-reconciler', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                _reconcile_asset_pipeline_once(self.app)
            except Exception as e:
                print(f"Pipeline reconciler loop error: {e}", flush=True)
            self._stop.wait(self.poll_interval)


def start_pipeline_reconciler_worker(app):
    global PIPELINE_RECONCILER_WORKER
    if PIPELINE_RECONCILER_WORKER is None:
        interval = int(os.environ.get('PIPELINE_RECONCILE_INTERVAL', '120'))
        PIPELINE_RECONCILER_WORKER = PipelineReconcilerWorker(app, interval)
        PIPELINE_RECONCILER_WORKER.start()
    return PIPELINE_RECONCILER_WORKER


@api_bp.route('/admin/pipeline/reconcile', methods=['POST'])
def admin_pipeline_reconcile():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    sync = request.args.get('sync', 'true').lower() in {'1', 'true', 'yes'}
    if sync:
        result = _reconcile_asset_pipeline_once(
            current_app._get_current_object(),
            optimize_limit=request.args.get('optimize_limit', type=int),
            lod_limit=request.args.get('lod_limit', type=int),
            impostor_limit=request.args.get('impostor_limit', type=int),
            enrich_limit=request.args.get('enrich_limit', type=int),
            conversion_limit=request.args.get('conversion_limit', type=int),
        )
        return jsonify({**result, 'pipeline': _pipeline_reconciler_status(), 'media_worker': _media_capture_worker_status()})

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_reconcile_asset_pipeline_once,
        args=(app,),
        name='pipeline-reconcile-manual',
        daemon=True,
    )
    thread.start()
    return jsonify({'success': True, 'status': 'started', 'pipeline': _pipeline_reconciler_status()})


@api_bp.route('/admin/render-thumbnails', methods=['POST'])
def admin_render_thumbnails():
    """Server-side render thumbnails. With ?model_id=, render that one (handy for
    testing a single asset); otherwise backfill up to ?limit (default 25)
    renderable models that lack a thumbnail. Synchronous so the response carries
    the real outcome."""
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    from app import render as render_mod
    if not render_mod.render_available():
        return jsonify({'success': False, 'error': 'Server render stack unavailable (trimesh/pyrender/OSMesa not installed).'}), 503

    model_id = (request.args.get('model_id') or '').strip()
    if model_id:
        model = Model3D.get_by_id(model_id)
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        ok = _server_render_thumbnail(model, size=request.args.get('size', 1024, type=int))
        return jsonify({
            'success': ok,
            'model_id': model_id,
            'thumbnail_file_id': model.thumbnail_file_id,
            'media_capture': _media_capture_state_for_model(model),
        }), (200 if ok else 422)

    result = _render_missing_thumbnails(request.args.get('limit', 25, type=int))
    return jsonify({'success': True, **result})


@api_bp.route('/admin/pipeline/status', methods=['GET'])
def admin_pipeline_status():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    from app import render as render_mod
    render_enabled = os.environ.get('SERVER_RENDER_THUMBNAILS', '1').lower() not in {'0', 'false', 'no', 'off'}
    media = _media_capture_queue_snapshot(
        limit=request.args.get('limit', 50, type=int),
        kind=request.args.get('kind') or 'all',
        include_not_ready=True,
    )
    return jsonify({
        'success': True,
        'pipeline': _pipeline_reconciler_status(),
        'media_worker': _media_capture_worker_status(),
        'thumbnail_render': {
            'enabled': render_enabled,
            'available': bool(render_enabled and render_mod.render_available()),
        },
        'media_queue': {
            'count': media.get('count', 0),
            'ready_count': media.get('ready_count', 0),
            'not_ready_count': media.get('not_ready_count', 0),
            'skipped_not_ready': media.get('skipped_not_ready', 0),
        },
        'models': media.get('models', []),
    })


def _conversion_backfill_candidate(model, *, force=False):
    fmt = (model.file_format or '').lower()
    if fmt not in {'fbx', 'bvh'}:
        return False
    if force:
        return True
    if fmt == 'bvh':
        return not bool(model.vrma_file_id)
    if model.conversion_status in (None, '', 'pending', 'processing', 'failed'):
        return True
    runtime = model.runtime_metadata or {}
    upload = runtime.get('upload') if isinstance(runtime, dict) else {}
    tags = {str(tag or '').strip().lower() for tag in (model.tags or [])}
    asset_types = {str(tag or '').strip().lower() for tag in (model.asset_types or [])}
    animation_like = (
        bool(model.vrma_file_id)
        or model.asset_category == 'animation'
        or bool(tags & {'animation-source', 'animation-library', 'vrma-library'})
        or bool(asset_types & {'animation', 'avatar-animation'})
        or (isinstance(upload, dict) and upload.get('source') == 'vrma-library-import')
    )
    if animation_like and not model.vrma_file_id:
        return True
    if not model.viewable_file_id and not animation_like:
        return True
    return False


def _run_conversion_backfill(app, *, force=False, limit=None):
    with app.app_context():
        try:
            from app.conversion import enqueue
            with app.config['DB_ENGINE'].begin() as conn:
                query = (
                    select(model_rows)
                    .where(model_rows.c.file_format.in_(['fbx', 'bvh']))
                    .order_by(model_rows.c.upload_date.asc())
                )
                if limit:
                    query = query.limit(int(limit))
                rows = conn.execute(query).mappings().all()

            candidates = [Model3D.from_doc(row) for row in rows]
            models_to_queue = [
                model for model in candidates
                if _conversion_backfill_candidate(model, force=force)
            ]
            with _CONVERSION_BACKFILL_LOCK:
                _conversion_backfill_state['total'] = len(models_to_queue)
                _conversion_backfill_state['skipped'] = len(rows) - len(models_to_queue)

            for model in models_to_queue:
                with _CONVERSION_BACKFILL_LOCK:
                    _conversion_backfill_state['current'] = model.name or model.id
                try:
                    enqueue(model, enabled=True)
                    model.conversion_error = None
                    model.conversion_claimed_at = None
                    model.save()
                    with _CONVERSION_BACKFILL_LOCK:
                        _conversion_backfill_state['queued'] += 1
                except Exception as e:
                    print(f"Conversion backfill enqueue failed for {model.id}: {e}", flush=True)
                    with _CONVERSION_BACKFILL_LOCK:
                        _conversion_backfill_state['failed'] += 1
                        _conversion_backfill_state['last_error'] = f"{model.name or model.id}: {str(e)[:200]}"
        except Exception as e:
            print(f"Conversion backfill runner crashed: {e}", flush=True)
            with _CONVERSION_BACKFILL_LOCK:
                _conversion_backfill_state['last_error'] = str(e)[:300]
        finally:
            with _CONVERSION_BACKFILL_LOCK:
                _conversion_backfill_state['running'] = False
                _conversion_backfill_state['current'] = None
                _conversion_backfill_state['finished_at'] = datetime.utcnow().isoformat()


@api_bp.route('/admin/conversion-backfill', methods=['POST', 'GET'])
def admin_conversion_backfill():
    """Bulk requeue FBX/BVH conversion.

    Use force=true to reconvert already-done FBX rows so legacy viewable GLBs
    are replaced by self-contained embedded-texture GLBs and humanoid FBX rows
    are stamped as avatar/VRM assets.
    """
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', 'false').lower() in {'1', 'true', 'yes'}
    sync = request.args.get('sync', 'false').lower() in {'1', 'true', 'yes'}
    limit = request.args.get('limit', type=int)
    if limit is not None:
        limit = max(1, min(limit, 1000))
    with _CONVERSION_BACKFILL_LOCK:
        if _conversion_backfill_state['running']:
            return jsonify({'status': 'already_running', **_conversion_backfill_state})
        _conversion_backfill_state.update({
            'running': True,
            'total': 0,
            'queued': 0,
            'skipped': 0,
            'failed': 0,
            'current': None,
            'started_at': datetime.utcnow().isoformat(),
            'finished_at': None,
            'last_error': None,
            'force': force,
            'limit': limit,
            'sync': sync,
        })
    if sync:
        _run_conversion_backfill(current_app._get_current_object(), force=force, limit=limit)
        with _CONVERSION_BACKFILL_LOCK:
            return jsonify({'status': 'finished', **_conversion_backfill_state})
    thread = threading.Thread(
        target=_run_conversion_backfill,
        args=(current_app._get_current_object(),),
        kwargs={'force': force, 'limit': limit},
        name='conversion-backfill',
        daemon=True,
    )
    thread.start()
    return jsonify({'status': 'started', **_conversion_backfill_state})


@api_bp.route('/admin/conversion-backfill/status', methods=['GET'])
def admin_conversion_backfill_status():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    with _CONVERSION_BACKFILL_LOCK:
        return jsonify(dict(_conversion_backfill_state))


def _run_lod_backfill(app, *, limit=None, force=False):
    with app.app_context():
        try:
            import shutil
            if not shutil.which('gltfpack'):
                with _LOD_BACKFILL_LOCK:
                    _lod_backfill_state['running'] = False
                    _lod_backfill_state['last_error'] = 'gltfpack is not installed on the server.'
                    _lod_backfill_state['finished_at'] = datetime.utcnow().isoformat()
                return

            ids = Model3D.optimizable_ids()
            todo = []
            skipped = 0
            for mid in ids:
                model = Model3D.get_by_id(mid)
                if not model:
                    skipped += 1
                    continue
                if _is_rigged_or_avatar(model):
                    skipped += 1
                    continue
                if not force and _lod_variants_complete(model):
                    skipped += 1
                    continue
                todo.append(model)
            if limit:
                todo = todo[:max(1, int(limit))]
            with _LOD_BACKFILL_LOCK:
                _lod_backfill_state['total'] = len(todo)
                _lod_backfill_state['skipped'] = skipped
            for model in todo:
                with _LOD_BACKFILL_LOCK:
                    _lod_backfill_state['current'] = model.name or model.id
                try:
                    _run_lod_optimizer(model, model.user_id)
                    with _LOD_BACKFILL_LOCK:
                        _lod_backfill_state['done'] += 1
                except Exception as e:
                    print(f"LOD backfill failed for {model.id}: {e}", flush=True)
                    with _LOD_BACKFILL_LOCK:
                        _lod_backfill_state['failed'] += 1
                        _lod_backfill_state['last_error'] = f"{model.name or model.id}: {str(e)[:200]}"
        except Exception as e:
            print(f"LOD backfill runner crashed: {e}", flush=True)
            with _LOD_BACKFILL_LOCK:
                _lod_backfill_state['last_error'] = str(e)[:300]
        finally:
            with _LOD_BACKFILL_LOCK:
                _lod_backfill_state['running'] = False
                _lod_backfill_state['current'] = None
                _lod_backfill_state['finished_at'] = datetime.utcnow().isoformat()


@api_bp.route('/admin/lod-backfill', methods=['POST', 'GET'])
def admin_lod_backfill():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    if request.method == 'GET':
        with _LOD_BACKFILL_LOCK:
            return jsonify(dict(_lod_backfill_state))

    sync = request.args.get('sync', 'false').lower() in {'1', 'true', 'yes'}
    force = request.args.get('force', 'false').lower() in {'1', 'true', 'yes'}
    limit = request.args.get('limit', type=int)
    with _LOD_BACKFILL_LOCK:
        if _lod_backfill_state['running']:
            return jsonify({'status': 'already_running', **_lod_backfill_state})
        _lod_backfill_state.update({
            'running': True,
            'total': 0,
            'done': 0,
            'failed': 0,
            'skipped': 0,
            'current': None,
            'started_at': datetime.utcnow().isoformat(),
            'finished_at': None,
            'last_error': None,
            'force': force,
        })

    if sync:
        _run_lod_backfill(current_app._get_current_object(), limit=limit, force=force)
        with _LOD_BACKFILL_LOCK:
            return jsonify({'status': 'finished', **_lod_backfill_state})

    thread = threading.Thread(
        target=_run_lod_backfill,
        args=(current_app._get_current_object(),),
        kwargs={'limit': limit, 'force': force},
        name='lod-backfill',
        daemon=True,
    )
    thread.start()
    with _LOD_BACKFILL_LOCK:
        return jsonify({'status': 'started', **_lod_backfill_state})


@api_bp.route('/admin/lod-backfill/status', methods=['GET'])
def admin_lod_backfill_status():
    if not _admin_or_asset_admin_session_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    with _LOD_BACKFILL_LOCK:
        return jsonify(dict(_lod_backfill_state))


def _run_backfill_optimization(app):
    with app.app_context():
        try:
            import shutil
            if not shutil.which('gltfpack'):
                with _BACKFILL_LOCK:
                    _backfill_state['running'] = False
                    _backfill_state['last_error'] = 'gltfpack is not installed on the server.'
                    _backfill_state['finished_at'] = datetime.utcnow().isoformat()
                return

            ids = Model3D.optimizable_ids()
            have = ModelVariant.model_ids_with_kind('game', ids)
            todo = [mid for mid in ids if mid not in have]
            with _BACKFILL_LOCK:
                _backfill_state['total'] = len(todo)
                _backfill_state['skipped'] = len(ids) - len(todo)

            for mid in todo:
                model = Model3D.get_by_id(mid)
                if not model:
                    with _BACKFILL_LOCK:
                        _backfill_state['failed'] += 1
                    continue
                with _BACKFILL_LOCK:
                    _backfill_state['current'] = model.name or mid
                try:
                    _run_game_optimizer(model, model.user_id, dict(GAME_OPTIMIZE_DEFAULTS))
                    with _BACKFILL_LOCK:
                        _backfill_state['done'] += 1
                except Exception as e:
                    print(f"Backfill optimize failed for {mid}: {str(e)[:200]}", flush=True)
                    with _BACKFILL_LOCK:
                        _backfill_state['failed'] += 1
                        _backfill_state['last_error'] = f"{model.name or mid}: {str(e)[:200]}"
        except Exception as e:
            print(f"Backfill runner crashed: {e}", flush=True)
            with _BACKFILL_LOCK:
                _backfill_state['last_error'] = str(e)[:300]
        finally:
            with _BACKFILL_LOCK:
                _backfill_state['running'] = False
                _backfill_state['current'] = None
                _backfill_state['finished_at'] = datetime.utcnow().isoformat()


@api_bp.route('/admin/optimize-all', methods=['POST', 'GET'])
def admin_optimize_all():
    """Start the background backfill that game-optimizes every GLB/GLTF model
    without a variant. Token-gated (ADMIN_TASK_TOKEN or a service token).
    Idempotent: returns the current job if one is already running."""
    if not _admin_token_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    with _BACKFILL_LOCK:
        if _backfill_state['running']:
            return jsonify({'status': 'already_running', **_backfill_state})
        # reset + mark running before spawning the worker
        _backfill_state.update({
            'running': True, 'total': 0, 'done': 0, 'failed': 0, 'skipped': 0,
            'current': None, 'started_at': datetime.utcnow().isoformat(),
            'finished_at': None, 'last_error': None,
        })
    thread = threading.Thread(
        target=_run_backfill_optimization,
        args=(current_app._get_current_object(),),
        name='optimize-backfill', daemon=True,
    )
    thread.start()
    return jsonify({'status': 'started', **_backfill_state})


@api_bp.route('/admin/optimize-all/status', methods=['GET'])
def admin_optimize_all_status():
    if not _admin_token_ok():
        return jsonify({'error': 'Unauthorized'}), 401
    with _BACKFILL_LOCK:
        return jsonify(dict(_backfill_state))


@api_bp.route('/optimization/defaults', methods=['GET'])
def game_optimization_defaults():
    """Public optimizer contract for Tellus and admin UI preset selectors."""
    return jsonify({'success': True, **_game_optimization_defaults_payload()})


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
            settings = _normalize_game_optimization_settings(data)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        owner_id = principal.id if principal else model.user_id
        job_id = _enqueue_game_optimization(model.id, owner_id, settings)
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
        asset_category = Model3D.normalize_category(request.form.get('asset_category'))
        asset_styles = Model3D.normalize_tags(request.form.get('asset_styles', ''))
        asset_types = Model3D.normalize_tags(request.form.get('asset_types', ''))
        world_id = _tellus_world_id()
        tags, asset_types = _with_generation_defaults(tags, asset_types)
        runtime_metadata = Model3D.normalize_runtime_metadata(request.form.get('runtime_metadata'))

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
                allowed_extensions, fs, max_bytes, owner_id=owner_id,
                asset_category=asset_category, asset_styles=asset_styles,
                asset_types=asset_types, runtime_metadata=runtime_metadata,
                world_id=world_id,
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
            error_text = errors[0]['error'].lower()
            status = 409 if (
                'duplicate model' in error_text
                or 'duplicate generation' in error_text
                or 'pixal3d direct uploads are disabled' in error_text
            ) else 400
            return jsonify({'error': errors[0]['error']}), status

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
        missing_thumbnail = _thumbnail_required_error(model)
        if missing_thumbnail:
            return jsonify({'error': 'Thumbnail required', 'detail': missing_thumbnail}), 409

        if _as_bool(data.get('async', False)):
            durable_job = (model.ai_metadata or {}).get('_job') if isinstance(model.ai_metadata, dict) else None
            if model.ai_status not in ('pending', 'processing') or not durable_job:
                _enqueue_ai_enrichment(model, data)
                _kick_ai_enrichment_worker(current_app._get_current_object())
            return jsonify({
                'success': True,
                'status': 'queued',
                'model': _serialize_model(model),
            }), 202

        try:
            _run_ai_enrichment(model, data)
        except Exception as e:
            model.ai_status = 'failed'
            model.ai_error = str(e)[:500]
            model.save()
            print(f"API autotag provider error for model {model.id}: {model.ai_error}", flush=True)
            return jsonify({'error': 'AI enrichment failed', 'detail': model.ai_error}), 502

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
