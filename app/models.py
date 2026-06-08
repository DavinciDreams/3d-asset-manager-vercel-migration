import uuid
import hashlib
import secrets
from datetime import datetime

from flask import current_app
from flask_login import UserMixin
from sqlalchemy import and_, desc, func, or_, select, true, update
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import api_keys, asset_files, count_rows, models, users, world_states


class User(UserMixin):
    def __init__(self, username=None, email=None, password_hash=None, _id=None, created_at=None):
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.id = str(_id) if _id else None
        self.created_at = created_at or datetime.utcnow()

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def save(self):
        engine = current_app.config["DB_ENGINE"]
        user_data = {
            "username": self.username,
            "email": self.email,
            "password_hash": self.password_hash,
            "created_at": self.created_at,
        }

        with engine.begin() as conn:
            if self.id:
                conn.execute(update(users).where(users.c.id == self.id).values(**user_data))
            else:
                self.id = str(uuid.uuid4())
                conn.execute(users.insert().values(id=self.id, **user_data))

        return self

    @staticmethod
    def from_row(row):
        if not row:
            return None
        return User(
            username=row.username,
            email=row.email,
            password_hash=row.password_hash,
            _id=row.id,
            created_at=row.created_at,
        )

    @staticmethod
    def get_by_id(user_id):
        try:
            engine = current_app.config["DB_ENGINE"]
            with engine.begin() as conn:
                row = conn.execute(select(users).where(users.c.id == str(user_id))).mappings().first()
            return User.from_row(row)
        except Exception as e:
            print(f"Error getting user by ID: {e}")
            return None

    @staticmethod
    def get_by_username(username):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            row = conn.execute(select(users).where(users.c.username == username)).mappings().first()
        return User.from_row(row)

    @staticmethod
    def get_by_email(email):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            row = conn.execute(select(users).where(users.c.email == email)).mappings().first()
        return User.from_row(row)


class ApiKey:
    def __init__(
        self,
        user_id=None,
        name=None,
        key_hash=None,
        key_prefix=None,
        scopes=None,
        _id=None,
        created_at=None,
        last_used_at=None,
        revoked_at=None,
    ):
        self.id = str(_id) if _id else None
        self.user_id = str(user_id) if user_id else None
        self.name = name or "API key"
        self.key_hash = key_hash
        self.key_prefix = key_prefix
        self.scopes = scopes or []
        self.created_at = created_at or datetime.utcnow()
        self.last_used_at = last_used_at
        self.revoked_at = revoked_at

    @staticmethod
    def _hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def from_row(row):
        if not row:
            return None
        return ApiKey(
            user_id=row.user_id,
            name=row.name,
            key_hash=row.key_hash,
            key_prefix=row.key_prefix,
            scopes=row.scopes or [],
            _id=row.id,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
        )

    @staticmethod
    def create_for_user(user_id, name="API key", scopes=None):
        token = "tam_" + secrets.token_urlsafe(32)
        api_key = ApiKey(
            user_id=user_id,
            name=name or "API key",
            key_hash=ApiKey._hash_token(token),
            key_prefix=token[:12],
            scopes=scopes or ["upload"],
        )
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            api_key.id = str(uuid.uuid4())
            conn.execute(api_keys.insert().values(
                id=api_key.id,
                user_id=api_key.user_id,
                name=api_key.name,
                key_hash=api_key.key_hash,
                key_prefix=api_key.key_prefix,
                scopes=api_key.scopes,
                created_at=api_key.created_at,
                last_used_at=None,
                revoked_at=None,
            ))
        return api_key, token

    @staticmethod
    def list_for_user(user_id):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(
                select(api_keys)
                .where(api_keys.c.user_id == str(user_id))
                .order_by(api_keys.c.created_at.desc())
            ).mappings().all()
        return [ApiKey.from_row(row) for row in rows]

    @staticmethod
    def verify_token(token, required_scope="upload"):
        if not token:
            return None
        token_hash = ApiKey._hash_token(token)
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            row = conn.execute(
                select(api_keys).where(
                    and_(
                        api_keys.c.key_hash == token_hash,
                        api_keys.c.revoked_at.is_(None),
                    )
                )
            ).mappings().first()
            api_key = ApiKey.from_row(row)
            if not api_key:
                return None
            if required_scope and required_scope not in (api_key.scopes or []):
                return None
            conn.execute(
                update(api_keys)
                .where(api_keys.c.id == api_key.id)
                .values(last_used_at=datetime.utcnow())
            )
        return api_key

    @staticmethod
    def revoke_for_user(key_id, user_id):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            result = conn.execute(
                update(api_keys)
                .where(and_(api_keys.c.id == str(key_id), api_keys.c.user_id == str(user_id)))
                .values(revoked_at=datetime.utcnow())
            )
        return result.rowcount > 0


class Model3D:
    def __init__(self, name=None, description=None, file_format=None, file_size=None,
                 original_filename=None, user_id=None, is_public=True, _id=None,
                 upload_date=None, download_count=0, gridfs_file_id=None,
                 camera_orbit=None, thumbnail_file_id=None, tags=None,
                 preview_file_id=None, default_animation=None, default_vrma_id=None,
                 viewable_file_id=None, viewable_format=None,
                 conversion_status=None, conversion_error=None,
                 conversion_claimed_at=None, vrma_file_id=None):
        self.name = name
        self.description = description
        self.file_format = file_format
        self.file_size = file_size
        self.original_filename = original_filename
        self.user_id = user_id
        self.is_public = is_public
        self.id = str(_id) if _id else None
        self.upload_date = upload_date or datetime.utcnow()
        self.download_count = download_count
        self.gridfs_file_id = gridfs_file_id
        self.camera_orbit = camera_orbit
        self.thumbnail_file_id = thumbnail_file_id
        self.tags = tags or []
        self.preview_file_id = preview_file_id
        self.default_animation = default_animation
        self.default_vrma_id = default_vrma_id
        self.viewable_file_id = viewable_file_id
        self.viewable_format = viewable_format
        self.conversion_status = conversion_status
        self.conversion_error = conversion_error
        self.conversion_claimed_at = conversion_claimed_at
        self.vrma_file_id = vrma_file_id

    def save(self):
        engine = current_app.config["DB_ENGINE"]
        model_data = {
            "name": self.name,
            "description": self.description or "",
            "file_format": self.file_format or "",
            "file_size": self.file_size or 0,
            "original_filename": self.original_filename or "",
            "user_id": self.user_id,
            "is_public": bool(self.is_public),
            "upload_date": self.upload_date,
            "download_count": self.download_count or 0,
            "file_id": self.gridfs_file_id,
            "camera_orbit": self.camera_orbit,
            "thumbnail_file_id": self.thumbnail_file_id,
            "tags": self.tags or [],
            "preview_file_id": self.preview_file_id,
            "default_animation": self.default_animation,
            "default_vrma_id": self.default_vrma_id,
            "viewable_file_id": self.viewable_file_id,
            "viewable_format": self.viewable_format,
            "conversion_status": self.conversion_status,
            "conversion_error": self.conversion_error,
            "conversion_claimed_at": self.conversion_claimed_at,
            "vrma_file_id": self.vrma_file_id,
        }

        with engine.begin() as conn:
            if self.id:
                conn.execute(update(models).where(models.c.id == self.id).values(**model_data))
            else:
                self.id = str(uuid.uuid4())
                conn.execute(models.insert().values(id=self.id, **model_data))

        return self

    def delete(self):
        engine = current_app.config["DB_ENGINE"]
        fs = current_app.config["FILE_STORE"]
        for file_id in [
            self.gridfs_file_id,
            self.thumbnail_file_id,
            self.preview_file_id,
            self.viewable_file_id,
            self.vrma_file_id,
        ]:
            if file_id:
                try:
                    fs.delete(file_id)
                except Exception as e:
                    print(f"Error deleting stored file: {e}")
        try:
            with engine.begin() as conn:
                rows = conn.execute(select(asset_files.c.id, asset_files.c.metadata)).all()
            for row in rows:
                metadata = row.metadata or {}
                if metadata.get("export_for") == self.id:
                    fs.delete(row.id)
        except Exception as e:
            print(f"Error deleting cached exports: {e}")
        with engine.begin() as conn:
            conn.execute(models.delete().where(models.c.id == self.id))

    def increment_download_count(self):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            conn.execute(
                update(models)
                .where(models.c.id == self.id)
                .values(download_count=models.c.download_count + 1)
            )
        self.download_count += 1

    def get_file_data(self):
        fs = current_app.config["FILE_STORE"]
        try:
            if self.gridfs_file_id:
                return fs.get(self.gridfs_file_id).read()
        except Exception as e:
            print(f"Error reading stored file: {e}")
        return None

    def _read_stored_file(self, file_id):
        fs = current_app.config["FILE_STORE"]
        try:
            if file_id:
                return fs.get(file_id).read()
        except Exception as e:
            print(f"Error reading stored file {file_id}: {e}")
        return None

    def get_viewable_data(self):
        if self.viewable_file_id:
            data = self._read_stored_file(self.viewable_file_id)
            if data is not None:
                return data, (self.viewable_format or "glb")
        return self.get_file_data(), self.file_format

    def get_vrma_data(self):
        return self._read_stored_file(self.vrma_file_id)

    def get_file_size_formatted(self):
        if not self.file_size:
            return "Unknown"

        size = self.file_size
        for unit in ["bytes", "KB", "MB", "GB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    @property
    def file_extension(self):
        return self.file_format

    @staticmethod
    def from_doc(model_data):
        return Model3D(
            name=model_data.get("name", "Untitled"),
            description=model_data.get("description", ""),
            file_format=model_data.get("file_format", ""),
            file_size=model_data.get("file_size", 0),
            original_filename=model_data.get("original_filename", ""),
            user_id=model_data.get("user_id"),
            is_public=model_data.get("is_public", False),
            _id=model_data.get("id") or model_data.get("_id"),
            upload_date=model_data.get("upload_date"),
            download_count=model_data.get("download_count", 0),
            gridfs_file_id=model_data.get("file_id") or model_data.get("gridfs_file_id"),
            camera_orbit=model_data.get("camera_orbit"),
            thumbnail_file_id=model_data.get("thumbnail_file_id"),
            tags=model_data.get("tags") or [],
            preview_file_id=model_data.get("preview_file_id"),
            default_animation=model_data.get("default_animation"),
            default_vrma_id=model_data.get("default_vrma_id"),
            viewable_file_id=model_data.get("viewable_file_id"),
            viewable_format=model_data.get("viewable_format"),
            conversion_status=model_data.get("conversion_status"),
            conversion_error=model_data.get("conversion_error"),
            conversion_claimed_at=model_data.get("conversion_claimed_at"),
            vrma_file_id=model_data.get("vrma_file_id"),
        )

    @staticmethod
    def normalize_tags(raw):
        if raw is None:
            return []
        if isinstance(raw, str):
            parts = raw.split(",")
        else:
            parts = list(raw)
        seen, out = set(), []
        for p in parts:
            t = str(p).strip().lower()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @staticmethod
    def get_by_id(model_id):
        try:
            engine = current_app.config["DB_ENGINE"]
            with engine.begin() as conn:
                row = conn.execute(select(models).where(models.c.id == str(model_id))).mappings().first()
            return Model3D.from_doc(row) if row else None
        except Exception as e:
            print(f"Error getting model by ID: {e}")
            return None

    SORT_OPTIONS = {
        "newest": (models.c.upload_date, True),
        "oldest": (models.c.upload_date, False),
        "downloads": (models.c.download_count, True),
        "name": (models.c.name, False),
    }

    @classmethod
    def _sort_clause(cls, sort):
        column, descending = cls.SORT_OPTIONS.get(sort, cls.SORT_OPTIONS["newest"])
        return desc(column) if descending else column.asc()

    @staticmethod
    def _tag_predicates(tag):
        tags = Model3D.normalize_tags(tag)
        if not tags:
            return []
        return [models.c.tags.contains([tag]) for tag in tags]

    @staticmethod
    def _search_predicate(search):
        if not search:
            return None
        pattern = f"%{search.lower()}%"
        return or_(
            func.lower(models.c.name).like(pattern),
            func.lower(models.c.description).like(pattern),
        )

    @staticmethod
    def get_public_models(page=1, per_page=20, search=None, sort="newest", tag=None):
        engine = current_app.config["DB_ENGINE"]
        predicates = [models.c.is_public.is_(True)]
        search_predicate = Model3D._search_predicate(search)
        if search_predicate is not None:
            predicates.append(search_predicate)
        predicates.extend(Model3D._tag_predicates(tag))
        where = and_(*predicates) if predicates else true()

        with engine.begin() as conn:
            total = count_rows(conn, models, where)
            rows = conn.execute(
                select(models)
                .where(where)
                .order_by(Model3D._sort_clause(sort))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).mappings().all()

        return [Model3D.from_doc(row) for row in rows], total

    @staticmethod
    def get_user_models(user_id, page=1, per_page=20, sort="newest", tag=None):
        engine = current_app.config["DB_ENGINE"]
        predicates = [models.c.user_id == str(user_id)]
        predicates.extend(Model3D._tag_predicates(tag))
        where = and_(*predicates) if predicates else true()

        with engine.begin() as conn:
            total = count_rows(conn, models, where)
            rows = conn.execute(
                select(models)
                .where(where)
                .order_by(Model3D._sort_clause(sort))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).mappings().all()

        return [Model3D.from_doc(row) for row in rows], total

    @staticmethod
    def _distinct_tags(where):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(select(models.c.tags).where(where)).all()
        tags = set()
        for row in rows:
            for tag in row.tags or []:
                tags.add(tag)
        return sorted(tags)

    @staticmethod
    def get_user_tags(user_id):
        try:
            return Model3D._distinct_tags(models.c.user_id == str(user_id))
        except Exception as e:
            print(f"Error getting user tags: {e}")
            return []

    @staticmethod
    def get_public_tags():
        try:
            return Model3D._distinct_tags(models.c.is_public.is_(True))
        except Exception as e:
            print(f"Error getting public tags: {e}")
            return []

    @staticmethod
    def list_vrma_for_user(user_id=None):
        engine = current_app.config["DB_ENGINE"]
        predicates = [models.c.file_format == "vrma"]
        if user_id:
            predicates.append(or_(models.c.is_public.is_(True), models.c.user_id == str(user_id)))
        else:
            predicates.append(models.c.is_public.is_(True))

        with engine.begin() as conn:
            rows = conn.execute(
                select(models).where(and_(*predicates)).order_by(models.c.name.asc())
            ).mappings().all()
        return [Model3D.from_doc(row) for row in rows]

    @staticmethod
    def list_generated_vrma_for_user(user_id=None):
        engine = current_app.config["DB_ENGINE"]
        predicates = [models.c.vrma_file_id.is_not(None)]
        if user_id:
            predicates.append(or_(models.c.is_public.is_(True), models.c.user_id == str(user_id)))
        else:
            predicates.append(models.c.is_public.is_(True))

        with engine.begin() as conn:
            rows = conn.execute(
                select(models).where(and_(*predicates)).order_by(models.c.name.asc())
            ).mappings().all()
        return [Model3D.from_doc(row) for row in rows]

    @staticmethod
    def get_stats():
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            total_models = count_rows(conn, models)
            public_models = count_rows(conn, models, models.c.is_public.is_(True))
            total_users = count_rows(conn, users)
            total_downloads = conn.execute(select(func.coalesce(func.sum(models.c.download_count), 0))).scalar_one()

        return {
            "total_models": total_models,
            "public_models": public_models,
            "total_users": total_users,
            "total_downloads": total_downloads,
        }

    @staticmethod
    def get_user_stats(user_id):
        engine = current_app.config["DB_ENGINE"]
        user_filter = models.c.user_id == str(user_id)
        with engine.begin() as conn:
            total_models = count_rows(conn, models, user_filter)
            public_models = count_rows(conn, models, and_(user_filter, models.c.is_public.is_(True)))
            total_downloads = conn.execute(
                select(func.coalesce(func.sum(models.c.download_count), 0)).where(user_filter)
            ).scalar_one()

        return {
            "total_models": total_models,
            "public_models": public_models,
            "total_downloads": total_downloads,
        }


class WorldState:
    def __init__(self, world_id, name=None, description="", owner_id=None,
                 is_public=False, source="tellus", state=None,
                 created_at=None, updated_at=None):
        self.world_id = world_id
        self.name = name or world_id
        self.description = description or ""
        self.owner_id = owner_id
        self.is_public = bool(is_public)
        self.source = source or "tellus"
        self.state = state or {}
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()

    @staticmethod
    def from_row(row):
        if not row:
            return None
        return WorldState(
            world_id=row.world_id,
            name=row.name,
            description=row.description,
            owner_id=row.owner_id,
            is_public=row.is_public,
            source=row.source,
            state=row.state or {},
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def to_api(self, include_state=True):
        data = {
            "worldId": self.world_id,
            "name": self.name,
            "description": self.description,
            "is_public": self.is_public,
            "source": self.source,
            "owner": {"id": self.owner_id},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_state:
            data.update(self.state or {})
            data.setdefault("worldId", self.world_id)
            data.setdefault("name", self.name)
            data.setdefault("description", self.description)
            data.setdefault("is_public", self.is_public)
        return data

    @staticmethod
    def get(world_id):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            row = conn.execute(
                select(world_states).where(world_states.c.world_id == str(world_id))
            ).mappings().first()
        return WorldState.from_row(row)

    @staticmethod
    def list_worlds(page=1, per_page=20, search=None, user_id=None, public_only=True):
        engine = current_app.config["DB_ENGINE"]
        predicates = []
        if not public_only and not user_id:
            predicates = []
        elif user_id and not public_only:
            predicates.append(world_states.c.owner_id == str(user_id))
        elif user_id:
            predicates.append(or_(world_states.c.is_public.is_(True), world_states.c.owner_id == str(user_id)))
        else:
            predicates.append(world_states.c.is_public.is_(True))
        if search:
            pattern = f"%{search.lower()}%"
            predicates.append(or_(
                func.lower(world_states.c.name).like(pattern),
                func.lower(world_states.c.description).like(pattern),
                func.lower(world_states.c.world_id).like(pattern),
            ))
        where = and_(*predicates) if predicates else true()
        with engine.begin() as conn:
            total = count_rows(conn, world_states, where)
            rows = conn.execute(
                select(world_states)
                .where(where)
                .order_by(desc(world_states.c.updated_at))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).mappings().all()
        return [WorldState.from_row(row) for row in rows], total

    @staticmethod
    def upsert(world_id, payload, owner_id=None):
        engine = current_app.config["DB_ENGINE"]
        now = datetime.utcnow()
        existing = WorldState.get(world_id)
        state = dict(payload or {})
        state["worldId"] = world_id
        name = state.get("name") or (existing.name if existing else world_id)
        description = state.get("description") or (existing.description if existing else "")
        is_public = state.get("is_public", existing.is_public if existing else False)
        source = state.get("source") or (existing.source if existing else "tellus")
        final_owner_id = owner_id or (existing.owner_id if existing else None)

        values = {
            "world_id": world_id,
            "name": name,
            "description": description,
            "owner_id": final_owner_id,
            "is_public": bool(is_public),
            "source": source,
            "state": state,
            "updated_at": now,
        }

        with engine.begin() as conn:
            if existing:
                conn.execute(
                    update(world_states)
                    .where(world_states.c.world_id == world_id)
                    .values(**{k: v for k, v in values.items() if k != "world_id"})
                )
            else:
                conn.execute(world_states.insert().values(created_at=now, **values))
        return WorldState.get(world_id)

    def patch_metadata(self, payload):
        engine = current_app.config["DB_ENGINE"]
        state = dict(self.state or {})
        values = {"updated_at": datetime.utcnow()}
        for key in ["name", "description", "source"]:
            if key in payload:
                values[key] = payload.get(key) or ""
                state[key] = values[key]
        if "is_public" in payload and isinstance(payload["is_public"], bool):
            values["is_public"] = payload["is_public"]
            state["is_public"] = payload["is_public"]
        values["state"] = state
        with engine.begin() as conn:
            conn.execute(update(world_states).where(world_states.c.world_id == self.world_id).values(**values))
        return WorldState.get(self.world_id)
