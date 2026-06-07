#!/usr/bin/env python3
"""Smoke-test model retrieval for dashboard and browse display."""
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))


def test_model_retrieval():
    print("Testing model retrieval for dashboard display...")
    from app import create_app
    from app.models import Model3D, User

    app = create_app()
    with app.app_context():
        stats = Model3D.get_stats()
        print(f"Stats: {stats}")

        public_models, public_total = Model3D.get_public_models(page=1, per_page=6)
        print(f"Public models: {public_total}")
        for model in public_models[:3]:
            print(f"  Public: {model.name} ({model.id})")

        user = None
        for model in public_models:
            if model.user_id:
                user = User.get_by_id(model.user_id)
                break

        if user:
            user_models, user_total = Model3D.get_user_models(user.id, page=1, per_page=10)
            print(f"User models for {user.username}: {user_total}")
            for model in user_models[:3]:
                print(f"  User model: {model.name} public={model.is_public}")
        else:
            print("No user-owned models found in the local database.")


if __name__ == "__main__":
    test_model_retrieval()
