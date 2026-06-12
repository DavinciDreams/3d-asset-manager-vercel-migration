import os

from flask import Flask
from flask_login import LoginManager

from app.db import create_database_engine, create_file_store, init_database


login_manager = LoginManager()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")

    max_upload_mb = int(os.environ.get("MAX_UPLOAD_MB", "100"))
    app.config["MAX_FILE_BYTES"] = max_upload_mb * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_FILE_BYTES"] + (5 * 1024 * 1024)
    app.config["ALLOWED_EXTENSIONS"] = {
        "obj", "fbx", "gltf", "glb", "dae", "3ds", "ply", "stl", "vrm", "vrma", "bvh"
    }
    enable_conversion = os.environ.get("ENABLE_CONVERSION")
    has_configured_database = bool(os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"))
    if enable_conversion is None:
        app.config["ENABLE_CONVERSION"] = has_configured_database
    else:
        app.config["ENABLE_CONVERSION"] = enable_conversion.lower() in {"1", "true", "yes", "on"}
    app.config["FBX2GLTF_BIN"] = os.environ.get("FBX2GLTF_BIN", "/usr/local/bin/FBX2glTF")
    app.config["ASSIMP_BIN"] = os.environ.get("ASSIMP_BIN", "assimp")
    app.config["NODE_BIN"] = os.environ.get("NODE_BIN", "node")
    app.config["FBX2VRMA_DIR"] = os.environ.get("FBX2VRMA_DIR", "/app/tools")

    try:
        engine = create_database_engine()
        init_database(engine)
        app.config["DB_ENGINE"] = engine
        app.config["FILE_STORE"] = create_file_store(engine)
        print(f"Database connected: {engine.url.render_as_string(hide_password=True)}")
    except Exception as e:
        print(f"Database connection failed: {e}")
        raise Exception(f"Database connection failed: {e}")

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "info"

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return User.get_by_id(user_id)
        except Exception as e:
            print(f"User loader error: {e}")
            return None

    from app.auth import auth_bp
    from app.main import main_bp
    from app.api import api_bp, start_ai_enrichment_worker

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    if os.environ.get("AI_AUTOTAG_WORKER", "1").lower() not in {"0", "false", "no", "off"}:
        try:
            start_ai_enrichment_worker(app)
            print("AI enrichment worker started")
        except Exception as e:
            print(f"AI enrichment worker failed to start: {e}")

    if app.config["ENABLE_CONVERSION"]:
        try:
            from app.conversion import start_worker
            start_worker(app)
            print("Conversion worker started")
        except Exception as e:
            print(f"Conversion worker failed to start: {e}")

    return app
