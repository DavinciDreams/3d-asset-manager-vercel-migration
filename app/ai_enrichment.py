"""AI-assisted metadata enrichment for uploaded 3D assets."""
import json
import os
import re
import urllib.error
import urllib.request

from app.models import Model3D


DEFAULT_MODEL = "gpt-4.1-mini"


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
    description = model.description or (
        f"{clean_name} is a {fmt.upper() if fmt else '3D'} asset prepared for cataloging, "
        "preview, and downstream packaging."
    )
    return {
        "tags": Model3D.normalize_tags(tags[:12]),
        "description": description,
        "summary": description[:180],
        "categories": [],
        "quality_notes": [],
    }


def _extract_output_text(payload):
    if payload.get("output_text"):
        return payload["output_text"]
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return content["text"]
    return ""


def _openai_metadata(model, extra_context=None):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
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
            "description": {"type": "string"},
            "summary": {"type": "string"},
            "categories": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "quality_notes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
        "required": ["tags", "description", "summary", "categories", "quality_notes"],
    }
    prompt = {
        "asset": {
            "name": model.name,
            "description": model.description,
            "original_filename": model.original_filename,
            "file_format": model.file_format,
            "file_size": model.file_size,
            "existing_tags": model.tags,
            "approve_game_ready": model.approve_game_ready,
            "approve_asset_store": model.approve_asset_store,
            "conversion_status": model.conversion_status,
        },
        "extra_context": extra_context or {},
    }
    body = {
        "model": os.environ.get("OPENAI_AUTOTAG_MODEL", DEFAULT_MODEL),
        "input": [
            {
                "role": "system",
                "content": (
                    "You enrich 3D asset store records. Return concise JSON only. "
                    "Tags should be lowercase marketplace/search tags, not sentences."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "asset_enrichment",
                "strict": True,
                "schema": schema,
            }
        },
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(os.environ.get("OPENAI_AUTOTAG_TIMEOUT", "30"))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"OpenAI enrichment failed ({error.code}): {detail}") from error

    output_text = _extract_output_text(payload)
    if not output_text:
        raise RuntimeError("OpenAI enrichment returned no output text.")
    enriched = json.loads(output_text)
    enriched["provider"] = "openai"
    enriched["model"] = body["model"]
    enriched["response_id"] = payload.get("id")
    return enriched


def enrich_model(model, extra_context=None):
    enriched = _openai_metadata(model, extra_context=extra_context)
    if enriched is None:
        enriched = _heuristic_metadata(model)
        enriched["provider"] = "heuristic"
    enriched["tags"] = Model3D.normalize_tags(enriched.get("tags", []))
    enriched["description"] = (enriched.get("description") or "").strip()
    enriched["summary"] = (enriched.get("summary") or "").strip()
    enriched.setdefault("categories", [])
    enriched.setdefault("quality_notes", [])
    return enriched
