#!/usr/bin/env python3
"""Inspect model rows through the current SQL/file-store backend."""
import os
import sys

from sqlalchemy import select

sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def debug_model_display_issues():
    print("Debugging model display data...")
    from app import create_app
    from app.db import models
    from app.models import Model3D

    app = create_app()
    with app.app_context():
        engine = app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(select(models).order_by(models.c.upload_date.desc())).mappings().all()

        print(f"Total models in database: {len(rows)}")
        for index, row in enumerate(rows[:10], start=1):
            print(f"\nModel {index}:")
            print(f"  ID: {row.id}")
            print(f"  Name: {row.name}")
            print(f"  Public: {row.is_public}")
            print(f"  User ID: {row.user_id}")
            print(f"  File Format: {row.file_format}")
            print(f"  File ID: {row.file_id}")
            print(f"  Conversion: {row.conversion_status}")

        public_models, public_total = Model3D.get_public_models(page=1, per_page=10)
        print(f"\nModel3D.get_public_models(): {public_total}")
        for model in public_models[:5]:
            print(f"  Public: {model.name} ({model.id})")

        stats = Model3D.get_stats()
        print(f"\nStats: {stats}")


if __name__ == "__main__":
    debug_model_display_issues()
