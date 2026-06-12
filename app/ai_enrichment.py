"""AI-assisted metadata enrichment for uploaded 3D assets."""
import base64
import json
import os
import re
import socket
import uuid
import urllib.error
import urllib.parse
import urllib.request

from app.models import Model3D


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_DEFAULT_MODEL = "glm-5.1"
HYADES_A2A_BASE_URL = "https://hyades.gnostr.cloud/a2a"
HYADES_OPENAI_BASE_URL = "https://hyades.gnostr.cloud/v1"
HYADES_DEFAULT_MODEL = "holo"
DEFAULT_USER_AGENT = "3d-asset-manager/1.0 (+https://github.com/DavinciDreams/3d-asset-manager-vercel-migration)"


class AIProviderTransientError(RuntimeError):
    """Raised for provider failures that may succeed on a lighter retry."""


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
    light_words = {"lantern", "lamp", "torch", "candle", "fire", "flame", "brazier", "sconce", "glow", "crystal"}
    emits_light = any(word in light_words for word in words)
    return {
        "title": title,
        "asset_category": None,
        "asset_styles": [],
        "asset_types": Model3D.normalize_tags((["game-ready"] if model.approve_game_ready else []) + (["light-emitter"] if emits_light else [])),
        "runtime_metadata": _default_runtime_metadata(emits_light),
        "tags": Model3D.normalize_tags(tags[:12]),
        "description": description,
        "summary": description[:180],
        "categories": [],
        "quality_notes": [],
    }


def _default_runtime_metadata(emits_light=False):
    if emits_light:
        return {
            "behaviors": ["light-emitter"],
            "light": {
                "enabled": True,
                "type": "point",
                "color": "#ffb35a",
                "intensity": 1.5,
                "range": 8,
                "cast_shadow": True,
                "attach_to": "",
                "offset": [0, 0.6, 0],
            },
        }
    return {
        "behaviors": [],
        "light": {
            "enabled": False,
            "type": "none",
            "color": "#ffffff",
            "intensity": 0,
            "range": 0,
            "cast_shadow": False,
            "attach_to": "",
            "offset": [0, 0, 0],
        },
    }


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _provider():
    return (os.environ.get("AI_AUTOTAG_PROVIDER") or os.environ.get("AI_PROVIDER") or "openai").strip().lower()


def _api_key():
    return (
        os.environ.get("AI_AUTOTAG_API_KEY")
        or os.environ.get("AI_API_KEY")
        or os.environ.get("HYADES_AUTOTAG_API_KEY")
        or os.environ.get("HYADES_VISION_API_KEY")
        or os.environ.get("HYADES_API_KEY")
        or os.environ.get("ZAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _base_url(provider):
    configured = (
        os.environ.get("AI_AUTOTAG_BASE_URL")
        or (os.environ.get("HYADES_AUTOTAG_BASE_URL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_VISION_BASE_URL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_BASE_URL") if provider == "hyades" else None)
        or os.environ.get("OPENAI_BASE_URL")
    )
    if configured:
        return configured.strip().rstrip("/")
    if provider == "zai":
        return ZAI_CODING_BASE_URL
    if provider == "hyades":
        if _transport(provider) == "a2a":
            return HYADES_A2A_BASE_URL
        return HYADES_OPENAI_BASE_URL
    return DEFAULT_BASE_URL


def _model_name(provider):
    return (
        os.environ.get("AI_AUTOTAG_MODEL")
        or (os.environ.get("HYADES_AUTOTAG_MODEL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_VISION_MODEL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_MODEL") if provider == "hyades" else None)
        or os.environ.get("OPENAI_AUTOTAG_MODEL")
        or (ZAI_DEFAULT_MODEL if provider == "zai" else HYADES_DEFAULT_MODEL if provider == "hyades" else DEFAULT_MODEL)
    )


def _transport(provider):
    configured = (
        os.environ.get("AI_AUTOTAG_TRANSPORT")
        or (os.environ.get("HYADES_AUTOTAG_TRANSPORT") if provider == "hyades" else None)
        or (os.environ.get("HYADES_TRANSPORT") if provider == "hyades" else None)
        or ""
    ).strip().lower()
    if configured:
        return configured
    return "a2a" if provider == "hyades" else "openai"


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


def _extract_part_text(part):
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    if isinstance(part.get("text"), str):
        return part["text"]
    data = part.get("data")
    if data is not None:
        return json.dumps(data)
    file_part = part.get("file")
    if isinstance(file_part, dict) and isinstance(file_part.get("text"), str):
        return file_part["text"]
    return ""


def _extract_a2a_output(payload):
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") or "A2A request failed"
        raise RuntimeError(f"AI enrichment failed ({error.get('code')}): {message}")
    result = payload.get("result") or payload
    candidates = []
    message = result.get("message") if isinstance(result, dict) else None
    if isinstance(message, dict):
        candidates.extend(message.get("parts") or [])
    task = result.get("task") if isinstance(result, dict) else None
    if isinstance(task, dict):
        for artifact in task.get("artifacts") or []:
            if isinstance(artifact, dict):
                candidates.extend(artifact.get("parts") or [])
        status = task.get("status") or {}
        status_message = status.get("message") if isinstance(status, dict) else None
        if isinstance(status_message, dict):
            candidates.extend(status_message.get("parts") or [])
    parts = [_extract_part_text(part) for part in candidates]
    return "\n".join(part for part in parts if part).strip()


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


def _a2a_image_part(model):
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
    return {
        "kind": "file",
        "file": {
            "bytes": base64.b64encode(stored).decode("ascii"),
            "name": "thumbnail.webp",
            "mimeType": "image/webp",
        },
    }


def _compact_response_detail(detail):
    detail = re.sub(r"<[^>]+>", " ", detail or "")
    detail = re.sub(r"\s+", " ", detail).strip()
    return detail[:500]


def _provider_label(url, provider=None, transport=None):
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or parsed.path or "unknown endpoint"
    label = provider or "ai provider"
    if transport:
        label = f"{label}/{transport}"
    return f"{label} at {host}"


def _is_transient_provider_failure(status_code, detail):
    if status_code in {502, 503, 504, 520, 522, 524}:
        return True
    lowered = (detail or "").lower()
    return "origin web server returned an invalid or incomplete response" in lowered


def _request_headers(headers):
    merged = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": os.environ.get("AI_AUTOTAG_USER_AGENT", DEFAULT_USER_AGENT),
    }
    merged.update(headers or {})
    return merged


def _post_json(url, body, headers, provider=None, transport=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=_request_headers(headers),
        method="POST",
    )
    label = _provider_label(url, provider=provider, transport=transport)
    try:
        timeout = int(os.environ.get("AI_AUTOTAG_TIMEOUT", os.environ.get("OPENAI_AUTOTAG_TIMEOUT", "30")))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as error:
                detail = _compact_response_detail(raw)
                raise RuntimeError(
                    f"AI enrichment returned invalid JSON from {label} (HTTP {response.status}). "
                    f"Response: {detail or 'empty response'}"
                ) from error
    except urllib.error.HTTPError as error:
        detail = _compact_response_detail(error.read().decode("utf-8", errors="ignore"))
        message = (
            f"AI enrichment provider request failed for {label} "
            f"(HTTP {error.code}). Response: {detail or error.reason or 'empty response'}"
        )
        if _is_transient_provider_failure(error.code, detail):
            raise AIProviderTransientError(message) from error
        raise RuntimeError(message) from error
    except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
        reason = getattr(error, "reason", error)
        raise AIProviderTransientError(
            f"AI enrichment provider request failed for {label}: {reason}"
        ) from error


def _a2a_metadata(model, provider, api_key, schema, user_text):
    text_part = {
        "kind": "text",
        "text": (
            "You enrich 3D asset store records. Return concise JSON only. "
            "Tags should be lowercase marketplace/search tags, not sentences. "
            "Descriptions should help a human understand what the asset is and how it may be used.\n\n"
            + user_text
            + "\n\nJSON schema:\n"
            + json.dumps(schema, sort_keys=True)
        ),
    }
    image_part = _a2a_image_part(model)

    def build_body(include_image=True):
        parts = [text_part]
        if include_image and image_part:
            parts.append(image_part)
        return {
            "jsonrpc": "2.0",
            "id": f"asset-enrichment-{uuid.uuid4()}",
            "method": os.environ.get("HYADES_A2A_METHOD", "message/send"),
            "params": {
                "message": {
                    "role": os.environ.get("HYADES_A2A_ROLE", "user"),
                    "parts": parts,
                    "messageId": f"asset-enrichment-{uuid.uuid4()}",
                },
                "metadata": {
                    "model": _model_name(provider),
                },
                "configuration": {
                    "acceptedOutputModes": ["application/json", "text/plain"],
                },
            },
        }

    vision_fallback = False
    try:
        payload = _post_json(
            _base_url(provider),
            build_body(include_image=True),
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            provider=provider,
            transport="a2a",
        )
    except AIProviderTransientError:
        if not image_part or not _env_bool("AI_AUTOTAG_RETRY_TEXT_ONLY", True):
            raise
        vision_fallback = True
        payload = _post_json(
            _base_url(provider),
            build_body(include_image=False),
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            provider=provider,
            transport="a2a-text",
        )
    output_text = _strip_json_fence(_extract_a2a_output(payload))
    if not output_text:
        raise RuntimeError("AI enrichment returned no A2A output text.")
    enriched = json.loads(output_text)
    enriched["provider"] = provider
    enriched["transport"] = "a2a"
    enriched["base_url"] = _base_url(provider)
    enriched["model"] = _model_name(provider)
    enriched["response_id"] = payload.get("id")
    enriched["vision_fallback"] = vision_fallback
    return enriched


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
                "description": "Technical/use traits such as rigged, animated, game-ready, modular, pbr, tileable, vrm, optimized, light-emitter.",
            },
            "runtime_metadata": {
                "type": "object",
                "additionalProperties": False,
                "description": "Runtime hints for Tellus/Three.js. Be conservative: only enable light when the asset is clearly a lantern, lamp, torch, fire, candle, glowing crystal, or similar emitter.",
                "properties": {
                    "behaviors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 8,
                    },
                    "light": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "type": {"type": "string", "enum": ["none", "point", "spot", "directional", "ambient"]},
                            "color": {"type": "string", "description": "Hex color such as #ffb35a."},
                            "intensity": {"type": "number"},
                            "range": {"type": "number"},
                            "cast_shadow": {"type": "boolean"},
                            "attach_to": {"type": "string", "description": "Optional GLB node name to attach the light to, empty when unknown."},
                            "offset": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                        },
                        "required": ["enabled", "type", "color", "intensity", "range", "cast_shadow", "attach_to", "offset"],
                    },
                },
                "required": ["behaviors", "light"],
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
            "runtime_metadata",
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
            "runtime_metadata": model.runtime_metadata,
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
    if _transport(provider) == "a2a":
        return _a2a_metadata(model, provider, api_key, schema, user_text)
    def build_body(content):
        return {
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

    content = user_text
    image = _image_part(model)
    if image:
        content = [{"type": "text", "text": user_text}, image]

    body = build_body(content)
    vision_fallback = False
    try:
        payload = _post_json(
            _request_url(_base_url(provider)),
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            provider=provider,
            transport="openai",
        )
    except AIProviderTransientError:
        if not image or not _env_bool("AI_AUTOTAG_RETRY_TEXT_ONLY", True):
            raise
        vision_fallback = True
        body = build_body(user_text)
        payload = _post_json(
            _request_url(_base_url(provider)),
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            provider=provider,
            transport="openai-text",
        )

    output_text = _strip_json_fence(_extract_chat_output(payload))
    if not output_text:
        raise RuntimeError("AI enrichment returned no output text.")
    enriched = json.loads(output_text)
    enriched["provider"] = provider
    enriched["transport"] = "openai"
    enriched["base_url"] = _base_url(provider)
    enriched["model"] = body["model"]
    enriched["response_id"] = payload.get("id")
    enriched["vision_fallback"] = vision_fallback
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
    enriched["runtime_metadata"] = Model3D.normalize_runtime_metadata(
        enriched.get("runtime_metadata", _default_runtime_metadata(False))
    )
    enriched["tags"] = Model3D.normalize_tags(enriched.get("tags", []))
    enriched["description"] = (enriched.get("description") or "").strip()
    enriched["summary"] = (enriched.get("summary") or "").strip()
    enriched.setdefault("categories", [])
    enriched.setdefault("quality_notes", [])
    return enriched
