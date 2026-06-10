"""WSGI entry point for production servers (gunicorn on Coolify, etc.).

Run with:  gunicorn wsgi:app
"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    # Local dev fallback; in production gunicorn imports `app` directly.
    # Enable template auto-reload + debug so edits to templates/Python are picked
    # up without a manual restart. This block never runs under gunicorn.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=True)
