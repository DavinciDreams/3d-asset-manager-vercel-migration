#!/usr/bin/env python3
"""Quick backend smoke for model display data."""
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def quick_fix_test():
    print("Quick model-display backend smoke...")
    from app import create_app
    from app.models import Model3D

    app = create_app()
    with app.app_context():
        print(f"Database: {app.config['DB_ENGINE'].url.render_as_string(hide_password=True)}")
        print(f"File store: {type(app.config['FILE_STORE']).__name__}")
        stats = Model3D.get_stats()
        print(f"Stats: {stats}")

        public_models, total = Model3D.get_public_models(page=1, per_page=5)
        print(f"Public models: {total}")
        for model in public_models:
            print(f"  {model.name} ({model.file_format}) public={model.is_public}")


if __name__ == "__main__":
    quick_fix_test()
