"""WSGI entry point for production servers (gunicorn on Coolify, etc.).

Run with:  gunicorn wsgi:app
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    # Local dev fallback; in production gunicorn imports `app` directly.
    app.run(host="0.0.0.0", port=8000)
