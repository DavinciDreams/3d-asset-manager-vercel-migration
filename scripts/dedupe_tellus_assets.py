"""Find and optionally delete visually duplicated Tellus asset-store records.

This is intentionally dry-run by default. It clusters assets by thumbnail
perceptual hash, prefers the copy referenced by Tellus world state, and deletes
only when --apply is provided.

Examples:
    python scripts/dedupe_tellus_assets.py --base-url https://3d.flobots.xyz
    python scripts/dedupe_tellus_assets.py --base-url https://3d.flobots.xyz --token %TELLUS_ADMIN_API_TOKEN% --apply
"""

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from io import BytesIO

from PIL import Image


WORLD_ASSET_ID_KEYS = {
    "assetid", "asset_id", "assetstoreid", "asset_store_id",
    "modelid", "model_id", "model", "asset",
}


UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def request_json(base_url, path, token=None, retries=3):
    url = base_url.rstrip("/") + path
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.load(response)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(attempt)


def request_bytes(base_url, path_or_url, token=None):
    url = path_or_url if path_or_url.startswith("http") else base_url.rstrip("/") + path_or_url
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.read()


def delete_model(base_url, model_id, token):
    if not token:
        raise RuntimeError("Deleting requires --token or TELLUS_ADMIN_API_TOKEN/ASSET_MANAGER_API_TOKEN.")
    url = base_url.rstrip("/") + f"/api/model/{urllib.parse.quote(model_id)}"
    req = urllib.request.Request(url, method="DELETE", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def iter_asset_ids(value):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key or "").replace("-", "_").lower()
            if normalized in WORLD_ASSET_ID_KEYS and isinstance(child, str):
                yield child
            yield from iter_asset_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_asset_ids(child)


def dct_1d(values):
    n = len(values)
    result = []
    factor = math.pi / n
    for k in range(n):
        total = 0.0
        for i, value in enumerate(values):
            total += value * math.cos((i + 0.5) * k * factor)
        result.append(total)
    return result


def phash(image_bytes, size=32, low=8):
    image = Image.open(BytesIO(image_bytes)).convert("L").resize((size, size), Image.Resampling.LANCZOS)
    rows = [list(image.getdata())[i * size:(i + 1) * size] for i in range(size)]
    row_dct = [dct_1d(row) for row in rows]
    columns = []
    for x in range(size):
        columns.append(dct_1d([row_dct[y][x] for y in range(size)]))
    coeffs = []
    for y in range(low):
        for x in range(low):
            if x == 0 and y == 0:
                continue
            coeffs.append(columns[x][y])
    median = sorted(coeffs)[len(coeffs) // 2]
    bits = 0
    for coeff in coeffs:
        bits = (bits << 1) | (1 if coeff >= median else 0)
    return bits


def hamming(a, b):
    return (a ^ b).bit_count()


def fetch_all_models(base_url, token, per_page):
    models = []
    page = 1
    while True:
        query = urllib.parse.urlencode({
            "page": page,
            "per_page": per_page,
            "include_private": "true" if token else "false",
        })
        body = request_json(base_url, f"/api/models?{query}", token=token)
        models.extend(body.get("models") or [])
        pagination = body.get("pagination") or {}
        if not pagination.get("has_next"):
            return models
        page += 1


def fetch_world_references(base_url, token, known_model_ids):
    references = {}
    page = 1
    while True:
        body = request_json(base_url, f"/api/tellus/worlds?page={page}&per_page=100", token=token)
        worlds = body.get("worlds") or []
        for world in worlds:
            world_id = world.get("worldId")
            if not world_id:
                continue
            try:
                state = request_json(base_url, f"/api/tellus/worlds/{urllib.parse.quote(world_id)}/state", token=token)
            except urllib.error.HTTPError:
                continue
            for asset_id in iter_asset_ids(state):
                references.setdefault(asset_id, set()).add(world_id)
            state_text = json.dumps(state, separators=(",", ":"))
            for asset_id in set(UUID_RE.findall(state_text)):
                if asset_id in known_model_ids:
                    references.setdefault(asset_id, set()).add(world_id)
        pagination = body.get("pagination") or {}
        if not pagination.get("has_next"):
            return references
        page += 1


def tags(model):
    return [str(tag).lower() for tag in (model.get("tags") or [])]


def is_tellusish(model):
    model_tags = tags(model)
    return (
        "tellus" in model_tags
        or any(tag.startswith("tellus-world-") for tag in model_tags)
        or "hyades" in model_tags
        or str(model.get("name") or "").strip().lower().startswith("hyades")
    )


def parsed_date(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def keep_score(model, references):
    model_tags = tags(model)
    score = 0
    if model.get("id") in references:
        score += 1000
    if any(tag.startswith("tellus-world-") for tag in model_tags):
        score += 250
    if model.get("has_game_optimized"):
        score += 100
    if model.get("has_thumbnail"):
        score += 40
    if model.get("has_preview"):
        score += 20
    if model.get("ai_status") == "done" and (model.get("description") or model.get("ai_description")):
        score += 20
    if str(model.get("name") or "").strip().lower() not in {"hyades", "hyades 3d object", "pixal3d ai-generated 3d model"}:
        score += 10
    score += min(9, len(model_tags))
    score += parsed_date(model.get("upload_date")).timestamp() / 10_000_000_000
    return score


def cluster_models(models, threshold):
    candidates = [model for model in models if model.get("_phash") is not None]
    used = set()
    groups = []
    for model in candidates:
        if model["id"] in used:
            continue
        group = [model]
        used.add(model["id"])
        changed = True
        while changed:
            changed = False
            for other in candidates:
                if other["id"] in used:
                    continue
                if any(hamming(other["_phash"], member["_phash"]) <= threshold for member in group):
                    group.append(other)
                    used.add(other["id"])
                    changed = True
        if len(group) > 1 and any(is_tellusish(member) for member in group):
            groups.append(group)
    return groups


def describe_model(model, references):
    owner = (model.get("owner") or {}).get("username") or "Unknown"
    ref = ",".join(sorted(references.get(model.get("id"), []))) or "-"
    return (
        f"{model.get('id')} | {model.get('name')!r} | owner={owner} | "
        f"refs={ref} | game={bool(model.get('has_game_optimized'))} | "
        f"thumb={bool(model.get('has_thumbnail'))} | tags={model.get('tags') or []}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("ASSET_MANAGER_BASE_URL", "https://3d.flobots.xyz"))
    parser.add_argument("--token", default=os.environ.get("TELLUS_ADMIN_API_TOKEN") or os.environ.get("ASSET_MANAGER_API_TOKEN"))
    parser.add_argument("--threshold", type=int, default=4, help="pHash Hamming distance threshold; lower is stricter.")
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicates. Default is dry-run.")
    args = parser.parse_args()

    models = fetch_all_models(args.base_url, args.token, args.per_page)
    references = fetch_world_references(args.base_url, args.token, {model.get("id") for model in models})
    print(f"Fetched {len(models)} models and {len(references)} referenced asset ids.")

    hashed = 0
    for model in models:
        thumbnail_url = model.get("thumbnail_url")
        if not thumbnail_url:
            continue
        try:
            model["_phash"] = phash(request_bytes(args.base_url, thumbnail_url, token=args.token))
            hashed += 1
        except Exception as error:
            model["_phash"] = None
            print(f"[skip thumbnail] {model.get('id')}: {error}")
    print(f"Hashed {hashed} thumbnails.")

    groups = cluster_models(models, args.threshold)
    print(f"Found {len(groups)} visual duplicate group(s) at threshold <= {args.threshold}.")
    if not args.apply:
        print("Dry-run only. Re-run with --apply to delete the proposed duplicates.")

    total_delete = 0
    for index, group in enumerate(groups, start=1):
        ranked = sorted(group, key=lambda item: keep_score(item, references), reverse=True)
        keep = ranked[0]
        delete = ranked[1:]
        total_delete += len(delete)
        print(f"\nGroup {index}: keep {keep.get('id')}")
        print(f"  KEEP   {describe_model(keep, references)}")
        for model in delete:
            print(f"  DELETE {describe_model(model, references)}")
            if args.apply:
                status, body = delete_model(args.base_url, model["id"], args.token)
                print(f"         -> HTTP {status} {body[:160]}")

    print(f"\nProposed deletions: {total_delete}")
    if args.apply:
        print("Deletion run complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
