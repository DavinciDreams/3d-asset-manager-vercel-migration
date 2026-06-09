from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, session
from flask_login import login_required, current_user
from app.models import ApiKey, Model3D, ModelVariant, User
from werkzeug.utils import secure_filename
import io

main_bp = Blueprint('main', __name__)


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
        
        
        # Add owner username to each model
        for model in recent_models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'

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
        # Support multiple ?tag= values (AND-matched).
        tags = Model3D.normalize_tags(request.args.getlist('tag'))

        # Show all of the user's models on the dashboard (not just the first
        # page), so the count card and the table agree.
        user_models, total_user_models = Model3D.get_user_models(
            current_user.id, page=1, per_page=1000,
            sort=sort, tag=tags if tags else None)

        # Calculate user stats from the full set
        total_downloads = sum(model.download_count for model in user_models)
        public_models = sum(1 for model in user_models if model.is_public)
        all_tags = Model3D.get_user_tags(current_user.id)

        return render_template('dashboard.html',
                             user_models=user_models,
                             total_models=total_user_models,
                             total_downloads=total_downloads,
                             public_models=public_models,
                             sort=sort, tags=tags, all_tags=all_tags)
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
                             error=str(e))

@main_bp.route('/browse')
def browse():
    """Browse public models"""
    try:
        
        search = request.args.get('search', '').strip()
        page = request.args.get('page', 1, type=int)
        sort = request.args.get('sort', 'newest')
        # Support multiple ?tag= values (AND-matched).
        tags = Model3D.normalize_tags(request.args.getlist('tag'))

        # Get public models with pagination. Page size matches the /api/models
        # endpoint used by the browse page's infinite scroll so page 1 (rendered
        # server-side) and later pages (fetched via JSON) stay consistent.
        per_page = 24
        models, total = Model3D.get_public_models(
            page=page, per_page=per_page,
            search=search if search else None,
            sort=sort, tag=tags if tags else None)

        # Add owner username + whether a game-optimized variant exists (browse
        # prefers that smaller file for the live preview) to each model.
        for model in models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'
            variant = ModelVariant.get(model.id, 'game')
            model.has_game_optimized = bool(variant and variant.file_id)

        pagination = Pagination(models, total, page, per_page)
        all_tags = Model3D.get_public_tags()
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
        has_vrm = False

    # Render outside the try so a template error surfaces instead of being
    # silently swallowed into a misleading "No Models Found" page.
    return render_template('browse.html', models=pagination, search=search,
                           sort=sort, tags=tags, all_tags=all_tags, has_vrm=has_vrm)


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
        
        
        # Check access permissions
        if not model.is_public:
            if not current_user.is_authenticated or model.user_id != current_user.id:
                flash('You do not have permission to view this model.', 'error')
                return redirect(url_for('main.browse'))
        
        # Get model owner info
        owner = User.get_by_id(model.user_id)

        # Tag suggestions for the owner's edit form (autocomplete).
        all_tags = Model3D.get_user_tags(model.user_id) if (
            current_user.is_authenticated and current_user.id == model.user_id) else []

        # Game-optimized variant (if any) so the detail page can show the
        # Original/Game-Optimized toggle + download on a fresh load.
        game_variant = ModelVariant.get(model.id, 'game')

        return render_template('model_detail.html', model=model, owner=owner,
                               all_tags=all_tags, game_variant=game_variant)
        
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
            
            # Check file size (100MB limit)
            if file_size > current_app.config['MAX_FILE_BYTES']:
                flash(f'File too large. Maximum size is {max_upload_mb}MB.', 'error')
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
                    'upload_date': Model3D().upload_date
                }
            )
            
            # Create model record
            model = Model3D(
                name=name,
                description=description,
                file_format=file_extension,
                file_size=file_size,
                original_filename=file.filename,
                user_id=current_user.id,
                is_public=is_public,
                gridfs_file_id=str(gridfs_file_id),
                tags=tags
            )

            from app.conversion import enqueue
            enqueue(model, enabled=current_app.config.get('ENABLE_CONVERSION', True))
            model.save()
            # Auto-generate a game-optimized variant for GLB/GLTF uploads.
            from app.api import _maybe_autostart_game_optimization
            _maybe_autostart_game_optimization(model)

            flash(f'Model "{name}" uploaded successfully!', 'success')
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
