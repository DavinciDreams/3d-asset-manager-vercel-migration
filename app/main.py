from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from app.models import Model3D, User
from werkzeug.utils import secure_filename
import io
from bson.objectid import ObjectId

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
        
        # Show all of the user's models on the dashboard (not just the first
        # page), so the count card and the table agree.
        user_models, total_user_models = Model3D.get_user_models(current_user.id, page=1, per_page=1000)

        # Calculate user stats from the full set
        total_downloads = sum(model.download_count for model in user_models)
        public_models = sum(1 for model in user_models if model.is_public)
        
        return render_template('dashboard.html', 
                             user_models=user_models,
                             total_models=total_user_models,
                             total_downloads=total_downloads,
                             public_models=public_models)
    except Exception as e:
        print(f"Dashboard error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('dashboard.html', 
                             user_models=[],
                             total_models=0,
                             total_downloads=0,
                             public_models=0,
                             error=str(e))

@main_bp.route('/browse')
def browse():
    """Browse public models"""
    try:
        
        search = request.args.get('search', '').strip()
        page = request.args.get('page', 1, type=int)
        
        
        # Get public models with pagination
        models, total = Model3D.get_public_models(page=page, per_page=12, search=search if search else None)
        
        
        # Add owner username to each model
        for model in models:
            user = User.get_by_id(model.user_id)
            model.owner_username = user.username if user else 'Unknown'

        pagination = Pagination(models, total, page, 12)

    except Exception as e:
        print(f"Browse error: {e}")
        import traceback
        traceback.print_exc()
        pagination = Pagination([], 0, 1, 12)
        search = ''

    # Render outside the try so a template error surfaces instead of being
    # silently swallowed into a misleading "No Models Found" page.
    return render_template('browse.html', models=pagination, search=search)


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
        
        return render_template('model_detail.html', model=model, owner=owner)
        
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
    if request.method == 'POST':
        try:
            # Get form data
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            is_public = request.form.get('is_public') == 'on'
            
            # Get uploaded file
            file = request.files.get('file')
            
            if not file or file.filename == '':
                flash('Please select a file to upload.', 'error')
                return render_template('upload.html')
            
            if not name:
                flash('Please provide a name for your model.', 'error')
                return render_template('upload.html')
            
            # Validate file extension
            filename = secure_filename(file.filename)
            file_extension = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
            
            allowed_extensions = current_app.config['ALLOWED_EXTENSIONS']
            if file_extension not in allowed_extensions:
                flash(f'File type not supported. Allowed: {", ".join(allowed_extensions)}', 'error')
                return render_template('upload.html')
            
            # Read file content
            file_content = file.read()
            file_size = len(file_content)
            
            # Check file size (100MB limit)
            if file_size > current_app.config['MAX_CONTENT_LENGTH']:
                flash('File too large. Maximum size is 100MB.', 'error')
                return render_template('upload.html')
            
            # Store file in GridFS
            fs = current_app.config['GRIDFS']
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
                gridfs_file_id=str(gridfs_file_id)
            )
            
            model.save()
            
            flash(f'Model "{name}" uploaded successfully!', 'success')
            return redirect(url_for('main.model_detail', model_id=model.id))
            
        except Exception as e:
            print(f"Upload error: {e}")
            flash('Upload failed. Please try again.', 'error')
            return render_template('upload.html')
    
    return render_template('upload.html')

@main_bp.route('/profile')
@login_required
def profile():
    """User profile page"""
    try:
        user_models, total = Model3D.get_user_models(current_user.id)
        stats = {
            'total_models': total,
            'public_models': sum(1 for model in user_models if model.is_public),
            'total_downloads': sum(model.download_count for model in user_models)
        }
        
        return render_template('profile.html', user=current_user, stats=stats)
        
    except Exception as e:
        print(f"Profile error: {e}")
        return render_template('profile.html', user=current_user, stats={
            'total_models': 0,
            'public_models': 0,
            'total_downloads': 0
        })
