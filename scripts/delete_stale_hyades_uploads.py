"""Delete stale Lisa/Hyades direct uploads that duplicate Tellus world assets.

Dry-run by default. This targets the duplicate shape created by the old direct
Pixel3D/Hyades route:

- owner username is lisa by default
- name is generic Hyades/Hyades 3D Object, or tags include hyades
- no tellus-world-* tag

It deliberately does not delete world-tagged Tellus assets or non-Hyades Gradio
uploads.

Examples:
    python scripts/delete_stale_hyades_uploads.py --base-url https://3d.flobots.xyz
    python scripts/delete_stale_hyades_uploads.py --base-url https://3d.flobots.xyz --token %TELLUS_ADMIN_API_TOKEN% --apply
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request


def request_json(base_url, path, token=None, retries=3):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base_url.rstrip("/") + path, headers=headers)
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.load(response)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(attempt)


def delete_model(base_url, model_id, token):
    if not token:
        raise RuntimeError("Deleting requires --token or TELLUS_ADMIN_API_TOKEN/ASSET_MANAGER_API_TOKEN.")
    req = urllib.request.Request(
        base_url.rstrip("/") + f"/api/model/{urllib.parse.quote(model_id)}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=45) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def fetch_models(base_url, token, per_page):
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


def model_tags(model):
    return [str(tag or "").strip().lower() for tag in (model.get("tags") or [])]


def owner_username(model):
    return str(((model.get("owner") or {}).get("username")) or "Unknown").strip().lower()


def is_stale_hyades(model, owner):
    tags = model_tags(model)
    if owner_username(model) != owner.lower():
        return False
    if any(tag.startswith("tellus-world-") for tag in tags):
        return False
    name = str(model.get("name") or "").strip().lower()
    generic_name = name in {"hyades", "hyades 3d object"}
    return generic_name or "hyades" in tags


def describe(model):
    return (
        f"{model.get('id')} | {model.get('name')!r} | owner={owner_username(model)} | "
        f"tags={model.get('tags') or []} | game={bool(model.get('has_game_optimized'))} | "
        f"thumb={bool(model.get('has_thumbnail'))} | uploaded={model.get('upload_date')}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("ASSET_MANAGER_BASE_URL", "https://3d.flobots.xyz"))
    parser.add_argument("--token", default=os.environ.get("TELLUS_ADMIN_API_TOKEN") or os.environ.get("ASSET_MANAGER_API_TOKEN"))
    parser.add_argument("--owner", default="lisa")
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    models = fetch_models(args.base_url, args.token, args.per_page)
    candidates = [model for model in models if is_stale_hyades(model, args.owner)]

    print(f"Fetched {len(models)} models.")
    print(f"Matched {len(candidates)} stale Hyades upload(s) for owner={args.owner!r}.")
    if not args.apply:
        print("Dry-run only. Re-run with --apply to delete these records.")

    deleted = failed = 0
    for model in candidates:
        print(("DELETE " if args.apply else "WOULD  ") + describe(model))
        if args.apply:
            try:
                status, body = delete_model(args.base_url, model["id"], args.token)
                print(f"       -> HTTP {status} {body[:160]}")
                deleted += 1
            except Exception as error:
                print(f"       -> FAILED {error}")
                failed += 1

    if args.apply:
        print(f"Done. deleted={deleted} failed={failed}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
