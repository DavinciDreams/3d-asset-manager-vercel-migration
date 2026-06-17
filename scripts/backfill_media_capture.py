"""Backfill browser-captured thumbnails/previews and optional AI enrichment.

The app captures media in the browser because the preview comes from WebGL.
Run this script from a machine/container with Playwright browsers installed:

    python scripts/backfill_media_capture.py --base-url https://3d.flobots.xyz --limit 50
    python scripts/backfill_media_capture.py --base-url https://3d.flobots.xyz --poll

Useful env vars:
    ASSET_CAPTURE_TOKEN       admin/service bearer token for API access
    ASSET_CAPTURE_USERNAME    browser login username/email for private detail pages
    ASSET_CAPTURE_PASSWORD    browser login password
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import urljoin


def _env_token() -> str:
    for name in (
        "ASSET_CAPTURE_TOKEN",
        "ADMIN_TASK_TOKEN",
        "TELLUS_ADMIN_API_TOKEN",
        "ASSET_MANAGER_API_TOKEN",
    ):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def _absolute(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _json_response(response, label: str) -> dict:
    if not response.ok:
        body = response.text()
        raise RuntimeError(f"{label} failed HTTP {response.status}: {body[:300]}")
    return response.json()


def _login(page, base_url: str, username: str, password: str) -> None:
    page.goto(_absolute(base_url, "/auth/login"), wait_until="networkidle")
    page.fill('input[name="login_field"]', username)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_load_state("networkidle")


def _fetch_queue(context, base_url: str, limit: int, kind: str, recapture: bool) -> list[dict]:
    recapture_value = "true" if recapture else "false"
    url = _absolute(
        base_url,
        f"/api/admin/media-capture/queue?limit={limit}&kind={kind}&recapture={recapture_value}",
    )
    data = _json_response(context.request.get(url), "queue fetch")
    return list(data.get("models") or [])


def _heartbeat(context, base_url: str, *, status: str, kind: str, count=None, captured=None, error=None) -> None:
    payload = {
        "status": status,
        "kind": kind,
        "count": count,
        "captured": captured,
        "error": str(error)[:300] if error else None,
    }
    try:
        context.request.post(
            _absolute(base_url, "/api/admin/media-capture/heartbeat"),
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        print(f"[heartbeat] failed: {exc}", file=sys.stderr)


def _model_state(context, base_url: str, model_id: str) -> dict:
    data = _json_response(
        context.request.get(_absolute(base_url, f"/api/model/{model_id}")),
        f"model fetch {model_id}",
    )
    return data.get("model") or {}


def _wait_for_media(context, base_url: str, item: dict, timeout_s: int) -> tuple[bool, dict]:
    deadline = time.time() + timeout_s
    force_capture = bool(item.get("force_capture"))
    original_thumbnail_id = item.get("thumbnail_file_id")
    original_preview_id = item.get("preview_file_id")
    model = {}
    while time.time() < deadline:
        model = _model_state(context, base_url, item["id"])
        media = model.get("media_capture") or {}
        thumbnail_id = media.get("thumbnail_file_id")
        preview_id = media.get("preview_file_id")
        thumbnail_done = (not item.get("needs_thumbnail")) or (
            model.get("has_thumbnail") and (not force_capture or thumbnail_id != original_thumbnail_id)
        )
        preview_done = (not item.get("needs_preview")) or (
            model.get("has_preview") and (not force_capture or preview_id != original_preview_id)
        )
        if thumbnail_done and preview_done:
            return True, model
        time.sleep(2)
    return False, model


def _enqueue_enrichment(context, base_url: str, model_id: str, overwrite: bool) -> None:
    payload = {
        "async": True,
        "overwrite": overwrite,
        "include_title": True,
        "include_description": True,
        "context": {"source": "media_capture_backfill"},
    }
    response = context.request.post(
        _absolute(base_url, f"/api/model/{model_id}/ai/autotag"),
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    if response.status in (200, 202):
        print(f"  enrichment queued for {model_id}")
        return
    print(f"  enrichment skipped for {model_id}: HTTP {response.status} {response.text()[:200]}")


def _process_item(page, context, base_url: str, item: dict, args) -> bool:
    model_id = item["id"]
    print(f"[capture] {model_id} {item.get('name')!r}")
    page.goto(_absolute(base_url, item["capture_url"]), wait_until="networkidle")
    ok, model = _wait_for_media(context, base_url, item, args.capture_timeout)
    if not ok:
        print(
            "  timed out waiting for media: "
            f"thumbnail={model.get('has_thumbnail')} preview={model.get('has_preview')}"
        )
        return False
    print("  media captured")
    if args.enrich and model.get("has_thumbnail"):
        ai_status = model.get("ai_status")
        if args.overwrite_enrichment or ai_status not in ("done", "processing", "pending"):
            _enqueue_enrichment(context, base_url, model_id, args.overwrite_enrichment)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("ASSET_MANAGER_BASE_URL", "http://127.0.0.1:5000"))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("MEDIA_CAPTURE_LIMIT", "50")))
    parser.add_argument(
        "--kind",
        choices=["all", "models", "animations"],
        default=os.environ.get("MEDIA_CAPTURE_KIND", "all"),
        help="Capture regular model media, animation clip media, or both.",
    )
    parser.add_argument(
        "--recapture",
        action="store_true",
        help="Queue matching items even when media already exists, replacing stale thumbnails/previews.",
    )
    parser.add_argument("--poll", action="store_true", help="Keep draining the queue for future uploads.")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("MEDIA_CAPTURE_INTERVAL", "60")), help="Seconds between poll cycles.")
    parser.add_argument("--capture-timeout", type=int, default=int(os.environ.get("MEDIA_CAPTURE_TIMEOUT", "45")))
    parser.add_argument("--show", action="store_true", help="Run a visible browser instead of headless.")
    parser.add_argument("--enrich", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--overwrite-enrichment", action="store_true")
    parser.add_argument("--username", default=os.environ.get("ASSET_CAPTURE_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("ASSET_CAPTURE_PASSWORD", ""))
    parser.add_argument("--token", default=_env_token())
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is required: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    if not headers and not (args.username and args.password):
        print("Set ASSET_CAPTURE_TOKEN or ASSET_CAPTURE_USERNAME/PASSWORD.", file=sys.stderr)
        return 2

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.show)
        context = browser.new_context(extra_http_headers=headers)
        page = context.new_page()
        if args.username and args.password:
            _login(page, args.base_url, args.username, args.password)

        try:
            while True:
                try:
                    items = _fetch_queue(context, args.base_url, args.limit, args.kind, args.recapture)
                except Exception as exc:
                    print(f"[queue] fetch failed: {exc}", file=sys.stderr)
                    _heartbeat(
                        context,
                        args.base_url,
                        status="queue_fetch_failed",
                        kind=args.kind,
                        error=exc,
                    )
                    if not args.poll:
                        return 1
                    time.sleep(args.interval)
                    continue
                if not items:
                    print("[queue] empty")
                    _heartbeat(
                        context,
                        args.base_url,
                        status="empty",
                        kind=args.kind,
                        count=0,
                        captured=0,
                    )
                    if not args.poll:
                        break
                    time.sleep(args.interval)
                    continue

                ok_count = 0
                for item in items:
                    try:
                        if _process_item(page, context, args.base_url, item, args):
                            ok_count += 1
                    except Exception as exc:
                        print(f"  failed {item.get('id')}: {exc}")
                print(f"[cycle] captured {ok_count}/{len(items)}")
                _heartbeat(
                    context,
                    args.base_url,
                    status="captured",
                    kind=args.kind,
                    count=len(items),
                    captured=ok_count,
                )

                if not args.poll:
                    break
                time.sleep(args.interval)
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
