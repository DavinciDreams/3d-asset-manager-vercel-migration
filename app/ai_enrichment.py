"""AI-assisted metadata enrichment for uploaded 3D assets."""
import base64
import io
import json
import os
import re
import queue
import shlex
import socket
import subprocess
import time
import tempfile
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request

from app.models import Model3D


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4.1-mini"
ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_DEFAULT_MODEL = "glm-5.1"
ZAI_MCP_COMMAND = "npx -y @z_ai/mcp-server@latest"
HYADES_A2A_BASE_URL = "https://hyades.gnostr.cloud/a2a"
HYADES_OPENAI_BASE_URL = "https://hyades.gnostr.cloud/v1"
HYADES_DEFAULT_MODEL = "holo"
DEFAULT_USER_AGENT = "3d-asset-manager/1.0 (+https://github.com/DavinciDreams/3d-asset-manager-vercel-migration)"
NOISY_ENRICHMENT_TAGS = {
    "3d", "3d-asset", "3d-model", "ai", "ai-generated", "asset", "generated", "generation",
    "glb", "gltf", "fbx", "obj", "stl", "vrm", "image-to-3d", "model", "pixal3d",
}
FAB_LISTING_GUIDANCE = (
    "Write descriptions as Fab marketplace product descriptions for a 3D listing. "
    "Use polished buyer-facing copy, not analysis notes. Start with what the asset is, "
    "then mention visual style/materials, likely use cases, and any visible limitations. "
    "Do not mention Pixal3D, AI pipeline, provider names, polygon counts, texture resolution, "
    "thumbnail availability, or uncertain technical claims unless those facts are directly supplied "
    "and useful to a buyer. Keep the title under 80 characters. Return up to 25 discoverability tags."
)


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


def _contains_any(text, words):
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


def _keyword_score(text, words):
    lowered = (text or "").lower()
    return sum(1 for word in words if word in lowered)


def _clean_enrichment_tags(tags):
    return [
        tag for tag in Model3D.normalize_tags(tags)
        if tag not in NOISY_ENRICHMENT_TAGS
    ]


def _vision_denies_light(text):
    lowered = (text or "").lower()
    denial_patterns = (
        "not function as a light emitter",
        "does not function as a light emitter",
        "not a light emitter",
        "does not emit light",
        "do not emit light",
        "no glowing elements",
        "no emissive",
        "no indications that it is designed to cast light",
    )
    return any(pattern in lowered for pattern in denial_patterns)


def _generic_description(description):
    lowered = (description or "").lower()
    generic_fragments = (
        "ai-generated 3d model",
        "produced via pixal3d",
        "image-to-3d pipeline",
        "specific visual subject matter cannot be determined",
        "no thumbnail is available",
        "baseline for rendering",
        "starting point for further 3d sculpting",
    )
    return not lowered.strip() or any(fragment in lowered for fragment in generic_fragments)


def _vision_subject_metadata(text):
    lowered = (text or "").lower()
    if "fountain" in lowered:
        tags = ["fountain", "water-feature", "decorative-prop"]
        if _contains_any(lowered, ("classical", "ornate", "antique", "historical")):
            tags.extend(["classical", "ornate", "antique"])
        if _contains_any(lowered, ("stone", "marble", "weathered")):
            tags.extend(["stone", "marble", "weathered"])
        if "two-tiered" in lowered or "two tiered" in lowered:
            tags.append("two-tiered")
        title_parts = []
        if _contains_any(lowered, ("classical", "ornate")):
            title_parts.append("Classical")
        if _contains_any(lowered, ("stone", "marble")):
            title_parts.append("Stone")
        title_parts.append("Fountain")
        return {
            "title": " ".join(title_parts),
            "asset_category": "environment",
            "tags": tags,
            "asset_types": ["static", "decorative-prop"],
            "description": (
                "A classical two-tiered fountain with stacked circular basins, an octagonal base, "
                "and weathered stone or marble-like material. Suitable as a decorative water feature "
                "for architectural visualization, historical scenes, gardens, courtyards, or environment dressing."
            ),
            "summary": "Classical two-tiered stone fountain for decorative environment scenes.",
        }
    return {}


def _resolve_style_conflicts(styles, text):
    styles = Model3D.normalize_tags(styles)
    if "painted" in styles and "painterly" in styles:
        styles = [style for style in styles if style != "painted"]
    stylized_family = {"stylized", "cartoon", "painted", "painterly", "watercolor", "hand-painted"}
    has_stylized_family = any(style in styles for style in stylized_family)
    if "realistic" in styles and has_stylized_family and not _contains_any(text, ("photorealistic", "lifelike", "real-world scan")):
        styles = [style for style in styles if style != "realistic"]
    return styles[:6]


def _resolve_type_conflicts(asset_types, category, text):
    normalized = []
    for asset_type in Model3D.normalize_tags(asset_types):
        if asset_type == "static-mesh":
            asset_type = "static"
        if asset_type not in normalized:
            normalized.append(asset_type)

    category_terms = {
        "building", "flora", "fauna", "person", "people", "vehicle", "environment", "material",
        "animation", "other", "prop", "props", "3d-model", "3d-models",
    }
    normalized = [item for item in normalized if item not in category_terms and item != category]

    if "static" in normalized:
        normalized = [item for item in normalized if item not in {"rigged", "animated"}]
    if "high-poly" in normalized and "low-poly" in normalized:
        high_score = _keyword_score(text, ("high-poly", "high poly", "100,000", "100000", "dense", "high detail"))
        low_score = _keyword_score(text, ("low-poly", "low poly", "optimized", "simple mesh"))
        remove = "high-poly" if low_score > high_score else "low-poly"
        normalized = [item for item in normalized if item != remove]
    return normalized[:8]


def _infer_missing_facets(enriched):
    text = " ".join([
        str(enriched.get("title") or ""),
        str(enriched.get("description") or ""),
        str(enriched.get("summary") or ""),
        str(enriched.get("vision_mcp_analysis") or ""),
        " ".join(str(tag) for tag in enriched.get("tags") or []),
    ]).lower()
    vision_text = str(enriched.get("vision_mcp_analysis") or "")
    vision_subject = _vision_subject_metadata(vision_text)
    if vision_subject:
        if _contains_any(str(enriched.get("title") or ""), ("unknown", "ai-generated 3d model", "generated 3d model", "pixal3d", "3d model", "untitled", "asset")):
            enriched["title"] = vision_subject["title"]
        if _generic_description(enriched.get("description")):
            enriched["description"] = vision_subject["description"]
            enriched["summary"] = vision_subject["summary"]
        enriched["tags"] = _clean_enrichment_tags((enriched.get("tags") or []) + vision_subject.get("tags", []))
        enriched["asset_types"] = Model3D.normalize_tags((enriched.get("asset_types") or []) + vision_subject.get("asset_types", []))
        if Model3D.normalize_category(enriched.get("asset_category")) in {None, "", "other", "prop", "props", "3d model", "3d models", "uncategorized"}:
            enriched["asset_category"] = vision_subject["asset_category"]

    category = Model3D.normalize_category(enriched.get("asset_category"))
    generic_categories = {None, "", "other", "prop", "props", "3d model", "3d models", "uncategorized"}
    category_rules = [
        ("building", ("building", "house", "cottage", "tower", "castle", "temple", "hut", "cabin", "wall", "roof", "architecture", "timber-framed", "half-timbered", "tudor", "cupola", "balcony")),
        ("flora", ("flower", "flowers", "floral", "bouquet", "bloom", "blooms", "leaf", "leaves", "stem", "stems", "plant", "plants", "tree", "trees", "grass", "moss", "vine", "vines")),
        ("fauna", ("animal", "creature", "bird", "fish", "insect", "horse", "cat", "dog", "wolf", "dragon")),
        ("person", ("person", "human", "character", "humanoid", "man", "woman", "figure")),
        ("vehicle", ("vehicle", "car", "truck", "ship", "boat", "aircraft", "spaceship", "wagon")),
        ("environment", ("terrain", "landscape", "scene", "environment", "diorama", "level")),
        ("material", ("material", "texture", "tileable", "surface", "fabric", "shader")),
    ]
    category_scores = [(candidate, _keyword_score(text, words)) for candidate, words in category_rules]
    best_category, best_score = max(category_scores, key=lambda item: item[1])
    current_score = next((score for candidate, score in category_scores if candidate == category), 0)
    protected_category = Model3D.normalize_category(vision_subject.get("asset_category")) if vision_subject else None
    if protected_category and category == protected_category:
        enriched["asset_category"] = protected_category
        category = protected_category
    elif best_score > 0 and (
        category in generic_categories
        or (best_category != category and best_score >= max(2, current_score + 2))
    ):
        enriched["asset_category"] = best_category
        category = best_category
    styles = Model3D.normalize_tags(enriched.get("asset_styles", []))
    style_rules = {
        "watercolor": ("watercolor", "translucent wash", "color blending"),
        "painterly": ("painterly", "painted", "hand-painted"),
        "stylized": ("stylized", "non-photorealistic", "illustrative"),
        "fantasy": ("fantasy", "magical", "enchanted"),
        "realistic": ("realistic", "photorealistic", "lifelike"),
        "low-poly": ("low-poly", "low poly"),
    }
    for style, words in style_rules.items():
        if style not in styles and _contains_any(text, words):
            styles.append(style)
    enriched["asset_styles"] = _resolve_style_conflicts(styles, text)

    asset_types = Model3D.normalize_tags(enriched.get("asset_types", []))
    type_rules = {
        "static": ("static", "not rigged", "not animated", "no visible joints", "single pose"),
        "rigged": ("rigged", "skeleton", "armature", "joints"),
        "animated": ("animated", "animation", "keyframes"),
        "light-emitter": ("light emitter", "emits light", "glowing", "lantern", "lamp", "torch", "candle"),
        "decorative-prop": ("decorative", "prop", "decoration", "ornamental"),
        "pbr": ("pbr", "physically based"),
        "high-poly": ("high-poly", "high poly"),
        "low-poly": ("low-poly", "low poly"),
    }
    for asset_type, words in type_rules.items():
        if asset_type not in asset_types and _contains_any(text, words):
            asset_types.append(asset_type)
    if _vision_denies_light(text):
        asset_types = [item for item in asset_types if item != "light-emitter"]
        runtime = Model3D.normalize_runtime_metadata(enriched.get("runtime_metadata", {}))
        runtime["behaviors"] = [item for item in runtime.get("behaviors", []) if item != "light-emitter"]
        runtime["light"] = {
            "enabled": False,
            "type": "none",
            "color": "#ffffff",
            "intensity": 0,
            "range": 0,
            "cast_shadow": False,
            "attach_to": "",
            "offset": [0, 0, 0],
        }
        enriched["runtime_metadata"] = runtime
    if "static" in asset_types:
        asset_types = [item for item in asset_types if item not in {"rigged", "animated"}]
    enriched["asset_types"] = _resolve_type_conflicts(asset_types, category, text)

    title = str(enriched.get("title") or "").strip()
    generic_title = _contains_any(
        title,
        ("ai-generated 3d model", "generated 3d model", "pixal3d", "3d model", "untitled", "asset"),
    )
    if generic_title and _contains_any(text, ("flower", "flowers", "floral", "bouquet", "bloom", "blooms")):
        title_parts = []
        for style in ("watercolor", "painterly", "stylized"):
            if style in styles:
                title_parts.append(style)
                break
        title_parts.append("floral arrangement" if _contains_any(text, ("arrangement", "bouquet")) else "flowers")
        enriched["title"] = " ".join(title_parts).title()
    elif generic_title and category and category not in generic_categories:
        subject = category
        skip_title_tags = {
            "glb", "gltf", "fbx", "3d-model", "ai-generated", "static", "stylized", "fantasy",
            "storybook", "medieval", "painterly", "watercolor", "environment-prop", "decorative-prop",
        }
        preferred_subject_tags = {
            "house", "cottage", "tower", "castle", "building", "lantern", "lamp", "flower", "flowers",
            "bouquet", "tree", "character", "vehicle",
        }
        tags = Model3D.normalize_tags(enriched.get("tags", []))
        for tag in tags:
            if tag in preferred_subject_tags:
                subject = tag
                break
        else:
            for tag in tags:
                if tag not in skip_title_tags:
                    subject = tag
                    break
        enriched["title"] = subject.replace("-", " ").title()
    return enriched


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
        or os.environ.get("Z_AI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _is_hyades_holo_model(provider):
    return provider == "hyades" and _model_name(provider).strip().lower() == "holo"


def _base_url(provider):
    configured = (
        os.environ.get("AI_AUTOTAG_BASE_URL")
        or (os.environ.get("HYADES_AUTOTAG_BASE_URL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_VISION_BASE_URL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_BASE_URL") if provider == "hyades" else None)
        or os.environ.get("OPENAI_BASE_URL")
    )
    if _is_hyades_holo_model(provider):
        if configured and _looks_like_a2a_url(configured.strip().rstrip("/")):
            return configured.strip().rstrip("/")
        return HYADES_A2A_BASE_URL
    if configured:
        return configured.strip().rstrip("/")
    if provider == "zai":
        return ZAI_CODING_BASE_URL
    if provider == "hyades":
        if _transport(provider) == "a2a":
            return HYADES_A2A_BASE_URL
        return HYADES_OPENAI_BASE_URL
    return DEFAULT_BASE_URL


def _looks_like_a2a_url(url):
    parsed = urllib.parse.urlparse(url or "")
    return parsed.path.rstrip("/").endswith("/a2a")


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
    base_url = (
        os.environ.get("AI_AUTOTAG_BASE_URL")
        or (os.environ.get("HYADES_AUTOTAG_BASE_URL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_VISION_BASE_URL") if provider == "hyades" else None)
        or (os.environ.get("HYADES_BASE_URL") if provider == "hyades" else None)
        or ""
    ).strip()
    if provider == "hyades" and _looks_like_a2a_url(base_url):
        return "a2a"
    if _is_hyades_holo_model(provider):
        return "a2a"
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


def _chat_payload_summary(payload):
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list):
        return "missing choices"
    if not choices:
        return "empty choices"
    message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return "first choice has no message"
    keys = ", ".join(sorted(message.keys())) or "no message keys"
    finish_reason = (choices[0] or {}).get("finish_reason")
    return f"first choice message keys: {keys}; finish_reason: {finish_reason or 'none'}"


def _strip_json_fence(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object_text(text):
    text = _strip_json_fence(text)
    if not text:
        return ""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
            return text[index:index + end]
        except json.JSONDecodeError:
            continue
    return text


def _looks_like_provider_error_output(text):
    lowered = (text or "").lower()
    markers = [
        "retry failed after",
        "no route to host",
        "connection refused",
        "connection reset",
        "upstream",
        "service unavailable",
        "gateway timeout",
    ]
    return any(marker in lowered for marker in markers)


def _parse_enrichment_json(output_text, provider=None, transport=None):
    candidate = _extract_json_object_text(output_text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        label = f"{provider or 'ai provider'}"
        if transport:
            label = f"{label}/{transport}"
        detail = _compact_response_detail(output_text)
        if _looks_like_provider_error_output(output_text):
            raise RuntimeError(
                f"AI enrichment provider returned error output for {label}. "
                f"Output: {detail or 'empty response'}"
            ) from error
        raise RuntimeError(
            f"AI enrichment returned non-JSON output from {label}: {error.msg}. "
            f"Output: {detail or 'empty response'}"
        ) from error
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"AI enrichment returned JSON {type(parsed).__name__}, expected object."
        )
    return parsed


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
    if not isinstance(task, dict) and isinstance(result, dict) and result.get("kind") == "task":
        task = result
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


def _a2a_task_state(payload):
    result = payload.get("result") or payload
    if not isinstance(result, dict):
        return None
    task = result.get("task") if isinstance(result.get("task"), dict) else result
    status = task.get("status") if isinstance(task, dict) else None
    if isinstance(status, dict):
        return status.get("state")
    return None


def _a2a_task_id(payload):
    result = payload.get("result") or payload
    if not isinstance(result, dict):
        return None
    for key in ("taskId", "id"):
        value = result.get(key)
        if value:
            return value
    task = result.get("task")
    if isinstance(task, dict):
        for key in ("id", "taskId"):
            value = task.get(key)
            if value:
                return value
    return None


def _a2a_tasks_get_body(task_id):
    return {
        "jsonrpc": "2.0",
        "id": f"asset-enrichment-task-{uuid.uuid4()}",
        "method": "tasks/get",
        "params": {"id": task_id},
    }


def _poll_a2a_task(provider, api_key, task_id):
    max_attempts = int(os.environ.get("HYADES_A2A_POLL_ATTEMPTS", "12"))
    interval = float(os.environ.get("HYADES_A2A_POLL_INTERVAL", "2"))
    payload = None
    for attempt in range(max_attempts):
        if attempt:
            time.sleep(interval)
        payload = _post_json(
            _base_url(provider),
            _a2a_tasks_get_body(task_id),
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            provider=provider,
            transport="a2a-tasks-get",
        )
        output_text = _strip_json_fence(_extract_a2a_output(payload))
        if output_text:
            return payload
        state = (_a2a_task_state(payload) or "").lower()
        if state in {"completed", "failed", "canceled", "cancelled", "rejected"}:
            return payload
    return payload


def _summarize_provider_payload(payload):
    try:
        return _compact_response_detail(json.dumps(payload, sort_keys=True))
    except (TypeError, ValueError):
        return _compact_response_detail(str(payload))


def _image_part(model):
    if not _env_bool("AI_AUTOTAG_USE_VISION", True):
        return None
    stored = _thumbnail_bytes(model)
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


def _openai_transport_supports_image_parts(provider):
    configured = os.environ.get("AI_AUTOTAG_OPENAI_IMAGE_PARTS")
    if configured is not None:
        return _env_bool("AI_AUTOTAG_OPENAI_IMAGE_PARTS", True)
    return provider != "zai"


def _a2a_image_part(model):
    if not _env_bool("AI_AUTOTAG_USE_VISION", True):
        return None
    stored = _thumbnail_bytes(model)
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


def _thumbnail_bytes(model):
    if not model.thumbnail_file_id:
        return None
    try:
        return model._read_stored_file(model.thumbnail_file_id)
    except Exception:
        return None


def _mcp_image_file_bytes(stored):
    suffix = os.environ.get("AI_AUTOTAG_MCP_IMAGE_SUFFIX", ".png")
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    if suffix.lower() in {".jpg", ".jpeg", ".png"}:
        try:
            from PIL import Image

            out = io.BytesIO()
            with Image.open(io.BytesIO(stored)) as image:
                if suffix.lower() in {".jpg", ".jpeg"}:
                    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                        background = Image.new("RGB", image.size, (255, 255, 255))
                        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
                        image = background
                    else:
                        image = image.convert("RGB")
                    image.save(out, format="JPEG", quality=90)
                else:
                    image.save(out, format="PNG")
            return out.getvalue(), suffix
        except Exception:
            pass
    return stored, suffix


def _mcp_command():
    configured = os.environ.get("AI_AUTOTAG_MCP_COMMAND") or os.environ.get("ZAI_MCP_COMMAND")
    return shlex.split(configured or ZAI_MCP_COMMAND)


def _mcp_read_json_line(proc, responses, timeout):
    try:
        line = responses.get(timeout=timeout)
    except queue.Empty as error:
        raise TimeoutError("MCP server did not respond before timeout") from error
    if not line:
        raise RuntimeError("MCP server closed stdout")
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"MCP server returned invalid JSON: {_compact_response_detail(line)}") from error


def _mcp_write(proc, payload):
    proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _extract_mcp_text(payload):
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"MCP tool call failed: {message or 'unknown error'}")
    result = payload.get("result") or {}
    parts = result.get("content") if isinstance(result, dict) else None
    if not isinstance(parts, list):
        return _compact_response_detail(json.dumps(result, sort_keys=True))
    texts = []
    for part in parts:
        if isinstance(part, dict):
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "text" and isinstance(part.get("content"), str):
                texts.append(part["content"])
    return "\n".join(texts).strip()


def _mcp_tools(payload):
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"MCP tools/list failed: {message or 'unknown error'}")
    result = payload.get("result") or {}
    tools = result.get("tools") if isinstance(result, dict) else None
    return tools if isinstance(tools, list) else []


def _mcp_tool_properties(tool):
    if not isinstance(tool, dict):
        return {}
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    return properties if isinstance(properties, dict) else {}


def _choose_mcp_tool(tools):
    configured = os.environ.get("AI_AUTOTAG_MCP_TOOL")
    if configured:
        return {"name": configured}
    best = None
    best_score = -1
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        name = str(tool.get("name") or "")
        description = str(tool.get("description") or "")
        haystack = f"{name} {description}".lower()
        score = 0
        for word in ("image", "vision", "visual", "thumbnail", "screenshot"):
            if word in haystack:
                score += 4
        for word in ("analyze", "analysis", "describe", "caption", "inspect"):
            if word in haystack:
                score += 2
        properties = _mcp_tool_properties(tool)
        if any("image" in key.lower() for key in properties):
            score += 3
        if any(key.lower() in {"path", "file_path", "image_path"} for key in properties):
            score += 2
        if score > best_score:
            best = tool
            best_score = score
    return best if best_score > 0 else None


def _choose_mcp_argument(tool, env_name, candidates, fallback):
    configured = os.environ.get(env_name)
    if configured:
        return configured
    properties = _mcp_tool_properties(tool)
    lowered = {key.lower(): key for key in properties}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    for key in properties:
        low = key.lower()
        if any(candidate in low for candidate in candidates):
            return key
    return fallback


def _zai_mcp_enabled(provider):
    if provider != "zai" or not _env_bool("AI_AUTOTAG_USE_VISION", True):
        return False
    return _transport(provider) == "zai-mcp" or _env_bool("AI_AUTOTAG_ZAI_MCP", False)


def _zai_mcp_visual_context(model, provider, api_key):
    return _zai_mcp_visual_context_result(model, provider, api_key).get("analysis")


def _zai_mcp_visual_context_result(model, provider, api_key):
    if not _zai_mcp_enabled(provider):
        return {"enabled": False, "analysis": None, "error": None}
    stored = _thumbnail_bytes(model)
    if not stored:
        return {"enabled": True, "analysis": None, "error": "No thumbnail image is available for MCP analysis."}
    max_bytes = int(os.environ.get("AI_AUTOTAG_MAX_IMAGE_BYTES", str(2 * 1024 * 1024)))
    if len(stored) > max_bytes:
        return {
            "enabled": True,
            "analysis": None,
            "error": f"Thumbnail image exceeds AI_AUTOTAG_MAX_IMAGE_BYTES ({len(stored)} > {max_bytes}).",
        }

    timeout = int(os.environ.get("AI_AUTOTAG_MCP_TIMEOUT", os.environ.get("AI_AUTOTAG_TIMEOUT", "120")))
    prompt = os.environ.get(
        "AI_AUTOTAG_MCP_PROMPT",
        "Describe this 3D asset preview for catalog metadata. Mention visible subject, style, materials, "
        "whether it appears rigged or animated if inferable, and whether it looks like a light emitter.",
    )
    image_bytes, suffix = _mcp_image_file_bytes(stored)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(image_bytes)
            temp_path = handle.name

        env = os.environ.copy()
        env["Z_AI_API_KEY"] = api_key
        env.setdefault("Z_AI_MODE", os.environ.get("Z_AI_MODE", "ZAI"))
        proc = subprocess.Popen(
            _mcp_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        responses = queue.Queue()
        stderr_lines = []

        def read_stdout():
            for line in proc.stdout:
                responses.put(line)

        def read_stderr():
            for line in proc.stderr:
                if len(stderr_lines) < 20:
                    stderr_lines.append(line)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        stderr_reader = threading.Thread(target=read_stderr, daemon=True)
        stderr_reader.start()

        _mcp_write(proc, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "3d-asset-manager", "version": "1.0"},
            },
        })
        _mcp_read_json_line(proc, responses, timeout)
        _mcp_write(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        if os.environ.get("AI_AUTOTAG_MCP_TOOL"):
            tool = _choose_mcp_tool([])
        else:
            _mcp_write(proc, {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            })
            tools = _mcp_tools(_mcp_read_json_line(proc, responses, timeout))
            tool = _choose_mcp_tool(tools)
            if not tool:
                names = ", ".join(str(item.get("name")) for item in tools if isinstance(item, dict) and item.get("name"))
                detail = f" Available tools: {names}." if names else ""
                return {"enabled": True, "analysis": None, "error": f"No image-capable MCP tool was found.{detail}"}
        tool_name = tool["name"]
        image_arg = _choose_mcp_argument(
            tool,
            "AI_AUTOTAG_MCP_IMAGE_ARG",
            ("image_path", "file_path", "path", "image", "image_file", "file"),
            "image_path",
        )
        prompt_arg = _choose_mcp_argument(
            tool,
            "AI_AUTOTAG_MCP_PROMPT_ARG",
            ("prompt", "query", "question", "instructions", "text"),
            "prompt",
        )
        _mcp_write(proc, {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {
                    image_arg: temp_path,
                    prompt_arg: prompt,
                },
            },
        })
        payload = _mcp_read_json_line(proc, responses, timeout)
        analysis = _extract_mcp_text(payload)
        if not analysis:
            return {"enabled": True, "analysis": None, "error": "MCP tool returned no text analysis."}
        return {"enabled": True, "analysis": analysis, "error": None}
    except Exception as error:
        if _env_bool("AI_AUTOTAG_ZAI_MCP_REQUIRED", False):
            raise RuntimeError(f"Z.AI Vision MCP failed: {error}") from error
        return {"enabled": True, "analysis": None, "error": _compact_response_detail(str(error))}
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        if "proc" in locals():
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


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
            "Do not use generator names, file formats, source pipeline labels, or generic tags like "
            "pixal3d, generated, image-to-3d, ai-generated, glb, or 3d-model. "
            + FAB_LISTING_GUIDANCE + "\n\n"
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
    task_id = _a2a_task_id(payload)
    if not output_text and task_id:
        payload = _poll_a2a_task(provider, api_key, task_id)
        output_text = _strip_json_fence(_extract_a2a_output(payload or {}))
    if not output_text:
        state = _a2a_task_state(payload or {}) or "unknown"
        task = _a2a_task_id(payload or {}) or task_id or "unknown"
        detail = _summarize_provider_payload(payload or {})
        raise RuntimeError(
            f"AI enrichment returned no A2A output text (task state: {state}; task id: {task}). "
            f"Response: {detail or 'empty response'}"
        )
    enriched = _parse_enrichment_json(output_text, provider=provider, transport="a2a")
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
                "maxItems": 25,
                "description": "Fab-style discoverability tags. Use concrete subject, style, material, genre, and use-case tags. Do not include file formats, generator names, or generic pipeline/source tags.",
            },
            "title": {
                "type": "string",
                "description": "A Fab listing title under 80 characters based on the visible subject and style, not the filename, generator, provider, or generic phrases like AI-generated 3D model.",
            },
            "asset_category": {
                "type": "string",
                "description": "One broad what-it-is bucket for filtering. Prefer the subject bucket over generic prop: flora for flowers/plants/trees/leaves, fauna for animals/creatures, building for architecture, person for characters, vehicle, environment, material, animation, prop, or other. Use prop only for objects that do not fit a more specific broad bucket.",
            },
            "asset_styles": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 6,
                "description": "Art direction, medium, or genre labels such as fantasy, sci-fi, medieval, modern, stylized, realistic, low-poly, cozy, horror, cyberpunk, watercolor, painterly, hand-painted.",
            },
            "asset_types": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": "Technical/use traits such as static, rigged, animated, game-ready, modular, decorative-prop, pbr, tileable, vrm, optimized, light-emitter, high-poly, low-poly. Do not include broad category labels such as building, flora, fauna, person, vehicle, environment, material, or prop.",
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
            "description": {
                "type": "string",
                "description": "Buyer-facing Fab product description for a 3D listing. Start with what the asset is, then describe style/materials and practical scene/use cases. Avoid AI pipeline provenance and unsupported technical specs.",
            },
            "summary": {"type": "string", "description": "One concise storefront summary of the product value and subject."},
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
    vision_mcp = _zai_mcp_visual_context_result(model, provider, api_key)
    vision_mcp_analysis = vision_mcp.get("analysis")
    if vision_mcp_analysis:
        prompt["vision_mcp_analysis"] = vision_mcp_analysis
        prompt["metadata_instruction"] = (
            "Use vision_mcp_analysis as the primary source for visible subject, materials, style, colors, "
            "and light-emitter hints. Avoid generic generation-pipeline wording such as baseline mesh, "
            "background prop, sculpting base, or retopology unless those details are visibly supported. "
            "Always fill asset_category, asset_styles, and asset_types from the visual analysis. For example, "
            "flowers/plants/leaves should use asset_category flora even when the object is also decorative; "
            "watercolor or hand-painted looks belong in asset_styles; static/not-rigged/non-animated observations "
            "belong in asset_types as static. The title should name the visible asset, for example Watercolor "
            "Floral Arrangement, not Pixal3D AI-Generated 3D Model. The description should read like a Fab "
            "store listing, not a visual-analysis report."
        )
    elif vision_mcp.get("enabled"):
        prompt["vision_mcp_status"] = {
            "analysis_available": False,
            "error": vision_mcp.get("error"),
        }
    user_text = (
        "Create marketplace metadata for this 3D asset. Return only JSON that matches the schema. "
        + FAB_LISTING_GUIDANCE + " "
        "Prefer concrete visible or file-derived details over generic filler. "
        "Write the title as a concise product/catalog name for the visible subject and style; never use the "
        "generator/provider name or generic source labels as the title. "
            "Do not leave asset_category, asset_styles, or asset_types empty when visual analysis provides evidence. "
            "Use asset_category for the broad subject bucket, asset_styles for aesthetic/genre/medium, "
            "and asset_types for technical/use traits. Avoid contradictory facet pairs such as realistic with "
            "cartoon/painterly/stylized, high-poly with low-poly, or static with rigged/animated unless the "
            "asset truly contains both distinct components. "
            "If visual analysis is unavailable, say only what can be inferred from the filename and asset fields, "
        "and avoid pretending to know the object's appearance.\n\n"
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
                        "Do not use generator names, file formats, source pipeline labels, or generic tags like "
                        "pixal3d, generated, image-to-3d, ai-generated, glb, or 3d-model. "
                        + FAB_LISTING_GUIDANCE
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
    image = _image_part(model) if _openai_transport_supports_image_parts(provider) else None
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
        label = _provider_label(_request_url(_base_url(provider)), provider=provider, transport="openai")
        raise RuntimeError(
            f"AI enrichment returned no output text from {label}. "
            f"Response shape: {_chat_payload_summary(payload)}. "
            f"Response: {_summarize_provider_payload(payload) or 'empty response'}"
        )
    enriched = _parse_enrichment_json(output_text, provider=provider, transport="openai")
    enriched["provider"] = provider
    enriched["transport"] = "openai"
    enriched["base_url"] = _base_url(provider)
    enriched["model"] = body["model"]
    enriched["response_id"] = payload.get("id")
    enriched["vision_fallback"] = vision_fallback
    enriched["vision_mcp"] = bool(vision_mcp_analysis)
    enriched["vision_mcp_attempted"] = bool(vision_mcp.get("enabled"))
    enriched["vision_mcp_analysis"] = vision_mcp_analysis
    enriched["vision_mcp_error"] = vision_mcp.get("error")
    return enriched


def enrich_model(model, extra_context=None):
    enriched = _ai_metadata(model, extra_context=extra_context)
    if enriched is None:
        enriched = _heuristic_metadata(model)
        enriched["provider"] = "heuristic"
    enriched["tags"] = _clean_enrichment_tags(enriched.get("tags", []))
    enriched = _infer_missing_facets(enriched)
    enriched["title"] = (enriched.get("title") or "").strip()
    enriched["asset_category"] = Model3D.normalize_category(enriched.get("asset_category"))
    cleanup_text = " ".join([
        str(enriched.get("title") or ""),
        str(enriched.get("description") or ""),
        str(enriched.get("summary") or ""),
        str(enriched.get("vision_mcp_analysis") or ""),
        " ".join(str(tag) for tag in enriched.get("tags") or []),
    ]).lower()
    enriched["asset_styles"] = _resolve_style_conflicts(enriched.get("asset_styles", []), cleanup_text)
    enriched["asset_types"] = _resolve_type_conflicts(enriched.get("asset_types", []), enriched["asset_category"], cleanup_text)
    enriched["runtime_metadata"] = Model3D.normalize_runtime_metadata(
        enriched.get("runtime_metadata", _default_runtime_metadata(False))
    )
    enriched["tags"] = _clean_enrichment_tags(enriched.get("tags", []))
    enriched["description"] = (enriched.get("description") or "").strip()
    enriched["summary"] = (enriched.get("summary") or "").strip()
    enriched.setdefault("categories", [])
    enriched.setdefault("quality_notes", [])
    return enriched
