# Vercel entry point for 3D Asset Manager
# Vercel's @vercel/python runtime looks for a top-level WSGI callable
# named `app`, `application`, or `handler`. We guarantee `app` is ALWAYS
# bound at module top level, even if app creation fails.
import sys
import os

from flask import Flask

# Add the project root to Python path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `app` MUST be defined at the top level no matter what. Start with a
# minimal fallback so the name always exists, then swap in the real app.
app = Flask(__name__)
_startup_error = None

try:
    from app import create_app

    app = create_app()
except Exception as e:  # noqa: BLE001 - we want to surface ANY startup error
    _startup_error = str(e)
    print(f"Error creating Flask app: {_startup_error}")

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def _debug_info(path):
        return (
            "Deployment Debug - App failed to start.\n"
            f"Error: {_startup_error}",
            500,
            {"Content-Type": "text/plain"},
        )

# Vercel imports `app` from this module. The block below only runs locally.
if __name__ == "__main__":
    app.run(debug=False)
