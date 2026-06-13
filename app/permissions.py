import os


def _env_values(*names):
    values = []
    for name in names:
        raw = os.environ.get(name, "")
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


def is_asset_admin_user(user):
    """Return True when a logged-in user can manage all asset records."""
    if not user:
        return False

    user_id = str(getattr(user, "id", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip().lower()
    email = str(getattr(user, "email", "") or "").strip().lower()

    admin_ids = {
        value
        for value in _env_values(
            "ASSET_MANAGER_ADMIN_USER_IDS",
            "TELLUS_ADMIN_USER_ID",
        )
    }
    admin_names = {
        value.lower()
        for value in _env_values(
            "ASSET_MANAGER_ADMIN_USERNAMES",
            "ASSET_MANAGER_ADMIN_USERS",
            "TELLUS_ADMIN_USERNAME",
        )
    }
    admin_emails = {
        value.lower()
        for value in _env_values(
            "ASSET_MANAGER_ADMIN_EMAILS",
            "ASSET_MANAGER_ADMIN_USERS",
            "TELLUS_ADMIN_EMAIL",
        )
    }

    return (
        bool(user_id and user_id in admin_ids)
        or bool(username and username in admin_names)
        or bool(email and email in admin_emails)
    )


def asset_admin_configured():
    return bool(
        _env_values(
            "ASSET_MANAGER_ADMIN_USER_IDS",
            "ASSET_MANAGER_ADMIN_USERNAMES",
            "ASSET_MANAGER_ADMIN_USERS",
            "ASSET_MANAGER_ADMIN_EMAILS",
            "TELLUS_ADMIN_USER_ID",
            "TELLUS_ADMIN_USERNAME",
            "TELLUS_ADMIN_EMAIL",
        )
    )


def can_manage_model(user, model):
    if not user or not model:
        return False
    return getattr(user, "id", None) == getattr(model, "user_id", None) or is_asset_admin_user(user)
