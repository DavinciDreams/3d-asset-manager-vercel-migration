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

    admin_ids = {value for value in _env_values("ASSET_MANAGER_ADMIN_USER_IDS")}
    admin_names = {
        value.lower()
        for value in _env_values("ASSET_MANAGER_ADMIN_USERNAMES", "ASSET_MANAGER_ADMIN_USERS")
    }
    admin_emails = {
        value.lower()
        for value in _env_values("ASSET_MANAGER_ADMIN_EMAILS", "ASSET_MANAGER_ADMIN_USERS")
    }

    return (
        bool(user_id and user_id in admin_ids)
        or bool(username and username in admin_names)
        or bool(email and email in admin_emails)
    )


def can_manage_model(user, model):
    if not user or not model:
        return False
    return getattr(user, "id", None) == getattr(model, "user_id", None) or is_asset_admin_user(user)
