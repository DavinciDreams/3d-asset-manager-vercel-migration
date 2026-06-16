from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_required, current_user
from app.models import ApiKey, Model3D, ModelVariant, User
from app.permissions import can_manage_model, is_asset_admin_user
from werkzeug.utils import secure_filename
import hashlib
import io
import json
import os

main_bp = Blueprint('main', __name__)

# Rows per page for the dashboard table's infinite scroll.
DASHBOARD_PER_PAGE = 30

_GLB_MAGIC = b'glTF'
_GLB_JSON_CHUNK = 0x4E4F534A


def _env_bool(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ('0', 'false', 'no', 'off')


def _glb_uses_compression(glb_bytes):
    """True if a GLB declares EXT_meshopt_compression or KHR_mesh_quantization.
    assimp can't read either, so the export menu hides assimp-only formats for
    such models. Parses just the JSON chunk; never raises."""
    import struct
    if not glb_bytes or glb_bytes[:4] != _GLB_MAGIC or len(glb_bytes) < 20:
        return False
    try:
        magic, version, declared = struct.unpack_from('<4sII', glb_bytes, 0)
        if magic != _GLB_MAGIC or version != 2 or declared > len(glb_bytes):
            return False
        offset = 12
        while offset + 8 <= declared:
            chunk_len, chunk_type = struct.unpack_from('<II', glb_bytes, offset)
            start = offset + 8
            end = start + chunk_len
            if end > declared:
                return False
            if chunk_type == _GLB_JSON_CHUNK:
                gltf = json.loads(glb_bytes[start:end].decode('utf-8').rstrip(' \t\r\n\0'))
                used = set(gltf.get('extensionsUsed') or [])
                return bool(used & {'EXT_meshopt_compression', 'KHR_mesh_quantization'})
            offset = end
    except Exception:
        pass
    return False


def _variant_uses_fixed_source(variant):
    return bool(variant and isinstance(variant.settings, dict) and variant.settings.get('source_is_fixed_eyes'))


def _attach_preview_variant_flags(models):
    game_by_id = ModelVariant.map_by_kind('game', [m.id for m in models])
    fixed_by_id = ModelVariant.map_by_kind('fixed_eyes', [m.id for m in models])
    for model in models:
        fixed = fixed_by_id.get(model.id)
        game = game_by_id.get(model.id)
        model.has_game_optimized = bool(game and (not fixed or _variant_uses_fixed_source(game)))
        model.has_fixed_eyes = bool(fixed)
        model.game_uses_fixed = _variant_uses_fixed_source(game)


def _enrich_dashboard_models(user_models):
    """Attach preview variant flags so stale optimized files do not hide a newer
    fixed-eyes/mouth bake."""
    _attach_preview_variant_flags(user_models)


class Pagination:
    """Minimal Flask-SQLAlchemy-style pagination object for templates."""

    def __init__(self, items, total, page, per_page):
        self.items = items
        self.total = total
        self.page = page
        self.per_page = per_page
        self.pages = (total + per_page - 1) // per_page if per_page else 0
        self.has_prev = page > 1
        self.has_next = page < self.pages
        self.prev_num = page - 1 if self.has_prev else None
        self.next_num = page + 1 if self.has_next else None

    def iter_pages(self, left_edge=2, left_current=2, right_current=3, right_edge=2):
        """Yield page numbers for pagination controls, with None for gaps."""
        last = 0
        for num in range(1, self.pages + 1):
            if (num <= left_edge
                    or (self.page - left_current - 1 < num < self.page + right_current)
                    or num > self.pages - right_edge):
                if last + 1 != num:
                    yield None
                yield num
                last = num

@main_bp.route('/')
def index():
    try:
        
        # Get recent public models
        recent_models, total_public = Model3D.get_public_models(
            page=1, per_page=6,
            exclude_animation_carriers=True)


        # Add owner username + current variant flags.
        _attach_preview_variant_flags(recent_models)
        for model in recent_models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'

        # Get statistics
        stats = Model3D.get_stats()
        
        return render_template('index.html', 
                             recent_models=recent_models,
                             total_models=stats['public_models'],
                             total_users=stats['total_users'],
                             total_downloads=stats['total_downloads'],
                             asset_admin=is_asset_admin_user(current_user) if current_user.is_authenticated else False)
    except Exception as e:
        print(f"Index page error: {e}")
        import traceback
        traceback.print_exc()
        # Fallback values if database query fails
        return render_template('index.html', 
                             recent_models=[],
                             total_models=0,
                             total_users=0,
                             total_downloads=0,
                             asset_admin=False)

@main_bp.route('/dashboard')
@login_required
def dashboard():
    """User dashboard"""
    try:
        
        sort = request.args.get('sort', 'newest')
        page = request.args.get('page', 1, type=int)
        # Support multiple ?tag= values (AND-matched).
        tags = Model3D.normalize_tags(request.args.getlist('tag'))
        category = Model3D.normalize_category(request.args.get('category'))
        styles = Model3D.normalize_tags(request.args.getlist('style'))
        asset_types = Model3D.normalize_tags(request.args.getlist('type'))
        asset_kinds = Model3D.normalize_tags(request.args.getlist('asset'))

        # Paginate the table (infinite scroll loads more); the headline stat
        # cards use account-wide aggregates so they don't depend on the page.
        per_page = DASHBOARD_PER_PAGE
        user_models, total_filtered = Model3D.get_user_models(
            current_user.id, page=page, per_page=per_page,
            sort=sort, tag=tags if tags else None,
            category=category, style=styles if styles else None,
            asset_type=asset_types if asset_types else None,
            asset_kind=asset_kinds if asset_kinds else None,
            exclude_formats=['vrma', 'bvh'],
            exclude_animation_carriers=True)
        _enrich_dashboard_models(user_models)

        stats = Model3D.get_user_stats(current_user.id)
        all_tags = Model3D.get_user_tags(
            current_user.id,
            exclude_formats=['vrma', 'bvh'],
            exclude_animation_carriers=True,
        )
        facets = Model3D.get_user_facets(
            current_user.id,
            exclude_formats=['vrma', 'bvh'],
            exclude_animation_carriers=True,
        )
        has_next = page * per_page < total_filtered

        return render_template('dashboard.html',
                             user_models=user_models,
                             total_models=stats['total_models'],
                             total_downloads=stats['total_downloads'],
                             public_models=stats['public_models'],
                             sort=sort, tags=tags, all_tags=all_tags,
                             category=category, styles=styles,
                             asset_types=asset_types, facets=facets,
                             page=page, has_next=has_next,
                             asset_admin=is_asset_admin_user(current_user))
    except Exception as e:
        print(f"Dashboard error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('dashboard.html',
                             user_models=[],
                             total_models=0,
                             total_downloads=0,
                             public_models=0,
                             sort='newest', tags=[], all_tags=[],
                             category=None, styles=[], asset_types=[],
                             facets={'categories': [], 'styles': [], 'types': []},
                             page=1, has_next=False,
                             asset_admin=is_asset_admin_user(current_user) if current_user.is_authenticated else False,
                             error=str(e))


@main_bp.route('/admin/optimize')
def admin_optimize_page():
    """Browser page to trigger + watch the game-optimize backfill. The page
    itself is not secret; the actions it calls are token-gated. The token is
    read from ?token= and passed through to the API calls."""
    token = request.args.get('token', '')
    return render_template('admin_optimize.html', token=token)


@main_bp.route('/dashboard/rows')
@login_required
def dashboard_rows():
    """HTML fragment of the next page of the user's model rows, for the
    dashboard's infinite scroll. Returns the rendered _dashboard_rows.html plus
    a tiny marker so the client knows whether more pages remain."""
    try:
        sort = request.args.get('sort', 'newest')
        page = request.args.get('page', 1, type=int)
        tags = Model3D.normalize_tags(request.args.getlist('tag'))
        category = Model3D.normalize_category(request.args.get('category'))
        styles = Model3D.normalize_tags(request.args.getlist('style'))
        asset_types = Model3D.normalize_tags(request.args.getlist('type'))
        asset_kinds = Model3D.normalize_tags(request.args.getlist('asset'))
        per_page = DASHBOARD_PER_PAGE
        user_models, total_filtered = Model3D.get_user_models(
            current_user.id, page=page, per_page=per_page,
            sort=sort, tag=tags if tags else None,
            category=category, style=styles if styles else None,
            asset_type=asset_types if asset_types else None,
            asset_kind=asset_kinds if asset_kinds else None,
            exclude_formats=['vrma', 'bvh'],
            exclude_animation_carriers=True)
        _enrich_dashboard_models(user_models)
        has_next = page * per_page < total_filtered
        rows_html = render_template('_dashboard_rows.html', user_models=user_models)
        # Sentinel comment lets the client read has_next without a JSON wrapper
        # (the fragment is injected straight into <tbody>).
        return rows_html + ('<!--has_next-->' if has_next else '<!--no_next-->')
    except Exception as e:
        print(f"Dashboard rows error: {e}")
        return '<!--no_next-->'


@main_bp.route('/browse')
def browse():
    """Browse public models"""
    try:
        
        search = request.args.get('search', '').strip()
        page = request.args.get('page', 1, type=int)
        sort = request.args.get('sort', 'newest')
        # Support multiple ?tag= values (AND-matched).
        tags = Model3D.normalize_tags(request.args.getlist('tag'))
        category = Model3D.normalize_category(request.args.get('category'))
        styles = Model3D.normalize_tags(request.args.getlist('style'))
        asset_types = Model3D.normalize_tags(request.args.getlist('type'))
        asset_kinds = Model3D.normalize_tags(request.args.getlist('asset'))

        # Get public models with pagination. Page size matches the /api/models
        # endpoint used by the browse page's infinite scroll so page 1 (rendered
        # server-side) and later pages (fetched via JSON) stay consistent.
        per_page = 24
        models, total = Model3D.get_public_models(
            page=page, per_page=per_page,
            search=search if search else None,
            sort=sort, tag=tags if tags else None,
            category=category, style=styles if styles else None,
            asset_type=asset_types if asset_types else None,
            asset_kind=asset_kinds if asset_kinds else None,
            exclude_formats=['vrma', 'bvh'],
            exclude_animation_carriers=True)

        # Add owner username + current variant flags.
        _attach_preview_variant_flags(models)
        for model in models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'

        pagination = Pagination(models, total, page, per_page)
        all_tags = Model3D.get_public_tags(exclude_animation_carriers=True)
        facets = Model3D.get_public_facets(exclude_animation_carriers=True)
        # Only load the (heavy) VRM viewer module if a VRM card is on the page.
        has_vrm = any(m.file_format == 'vrm' for m in models)

    except Exception as e:
        print(f"Browse error: {e}")
        import traceback
        traceback.print_exc()
        pagination = Pagination([], 0, 1, 24)
        search = ''
        sort = 'newest'
        tags = []
        all_tags = []
        category = None
        styles = []
        asset_types = []
        asset_kinds = []
        facets = {'categories': [], 'styles': [], 'types': []}
        has_vrm = False

    # Render outside the try so a template error surfaces instead of being
    # silently swallowed into a misleading "No Models Found" page.
    return render_template('browse.html', models=pagination, search=search,
                           sort=sort, tags=tags, all_tags=all_tags,
                           category=category, styles=styles,
                           asset_types=asset_types, asset_kinds=asset_kinds,
                           facets=facets,
                           has_vrm=has_vrm,
                           asset_admin=is_asset_admin_user(current_user) if current_user.is_authenticated else False)


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


def _animation_asset_item(model, *, generated=False, source_only=False):
    owner = User.get_by_id(model.user_id)
    playable = False
    if generated and model.vrma_file_id:
        view_url = url_for('api.export_model', model_id=model.id) + '?format=vrma'
        download_url = view_url
        source = 'Generated'
        playable = True
    elif source_only:
        view_url = None
        download_url = url_for('api.download_model', model_id=model.id)
        source = (model.file_format or 'source').upper()
    else:
        view_url = url_for('api.view_model', model_id=model.id)
        download_url = url_for('api.download_model', model_id=model.id)
        source = 'VRMA'
        playable = True
    return {
        'id': (model.id + ':vrma') if generated else model.id,
        'model_id': model.id,
        'name': _animation_clip_name(model),
        'description': model.description or '',
        'file_format': model.file_format,
        'conversion_status': model.conversion_status,
        'conversion_error': model.conversion_error,
        'source': source,
        'playable': playable,
        'can_manage': can_manage_model(current_user, model) if current_user.is_authenticated else False,
        'owner_username': owner.username if owner else 'Unknown',
        'download_count': model.download_count or 0,
        'upload_date': model.upload_date,
        'tags': model.tags or [],
        'asset_category': model.asset_category,
        'asset_styles': model.asset_styles or [],
        'asset_types': model.asset_types or [],
        'view_url': view_url,
        'download_url': download_url,
        'detail_url': url_for('main.model_detail', model_id=model.id),
        'thumbnail_url': url_for('api.get_thumbnail', model_id=model.id) if model.thumbnail_file_id else None,
        'preview_url': url_for('api.get_preview', model_id=model.id) if model.preview_file_id else None,
    }


def _sort_animation_items(items, sort):
    if sort == 'name':
        return sorted(items, key=lambda item: (item['name'] or '').lower())
    if sort == 'oldest':
        return sorted(items, key=lambda item: item['upload_date'] or 0)
    if sort == 'downloads':
        return sorted(items, key=lambda item: item['download_count'], reverse=True)
    return sorted(items, key=lambda item: item['upload_date'] or 0, reverse=True)


def _preview_avatar_url(avatars):
    seen = set()
    for avatar in avatars:
        if not avatar or avatar.id in seen:
            continue
        seen.add(avatar.id)
        if (avatar.file_format or '').lower() == 'vrm':
            return url_for('api.view_model', model_id=avatar.id)
        variant = ModelVariant.get(avatar.id, 'vrm_optimized') or ModelVariant.get(avatar.id, 'vrm')
        if variant and variant.file_id:
            if variant.kind == 'vrm_optimized':
                return url_for('api.get_optimized_vrm', model_id=avatar.id)
            return url_for('api.get_vrm_variant', model_id=avatar.id)
    return None


@main_bp.route('/animations')
def animations():
    """Browse VRMA animation clips separately from model assets."""
    try:
        search = request.args.get('search', '').strip().lower()
        sort = request.args.get('sort', 'newest')
        user_id = current_user.id if current_user.is_authenticated else None
        items = [
            _animation_asset_item(model)
            for model in Model3D.list_vrma_for_user(user_id)
        ]
        items.extend(
            _animation_asset_item(model, generated=True)
            for model in Model3D.list_generated_vrma_for_user(user_id)
            if (model.file_format or '').lower() != 'vrma'
        )
        generated_ids = {item['model_id'] for item in items if item['id'].endswith(':vrma')}
        items.extend(
            _animation_asset_item(model, source_only=True)
            for model in Model3D.list_animation_sources_for_user(user_id)
            if model.id not in generated_ids
        )
        if search:
            items = [
                item for item in items
                if search in (item['name'] or '').lower()
                or search in (item['owner_username'] or '').lower()
                or any(search in str(tag or '').lower() for tag in item['tags'])
            ]
        items = _sort_animation_items(items, sort)
        avatars = Model3D.list_vrm_for_user(user_id) + Model3D.list_with_vrm_variant_for_user(user_id)
        capture_clip_id = (request.args.get('capture_clip') or '').strip()
        return render_template(
            'animations.html',
            animations=items,
            search=search,
            sort=sort,
            avatar_count=len({avatar.id for avatar in avatars}),
            preview_avatar_url=_preview_avatar_url(avatars),
            capture_clip_id=capture_clip_id,
        )
    except Exception as e:
        print(f"Animations page error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('animations.html', animations=[], search='', sort='newest',
                               avatar_count=0, preview_avatar_url=None,
                               capture_clip_id='', error=str(e))


@main_bp.route('/local-assets')
def local_assets():
    """Browser-local asset manager backed by IndexedDB."""
    return render_template('local_assets.html')


@main_bp.route('/model/<model_id>')
def model_detail(model_id):
    """View model details"""
    try:
        
        model = Model3D.get_by_id(model_id)
        if not model:
            flash('Model not found.', 'error')
            return redirect(url_for('main.browse'))
        
        user_can_manage_model = can_manage_model(current_user, model) if current_user.is_authenticated else False
        
        # Check access permissions
        if not model.is_public:
            if not user_can_manage_model:
                flash('You do not have permission to view this model.', 'error')
                return redirect(url_for('main.browse'))
        
        # Get model owner info
        owner = User.get_by_id(model.user_id)

        # Tag suggestions for the owner's edit form (autocomplete).
        all_tags = Model3D.get_user_tags(model.user_id) if user_can_manage_model else []

        # Game-optimized variant (if any) so the detail page can show the
        # Original/Game-Optimized toggle + download on a fresh load.
        game_variant = ModelVariant.get(model.id, 'game')
        # Fixed-eyes variant (if any): owner-baked GLB with blinker eyeballs
        # covering reconstruction holes; surfaces its own toggle + download.
        fixed_eyes_variant = ModelVariant.get(model.id, 'fixed_eyes')
        game_variant_uses_fixed = _variant_uses_fixed_source(game_variant)
        game_variant_current = bool(game_variant and (not fixed_eyes_variant or game_variant_uses_fixed))
        # VRM variant (if any): a rigged GLB converted to a VRM avatar via
        # glb2vrm; lets the owner play VRMA clips on it.
        vrm_variant = ModelVariant.get(model.id, 'vrm')
        # Rigged variant (if any): owner-rigged GLB from the Rig Avatar editor.
        rigged_variant = ModelVariant.get(model.id, 'rigged')
        model_tags = {str(tag or '').strip().lower() for tag in (model.tags or [])}
        model_types = {str(tag or '').strip().lower() for tag in (model.asset_types or [])}
        is_avatar_asset = (
            (model.file_format or '').lower() == 'vrm'
            or bool(vrm_variant and vrm_variant.file_id)
            or bool((model_tags | model_types) & {'avatar', 'vrm'})
        )
        is_animation_clip_asset = bool(model.vrma_file_id) and not is_avatar_asset
        detail_animation_preview_url = None
        if is_animation_clip_asset:
            user_id = current_user.id if current_user.is_authenticated else None
            avatars = Model3D.list_vrm_for_user(user_id) + Model3D.list_with_vrm_variant_for_user(user_id)
            detail_animation_preview_url = _preview_avatar_url(avatars)

        # Is the viewable GLB meshopt-compressed / quantized? assimp can't read
        # those, so the export menu hides the assimp-only formats (obj/fbx/...)
        # for them (avoids the 502 they'd otherwise produce). Only worth checking
        # for GLB/GLTF models; a missing/odd file just leaves the flag False.
        model_is_meshopt = False
        if (model.file_format or '').lower() in ('glb', 'gltf'):
            try:
                data, _ = model.get_viewable_data()
                model_is_meshopt = _glb_uses_compression(data)
            except Exception:
                model_is_meshopt = False

        return render_template('model_detail.html', model=model, owner=owner,
                               all_tags=all_tags, game_variant=game_variant,
                               game_variant_current=game_variant_current,
                               game_variant_uses_fixed=game_variant_uses_fixed,
                               fixed_eyes_variant=fixed_eyes_variant,
                               vrm_variant=vrm_variant,
                               rigged_variant=rigged_variant,
                               detail_animation_preview_url=detail_animation_preview_url,
                               model_is_meshopt=model_is_meshopt,
                               can_manage_model=user_can_manage_model,
                               client_capture_enabled=_env_bool('CLIENT_MEDIA_CAPTURE_ENABLED', True),
                               auto_capture_thumbnail=_env_bool('AUTO_CAPTURE_THUMBNAILS', True),
                               auto_capture_preview=_env_bool('AUTO_CAPTURE_PREVIEW_VIDEOS', True))
        
    except Exception as e:
        print(f"Model detail error: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading model details.', 'error')
        return redirect(url_for('main.browse'))

@main_bp.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    """Upload 3D model"""
    # Per-file size limit (MB) shown in the UI and enforced by the API.
    max_upload_mb = current_app.config['MAX_FILE_BYTES'] // (1024 * 1024)
    if request.method == 'POST':
        try:
            # Get form data
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            is_public = request.form.get('is_public') == 'on'
            tags = Model3D.normalize_tags(request.form.get('tags', ''))
            asset_category = Model3D.normalize_category(request.form.get('asset_category'))
            asset_styles = Model3D.normalize_tags(request.form.get('asset_styles', ''))
            asset_types = Model3D.normalize_tags(request.form.get('asset_types', ''))
            runtime_metadata = Model3D.normalize_runtime_metadata(request.form.get('runtime_metadata'))

            # Get uploaded file
            file = request.files.get('file')
            
            if not file or file.filename == '':
                flash('Please select a file to upload.', 'error')
                return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)
            
            if not name:
                flash('Please provide a name for your model.', 'error')
                return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)
            
            # Validate file extension
            filename = secure_filename(file.filename)
            file_extension = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            
            allowed_extensions = current_app.config['ALLOWED_EXTENSIONS']
            if file_extension not in allowed_extensions:
                flash(f'File type not supported. Allowed: {", ".join(allowed_extensions)}', 'error')
                return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)
            
            # Read file content
            file_content = file.read()
            file_size = len(file_content)
            if file_size == 0:
                flash('File is empty.', 'error')
                return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)
            
            # Check file size (100MB limit)
            if file_size > current_app.config['MAX_FILE_BYTES']:
                flash(f'File too large. Maximum size is {max_upload_mb}MB.', 'error')
                return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)
            content_hash = hashlib.sha256(file_content).hexdigest()
            duplicate = Model3D.get_by_content_hash(content_hash)
            if duplicate:
                if duplicate.is_public or duplicate.user_id == current_user.id:
                    flash(f'Duplicate model already exists: {duplicate.name}.', 'error')
                    return redirect(url_for('main.model_detail', model_id=duplicate.id))
                flash('Duplicate model already exists in the asset library.', 'error')
                return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)

            from app.api import _file_derived_metadata, _merge_runtime_metadata, _merge_tags
            derived_asset_types, derived_runtime_metadata = _file_derived_metadata(file_content, file_extension)
            asset_types = _merge_tags(asset_types, derived_asset_types)
            runtime_metadata = _merge_runtime_metadata(runtime_metadata, derived_runtime_metadata)
            
            # Store file in the configured database-backed file store.
            fs = current_app.config['FILE_STORE']
            gridfs_file_id = fs.put(
                file_content,
                filename=filename,
                content_type=file.content_type,
                metadata={
                    'original_filename': file.filename,
                    'uploaded_by': current_user.id,
                    'upload_date': Model3D().upload_date,
                    'content_hash': content_hash,
                }
            )
            
            # Create model record
            model = Model3D(
                name=name,
                description=description,
                file_format=file_extension,
                file_size=file_size,
                content_hash=content_hash,
                original_filename=file.filename,
                user_id=current_user.id,
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
            # Auto-generate a game-optimized variant for GLB/GLTF uploads.
            from app.api import _maybe_autostart_game_optimization, _maybe_autotag_on_upload
            _maybe_autostart_game_optimization(model)
            _maybe_autotag_on_upload(model, context={'source': 'web_upload'})

            flash(f'Model "{model.name}" uploaded successfully!', 'success')
            return redirect(url_for('main.model_detail', model_id=model.id, capture=1))
            
        except Exception as e:
            print(f"Upload error: {e}")
            flash('Upload failed. Please try again.', 'error')
            return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)
    
    return render_template('upload.html', all_tags=Model3D.get_user_tags(current_user.id), max_upload_mb=max_upload_mb)

@main_bp.route('/profile')
@login_required
def profile():
    """User profile page"""
    try:
        user_models, _ = Model3D.get_user_models(current_user.id, page=1, per_page=6)
        stats = Model3D.get_user_stats(current_user.id)
        api_keys = ApiKey.list_for_user(current_user.id)
        new_api_key = session.pop('new_api_key', None)
        
        return render_template('profile.html', user=current_user, stats=stats,
                               user_models=user_models, api_keys=api_keys,
                               new_api_key=new_api_key)
        
    except Exception as e:
        print(f"Profile error: {e}")
        return render_template('profile.html', user=current_user, stats={
            'total_models': 0,
            'public_models': 0,
            'total_downloads': 0
        }, user_models=[], api_keys=[], new_api_key=None)


@main_bp.route('/profile/api-keys', methods=['POST'])
@login_required
def create_api_key():
    """Create an upload API key for the current user."""
    name = request.form.get('name', '').strip() or 'Upload API key'
    try:
        api_key, token = ApiKey.create_for_user(current_user.id, name=name, scopes=['upload'])
        session['new_api_key'] = {
            'id': api_key.id,
            'name': api_key.name,
            'prefix': api_key.key_prefix,
            'token': token,
        }
        flash('API key created. Copy it from the API Keys panel.', 'success')
    except Exception as e:
        print(f"API key creation error: {e}")
        flash('Could not create API key.', 'error')
    return redirect(url_for('main.profile'))


@main_bp.route('/profile/api-keys/<key_id>/revoke', methods=['POST'])
@login_required
def revoke_api_key(key_id):
    """Revoke one of the current user's API keys."""
    if ApiKey.revoke_for_user(key_id, current_user.id):
        flash('API key revoked.', 'success')
    else:
        flash('API key not found.', 'error')
    return redirect(url_for('main.profile'))
