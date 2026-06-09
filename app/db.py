import os
import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError


metadata = MetaData()


def _ignore_duplicate_column(error):
    message = str(error).lower()
    return "duplicate column" in message or "already exists" in message


def _json_type():
    return JSONB().with_variant(JSON(), "sqlite")


users = Table(
    "users",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("username", String(80), nullable=False, unique=True, index=True),
    Column("email", String(255), nullable=False, unique=True, index=True),
    Column("password_hash", String(255), nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

asset_files = Table(
    "asset_files",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("filename", String(255)),
    Column("content_type", String(120)),
    Column("data", LargeBinary, nullable=True),
    Column("storage_backend", String(40), nullable=False, default="database"),
    Column("bucket", String(255)),
    Column("object_key", String(1024)),
    Column("size", Integer),
    Column("metadata", _json_type(), nullable=False, default=dict),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
)

models = Table(
    "models",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("file_format", String(20), nullable=False),
    Column("file_size", Integer, nullable=False, default=0),
    Column("original_filename", String(500), nullable=False, default=""),
    Column("user_id", String(36), ForeignKey("users.id"), index=True),
    Column("is_public", Boolean, nullable=False, default=False, index=True),
    Column("upload_date", DateTime, nullable=False, default=datetime.utcnow, index=True),
    Column("download_count", Integer, nullable=False, default=0),
    Column("file_id", String(36), ForeignKey("asset_files.id")),
    Column("camera_orbit", String(120)),
    Column("thumbnail_file_id", String(36), ForeignKey("asset_files.id")),
    Column("tags", _json_type(), nullable=False, default=list),
    Column("preview_file_id", String(36), ForeignKey("asset_files.id")),
    Column("default_animation", String(255)),
    Column("default_vrma_id", String(36)),
    Column("viewable_file_id", String(36), ForeignKey("asset_files.id")),
    Column("viewable_format", String(20)),
    Column("conversion_status", String(40), index=True),
    Column("conversion_error", Text),
    Column("conversion_claimed_at", DateTime),
    Column("vrma_file_id", String(36), ForeignKey("asset_files.id")),
    Column("ai_status", String(40)),
    Column("ai_error", Text),
    Column("ai_description", Text),
    Column("ai_tags", _json_type(), nullable=False, default=list),
    Column("ai_metadata", _json_type(), nullable=False, default=dict),
    Column("approve_game_ready", Boolean, nullable=False, default=False, index=True),
    Column("approve_asset_store", Boolean, nullable=False, default=False, index=True),
    Column("approval_notes", Text),
    Column("approval_updated_at", DateTime),
)

bundles = Table(
    "bundles",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("owner_id", String(36), ForeignKey("users.id"), index=True),
    Column("is_public", Boolean, nullable=False, default=False, index=True),
    Column("model_ids", _json_type(), nullable=False, default=list),
    Column("tags", _json_type(), nullable=False, default=list),
    Column("status", String(40), nullable=False, default="draft", index=True),
    Column("file_id", String(36), ForeignKey("asset_files.id")),
    Column("metadata", _json_type(), nullable=False, default=dict),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
    Column("updated_at", DateTime, nullable=False, default=datetime.utcnow),
)

world_states = Table(
    "world_states",
    metadata,
    Column("world_id", String(120), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("owner_id", String(36), ForeignKey("users.id"), index=True),
    Column("is_public", Boolean, nullable=False, default=False, index=True),
    Column("source", String(80), nullable=False, default="tellus"),
    Column("state", _json_type(), nullable=False, default=dict),
    Column("created_at", DateTime, nullable=False, default=datetime.utcnow),
    Column("updated_at", DateTime, nullable=False, default=datetime.utcnow),
    UniqueConstraint("world_id", name="uq_world_states_world_id"),
)


class StoredFile:
    def __init__(self, row, data=None):
        self.id = row.id
        self.filename = row.filename
        self.content_type = row.content_type
        self.metadata = row.metadata or {}
        self.storage_backend = row.storage_backend
        self.bucket = row.bucket
        self.object_key = row.object_key
        self.size = row.size
        self._data = row.data if data is None else data

    def read(self):
        return self._data


class DatabaseFileStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def put(self, data, filename=None, content_type=None, metadata=None):
        file_id = str(uuid.uuid4())
        with self.engine.begin() as conn:
            conn.execute(asset_files.insert().values(
                id=file_id,
                filename=filename,
                content_type=content_type,
                data=data,
                storage_backend="database",
                bucket=None,
                object_key=None,
                size=len(data),
                metadata=json_safe(metadata or {}),
                created_at=datetime.utcnow(),
            ))
        return file_id

    def get(self, file_id):
        with self.engine.begin() as conn:
            row = conn.execute(
                select(asset_files).where(asset_files.c.id == str(file_id))
            ).mappings().first()
        if not row:
            raise FileNotFoundError(file_id)
        return StoredFile(row)

    def delete(self, file_id):
        with self.engine.begin() as conn:
            conn.execute(asset_files.delete().where(asset_files.c.id == str(file_id)))


class S3FileStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.bucket = os.environ["S3_BUCKET"]
        self.prefix = os.environ.get("S3_PREFIX", "assets").strip("/")
        self.client = self._client()
        self._ensure_bucket()

    @staticmethod
    def _client():
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
            region_name=os.environ.get("S3_REGION", "us-east-1"),
        )

    def _ensure_bucket(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as error:
                code = getattr(error, "response", {}).get("Error", {}).get("Code")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise

    def _key(self, file_id, filename):
        clean_name = (filename or "asset.bin").replace("\\", "/").split("/")[-1]
        return f"{self.prefix}/{file_id}/{clean_name}" if self.prefix else f"{file_id}/{clean_name}"

    def put(self, data, filename=None, content_type=None, metadata=None):
        file_id = str(uuid.uuid4())
        key = self._key(file_id, filename)
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra_args)
        with self.engine.begin() as conn:
            conn.execute(asset_files.insert().values(
                id=file_id,
                filename=filename,
                content_type=content_type,
                data=None,
                storage_backend="s3",
                bucket=self.bucket,
                object_key=key,
                size=len(data),
                metadata=json_safe(metadata or {}),
                created_at=datetime.utcnow(),
            ))
        return file_id

    def get(self, file_id):
        with self.engine.begin() as conn:
            row = conn.execute(
                select(asset_files).where(asset_files.c.id == str(file_id))
            ).mappings().first()
        if not row:
            raise FileNotFoundError(file_id)
        if row.storage_backend == "database":
            return StoredFile(row)
        response = self.client.get_object(Bucket=row.bucket or self.bucket, Key=row.object_key)
        return StoredFile(row, data=response["Body"].read())

    def delete(self, file_id):
        with self.engine.begin() as conn:
            row = conn.execute(
                select(asset_files).where(asset_files.c.id == str(file_id))
            ).mappings().first()
            if row and row.storage_backend == "s3" and row.object_key:
                self.client.delete_object(Bucket=row.bucket or self.bucket, Key=row.object_key)
            conn.execute(asset_files.delete().where(asset_files.c.id == str(file_id)))


def create_file_store(engine):
    if os.environ.get("S3_ENDPOINT_URL") and os.environ.get("S3_BUCKET"):
        return S3FileStore(engine)
    return DatabaseFileStore(engine)


def normalize_database_url(url):
    if not url:
        local_path = os.environ.get("SQLITE_PATH", "asset_manager_dev.sqlite3")
        return f"sqlite:///{local_path}"
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def create_database_engine():
    database_url = normalize_database_url(os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL"))
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite:") else {}
    return create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


def init_database(engine):
    metadata.create_all(engine)
    _ensure_asset_file_columns(engine)
    _ensure_model_columns(engine)
    _ensure_bundle_table(engine)


def _ensure_asset_file_columns(engine):
    existing = {column["name"] for column in inspect(engine).get_columns("asset_files")}
    desired = {
        "storage_backend": "VARCHAR(40) DEFAULT 'database' NOT NULL",
        "bucket": "VARCHAR(255)",
        "object_key": "VARCHAR(1024)",
        "size": "INTEGER",
    }
    with engine.begin() as conn:
        for column, ddl_type in desired.items():
            if column not in existing:
                try:
                    conn.execute(text(f"ALTER TABLE asset_files ADD COLUMN {column} {ddl_type}"))
                except OperationalError as error:
                    if not _ignore_duplicate_column(error):
                        raise
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE asset_files ALTER COLUMN data DROP NOT NULL"))


def _ensure_model_columns(engine):
    existing = {column["name"] for column in inspect(engine).get_columns("models")}
    desired = {
        "viewable_file_id": "VARCHAR(36)",
        "viewable_format": "VARCHAR(20)",
        "conversion_status": "VARCHAR(40)",
        "conversion_error": "TEXT",
        "conversion_claimed_at": "TIMESTAMP",
        "vrma_file_id": "VARCHAR(36)",
        "ai_status": "VARCHAR(40)",
        "ai_error": "TEXT",
        "ai_description": "TEXT",
        "ai_tags": "JSONB NOT NULL DEFAULT '[]'::jsonb" if engine.dialect.name == "postgresql" else "JSON DEFAULT '[]' NOT NULL",
        "ai_metadata": "JSONB NOT NULL DEFAULT '{}'::jsonb" if engine.dialect.name == "postgresql" else "JSON DEFAULT '{}' NOT NULL",
        "approve_game_ready": "BOOLEAN NOT NULL DEFAULT FALSE",
        "approve_asset_store": "BOOLEAN NOT NULL DEFAULT FALSE",
        "approval_notes": "TEXT",
        "approval_updated_at": "TIMESTAMP",
    }
    with engine.begin() as conn:
        for column, ddl_type in desired.items():
            if column not in existing:
                try:
                    conn.execute(text(f"ALTER TABLE models ADD COLUMN {column} {ddl_type}"))
                except OperationalError as error:
                    if not _ignore_duplicate_column(error):
                        raise


def _ensure_bundle_table(engine):
    # metadata.create_all creates this table for fresh installs. Keeping a small
    # explicit hook makes startup migrations symmetrical with models/files.
    if not inspect(engine).has_table("bundles"):
        bundles.create(engine, checkfirst=True)


@contextmanager
def db_session(engine):
    with engine.begin() as conn:
        yield conn


def count_rows(conn, table, where=None):
    stmt = select(func.count()).select_from(table)
    if where is not None:
        stmt = stmt.where(where)
    return conn.execute(stmt).scalar_one()


def json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
