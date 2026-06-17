import uuid
import hashlib
import secrets
from datetime import datetime

from flask import current_app
from flask_login import UserMixin
from sqlalchemy import String, and_, cast, delete, desc, func, insert, or_, select, true, update
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import (
    api_keys,
    asset_files,
    bundles,
    count_rows,
    model_variants,
    models,
    optimization_jobs,
    users,
    world_states,
)


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
                 content_hash=None,
                 upload_date=None, download_count=0, gridfs_file_id=None,
                 camera_orbit=None, thumbnail_file_id=None, tags=None,
                 asset_category=None, asset_styles=None, asset_types=None,
                 runtime_metadata=None,
                 preview_file_id=None, default_animation=None, default_vrma_id=None,
                 viewable_file_id=None, viewable_format=None,
                 conversion_status=None, conversion_error=None,
                 conversion_claimed_at=None, vrma_file_id=None,
                 ai_status=None, ai_error=None, ai_description=None,
                 ai_tags=None, ai_metadata=None, approve_game_ready=False,
                 approve_asset_store=False, approval_notes=None,
                 approval_updated_at=None):
        self.name = name
        self.description = description
        self.file_format = file_format
        self.file_size = file_size
        self.content_hash = content_hash
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
        self.asset_category = asset_category
        self.asset_styles = asset_styles or []
        self.asset_types = asset_types or []
        self.runtime_metadata = self.normalize_runtime_metadata(runtime_metadata)
        self.preview_file_id = preview_file_id
        self.default_animation = default_animation
        self.default_vrma_id = default_vrma_id
        self.viewable_file_id = viewable_file_id
        self.viewable_format = viewable_format
        self.conversion_status = conversion_status
        self.conversion_error = conversion_error
        self.conversion_claimed_at = conversion_claimed_at
        self.vrma_file_id = vrma_file_id
        self.ai_status = ai_status
        self.ai_error = ai_error
        self.ai_description = ai_description
        self.ai_tags = ai_tags or []
        self.ai_metadata = ai_metadata or {}
        self.approve_game_ready = bool(approve_game_ready)
        self.approve_asset_store = bool(approve_asset_store)
        self.approval_notes = approval_notes
        self.approval_updated_at = approval_updated_at

    def save(self):
        engine = current_app.config["DB_ENGINE"]
        model_data = {
            "name": self.name,
            "description": self.description or "",
            "file_format": self.file_format or "",
            "file_size": self.file_size or 0,
            "content_hash": self.content_hash,
            "original_filename": self.original_filename or "",
            "user_id": self.user_id,
            "is_public": bool(self.is_public),
            "upload_date": self.upload_date,
            "download_count": self.download_count or 0,
            "file_id": self.gridfs_file_id,
            "camera_orbit": self.camera_orbit,
            "thumbnail_file_id": self.thumbnail_file_id,
            "tags": self.tags or [],
            "asset_category": self.asset_category,
            "asset_styles": self.asset_styles or [],
            "asset_types": self.asset_types or [],
            "runtime_metadata": self.runtime_metadata or {},
            "preview_file_id": self.preview_file_id,
            "default_animation": self.default_animation,
            "default_vrma_id": self.default_vrma_id,
            "viewable_file_id": self.viewable_file_id,
            "viewable_format": self.viewable_format,
            "conversion_status": self.conversion_status,
            "conversion_error": self.conversion_error,
            "conversion_claimed_at": self.conversion_claimed_at,
            "vrma_file_id": self.vrma_file_id,
            "ai_status": self.ai_status,
            "ai_error": self.ai_error,
            "ai_description": self.ai_description,
            "ai_tags": self.ai_tags or [],
            "ai_metadata": self.ai_metadata or {},
            "approve_game_ready": self.approve_game_ready,
            "approve_asset_store": self.approve_asset_store,
            "approval_notes": self.approval_notes,
            "approval_updated_at": self.approval_updated_at,
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

        # Delete derived variant files (game-optimized GLB, future LOD) and
        # their rows. The model_variants.model_id FK -> models.id would
        # otherwise block the model delete on Postgres.
        try:
            with engine.begin() as conn:
                variant_rows = conn.execute(
                    select(model_variants.c.file_id)
                    .where(model_variants.c.model_id == self.id)
                ).all()
            for row in variant_rows:
                if row.file_id:
                    try:
                        fs.delete(row.file_id)
                    except Exception as e:
                        print(f"Error deleting variant file: {e}")
            with engine.begin() as conn:
                conn.execute(model_variants.delete().where(model_variants.c.model_id == self.id))
        except Exception as e:
            print(f"Error deleting model variants: {e}")

        # Clear optimization jobs that reference this model. Both source_model_id
        # and result_model_id are FKs -> models.id, so any job pointing here
        # (e.g. an old game-optimized copy was a job's result) blocks the delete.
        try:
            with engine.begin() as conn:
                conn.execute(
                    optimization_jobs.delete().where(
                        optimization_jobs.c.source_model_id == self.id
                    )
                )
                conn.execute(
                    update(optimization_jobs)
                    .where(optimization_jobs.c.result_model_id == self.id)
                    .values(result_model_id=None)
                )
        except Exception as e:
            print(f"Error clearing optimization jobs: {e}")

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

    def is_animation_carrier(self):
        runtime = self.runtime_metadata or {}
        upload = runtime.get("upload") if isinstance(runtime, dict) else {}
        tags = {str(tag or "").strip().lower() for tag in (self.tags or [])}
        asset_types = {str(tag or "").strip().lower() for tag in (self.asset_types or [])}
        if (self.file_format or "").lower() != "vrma" and bool((tags | asset_types) & {"avatar", "vrm"}):
            return False
        return (
            bool(self.vrma_file_id)
            or (self.file_format or "").lower() in {"vrma", "bvh"}
            or self.asset_category == "animation"
            or bool(tags & {"animation-source", "animation-library", "vrma-library"})
            or bool(asset_types & {"animation", "avatar-animation"})
            or (isinstance(upload, dict) and upload.get("source") == "vrma-library-import")
        )

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
            content_hash=model_data.get("content_hash"),
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
            asset_category=model_data.get("asset_category"),
            asset_styles=model_data.get("asset_styles") or [],
            asset_types=model_data.get("asset_types") or [],
            runtime_metadata=model_data.get("runtime_metadata") or {},
            preview_file_id=model_data.get("preview_file_id"),
            default_animation=model_data.get("default_animation"),
            default_vrma_id=model_data.get("default_vrma_id"),
            viewable_file_id=model_data.get("viewable_file_id"),
            viewable_format=model_data.get("viewable_format"),
            conversion_status=model_data.get("conversion_status"),
            conversion_error=model_data.get("conversion_error"),
            conversion_claimed_at=model_data.get("conversion_claimed_at"),
            vrma_file_id=model_data.get("vrma_file_id"),
            ai_status=model_data.get("ai_status"),
            ai_error=model_data.get("ai_error"),
            ai_description=model_data.get("ai_description"),
            ai_tags=model_data.get("ai_tags") or [],
            ai_metadata=model_data.get("ai_metadata") or {},
            approve_game_ready=model_data.get("approve_game_ready", False),
            approve_asset_store=model_data.get("approve_asset_store", False),
            approval_notes=model_data.get("approval_notes"),
            approval_updated_at=model_data.get("approval_updated_at"),
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
    def normalize_category(raw):
        value = str(raw or "").strip().lower()
        value = " ".join(token for token in value.replace("_", " ").replace("-", " ").split())
        return value or None

    @staticmethod
    def normalize_runtime_metadata(raw):
        if raw is None or raw == "":
            return {}
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return {}
        if not isinstance(raw, dict):
            return {}

        metadata = dict(raw)
        behaviors = Model3D.normalize_tags(metadata.get("behaviors", []))
        light = metadata.get("light")
        normalized = {}
        if behaviors:
            normalized["behaviors"] = behaviors
        animations = []
        if isinstance(metadata.get("animations"), list):
            seen_animations = set()
            for index, item in enumerate(metadata.get("animations") or []):
                if isinstance(item, dict):
                    name = str(item.get("name") or f"animation-{index + 1}").strip()
                    clip = {"name": name}
                    if item.get("duration") is not None:
                        try:
                            clip["duration"] = round(max(0, float(item.get("duration"))), 3)
                        except (TypeError, ValueError):
                            pass
                else:
                    name = str(item or "").strip()
                    clip = {"name": name}
                if not name:
                    continue
                key = name.lower()
                if key in seen_animations:
                    continue
                seen_animations.add(key)
                animations.append(clip)
        if animations:
            normalized["animations"] = animations
        mesh_stats = metadata.get("mesh_stats")
        if isinstance(mesh_stats, dict):
            cleaned_stats = {}
            for source_key, target_key in [
                ("vertices", "vertices"),
                ("vertex_count", "vertices"),
                ("triangles", "triangles"),
                ("triangle_count", "triangles"),
                ("primitives", "primitives"),
                ("primitive_count", "primitives"),
            ]:
                if source_key not in mesh_stats:
                    continue
                try:
                    value = int(mesh_stats.get(source_key) or 0)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    cleaned_stats[target_key] = value
            if cleaned_stats:
                normalized["mesh_stats"] = cleaned_stats
        physical = metadata.get("physical")
        if isinstance(physical, dict):
            cleaned_physical = {}
            for key in ("width", "height", "depth", "radius", "suggested_scale"):
                try:
                    value = float(physical.get(key))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    cleaned_physical[key] = round(value, 6)
            for key in ("center", "size", "min", "max"):
                raw = physical.get(key)
                if not isinstance(raw, list):
                    continue
                values = []
                for item in (raw + [0, 0, 0])[:3]:
                    try:
                        values.append(round(float(item), 6))
                    except (TypeError, ValueError):
                        values.append(0.0)
                cleaned_physical[key] = values
            if cleaned_physical:
                normalized["physical"] = cleaned_physical
        if isinstance(light, dict):
            enabled = bool(light.get("enabled"))
            light_type = str(light.get("type") or ("point" if enabled else "none")).strip().lower()
            if light_type not in {"none", "point", "spot", "directional", "ambient"}:
                light_type = "point" if enabled else "none"
            color = str(light.get("color") or "#ffb35a").strip()
            if not color.startswith("#") or len(color) not in {4, 7}:
                color = "#ffb35a"
            try:
                intensity = float(light.get("intensity", 1.5 if enabled else 0))
            except (TypeError, ValueError):
                intensity = 1.5 if enabled else 0
            try:
                range_value = float(light.get("range", 8 if enabled else 0))
            except (TypeError, ValueError):
                range_value = 8 if enabled else 0
            raw_offset = light.get("offset") if isinstance(light.get("offset"), list) else [0, 0, 0]
            offset = []
            for item in (raw_offset + [0, 0, 0])[:3]:
                try:
                    offset.append(float(item))
                except (TypeError, ValueError):
                    offset.append(0.0)
            attach_to = str(light.get("attach_to") or "").strip()
            normalized["light"] = {
                "enabled": enabled,
                "type": light_type,
                "color": color,
                "intensity": max(0, intensity),
                "range": max(0, range_value),
                "cast_shadow": bool(light.get("cast_shadow", False)),
                "attach_to": attach_to,
                "offset": offset,
            }
            if enabled and "light-emitter" not in behaviors:
                normalized["behaviors"] = (behaviors or []) + ["light-emitter"]
        for key in ["physics", "interaction", "spawn"]:
            if isinstance(metadata.get(key), dict):
                normalized[key] = metadata[key]
        upload = metadata.get("upload")
        if isinstance(upload, dict):
            allowed = {
                "source",
                "world_id",
                "asset_username",
                "asset_user_id",
                "user_agent",
                "content_hash",
                "generation_id",
            }
            cleaned_upload = {}
            for key in allowed:
                value = upload.get(key)
                if value in (None, "", [], {}):
                    continue
                cleaned_upload[key] = str(value)[:500]
            if cleaned_upload:
                normalized["upload"] = cleaned_upload
        return normalized

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

    @staticmethod
    def get_by_content_hash(content_hash):
        digest = str(content_hash or "").strip().lower()
        if not digest:
            return None
        try:
            engine = current_app.config["DB_ENGINE"]
            with engine.begin() as conn:
                row = conn.execute(select(models).where(models.c.content_hash == digest)).mappings().first()
            return Model3D.from_doc(row) if row else None
        except Exception as e:
            print(f"Error getting model by content hash: {e}")
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
        return [Model3D._json_list_contains(models.c.tags, tag) for tag in tags]

    @staticmethod
    def _json_list_contains(column, value):
        engine = current_app.config["DB_ENGINE"]
        if engine.dialect.name == "sqlite":
            safe_value = str(value).replace("%", "\\%").replace("_", "\\_").replace('"', '\\"')
            safe_single = safe_value.replace("'", "''")
            text_value = func.coalesce(cast(column, String), "")
            return or_(
                text_value.like(f'%"{safe_value}"%', escape="\\"),
                text_value.like(f"%'{safe_single}'%", escape="\\"),
            )
        return and_(column.is_not(None), column.contains([value]))

    @staticmethod
    def _animation_carrier_predicate():
        """Rows that should live in the animation catalog, not model browse.

        A clean generated clip has vrma_file_id. Older FBX library imports can
        miss that field if conversion failed, so also recognize their source
        metadata/category/tags to keep raw FBX animation shells out of asset
        browse.
        """
        runtime_text = func.lower(func.coalesce(cast(models.c.runtime_metadata, String), ""))
        has_vrm_variant = models.c.id.in_(select(model_variants.c.model_id).where(and_(
            model_variants.c.kind == "vrm",
            model_variants.c.file_id.is_not(None),
        )))
        explicit_animation_source = or_(
            models.c.file_format.in_(["vrma", "bvh"]),
            func.coalesce(models.c.asset_category, "") == "animation",
            Model3D._json_list_contains(models.c.tags, "animation-source"),
            Model3D._json_list_contains(models.c.tags, "animation-library"),
            Model3D._json_list_contains(models.c.tags, "vrma-library"),
            Model3D._json_list_contains(models.c.asset_types, "animation"),
            Model3D._json_list_contains(models.c.asset_types, "avatar-animation"),
            runtime_text.like("%vrma-library-import%"),
        )
        return and_(
            models.c.file_format != "vrm",
            or_(
                explicit_animation_source,
                and_(
                    models.c.vrma_file_id.is_not(None),
                    ~has_vrm_variant,
                    ~Model3D._json_list_contains(models.c.tags, "avatar"),
                    ~Model3D._json_list_contains(models.c.tags, "vrm"),
                    ~Model3D._json_list_contains(models.c.asset_types, "avatar"),
                    ~Model3D._json_list_contains(models.c.asset_types, "vrm"),
                ),
            ),
        )

    @staticmethod
    def _facet_predicates(category=None, style=None, asset_type=None):
        predicates = []
        category = Model3D.normalize_category(category)
        if category:
            predicates.append(models.c.asset_category == category)
        for style in Model3D.normalize_tags(style):
            predicates.append(Model3D._json_list_contains(models.c.asset_styles, style))
        for asset_type in Model3D.normalize_tags(asset_type):
            predicates.append(Model3D._json_list_contains(models.c.asset_types, asset_type))
        return predicates

    @staticmethod
    def _asset_kind_predicates(asset_kind=None):
        predicates = []
        for kind in Model3D.normalize_tags(asset_kind):
            if kind in {"vrm", "avatar"}:
                predicates.append(or_(
                    models.c.file_format == "vrm",
                    Model3D._json_list_contains(models.c.tags, "vrm"),
                    Model3D._json_list_contains(models.c.tags, "avatar"),
                    Model3D._json_list_contains(models.c.asset_types, "vrm"),
                    Model3D._json_list_contains(models.c.asset_types, "avatar"),
                    models.c.id.in_(select(model_variants.c.model_id).where(and_(
                        model_variants.c.kind == "vrm",
                        model_variants.c.file_id.is_not(None),
                    ))),
                ))
            elif kind in {"animated", "animation"}:
                predicates.append(and_(
                    models.c.file_format.in_(["glb", "gltf"]),
                    Model3D._json_list_contains(models.c.asset_types, "rigged"),
                    Model3D._json_list_contains(models.c.asset_types, "animated"),
                ))
        return predicates

    @staticmethod
    def _search_predicate(search):
        if not search:
            return None
        pattern = f"%{search.lower()}%"
        return or_(
            func.lower(models.c.name).like(pattern),
            func.lower(models.c.description).like(pattern),
            func.lower(models.c.original_filename).like(pattern),
            func.lower(models.c.asset_category).like(pattern),
            func.lower(models.c.ai_description).like(pattern),
            func.lower(cast(models.c.tags, String)).like(pattern),
            func.lower(cast(models.c.asset_styles, String)).like(pattern),
            func.lower(cast(models.c.asset_types, String)).like(pattern),
            func.lower(cast(models.c.runtime_metadata, String)).like(pattern),
            func.lower(cast(models.c.ai_tags, String)).like(pattern),
            func.lower(cast(models.c.ai_metadata, String)).like(pattern),
        )

    @staticmethod
    def list_models(page=1, per_page=20, search=None, sort="newest", tag=None,
                    category=None, style=None, asset_type=None, public_only=True,
                    owner_id=None, exclude_formats=None, exclude_animation_carriers=False,
                    asset_kind=None):
        engine = current_app.config["DB_ENGINE"]
        predicates = []
        if public_only:
            predicates.append(models.c.is_public.is_(True))
        if owner_id:
            predicates.append(models.c.user_id == str(owner_id))
        if exclude_formats:
            predicates.append(models.c.file_format.not_in([
                str(fmt).strip().lower() for fmt in exclude_formats if str(fmt).strip()
            ]))
        asset_kind_values = Model3D.normalize_tags(asset_kind)
        if exclude_animation_carriers and not ({"animated", "animation"} & set(asset_kind_values)):
            predicates.append(~Model3D._animation_carrier_predicate())
        search_predicate = Model3D._search_predicate(search)
        if search_predicate is not None:
            predicates.append(search_predicate)
        predicates.extend(Model3D._tag_predicates(tag))
        predicates.extend(Model3D._facet_predicates(category=category, style=style, asset_type=asset_type))
        predicates.extend(Model3D._asset_kind_predicates(asset_kind_values))
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
    def list_animated_models(page=1, per_page=20, sort="newest", public_only=True,
                             owner_id=None, formats=None):
        """List renderable model assets that carry their own rig and animation.

        This is intentionally narrower than the general model search: Tellus
        needs loadable animated avatars/creatures/props, not VRMA/BVH clips or
        raw animation-library FBX source records.
        """
        engine = current_app.config["DB_ENGINE"]
        allowed_formats = [
            fmt for fmt in Model3D.normalize_tags(formats or ["glb", "gltf"])
            if fmt in {"glb", "gltf"}
        ] or ["glb", "gltf"]
        predicates = [
            models.c.file_format.in_(allowed_formats),
            Model3D._json_list_contains(models.c.asset_types, "rigged"),
            Model3D._json_list_contains(models.c.asset_types, "animated"),
            models.c.file_format.not_in(["vrma", "bvh"]),
        ]
        if public_only:
            predicates.append(models.c.is_public.is_(True))
        if owner_id:
            predicates.append(models.c.user_id == str(owner_id))
        where = and_(*predicates)

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
    def get_public_models(page=1, per_page=20, search=None, sort="newest", tag=None,
                          category=None, style=None, asset_type=None, exclude_formats=None,
                          exclude_animation_carriers=False, asset_kind=None):
        return Model3D.list_models(
            page=page, per_page=per_page, search=search, sort=sort, tag=tag,
            category=category, style=style, asset_type=asset_type,
            public_only=True, exclude_formats=exclude_formats,
            exclude_animation_carriers=exclude_animation_carriers,
            asset_kind=asset_kind,
        )

    @staticmethod
    def get_user_models(user_id, page=1, per_page=20, sort="newest", tag=None,
                        category=None, style=None, asset_type=None, exclude_formats=None,
                        exclude_animation_carriers=False, asset_kind=None):
        engine = current_app.config["DB_ENGINE"]
        predicates = [models.c.user_id == str(user_id)]
        if exclude_formats:
            predicates.append(models.c.file_format.not_in([
                str(fmt).strip().lower() for fmt in exclude_formats if str(fmt).strip()
            ]))
        asset_kind_values = Model3D.normalize_tags(asset_kind)
        if exclude_animation_carriers and not ({"animated", "animation"} & set(asset_kind_values)):
            predicates.append(~Model3D._animation_carrier_predicate())
        predicates.extend(Model3D._tag_predicates(tag))
        predicates.extend(Model3D._facet_predicates(category=category, style=style, asset_type=asset_type))
        predicates.extend(Model3D._asset_kind_predicates(asset_kind_values))
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
    def _distinct_column_values(column, where, *, list_values=False):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(select(column).where(where)).all()
        values = set()
        for row in rows:
            value = row[0]
            if list_values:
                for item in value or []:
                    if item:
                        values.add(item)
            elif value:
                values.add(value)
        return sorted(values)

    @staticmethod
    def get_user_tags(user_id, exclude_formats=None, exclude_animation_carriers=False):
        try:
            predicates = [models.c.user_id == str(user_id)]
            if exclude_formats:
                predicates.append(models.c.file_format.not_in([
                    str(fmt).strip().lower() for fmt in exclude_formats if str(fmt).strip()
                ]))
            if exclude_animation_carriers:
                predicates.append(~Model3D._animation_carrier_predicate())
            return Model3D._distinct_tags(and_(*predicates))
        except Exception as e:
            print(f"Error getting user tags: {e}")
            return []

    @staticmethod
    def get_public_tags(exclude_animation_carriers=False):
        try:
            predicates = [models.c.is_public.is_(True)]
            if exclude_animation_carriers:
                predicates.append(~Model3D._animation_carrier_predicate())
            return Model3D._distinct_tags(and_(*predicates))
        except Exception as e:
            print(f"Error getting public tags: {e}")
            return []

    @staticmethod
    def get_user_facets(user_id, exclude_formats=None, exclude_animation_carriers=False):
        predicates = [models.c.user_id == str(user_id)]
        if exclude_formats:
            predicates.append(models.c.file_format.not_in([
                str(fmt).strip().lower() for fmt in exclude_formats if str(fmt).strip()
            ]))
        if exclude_animation_carriers:
            predicates.append(~Model3D._animation_carrier_predicate())
        where = and_(*predicates)
        return {
            "categories": Model3D._distinct_column_values(models.c.asset_category, where),
            "styles": Model3D._distinct_column_values(models.c.asset_styles, where, list_values=True),
            "types": Model3D._distinct_column_values(models.c.asset_types, where, list_values=True),
        }

    @staticmethod
    def get_public_facets(exclude_animation_carriers=False):
        predicates = [models.c.is_public.is_(True)]
        if exclude_animation_carriers:
            predicates.append(~Model3D._animation_carrier_predicate())
        where = and_(*predicates)
        return {
            "categories": Model3D._distinct_column_values(models.c.asset_category, where),
            "styles": Model3D._distinct_column_values(models.c.asset_styles, where, list_values=True),
            "types": Model3D._distinct_column_values(models.c.asset_types, where, list_values=True),
        }

    @staticmethod
    def optimizable_ids():
        """All GLB/GLTF model ids (the formats gltfpack can game-optimize),
        newest first. Used by the admin backfill to find work."""
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(
                select(models.c.id)
                .where(models.c.file_format.in_(["glb", "gltf"]))
                .order_by(models.c.upload_date.desc())
            ).all()
        return [str(r.id) for r in rows]

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
        predicates = [
            models.c.vrma_file_id.is_not(None),
            models.c.file_format != "vrm",
        ]
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
    def list_animation_sources_for_user(user_id=None):
        """Animation catalog rows that are source files or failed/pending
        conversion records rather than native .vrma uploads."""
        engine = current_app.config["DB_ENGINE"]
        predicates = [
            Model3D._animation_carrier_predicate(),
            models.c.file_format.not_in(["vrma", "vrm"]),
        ]
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
    def list_vrm_for_user(user_id=None, include_private=False):
        """Models uploaded as a native .vrm avatar, visible to the user."""
        engine = current_app.config["DB_ENGINE"]
        predicates = [models.c.file_format == "vrm"]
        if not include_private:
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
    def list_with_vrm_variant_for_user(user_id=None, include_private=False):
        """Models that have a derived VRM avatar (a 'vrm' ModelVariant) -- e.g.
        a rigged GLB converted via glb2vrm. Visible to the user. Joins
        model_variants so we return the source models (one row each)."""
        engine = current_app.config["DB_ENGINE"]
        predicates = [
            model_variants.c.kind == "vrm",
            model_variants.c.file_id.is_not(None),
            ~Model3D._animation_carrier_predicate(),
        ]
        if not include_private:
            if user_id:
                predicates.append(or_(models.c.is_public.is_(True), models.c.user_id == str(user_id)))
            else:
                predicates.append(models.c.is_public.is_(True))

        with engine.begin() as conn:
            rows = conn.execute(
                select(models)
                .select_from(models.join(model_variants, model_variants.c.model_id == models.c.id))
                .where(and_(*predicates))
                .order_by(models.c.name.asc())
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


class AssetBundle:
    def __init__(self, name=None, description=None, owner_id=None, is_public=False,
                 model_ids=None, tags=None, status="draft", file_id=None,
                 metadata=None, _id=None, created_at=None, updated_at=None):
        self.id = str(_id) if _id else None
        self.name = name
        self.description = description or ""
        self.owner_id = owner_id
        self.is_public = bool(is_public)
        self.model_ids = model_ids or []
        self.tags = tags or []
        self.status = status or "draft"
        self.file_id = file_id
        self.metadata = metadata or {}
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()

    @staticmethod
    def from_row(row):
        if not row:
            return None
        return AssetBundle(
            name=row.name,
            description=row.description,
            owner_id=row.owner_id,
            is_public=row.is_public,
            model_ids=row.model_ids or [],
            tags=row.tags or [],
            status=row.status,
            file_id=row.file_id,
            metadata=row.metadata or {},
            _id=row.id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def save(self):
        engine = current_app.config["DB_ENGINE"]
        self.updated_at = datetime.utcnow()
        values = {
            "name": self.name,
            "description": self.description or "",
            "owner_id": self.owner_id,
            "is_public": self.is_public,
            "model_ids": self.model_ids or [],
            "tags": self.tags or [],
            "status": self.status or "draft",
            "file_id": self.file_id,
            "metadata": self.metadata or {},
            "updated_at": self.updated_at,
        }
        with engine.begin() as conn:
            if self.id:
                conn.execute(update(bundles).where(bundles.c.id == self.id).values(**values))
            else:
                self.id = str(uuid.uuid4())
                conn.execute(bundles.insert().values(id=self.id, created_at=self.created_at, **values))
        return self

    def models(self):
        found = []
        for model_id in self.model_ids:
            model = Model3D.get_by_id(model_id)
            if model:
                found.append(model)
        return found

    def to_api(self, include_models=False):
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "owner": {"id": self.owner_id},
            "is_public": self.is_public,
            "model_ids": self.model_ids,
            "tags": self.tags,
            "status": self.status,
            "has_file": bool(self.file_id),
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_models:
            data["models"] = [
                {
                    "id": model.id,
                    "name": model.name,
                    "file_format": model.file_format,
                    "approve_game_ready": model.approve_game_ready,
                    "approve_asset_store": model.approve_asset_store,
                }
                for model in self.models()
            ]
        return data

    @staticmethod
    def get(bundle_id):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            row = conn.execute(select(bundles).where(bundles.c.id == str(bundle_id))).mappings().first()
        return AssetBundle.from_row(row)

    @staticmethod
    def list_for_user(user_id=None, page=1, per_page=20, public_only=True):
        engine = current_app.config["DB_ENGINE"]
        predicates = []
        if public_only:
            predicates.append(bundles.c.is_public.is_(True))
        elif user_id:
            predicates.append(or_(bundles.c.is_public.is_(True), bundles.c.owner_id == str(user_id)))
        where = and_(*predicates) if predicates else true()
        with engine.begin() as conn:
            total = count_rows(conn, bundles, where)
            rows = conn.execute(
                select(bundles)
                .where(where)
                .order_by(desc(bundles.c.updated_at))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).mappings().all()
        return [AssetBundle.from_row(row) for row in rows], total


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


class ModelVariant:
    """A derived file attached to a source model: the game-optimized GLB today,
    LOD levels later. Identified by (model_id, kind, level)."""

    def __init__(self, row=None, **kwargs):
        data = dict(row) if row is not None else kwargs
        self.id = str(data.get("id")) if data.get("id") else None
        self.model_id = str(data["model_id"]) if data.get("model_id") else None
        self.kind = data.get("kind")
        self.level = data.get("level")
        self.file_id = str(data["file_id"]) if data.get("file_id") else None
        self.file_format = data.get("file_format") or "glb"
        self.size = data.get("size") or 0
        self.settings = data.get("settings") or {}
        self.status = data.get("status") or "ready"
        self.created_at = data.get("created_at")
        self.updated_at = data.get("updated_at")

    def to_api(self):
        return {
            "id": self.id,
            "model_id": self.model_id,
            "kind": self.kind,
            "level": self.level,
            "file_format": self.file_format,
            "size": self.size,
            "settings": self.settings,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def read_data(self):
        fs = current_app.config["FILE_STORE"]
        try:
            if self.file_id:
                return fs.get(self.file_id).read()
        except Exception as e:
            print(f"Error reading variant file: {e}")
        return None

    @staticmethod
    def get(model_id, kind, level=None):
        engine = current_app.config["DB_ENGINE"]
        where = and_(
            model_variants.c.model_id == str(model_id),
            model_variants.c.kind == kind,
            model_variants.c.level.is_(None) if level is None else (model_variants.c.level == level),
        )
        with engine.begin() as conn:
            row = conn.execute(select(model_variants).where(where)).mappings().first()
        return ModelVariant(row) if row else None

    @staticmethod
    def list_for_model(model_id):
        engine = current_app.config["DB_ENGINE"]
        with engine.begin() as conn:
            rows = conn.execute(
                select(model_variants)
                .where(model_variants.c.model_id == str(model_id))
                .order_by(model_variants.c.kind, model_variants.c.level)
            ).mappings().all()
        return [ModelVariant(row) for row in rows]

    @staticmethod
    def map_by_kind(kind, model_ids=None):
        engine = current_app.config["DB_ENGINE"]
        where = and_(model_variants.c.kind == kind, model_variants.c.file_id.isnot(None))
        ids = [str(i) for i in (model_ids or [])]
        if model_ids is not None:
            if not ids:
                return {}
            where = and_(where, model_variants.c.model_id.in_(ids))
        with engine.begin() as conn:
            rows = conn.execute(select(model_variants).where(where)).mappings().all()
        return {str(row["model_id"]): ModelVariant(row) for row in rows}

    @staticmethod
    def upsert(model_id, kind, file_id, *, level=None, file_format="glb",
               size=0, settings=None, status="ready"):
        """Create or replace the variant for (model_id, kind, level). Returns the
        ModelVariant and the previous file_id (if any) so callers can clean up
        the now-orphaned blob after the pointer is swapped."""
        engine = current_app.config["DB_ENGINE"]
        now = datetime.utcnow()
        existing = ModelVariant.get(model_id, kind, level)
        old_file_id = existing.file_id if existing else None
        values = {
            "model_id": str(model_id),
            "kind": kind,
            "level": level,
            "file_id": str(file_id) if file_id else None,
            "file_format": file_format,
            "size": size or 0,
            "settings": settings or {},
            "status": status,
            "updated_at": now,
        }
        with engine.begin() as conn:
            if existing:
                conn.execute(
                    update(model_variants)
                    .where(model_variants.c.id == existing.id)
                    .values(**values)
                )
                variant_id = existing.id
            else:
                variant_id = str(uuid.uuid4())
                conn.execute(insert(model_variants).values(
                    id=variant_id, created_at=now, **values))
        return ModelVariant.get(model_id, kind, level), old_file_id

    @staticmethod
    def delete_for(model_id, kind, level=None):
        engine = current_app.config["DB_ENGINE"]
        where = and_(
            model_variants.c.model_id == str(model_id),
            model_variants.c.kind == kind,
            model_variants.c.level.is_(None) if level is None else (model_variants.c.level == level),
        )
        with engine.begin() as conn:
            conn.execute(delete(model_variants).where(where))

    @staticmethod
    def model_ids_with_kind(kind, model_ids=None):
        """Return a set of model_ids that have a variant of `kind` (with a
        file). Optionally restrict to `model_ids`. One query -- use this for
        list views to avoid a per-model lookup (N+1)."""
        engine = current_app.config["DB_ENGINE"]
        where = and_(
            model_variants.c.kind == kind,
            model_variants.c.file_id.isnot(None),
        )
        if model_ids is not None:
            ids = [str(m) for m in model_ids]
            if not ids:
                return set()
            where = and_(where, model_variants.c.model_id.in_(ids))
        with engine.begin() as conn:
            rows = conn.execute(
                select(model_variants.c.model_id).where(where)
            ).all()
        return {str(r.model_id) for r in rows}
