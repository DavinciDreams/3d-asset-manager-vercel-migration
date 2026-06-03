from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from bson.objectid import ObjectId
from datetime import datetime
from flask import current_app
import gridfs

class User(UserMixin):
    def __init__(self, username=None, email=None, password_hash=None, _id=None, created_at=None):
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.id = str(_id) if _id else None
        self.created_at = created_at or datetime.utcnow()
    
    def set_password(self, password):
        """Set password hash"""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        """Check password against hash"""
        return check_password_hash(self.password_hash, password)
    
    def save(self):
        """Save user to MongoDB"""
        db = current_app.config['MONGODB_DB']
        
        user_data = {
            'username': self.username,
            'email': self.email,
            'password_hash': self.password_hash,
            'created_at': self.created_at
        }
        
        if self.id:
            # Update existing user
            db.users.update_one(
                {'_id': ObjectId(self.id)},
                {'$set': user_data}
            )
        else:
            # Create new user
            result = db.users.insert_one(user_data)
            self.id = str(result.inserted_id)
        
        return self
    
    @staticmethod
    def get_by_id(user_id):
        """Get user by ID"""
        try:
            db = current_app.config['MONGODB_DB']
            user_data = db.users.find_one({'_id': ObjectId(user_id)})
            
            if user_data:
                return User(
                    username=user_data['username'],
                    email=user_data['email'],
                    password_hash=user_data['password_hash'],
                    _id=user_data['_id'],
                    created_at=user_data.get('created_at')
                )
        except Exception as e:
            print(f"Error getting user by ID: {e}")
        return None
    
    @staticmethod
    def get_by_username(username):
        """Get user by username"""
        db = current_app.config['MONGODB_DB']
        user_data = db.users.find_one({'username': username})
        
        if user_data:
            return User(
                username=user_data['username'],
                email=user_data['email'],
                password_hash=user_data['password_hash'],
                _id=user_data['_id'],
                created_at=user_data.get('created_at')
            )
        return None
    
    @staticmethod
    def get_by_email(email):
        """Get user by email"""
        db = current_app.config['MONGODB_DB']
        user_data = db.users.find_one({'email': email})
        
        if user_data:
            return User(
                username=user_data['username'],
                email=user_data['email'],
                password_hash=user_data['password_hash'],
                _id=user_data['_id'],
                created_at=user_data.get('created_at')
            )
        return None

class Model3D:
    def __init__(self, name=None, description=None, file_format=None, file_size=None,
                 original_filename=None, user_id=None, is_public=True, _id=None,
                 upload_date=None, download_count=0, gridfs_file_id=None,
                 camera_orbit=None, thumbnail_file_id=None):
        self.name = name
        self.description = description
        self.file_format = file_format
        self.file_size = file_size
        self.original_filename = original_filename
        self.user_id = user_id
        self.is_public = is_public
        self.id = str(_id) if _id else None
        self.upload_date = upload_date or datetime.utcnow()
        self.download_count = download_count
        self.gridfs_file_id = gridfs_file_id
        # Default <model-viewer> camera-orbit string, e.g. "180deg 75deg 105%".
        # None means "use the viewer's automatic framing".
        self.camera_orbit = camera_orbit
        # GridFS id of a captured PNG thumbnail. None until first generated.
        self.thumbnail_file_id = thumbnail_file_id
    
    def save(self):
        """Save model to MongoDB"""
        db = current_app.config['MONGODB_DB']
        
        model_data = {
            'name': self.name,
            'description': self.description,
            'file_format': self.file_format,
            'file_size': self.file_size,
            'original_filename': self.original_filename,
            'user_id': self.user_id,
            'is_public': self.is_public,
            'upload_date': self.upload_date,
            'download_count': self.download_count,
            'gridfs_file_id': self.gridfs_file_id,
            'camera_orbit': self.camera_orbit,
            'thumbnail_file_id': self.thumbnail_file_id
        }
        
        if self.id:
            # Update existing model
            db.models.update_one(
                {'_id': ObjectId(self.id)},
                {'$set': model_data}
            )
        else:
            # Create new model
            result = db.models.insert_one(model_data)
            self.id = str(result.inserted_id)
        
        return self
    
    def delete(self):
        """Delete model and associated file from MongoDB"""
        db = current_app.config['MONGODB_DB']
        fs = current_app.config['GRIDFS']
        
        # Delete file from GridFS
        if self.gridfs_file_id:
            try:
                fs.delete(ObjectId(self.gridfs_file_id))
            except Exception as e:
                print(f"Error deleting file from GridFS: {e}")

        # Delete thumbnail from GridFS
        if self.thumbnail_file_id:
            try:
                fs.delete(ObjectId(self.thumbnail_file_id))
            except Exception as e:
                print(f"Error deleting thumbnail from GridFS: {e}")

        # Delete model document
        db.models.delete_one({'_id': ObjectId(self.id)})
    
    def increment_download_count(self):
        """Increment download counter"""
        db = current_app.config['MONGODB_DB']
        db.models.update_one(
            {'_id': ObjectId(self.id)},
            {'$inc': {'download_count': 1}}
        )
        self.download_count += 1
    
    def get_file_data(self):
        """Get file data from GridFS"""
        fs = current_app.config['GRIDFS']
        try:
            if self.gridfs_file_id:
                grid_out = fs.get(ObjectId(self.gridfs_file_id))
                return grid_out.read()
        except Exception as e:
            print(f"Error reading file from GridFS: {e}")
        return None
    
    def get_file_size_formatted(self):
        """Format file size in human readable format"""
        if not self.file_size:
            return "Unknown"
        
        size = self.file_size
        for unit in ['bytes', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
    
    @property
    def file_extension(self):
        """Get file extension (alias for file_format for template compatibility)"""
        return self.file_format
    
    @staticmethod
    def get_by_id(model_id):
        """Get model by ID"""
        try:
            db = current_app.config['MONGODB_DB']
            model_data = db.models.find_one({'_id': ObjectId(model_id)})
            
            if model_data:
                return Model3D(
                    name=model_data['name'],
                    description=model_data['description'],
                    file_format=model_data['file_format'],
                    file_size=model_data['file_size'],
                    original_filename=model_data['original_filename'],
                    user_id=model_data['user_id'],
                    is_public=model_data['is_public'],
                    _id=model_data['_id'],
                    upload_date=model_data.get('upload_date'),
                    download_count=model_data.get('download_count', 0),
                    gridfs_file_id=model_data.get('gridfs_file_id'),
                    camera_orbit=model_data.get('camera_orbit'),
                    thumbnail_file_id=model_data.get('thumbnail_file_id')
                )
        except Exception as e:
            print(f"Error getting model by ID: {e}")
        return None
    
    @staticmethod
    def get_public_models(page=1, per_page=20, search=None):
        """Get public models with pagination"""
        db = current_app.config['MONGODB_DB']
        
        query = {'is_public': True}
        if search:
            query['$text'] = {'$search': search}
        
        total = db.models.count_documents(query)
        
        models = list(db.models.find(query)
                     .sort('upload_date', -1)
                     .skip((page - 1) * per_page)
                     .limit(per_page))
        
        model_objects = []
        for model_data in models:
            model_objects.append(Model3D(
                name=model_data['name'],
                description=model_data['description'],
                file_format=model_data['file_format'],
                file_size=model_data['file_size'],
                original_filename=model_data['original_filename'],
                user_id=model_data['user_id'],
                is_public=model_data['is_public'],
                _id=model_data['_id'],
                upload_date=model_data.get('upload_date'),
                download_count=model_data.get('download_count', 0),
                gridfs_file_id=model_data.get('gridfs_file_id'),
                camera_orbit=model_data.get('camera_orbit'),
                thumbnail_file_id=model_data.get('thumbnail_file_id')
            ))
        
        return model_objects, total
    
    @staticmethod
    def get_user_models(user_id, page=1, per_page=20):
        """Get user's models with pagination"""
        db = current_app.config['MONGODB_DB']
        
        query = {'user_id': user_id}
        total = db.models.count_documents(query)
        
        models = list(db.models.find(query)
                     .sort('upload_date', -1)
                     .skip((page - 1) * per_page)
                     .limit(per_page))
        
        model_objects = []
        for model_data in models:
            model_objects.append(Model3D(
                name=model_data['name'],
                description=model_data['description'],
                file_format=model_data['file_format'],
                file_size=model_data['file_size'],
                original_filename=model_data['original_filename'],
                user_id=model_data['user_id'],
                is_public=model_data['is_public'],
                _id=model_data['_id'],
                upload_date=model_data.get('upload_date'),
                download_count=model_data.get('download_count', 0),
                gridfs_file_id=model_data.get('gridfs_file_id'),
                camera_orbit=model_data.get('camera_orbit'),
                thumbnail_file_id=model_data.get('thumbnail_file_id')
            ))
        
        return model_objects, total
    
    @staticmethod
    def get_stats():
        """Get database statistics"""
        db = current_app.config['MONGODB_DB']
        
        total_models = db.models.count_documents({})
        public_models = db.models.count_documents({'is_public': True})
        total_users = db.users.count_documents({})
        
        # Total downloads
        pipeline = [
            {'$group': {'_id': None, 'total_downloads': {'$sum': '$download_count'}}}
        ]
        download_result = list(db.models.aggregate(pipeline))
        total_downloads = download_result[0]['total_downloads'] if download_result else 0
        
        return {
            'total_models': total_models,
            'public_models': public_models,
            'total_users': total_users,
            'total_downloads': total_downloads
        }
