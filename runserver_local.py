import os
from pathlib import Path

repo_root = Path(__file__).resolve().parent
local_bin = repo_root / "tools" / "bin"
if local_bin.exists():
    os.environ["PATH"] = str(local_bin) + os.pathsep + os.environ.get("PATH", "")

from app import create_app
app = create_app()
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.run(host="0.0.0.0", port=8000, debug=False, use_reloader=False, threaded=True)
