from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_required, current_user
from app.models import ApiKey, Model3D, ModelVariant, User
from app.permissions import can_manage_model, is_asset_admin_user
from werkzeug.utils import secure_filename
import hashlib
import io
import json

main_bp = Blueprint('main', __name__)

# Rows per page for the dashboard table's infinite scroll.
DASHBOARD_PER_PAGE = 30

_GLB_MAGIC = b'glTF'
_GLB_JSON_CHUNK = 0x4E4F534A


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


def _enrich_dashboard_models(user_models):
    """Attach has_game_optimized + has_fixed_eyes to each model (two batched
    variant queries) so previews can pick the best source: the game-optimized
    file (which now bakes in any fixed eyes and is smallest) is preferred, then
    the fixed-eyes variant, then the original."""
    ids = [m.id for m in user_models]
    optimized_ids = ModelVariant.model_ids_with_kind('game', ids)
    fixed_ids = ModelVariant.model_ids_with_kind('fixed_eyes', ids)
    for model in user_models:
        model.has_game_optimized = model.id in optimized_ids
        model.has_fixed_eyes = model.id in fixed_ids


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
        recent_models, total_public = Model3D.get_public_models(page=1, per_page=6)


        # Add owner username + variant flags (landing cards prefer the
        # game-optimized variant, then fixed-eyes, for the live preview).
        # Batched variant queries.
        _recent_ids = [m.id for m in recent_models]
        optimized_ids = ModelVariant.model_ids_with_kind('game', _recent_ids)
        fixed_ids = ModelVariant.model_ids_with_kind('fixed_eyes', _recent_ids)
        for model in recent_models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'
            model.has_game_optimized = model.id in optimized_ids
            model.has_fixed_eyes = model.id in fixed_ids

        # Get statistics
        stats = Model3D.get_stats()
        
        return render_template('index.html', 
                             recent_models=recent_models,
                             total_models=stats['public_models'],
                             total_users=stats['total_users'],
                             total_downloads=stats['total_downloads'])
    except Exception as e:
        print(f"Index page error: {e}")
        import traceback
        traceback.print_exc()
        # Fallback values if database query fails
        return render_template('index.html', 
                             recent_models=[],
                             total_models=0,
                             total_users=0,
                             total_downloads=0)

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

        # Paginate the table (infinite scroll loads more); the headline stat
        # cards use account-wide aggregates so they don't depend on the page.
        per_page = DASHBOARD_PER_PAGE
        user_models, total_filtered = Model3D.get_user_models(
            current_user.id, page=page, per_page=per_page,
            sort=sort, tag=tags if tags else None,
            category=category, style=styles if styles else None,
            asset_type=asset_types if asset_types else None)
        _enrich_dashboard_models(user_models)

        stats = Model3D.get_user_stats(current_user.id)
        all_tags = Model3D.get_user_tags(current_user.id)
        facets = Model3D.get_user_facets(current_user.id)
        has_next = page * per_page < total_filtered

        return render_template('dashboard.html',
                             user_models=user_models,
                             total_models=stats['total_models'],
                             total_downloads=stats['total_downloads'],
                             public_models=stats['public_models'],
                             sort=sort, tags=tags, all_tags=all_tags,
                             category=category, styles=styles,
                             asset_types=asset_types, facets=facets,
                             page=page, has_next=has_next)
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
        per_page = DASHBOARD_PER_PAGE
        user_models, total_filtered = Model3D.get_user_models(
            current_user.id, page=page, per_page=per_page,
            sort=sort, tag=tags if tags else None,
            category=category, style=styles if styles else None,
            asset_type=asset_types if asset_types else None)
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

        # Get public models with pagination. Page size matches the /api/models
        # endpoint used by the browse page's infinite scroll so page 1 (rendered
        # server-side) and later pages (fetched via JSON) stay consistent.
        per_page = 24
        models, total = Model3D.get_public_models(
            page=page, per_page=per_page,
            search=search if search else None,
            sort=sort, tag=tags if tags else None,
            category=category, style=styles if styles else None,
            asset_type=asset_types if asset_types else None)

        # Add owner username + variant flags (browse prefers the game-optimized
        # file, then fixed-eyes, for the live preview). Batched variant queries.
        _ids = [m.id for m in models]
        optimized_ids = ModelVariant.model_ids_with_kind('game', _ids)
        fixed_ids = ModelVariant.model_ids_with_kind('fixed_eyes', _ids)
        for model in models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'
            model.has_game_optimized = model.id in optimized_ids
            model.has_fixed_eyes = model.id in fixed_ids

        pagination = Pagination(models, total, page, per_page)
        all_tags = Model3D.get_public_tags()
        facets = Model3D.get_public_facets()
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
        facets = {'categories': [], 'styles': [], 'types': []}
        has_vrm = False

    # Render outside the try so a template error surfaces instead of being
    # silently swallowed into a misleading "No Models Found" page.
    return render_template('browse.html', models=pagination, search=search,
                           sort=sort, tags=tags, all_tags=all_tags,
                           category=category, styles=styles,
                           asset_types=asset_types, facets=facets,
                           has_vrm=has_vrm,
                           asset_admin=is_asset_admin_user(current_user) if current_user.is_authenticated else False)


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
        # VRM variant (if any): a rigged GLB converted to a VRM avatar via
        # glb2vrm; lets the owner play VRMA clips on it.
        vrm_variant = ModelVariant.get(model.id, 'vrm')
        # Rigged variant (if any): owner-rigged GLB from the Rig Avatar editor.
        rigged_variant = ModelVariant.get(model.id, 'rigged')

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
                               fixed_eyes_variant=fixed_eyes_variant,
                               vrm_variant=vrm_variant,
                               rigged_variant=rigged_variant,
                               model_is_meshopt=model_is_meshopt,
                               can_manage_model=user_can_manage_model)
        
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
            return redirect(url_for('main.model_detail', model_id=model.id))
            
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
