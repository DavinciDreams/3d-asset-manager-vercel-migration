"""AI-assisted metadata enrichment for uploaded 3D assets."""
import base64
import json
import os
import re
import urllib.error
import urllib.request

from app.models import Model3D


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_DEFAULT_MODEL = "glm-5.1"


def _tokens(*parts):
    raw = " ".join(str(part or "") for part in parts)
    raw = re.sub(r"[^A-Za-z0-9]+", " ", raw).lower()
    return [p for p in raw.split() if len(p) > 2]


def _heuristic_metadata(model):
    words = _tokens(model.name, model.original_filename, model.description)
    tags = []
    for word in words:
        if word not in tags:
            tags.append(word)
    fmt = (model.file_format or "").lower()
    if fmt and fmt not in tags:
        tags.append(fmt)
    if fmt in {"glb", "gltf", "fbx", "vrm"}:
        tags.append("3d-model")
    if model.approve_game_ready:
        tags.append("game-ready")
    if model.approve_asset_store:
        tags.append("asset-store")
    clean_name = model.name
    if not clean_name and model.original_filename:
        clean_name = model.original_filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ")
    clean_name = clean_name or "asset"
    title = " ".join(word.capitalize() for word in clean_name.split())
    description = model.description or (
        f"{clean_name} is a {fmt.upper() if fmt else '3D'} asset prepared for cataloging, "
        "preview, and downstream packaging."
    )
    return {
        "title": title,
        "asset_category": None,
        "asset_styles": [],
        "asset_types": ["game-ready"] if model.approve_game_ready else [],
        "tags": Model3D.normalize_tags(tags[:12]),
        "description": description,
        "summary": description[:180],
        "categories": [],
        "quality_notes": [],
    }


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _provider():
    return os.environ.get("AI_AUTOTAG_PROVIDER", os.environ.get("AI_PROVIDER", "openai")).strip().lower()


def _api_key():
    return (
        os.environ.get("AI_AUTOTAG_API_KEY")
        or os.environ.get("AI_API_KEY")
        or os.environ.get("ZAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _base_url(provider):
    configured = os.environ.get("AI_AUTOTAG_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    if configured:
        return configured.strip().rstrip("/")
    if provider == "zai":
        return ZAI_CODING_BASE_URL
    return DEFAULT_BASE_URL


def _model_name(provider):
    return (
        os.environ.get("AI_AUTOTAG_MODEL")
        or os.environ.get("OPENAI_AUTOTAG_MODEL")
        or (ZAI_DEFAULT_MODEL if provider == "zai" else DEFAULT_MODEL)
    )


def _request_url(base_url):
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _strip_json_fence(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_chat_output(payload):
    choices = payload.get("choices") or []
    if not choices:
        return ""
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _image_part(model):
    if not _env_bool("AI_AUTOTAG_USE_VISION", True):
        return None
    if not model.thumbnail_file_id:
        return None
    try:
        stored = model._read_stored_file(model.thumbnail_file_id)
    except Exception:
        return None
    if not stored:
        return None
    max_bytes = int(os.environ.get("AI_AUTOTAG_MAX_IMAGE_BYTES", str(2 * 1024 * 1024)))
    if len(stored) > max_bytes:
        return None
    content_type = "image/webp"
    data_url = f"data:{content_type};base64,{base64.b64encode(stored).decode('ascii')}"
    return {
        "type": "image_url",
        "image_url": {"url": data_url},
    }


def _ai_metadata(model, extra_context=None):
    provider = _provider()
    api_key = _api_key()
    if not api_key:
        return None

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 16,
            },
            "title": {
                "type": "string",
                "description": "A short descriptive catalog title, not just the filename.",
            },
            "asset_category": {
                "type": "string",
                "description": "One broad what-it-is bucket such as flora, fauna, building, person, prop, vehicle, environment, animation, material, or other. Use other when uncertain.",
            },
            "asset_styles": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
                "description": "Art direction or genre labels such as fantasy, sci-fi, medieval, modern, stylized, realistic, low-poly, cozy, horror, cyberpunk.",
            },
            "asset_types": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Technical/use traits such as rigged, animated, game-ready, modular, pbr, tileable, vrm, optimized.",
            },
            "description": {"type": "string"},
            "summary": {"type": "string"},
            "categories": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "quality_notes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
        "required": [
            "title",
            "asset_category",
            "asset_styles",
            "asset_types",
            "tags",
            "description",
            "summary",
            "categories",
            "quality_notes",
        ],
    }
    prompt = {
        "asset": {
            "name": model.name,
            "description": model.description,
            "original_filename": model.original_filename,
            "file_format": model.file_format,
            "file_size": model.file_size,
            "existing_tags": model.tags,
            "asset_category": model.asset_category,
            "asset_styles": model.asset_styles,
            "asset_types": model.asset_types,
            "approve_game_ready": model.approve_game_ready,
            "approve_asset_store": model.approve_asset_store,
            "conversion_status": model.conversion_status,
        },
        "extra_context": extra_context or {},
    }
    user_text = (
        "Create marketplace metadata for this 3D asset. Return only JSON that matches the schema. "
        "Prefer concrete visible or file-derived details over generic filler.\n\n"
        + json.dumps(prompt, sort_keys=True)
    )
    content = user_text
    image = _image_part(model)
    if image:
        content = [{"type": "text", "text": user_text}, image]
    body = {
        "model": _model_name(provider),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You enrich 3D asset store records. Return concise JSON only. "
                    "Tags should be lowercase marketplace/search tags, not sentences. "
                    "Descriptions should help a human understand what the asset is and how it may be used."
                ),
            },
            {"role": "user", "content": content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "asset_enrichment",
                "strict": True,
                "schema": schema,
            },
        },
    }
    request = urllib.request.Request(
        _request_url(_base_url(provider)),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        timeout = int(os.environ.get("AI_AUTOTAG_TIMEOUT", os.environ.get("OPENAI_AUTOTAG_TIMEOUT", "30")))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"AI enrichment failed ({error.code}): {detail}") from error

    output_text = _strip_json_fence(_extract_chat_output(payload))
    if not output_text:
        raise RuntimeError("AI enrichment returned no output text.")
    enriched = json.loads(output_text)
    enriched["provider"] = provider
    enriched["base_url"] = _base_url(provider)
    enriched["model"] = body["model"]
    enriched["response_id"] = payload.get("id")
    return enriched


def enrich_model(model, extra_context=None):
    enriched = _ai_metadata(model, extra_context=extra_context)
    if enriched is None:
        enriched = _heuristic_metadata(model)
        enriched["provider"] = "heuristic"
    enriched["title"] = (enriched.get("title") or "").strip()
    enriched["asset_category"] = Model3D.normalize_category(enriched.get("asset_category"))
    enriched["asset_styles"] = Model3D.normalize_tags(enriched.get("asset_styles", []))
    enriched["asset_types"] = Model3D.normalize_tags(enriched.get("asset_types", []))
    enriched["tags"] = Model3D.normalize_tags(enriched.get("tags", []))
    enriched["description"] = (enriched.get("description") or "").strip()
    enriched["summary"] = (enriched.get("summary") or "").strip()
    enriched.setdefault("categories", [])
    enriched.setdefault("quality_notes", [])
    return enriched
