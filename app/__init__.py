from flask import Flask
from flask_login import LoginManager
from pymongo import MongoClient
import gridfs
import os

# Global variables for MongoDB
mongo_client = None
db = None
fs = None
login_manager = LoginManager()


def _database_name_from_uri(mongo_uri):
    if mongo_uri and '/' in mongo_uri and '?' in mongo_uri:
        db_name = mongo_uri.split('/')[-1].split('?')[0]
        if db_name:
            return db_name
    return '3d_asset_manager'


def create_app():
    app = Flask(__name__)

    # Configuration
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    # Max upload size in MB. Defaults to 100MB (no Vercel 4MB cap on Coolify).
    # Override with MAX_UPLOAD_MB env var.
    max_upload_mb = int(os.environ.get('MAX_UPLOAD_MB', '100'))
    app.config['MAX_CONTENT_LENGTH'] = max_upload_mb * 1024 * 1024
    app.config['ALLOWED_EXTENSIONS'] = {'obj', 'fbx', 'gltf', 'glb', 'dae', '3ds', 'ply', 'stl'}

    mongo_uri = os.environ.get('MONGODB_URI')
    is_production = os.environ.get('FLASK_ENV') == 'production'
    use_memory_db = not mongo_uri and not is_production

    if not mongo_uri and not use_memory_db:
        raise Exception("MONGODB_URI environment variable is required")

    try:
        global mongo_client, db, fs

        if use_memory_db:
            import mongomock
            import mongomock.gridfs

            mongomock.gridfs.enable_gridfs_integration()
            mongo_client = mongomock.MongoClient()
            db_name = os.environ.get('MONGODB_DB', '3d_asset_manager_dev')
            print("Using in-memory MongoDB for local development")
        else:
            mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
            mongo_client.admin.command('ping')
            db_name = _database_name_from_uri(mongo_uri)
            print("MongoDB connection successful")

        db = mongo_client[db_name]
        fs = gridfs.GridFS(db)

        app.config['MONGODB_CLIENT'] = mongo_client
        app.config['MONGODB_DB'] = db
        app.config['GRIDFS'] = fs

        print(f"Database '{db_name}' connected")

    except Exception as e:
        print(f"MongoDB connection failed: {e}")
        raise Exception(f"Database connection failed: {e}")

    # Initialize Flask-Login
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'

    # User loader for Flask-Login
    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return User.get_by_id(user_id)
        except Exception as e:
            print(f"User loader error: {e}")
            return None

    # Register blueprints
    from app.auth import auth_bp
    from app.main import main_bp
    from app.api import api_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix='/api')

    # Create indexes for better performance
    try:
        with app.app_context():
            db.users.create_index("username", unique=True)
            db.users.create_index("email", unique=True)
            db.models.create_index("user_id")
            db.models.create_index("is_public")
            db.models.create_index("upload_date")
            db.models.create_index([("name", "text"), ("description", "text")])
            print("Database indexes created")
    except Exception as e:
        print(f"Index creation warning: {e}")

    return app
