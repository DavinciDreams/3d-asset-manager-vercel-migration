import os
import sys
from datetime import datetime
from pathlib import Path

from bson import ObjectId
from pymongo import MongoClient
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import insert as generic_insert

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import (  # noqa: E402
    create_database_engine,
    create_file_store,
    init_database,
    models,
    users,
    world_states,
)


DEFAULT_DB_NAME = "3d_asset_manager"


def mongo_database_name(uri):
    if "/" in uri and "?" in uri:
        name = uri.split("/")[-1].split("?")[0]
        if name:
            return name
    return os.environ.get("MONGODB_DB", DEFAULT_DB_NAME)


def upsert(conn, table, values, key):
    if conn.dialect.name == "postgresql":
        stmt = pg_insert(table).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[key],
            set_={column.name: stmt.excluded[column.name] for column in table.columns if column.name != key},
        )
        conn.execute(stmt)
        return

    exists = conn.execute(table.select().where(table.c[key] == values[key])).first()
    if exists:
        conn.execute(table.update().where(table.c[key] == values[key]).values(**values))
    else:
        conn.execute(generic_insert(table).values(**values))


def oid(value):
    return str(value) if value is not None else None


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    return bool(value)


def migrate_users(conn, mongo_db):
    count = 0
    for doc in mongo_db.users.find({}):
        upsert(conn, users, {
            "id": oid(doc.get("_id")),
            "username": doc.get("username", ""),
            "email": doc.get("email", ""),
            "password_hash": doc.get("password_hash", ""),
            "created_at": doc.get("created_at") or datetime.utcnow(),
        }, "id")
        count += 1
    return count


def migrate_file(file_store, fs, file_id):
    if not file_id:
        return None
    try:
        grid_out = fs.get(ObjectId(str(file_id)))
    except Exception:
        return None
    data = grid_out.read()
    return file_store.put(
        data,
        filename=getattr(grid_out, "filename", None),
        content_type=getattr(grid_out, "content_type", None),
        metadata=getattr(grid_out, "metadata", None) or {},
    )


def migrate_models(conn, mongo_db, file_store):
    import gridfs

    fs = gridfs.GridFS(mongo_db)
    count = 0
    for doc in mongo_db.models.find({}):
        file_id = migrate_file(file_store, fs, doc.get("gridfs_file_id"))
        thumbnail_file_id = migrate_file(file_store, fs, doc.get("thumbnail_file_id"))
        preview_file_id = migrate_file(file_store, fs, doc.get("preview_file_id"))
        upsert(conn, models, {
            "id": oid(doc.get("_id")),
            "name": doc.get("name") or "Untitled",
            "description": doc.get("description") or "",
            "file_format": doc.get("file_format") or "",
            "file_size": int(doc.get("file_size") or 0),
            "original_filename": doc.get("original_filename") or "",
            "user_id": oid(doc.get("user_id")),
            "is_public": as_bool(doc.get("is_public")),
            "upload_date": doc.get("upload_date") or datetime.utcnow(),
            "download_count": int(doc.get("download_count") or 0),
            "file_id": file_id,
            "camera_orbit": doc.get("camera_orbit"),
            "thumbnail_file_id": thumbnail_file_id,
            "tags": doc.get("tags") or [],
            "preview_file_id": preview_file_id,
            "default_animation": doc.get("default_animation"),
            "default_vrma_id": oid(doc.get("default_vrma_id")),
        }, "id")
        count += 1
    return count


def migrate_worlds(conn, mongo_db):
    count = 0
    collection = mongo_db.tellus_world_states
    for doc in collection.find({}):
        world_id = doc.get("worldId") or doc.get("world_id")
        if not world_id:
            continue
        state = dict(doc)
        state.pop("_id", None)
        state["worldId"] = world_id
        owner = doc.get("owner") or {}
        owner_id = oid(owner.get("id") or doc.get("owner_id"))
        upsert(conn, world_states, {
            "world_id": world_id,
            "name": doc.get("name") or world_id,
            "description": doc.get("description") or "",
            "owner_id": owner_id,
            "is_public": as_bool(doc.get("is_public")),
            "source": doc.get("source") or "tellus",
            "state": state,
            "created_at": doc.get("created_at") or datetime.utcnow(),
            "updated_at": doc.get("updated_at") or datetime.utcnow(),
        }, "world_id")
        count += 1
    return count


def main():
    mongo_uri = os.environ.get("MONGODB_URI")
    if not mongo_uri:
        raise SystemExit("Set MONGODB_URI to the source MongoDB database.")
    if not (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")):
        raise SystemExit("Set DATABASE_URL or POSTGRES_URL to the destination Postgres database.")

    mongo_client = MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    mongo_db = mongo_client[mongo_database_name(mongo_uri)]
    engine = create_database_engine()
    init_database(engine)
    file_store = create_file_store(engine)

    with engine.begin() as conn:
        user_count = migrate_users(conn, mongo_db)
        model_count = migrate_models(conn, mongo_db, file_store)
        world_count = migrate_worlds(conn, mongo_db)

    print(f"Migrated {user_count} users, {model_count} models, and {world_count} worlds.")


if __name__ == "__main__":
    main()
