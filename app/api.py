from flask import Blueprint, jsonify, request, current_app, make_response
from flask_login import current_user, login_required
from app.models import Model3D, User
from bson.objectid import ObjectId
import io

api_bp = Blueprint('api', __name__)

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
        
        if user_only and current_user.is_authenticated:
            # Get user's models
            models, total = Model3D.get_user_models(current_user.id, page=page, per_page=per_page)
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
        
        # Check access permissions
        if not model.is_public:
            if not current_user.is_authenticated or model.user_id != current_user.id:
                return jsonify({'error': 'Access denied'}), 403
        
        # Get file data from GridFS
        file_data = model.get_file_data()
        
        if not file_data:
            return jsonify({'error': 'File not found'}), 404
        
        # Increment download counter
        model.increment_download_count()
        
        # Determine MIME type
        mime_types = {
            'glb': 'model/gltf-binary',
            'gltf': 'application/json',
            'obj': 'text/plain',
            'fbx': 'application/octet-stream',
            'dae': 'application/xml',
            '3ds': 'application/octet-stream',
            'ply': 'application/octet-stream',
            'stl': 'application/octet-stream'
        }
        
        mimetype = mime_types.get(model.file_format.lower(), 'application/octet-stream')
        
        # Create response
        response = make_response(file_data)
        response.headers['Content-Type'] = mimetype
        response.headers['Content-Disposition'] = f'attachment; filename="{model.original_filename}"'
        response.headers['Content-Length'] = str(len(file_data))
        
        return response
        
    except Exception as e:
        print(f"API download error: {e}")
        return jsonify({'error': 'Download failed'}), 500

@api_bp.route('/view/<model_id>')
def view_model(model_id):
    """Serve model file for 3D viewing (not as download)"""
    try:
        model = Model3D.get_by_id(model_id)
        
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        
        # Check access permissions
        if not model.is_public:
            if not current_user.is_authenticated or model.user_id != current_user.id:
                return jsonify({'error': 'Access denied'}), 403
        
        # Get file data from GridFS
        file_data = model.get_file_data()
        
        if not file_data:
            return jsonify({'error': 'File not found'}), 404
        
        # Determine MIME type
        mime_types = {
            'glb': 'model/gltf-binary',
            'gltf': 'application/json',
            'obj': 'text/plain',
            'fbx': 'application/octet-stream',
            'dae': 'application/xml',
            '3ds': 'application/octet-stream',
            'ply': 'application/octet-stream',
            'stl': 'application/octet-stream'
        }
        
        mimetype = mime_types.get(model.file_format.lower(), 'application/octet-stream')
        
        # Create response for viewing (not download)
        response = make_response(file_data)
        response.headers['Content-Type'] = mimetype
        response.headers['Content-Length'] = str(len(file_data))
        response.headers['Cache-Control'] = 'public, max-age=3600'  # Cache for 1 hour
        
        return response
        
    except Exception as e:
        print(f"API view error: {e}")
        return jsonify({'error': 'View failed'}), 500

@api_bp.route('/model/<model_id>', methods=['PUT', 'PATCH'])
@login_required
def update_model(model_id):
    """Update a model's metadata (name, description, visibility)."""
    try:
        model = Model3D.get_by_id(model_id)

        if not model:
            return jsonify({'error': 'Model not found'}), 404

        # Check ownership
        if model.user_id != current_user.id:
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

        model.save()

        return jsonify({
            'success': True,
            'message': 'Model updated successfully.',
            'model': {
                'id': model.id,
                'name': model.name,
                'description': model.description,
                'is_public': model.is_public,
            }
        })

    except Exception as e:
        print(f"API update error: {e}")
        return jsonify({'error': 'Update failed. Please try again.'}), 500


@api_bp.route('/model/<model_id>', methods=['DELETE'])
@login_required
def delete_model(model_id):
    """Delete a model"""
    try:
        model = Model3D.get_by_id(model_id)
        
        if not model:
            return jsonify({'error': 'Model not found'}), 404
        
        # Check ownership
        if model.user_id != current_user.id:
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

@api_bp.route('/user/models')
@login_required
def get_user_models():
    """Get current user's models"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        
        models, total = Model3D.get_user_models(current_user.id, page=page, per_page=per_page)
        
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
                'download_count': model.download_count
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

@api_bp.route('/upload', methods=['POST'])
@login_required
def upload_model():
    """API endpoint for uploading 3D models"""
    print("🔄 Upload API called - Starting processing...")
    
    try:
        # Log request info
        print(f"📋 Request method: {request.method}")
        print(f"📋 Content length: {request.content_length}")
        print(f"📋 Form keys: {list(request.form.keys())}")
        print(f"📋 Files keys: {list(request.files.keys())}")
        print(f"📋 Current user: {current_user.username if current_user.is_authenticated else 'Not authenticated'}")
        
        # Get form data
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        is_public = request.form.get('is_public') == 'true'  # Note: 'true' for JSON boolean
        
        print(f"📋 Form data - Name: {name}, Description: {description[:50] if description else 'None'}, Public: {is_public}")
        
        # Get uploaded file
        file = request.files.get('file')
        
        if not file or file.filename == '':
            print("❌ No file provided")
            return jsonify({'error': 'Please select a file to upload.'}), 400
        
        if not name:
            print("❌ No name provided")
            return jsonify({'error': 'Please provide a name for your model.'}), 400
        
        print(f"📋 File info - Name: {file.filename}, Content-Type: {file.content_type}")
        
        # Import secure_filename
        from werkzeug.utils import secure_filename
        
        # Validate file extension
        filename = secure_filename(file.filename)
        file_extension = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
        
        print(f"📋 File extension: {file_extension}")
        
        allowed_extensions = current_app.config['ALLOWED_EXTENSIONS']
        if file_extension not in allowed_extensions:
            print(f"❌ Invalid file extension: {file_extension}")
            return jsonify({'error': f'File type not supported. Allowed: {", ".join(allowed_extensions)}'}), 400
        
        # Read file content
        print("📋 Reading file content...")
        file_content = file.read()
        file_size = len(file_content)
        
        print(f"📋 File size: {file_size} bytes ({file_size / (1024*1024):.2f} MB)")
        
        # Check file size (4MB limit for Vercel)
        if file_size > current_app.config['MAX_CONTENT_LENGTH']:
            print(f"❌ File too large: {file_size} > {current_app.config['MAX_CONTENT_LENGTH']}")
            return jsonify({'error': 'File too large. Maximum size is 4MB for Vercel deployment.'}), 400
        
        # Store file in GridFS
        print("📋 Storing file in GridFS...")
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
        
        print(f"📋 GridFS file ID: {gridfs_file_id}")
        
        # Create model record
        print("📋 Creating model record...")
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
        print(f"📋 Model saved with ID: {model.id}")
        
        # Return success response with model data
        return jsonify({
            'success': True,
            'message': f'Model "{name}" uploaded successfully!',
            'model': {
                'id': model.id,
                'name': model.name,
                'description': model.description,
                'file_format': model.file_format,
                'file_size': model.file_size,
                'original_filename': model.original_filename,
                'is_public': model.is_public,
                'upload_date': model.upload_date.isoformat() if model.upload_date else None
            }
        }), 201
        
    except Exception as e:
        print(f"❌ API upload error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Upload failed. Please try again.'}), 500
