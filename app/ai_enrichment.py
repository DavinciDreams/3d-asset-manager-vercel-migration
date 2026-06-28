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
    "glb", "gltf", "fbx", "obj", "stl", "vrm", "vrma", "bvh", "image-to-3d", "model", "pixal3d",
}
FAB_LISTING_GUIDANCE = (
    "Write compelling copy describing the asset in detail. Use polished buyer-facing prose; "
    "you may include bullet points in the description where useful. Describe the asset subject, "
    "visual style, materials, and scenarios it is well suited for. Utilize terminology that buyers "
    "would use to search for this type of item, including appropriate keywords and tags. "
    "Keep the title under 80 characters. Return up to 10 discoverability tags."
)
CATEGORY_TAXONOMY_GUIDANCE = (
    "Use exactly one asset_category subject bucket. Animals and creatures are always fauna. "
    "Plants, trees, flowers, bushes, grass, vines, leaves, and mushrooms are flora. "
    "Buildings and architectural structures are building. Furniture is furniture. "
    "People, humanoids, avatars, and human characters are person. Vehicles are vehicle. "
    "Outdoor non-living scene objects such as bridges, fountains, lanterns, signs, paths, rocks, "
    "terrain pieces, and water features are environment. Do not use material as a category; "
    "texture, shader, tileable surface, and material-map details belong in tags, asset_types, "
    "or description copy. Use prop only when no specific bucket applies."
)
TAGGER_SYSTEM_PROMPT = (
    "You are a specialist in 3D assets, 3D modeling, game design, and rigging. "
    "You are writing marketing copy to enrich 3D asset store records. Return concise JSON only, "
    "with one discrete value per requested field. Do not return visual-analysis essays, markdown "
    "headings, chain-of-thought, main/detailed response sections, or runtime capability debates. "
    "Tags should be lowercase marketplace/search tags, not sentences, and should represent actual "
    "groups useful for game designers and 3D artists. Use tags that describe what the asset is, "
    "and the style or category it belongs to. Do not use generic names, file formats, source "
    "pipeline labels, or generic tags like pixal3d, generated, image-to-3d, ai-generated, glb, "
    "or 3d-model. Do not invent formats, file sizes, vertex counts, texture resolution, rigging, "
    "animation, emissive/light behavior, static/dynamic state, or optimization details; those are "
    "filled from file metadata or explicit runtime metadata. "
    + CATEGORY_TAXONOMY_GUIDANCE
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


ANIMATION_INTENT_KEYWORDS = {
    "idle": ["idle", "stand", "standing", "breathing", "rest"],
    "walk": ["walk", "walking", "stroll"],
    "run": ["run", "running", "sprint", "jog"],
    "dance": ["dance", "dancing", "hiphop", "hip-hop", "samba"],
    "wave": ["wave", "waving", "hello", "greeting", "greet"],
    "talk_idle": ["talk", "talking", "conversation", "speaking"],
    "jump": ["jump", "jumping", "hop"],
    "throw": ["throw", "throwing"],
    "sit": ["sit", "sitting", "sitdown", "sit-down"],
    "mount": ["mount", "mounting", "ride", "riding"],
    "dismount": ["dismount", "dismounting"],
    "attack": ["attack", "punch", "kick", "slash"],
    "graze": ["graze", "grazing", "eat", "eating"],
    "fly": ["fly", "flying", "flap", "flapping"],
    "flap": ["flap", "flapping", "wingbeat", "wing-beat"],
}

LOCOMOTION_INTENTS = {"walk", "run", "jump", "fly", "mount", "dismount"}
ANIMATION_QUALITY_ISSUES = (
    "foot-sliding",
    "bad-loop",
    "wrong-scale",
    "jaw-smushed",
    "hand-clipping",
    "pose-drift",
    "broken-retarget",
)


def _animation_clip_name(model):
    runtime = model.runtime_metadata or {}
    for clip in runtime.get("animations") or []:
        if isinstance(clip, dict) and str(clip.get("name") or "").strip():
            return str(clip.get("name")).strip()
        if isinstance(clip, str) and clip.strip():
            return clip.strip()
    name = model.name or model.original_filename or "Untitled Animation"
    name = re.sub(r"\.(vrma|bvh|fbx)$", "", str(name), flags=re.IGNORECASE)
    for suffix in (" Humanoid Animation Clip", " animation", " Animation"):
        if name.lower().endswith(suffix.lower()):
            name = name[:-len(suffix)].strip() or name
    return name.replace("_", " ").replace("-", " ").strip() or "Untitled Animation"


def _animation_duration(model):
    runtime = model.runtime_metadata or {}
    durations = []
    for clip in runtime.get("animations") or []:
        if not isinstance(clip, dict) or clip.get("duration") is None:
            continue
        try:
            value = float(clip.get("duration"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            durations.append(value)
    if durations:
        return round(max(durations), 3)
    return None


def _animation_text(model):
    runtime = model.runtime_metadata if isinstance(model.runtime_metadata, dict) else {}
    values = [
        model.name,
        model.original_filename,
        model.description,
        " ".join(str(tag) for tag in (model.tags or [])),
        " ".join(str(kind) for kind in (model.asset_types or [])),
        json.dumps(runtime.get("animations") or [], sort_keys=True),
    ]
    return " ".join(str(value or "") for value in values).lower()


def _animation_intent_from_text(text):
    scores = {
        intent: _keyword_score(text, words)
        for intent, words in ANIMATION_INTENT_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "gesture"


def _animation_energy(intent, text):
    if intent in {"run", "dance", "jump", "throw", "attack", "fly"}:
        return "high"
    if _contains_any(text, ("sprint", "fast", "running", "run", "jump", "attack", "kick", "punch", "dance")):
        return "high"
    if intent in {"walk", "talk_idle", "wave", "graze", "mount", "dismount"}:
        return "medium"
    return "low"


def _animation_body_type(model, text):
    tags = set(Model3D.normalize_tags((model.tags or []) + (model.asset_types or [])))
    if "quadruped" in tags or _contains_any(text, ("horse", "deer", "dog", "wolf", "cat", "unicorn", "quadruped")):
        return "quadruped"
    if _contains_any(text, ("bird", "wing", "flap", "fly")):
        return "avian"
    return "humanoid"


def _animation_actor_kind(body_type, intent, text):
    if body_type == "humanoid":
        return "avatar"
    if body_type == "avian":
        return "animal"
    if body_type == "quadruped":
        if intent in {"mount", "dismount"} or _contains_any(text, ("horse", "unicorn", "mount", "mounted", "saddle", "riding")):
            return "mount"
        return "animal"
    if body_type in {"creature", "mount"}:
        return "mount" if body_type == "mount" else "animal"
    if _contains_any(text, ("vehicle", "car", "boat", "ship", "airplane", "plane")):
        return "vehicle"
    return "object"


def _animation_skeleton_profile(body_type, text):
    if _contains_any(text, ("mixamo", "fbx")):
        return "mixamo-humanoid"
    if body_type == "humanoid":
        return "vrm-humanoid"
    if body_type == "avian":
        return "bird"
    if _contains_any(text, ("fish", "swim", "swimming")):
        return "fish"
    if body_type == "quadruped":
        return "quadruped"
    if _contains_any(text, ("vehicle", "wheels", "boat", "ship", "airplane", "plane")):
        return "vehicle"
    return "unknown"


def _animation_category(intent, locomotion):
    if intent == "idle":
        return "ambient"
    if intent == "dance":
        return "dance"
    if locomotion:
        return "locomotion"
    if intent in {"dance", "wave", "talk_idle", "throw", "attack"}:
        return "gesture"
    return "action"


def _animation_gait(intent, text):
    if _contains_any(text, ("gallop", "galloping")):
        return "gallop"
    if _contains_any(text, ("trot", "trotting")):
        return "trot"
    if _contains_any(text, ("canter", "cantering")):
        return "canter"
    if intent in {"walk", "run", "jump", "idle", "flap", "fly"}:
        return intent
    if intent == "graze":
        return "idle"
    if _contains_any(text, ("swim", "swimming")):
        return "swim"
    return "unknown"


def _animation_speed(intent, gait, text):
    if _contains_any(text, ("in-place", "in place", "stationary", "no root motion")):
        return 0.0
    if intent == "idle" or gait == "idle":
        return 0.0
    if gait == "walk" or intent == "walk":
        return 1.3
    if gait == "trot":
        return 3.5
    if gait == "canter":
        return 5.5
    if gait == "gallop":
        return 8.0
    if intent == "run" or gait == "run":
        return 4.5
    if intent in {"fly", "flap"}:
        return 2.5
    return None


def _animation_root_motion(intent, locomotion, text):
    if _contains_any(text, ("mixed root", "root motion mixed", "mixed")):
        return "mixed"
    if _contains_any(text, ("root motion", "root-motion", "moves forward", "travels forward")):
        return "root-motion"
    if _contains_any(text, ("in-place", "in place", "stationary", "no root motion")):
        return "in-place"
    if locomotion and intent in {"walk", "run", "fly"}:
        return "unknown"
    return "in-place"


def _animation_direction(intent, text):
    if _contains_any(text, ("turn left", "turn-left", "left turn")):
        return "turn-left"
    if _contains_any(text, ("turn right", "turn-right", "right turn")):
        return "turn-right"
    if _contains_any(text, ("backward", "backwards", "reverse")):
        return "backward"
    if _contains_any(text, ("strafe left", "step left")):
        return "left"
    if _contains_any(text, ("strafe right", "step right")):
        return "right"
    if intent in {"walk", "run", "fly", "flap"}:
        return "forward"
    if intent in {"idle", "dance", "wave", "talk_idle", "graze", "sit"}:
        return "none"
    return "unknown"


def _animation_quality_issues(text, quality_notes=None):
    values = list(quality_notes or [])
    lowered = " ".join([text, " ".join(str(note) for note in values)]).lower()
    for issue in ANIMATION_QUALITY_ISSUES:
        if issue in lowered or issue.replace("-", " ") in lowered:
            values.append(issue)
    return Model3D.normalize_tags(values)


def _animation_clip_label(clip, fallback):
    if isinstance(clip, dict):
        return str(clip.get("name") or fallback).strip() or fallback
    if isinstance(clip, str):
        return clip.strip() or fallback
    return fallback


def _animation_clip_duration(clip):
    if not isinstance(clip, dict) or clip.get("duration") is None:
        return None
    try:
        value = float(clip.get("duration"))
    except (TypeError, ValueError):
        return None
    return round(value, 3) if value > 0 else None


def _animation_clip_registry_entry(model, clip, index):
    name = _animation_clip_label(clip, f"Clip {index + 1}")
    text = " ".join([
        str(model.name or ""),
        str(model.original_filename or ""),
        str(model.description or ""),
        " ".join(str(tag) for tag in (model.tags or [])),
        " ".join(str(kind) for kind in (model.asset_types or [])),
        name,
        json.dumps(clip, sort_keys=True) if isinstance(clip, dict) else "",
    ]).lower()
    label_intent = _animation_intent_from_text(name.lower())
    intent = label_intent if label_intent != "gesture" else _animation_intent_from_text(text)
    energy = _animation_energy(intent, text)
    body_type = _animation_body_type(model, text)
    locomotion = intent in LOCOMOTION_INTENTS or _contains_any(text, ("locomotion", "root motion", "in-place", "in place"))
    actor_kind = _animation_actor_kind(body_type, intent, text)
    skeleton_profile = _animation_skeleton_profile(body_type, text)
    category = _animation_category(intent, locomotion)
    gait = _animation_gait(intent, text)
    root_motion = _animation_root_motion(intent, locomotion, text)
    speed = _animation_speed(intent, gait, text)
    direction = _animation_direction(intent, text)
    aliases = _clean_enrichment_tags([name, intent, intent.replace("_", " "), *(_tokens(name)[:8])])[:12]
    quality_issues = _animation_quality_issues(text)
    tags = _clean_enrichment_tags([
        intent,
        actor_kind,
        skeleton_profile,
        category,
        gait,
        *(_tokens(name)[:6]),
    ])[:14]
    slug = Model3D.normalize_tags([name])[0] if name else f"clip-{index + 1}"
    return {
        "id": f"{model.id}:{slug or f'clip-{index + 1}'}",
        "assetId": model.id,
        "name": name,
        "aliases": aliases,
        "format": (model.file_format or "glb").lower(),
        "actorKind": actor_kind,
        "skeletonProfile": skeleton_profile,
        "intents": [intent],
        "intent": intent,
        "category": category,
        "loop": intent in {"idle", "walk", "run", "dance", "talk_idle", "graze", "fly", "flap"},
        "durationSeconds": _animation_clip_duration(clip),
        "duration": _animation_clip_duration(clip),
        "rootMotion": root_motion,
        "speedMetersPerSecond": speed,
        "direction": direction,
        "gait": gait,
        "transition": {"from": [], "to": []},
        "quality": {"score": 1.0 if not quality_issues else 0.5, "issues": quality_issues},
        "searchText": " ".join(_clean_enrichment_tags([name, intent, actor_kind, skeleton_profile, category, gait, *aliases])),
        "bodyType": body_type,
        "tags": tags,
        "energy": energy,
        "locomotion": locomotion,
        "requiresMount": actor_kind == "mount" or intent in {"mount", "dismount"},
    }


def _species_or_type(model, actor_kind, text):
    for value in ("horse", "unicorn", "deer", "wolf", "dog", "cat", "bird", "fish", "dragon", "fox"):
        if value in text:
            return value
    if actor_kind == "vehicle":
        return "vehicle"
    if actor_kind == "mount":
        return "mount"
    if actor_kind == "animal":
        return "animal"
    return "object"


def _vehicle_mode(actor_kind, skeleton_profile, text):
    if actor_kind == "vehicle":
        if _contains_any(text, ("boat", "ship", "hull", "swim", "water")):
            return "water"
        if _contains_any(text, ("air", "fly", "plane", "airplane", "bird", "wing")):
            return "air"
        return "ground"
    if actor_kind in {"animal", "mount"}:
        if skeleton_profile == "bird" or _contains_any(text, ("fly", "flap", "wing")):
            return "air"
        if skeleton_profile == "fish" or _contains_any(text, ("swim", "fish")):
            return "water"
        return "ground"
    return "none"


def _ground_contact(actor_kind, skeleton_profile, text):
    if actor_kind == "vehicle":
        if _contains_any(text, ("boat", "ship", "hull")):
            return "hull"
        if _contains_any(text, ("hover", "drone")):
            return "hover"
        return "wheels"
    if skeleton_profile in {"quadruped", "bird", "vrm-humanoid", "mixamo-humanoid"}:
        return "feet"
    return "unknown"


def enrich_embedded_model_animations(model, extra_context=None):
    runtime = model.runtime_metadata if isinstance(model.runtime_metadata, dict) else {}
    clips = [clip for clip in (runtime.get("animations") or []) if clip]
    if not clips:
        return None
    clip_entries = [_animation_clip_registry_entry(model, clip, index) for index, clip in enumerate(clips)]
    search_text = " ".join([
        str(model.name or ""),
        str(model.original_filename or ""),
        str(model.description or ""),
        " ".join(str(tag) for tag in (model.tags or [])),
        " ".join(str(kind) for kind in (model.asset_types or [])),
        " ".join(entry.get("searchText") or "" for entry in clip_entries),
    ]).lower()
    first = clip_entries[0]
    actor_kind = first["actorKind"]
    skeleton_profile = first["skeletonProfile"]
    movement = {
        "idleIntent": next((entry["intent"] for entry in clip_entries if entry["intent"] == "graze"), None)
            or next((entry["intent"] for entry in clip_entries if entry["intent"] == "idle"), None),
        "walkIntent": next((entry["intent"] for entry in clip_entries if entry["intent"] == "walk"), None),
        "runIntent": next((entry["intent"] for entry in clip_entries if entry["intent"] in {"run", "fly", "flap"}), None),
        "turnRateDegreesPerSecond": None,
    }
    return {
        "assetId": model.id,
        "actorKind": actor_kind,
        "skeletonProfile": skeleton_profile,
        "speciesOrType": _species_or_type(model, actor_kind, search_text),
        "mountable": bool(actor_kind == "mount" or _contains_any(search_text, ("mountable", "ride", "riding", "saddle", "horse", "unicorn"))),
        "vehicleMode": _vehicle_mode(actor_kind, skeleton_profile, search_text),
        "canonicalHeightMeters": None,
        "groundContact": _ground_contact(actor_kind, skeleton_profile, search_text),
        "movement": movement,
        "anchors": {},
        "animationClipIds": [entry["id"] for entry in clip_entries],
        "animationClips": clip_entries,
    }


def _heuristic_animation_metadata(model, extra_context=None):
    text = _animation_text(model)
    clip_name = _animation_clip_name(model)
    intent = _animation_intent_from_text(text)
    energy = _animation_energy(intent, text)
    body_type = _animation_body_type(model, text)
    duration = _animation_duration(model)
    locomotion = intent in LOCOMOTION_INTENTS or _contains_any(text, ("locomotion", "in-place", "root motion"))
    requires_mount = intent in {"mount", "dismount"} or _contains_any(text, ("horseback", "saddle", "mounted", "riding"))
    actor_kind = _animation_actor_kind(body_type, intent, text)
    skeleton_profile = _animation_skeleton_profile(body_type, text)
    category = _animation_category(intent, locomotion)
    gait = _animation_gait(intent, text)
    root_motion = _animation_root_motion(intent, locomotion, text)
    direction = _animation_direction(intent, text)
    speed = _animation_speed(intent, gait, text)
    aliases = _clean_enrichment_tags([clip_name, intent, intent.replace("_", " "), *(_tokens(clip_name)[:8])])
    quality_issues = _animation_quality_issues(text)
    tags = _clean_enrichment_tags([
        intent,
        body_type,
        actor_kind,
        category,
        energy,
        "locomotion" if locomotion else "gesture",
        *(model.tags or []),
        *(_tokens(clip_name)[:6]),
    ])
    title = " ".join(word.capitalize() for word in clip_name.split())
    description = (
        f"{title} is a {body_type} animation clip for the {intent.replace('_', ' ')} intent. "
        f"It is suitable for deterministic avatar playback, intent matching, and short staged sequences."
    )
    return {
        "title": title[:80],
        "description": description,
        "summary": f"{body_type} {intent.replace('_', ' ')} animation clip.",
        "asset_category": "animation",
        "asset_styles": [],
        "asset_types": ["avatar-animation"] + (["locomotion"] if locomotion else ["gesture"]),
        "tags": tags[:14],
        "categories": ["animation", intent],
        "quality_notes": [],
        "animation": {
            "intent": intent,
            "intents": [intent],
            "actorKind": actor_kind,
            "skeletonProfile": skeleton_profile,
            "category": category,
            "bodyType": body_type,
            "tags": tags[:14],
            "loop": intent in {"idle", "walk", "run", "dance", "talk_idle", "graze", "fly"},
            "duration": duration,
            "durationSeconds": duration,
            "transitionIn": 0.2,
            "transitionOut": 0.2,
            "energy": energy,
            "locomotion": locomotion,
            "rootMotion": root_motion,
            "speedMetersPerSecond": speed,
            "direction": direction,
            "gait": gait,
            "transition": {"from": [], "to": []},
            "aliases": aliases[:12],
            "quality": {"score": 1.0 if not quality_issues else 0.5, "issues": quality_issues},
            "searchText": " ".join(_clean_enrichment_tags([clip_name, intent, body_type, actor_kind, skeleton_profile, category, gait, *aliases])),
            "requiresMount": requires_mount,
        },
        "provider": "heuristic",
    }


def _normalize_animation_metadata(parsed, model, provider=None, transport=None, response_id=None):
    parsed = parsed if isinstance(parsed, dict) else {}
    fallback = _heuristic_animation_metadata(model)
    animation = parsed.get("animation") if isinstance(parsed.get("animation"), dict) else {}
    intent = Model3D.normalize_tags(animation.get("intent") or parsed.get("intent") or fallback["animation"]["intent"])
    intent = intent[0] if intent else fallback["animation"]["intent"]
    body_type = str(animation.get("bodyType") or animation.get("body_type") or parsed.get("bodyType") or fallback["animation"]["bodyType"]).strip()
    if body_type not in {"humanoid", "quadruped", "avian", "creature", "mount", "prop", "unknown"}:
        body_type = fallback["animation"]["bodyType"]
    skeleton_profile = str(animation.get("skeletonProfile") or animation.get("skeleton_profile") or fallback["animation"]["skeletonProfile"]).strip()
    if skeleton_profile not in {"vrm-humanoid", "mixamo-humanoid", "quadruped", "bird", "fish", "vehicle", "unknown"}:
        skeleton_profile = fallback["animation"]["skeletonProfile"]
    actor_kind = str(animation.get("actorKind") or animation.get("actor_kind") or fallback["animation"]["actorKind"]).strip()
    if actor_kind not in {"avatar", "agent", "animal", "mount", "vehicle", "object"}:
        actor_kind = fallback["animation"]["actorKind"]
    energy = str(animation.get("energy") or parsed.get("energy") or fallback["animation"]["energy"]).strip().lower()
    if energy not in {"low", "medium", "high"}:
        energy = fallback["animation"]["energy"]
    try:
        duration = animation.get("duration", parsed.get("duration", fallback["animation"]["duration"]))
        duration = round(float(duration), 3) if duration is not None else None
        if duration is not None and duration <= 0:
            duration = None
    except (TypeError, ValueError):
        duration = fallback["animation"]["duration"]
    tags = _clean_enrichment_tags((parsed.get("tags") or []) + (animation.get("tags") or []) + fallback["animation"]["tags"])[:14]
    intents = Model3D.normalize_tags(animation.get("intents") or parsed.get("intents") or [])
    intents = _clean_enrichment_tags([intent, *intents])[:8]
    category = str(animation.get("category") or fallback["animation"]["category"]).strip().lower()
    if category not in {"locomotion", "gesture", "dance", "action", "sport", "pose", "ambient", "transition", "other"}:
        category = fallback["animation"]["category"]
    root_motion = str(animation.get("rootMotion") or animation.get("root_motion") or fallback["animation"]["rootMotion"]).strip().lower()
    if root_motion not in {"in-place", "root-motion", "mixed", "unknown"}:
        root_motion = fallback["animation"]["rootMotion"]
    try:
        speed = animation.get("speedMetersPerSecond", animation.get("speed_meters_per_second", fallback["animation"]["speedMetersPerSecond"]))
        speed = round(float(speed), 3) if speed is not None else None
        if speed is not None and speed < 0:
            speed = None
    except (TypeError, ValueError):
        speed = fallback["animation"]["speedMetersPerSecond"]
    gait = animation.get("gait", fallback["animation"]["gait"])
    gait = Model3D.normalize_tags([gait])[0] if gait else "unknown"
    if gait not in {"walk", "trot", "canter", "gallop", "flap", "swim", "idle", "run", "unknown"}:
        gait = fallback["animation"]["gait"]
    direction = str(animation.get("direction") or fallback["animation"]["direction"]).strip().lower()
    if direction not in {"forward", "backward", "left", "right", "turn-left", "turn-right", "none", "unknown"}:
        direction = fallback["animation"]["direction"]
    transition = animation.get("transition") if isinstance(animation.get("transition"), dict) else {}
    normalized_transition = {
        "from": Model3D.normalize_tags(transition.get("from") or fallback["animation"]["transition"]["from"]),
        "to": Model3D.normalize_tags(transition.get("to") or fallback["animation"]["transition"]["to"]),
    }
    aliases = _clean_enrichment_tags((animation.get("aliases") or parsed.get("aliases") or []) + fallback["animation"]["aliases"])[:12]
    quality = animation.get("quality") if isinstance(animation.get("quality"), dict) else {}
    try:
        quality_score = float(quality.get("score", fallback["animation"]["quality"]["score"]))
        quality_score = max(0.0, min(1.0, round(quality_score, 3)))
    except (TypeError, ValueError):
        quality_score = fallback["animation"]["quality"]["score"]
    quality_issues = _animation_quality_issues(
        _animation_text(model),
        (quality.get("issues") if isinstance(quality.get("issues"), list) else []) + (parsed.get("quality_notes") or []),
    )
    search_text = str(animation.get("searchText") or animation.get("search_text") or fallback["animation"]["searchText"]).strip()
    normalized_animation = {
        "intent": intent,
        "intents": intents,
        "actorKind": actor_kind,
        "skeletonProfile": skeleton_profile,
        "category": category,
        "bodyType": body_type,
        "tags": tags,
        "loop": bool(animation.get("loop", fallback["animation"]["loop"])),
        "duration": duration,
        "durationSeconds": duration,
        "transitionIn": float(animation.get("transitionIn", animation.get("transition_in", fallback["animation"]["transitionIn"])) or 0.2),
        "transitionOut": float(animation.get("transitionOut", animation.get("transition_out", fallback["animation"]["transitionOut"])) or 0.2),
        "energy": energy,
        "locomotion": bool(animation.get("locomotion", fallback["animation"]["locomotion"])),
        "rootMotion": root_motion,
        "speedMetersPerSecond": speed,
        "direction": direction,
        "gait": gait,
        "transition": normalized_transition,
        "aliases": aliases,
        "quality": {"score": quality_score, "issues": quality_issues},
        "searchText": search_text,
        "requiresMount": bool(animation.get("requiresMount", animation.get("requires_mount", fallback["animation"]["requiresMount"]))),
    }
    title = (parsed.get("title") or fallback["title"]).strip()[:80]
    description = (parsed.get("description") or fallback["description"]).strip()
    return {
        "title": title,
        "description": description,
        "summary": (parsed.get("summary") or fallback["summary"]).strip(),
        "asset_category": "animation",
        "asset_styles": Model3D.normalize_tags(parsed.get("asset_styles", [])),
        "asset_types": _clean_enrichment_tags((parsed.get("asset_types") or []) + ["avatar-animation", "locomotion" if normalized_animation["locomotion"] else "gesture"]),
        "tags": tags,
        "categories": parsed.get("categories") if isinstance(parsed.get("categories"), list) else ["animation", intent],
        "quality_notes": parsed.get("quality_notes") if isinstance(parsed.get("quality_notes"), list) else [],
        "animation": normalized_animation,
        "provider": provider or parsed.get("provider") or fallback["provider"],
        "transport": transport or parsed.get("transport"),
        "base_url": _base_url(provider) if provider else parsed.get("base_url"),
        "model": _model_name(provider) if provider else parsed.get("model"),
        "response_id": response_id or parsed.get("response_id"),
        "vision_frame": bool(parsed.get("vision_frame", fallback.get("vision_frame", False))),
        "preview_video_available": bool(parsed.get("preview_video_available", fallback.get("preview_video_available", False))),
    }


def _animation_metadata_prompt(model, extra_context=None):
    has_thumbnail = bool(model.thumbnail_file_id)
    has_preview = bool(model.preview_file_id)
    payload = {
        "asset": {
            "name": model.name,
            "description": model.description,
            "original_filename": model.original_filename,
            "file_format": model.file_format,
            "existing_tags": model.tags,
            "asset_types": model.asset_types,
            "runtime_metadata": model.runtime_metadata,
            "has_animation_thumbnail_frame": has_thumbnail,
            "has_animation_preview_video": has_preview,
        },
        "extra_context": extra_context or {},
        "contract": {
            "actorKind": "avatar, agent, animal, mount, vehicle, or object.",
            "skeletonProfile": "vrm-humanoid, mixamo-humanoid, quadruped, bird, fish, vehicle, or unknown.",
            "intents": "One or more short runtime intents such as idle, walk, run, dance, wave, talk_idle, jump, throw, mount, dismount, graze, flap, fly, attack, sit, or gesture.",
            "category": "locomotion, gesture, dance, action, sport, pose, ambient, transition, or other.",
            "tags": "Search tags for text-intent matching, not file formats.",
            "loop": "True for ambient/continuous clips like idle, walk, run, dance, talk_idle, graze, fly.",
            "rootMotion": "in-place, root-motion, mixed, or unknown.",
            "speedMetersPerSecond": "Approximate locomotion speed when knowable, otherwise null.",
            "direction": "forward, backward, left, right, turn-left, turn-right, none, or unknown.",
            "gait": "walk, trot, canter, gallop, flap, swim, idle, run, or unknown.",
            "aliases": "Search aliases, including common names users might type.",
            "quality.issues": "Known problems such as foot-sliding, bad-loop, wrong-scale, jaw-smushed, hand-clipping, pose-drift, or broken-retarget.",
            "searchText": "Flattened searchable text covering name, aliases, intents, category, actorKind, skeletonProfile, and gait.",
        },
    }
    return (
        "Create searchable runtime metadata for a VRMA/BVH avatar animation catalog. "
        "Agents will use this metadata to call playIntent(actorId, intent), playSequence, and setLocomotion instead of filenames. "
        "Return JSON only. Do not write marketplace copy for a mesh; classify the motion clip. "
        "When a visual frame is attached, treat the visible pose/action as stronger evidence than the filename; "
        "filenames can be wrong (for example a file named bowing may visually read as elbowing). "
        "Use filename, tags, and runtime metadata only as secondary hints. "
        "Use deterministic concise fields and avoid inventing details not present in the visual frame or supplied metadata.\n\n"
        + json.dumps(payload, sort_keys=True)
    )


def _ai_animation_metadata(model, extra_context=None):
    provider = _provider()
    api_key = _api_key()
    if not api_key:
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "summary": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 14},
            "asset_styles": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "asset_types": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "categories": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
            "quality_notes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "animation": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "intent": {"type": "string"},
                    "intents": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "actorKind": {"type": "string"},
                    "skeletonProfile": {"type": "string"},
                    "category": {"type": "string"},
                    "bodyType": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 14},
                    "loop": {"type": "boolean"},
                    "duration": {"type": ["number", "null"]},
                    "durationSeconds": {"type": ["number", "null"]},
                    "transitionIn": {"type": "number"},
                    "transitionOut": {"type": "number"},
                    "energy": {"type": "string"},
                    "locomotion": {"type": "boolean"},
                    "rootMotion": {"type": "string"},
                    "speedMetersPerSecond": {"type": ["number", "null"]},
                    "direction": {"type": "string"},
                    "gait": {"type": "string"},
                    "transition": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "from": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                            "to": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                        },
                        "required": ["from", "to"],
                    },
                    "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                    "quality": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "score": {"type": "number"},
                            "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                        },
                        "required": ["score", "issues"],
                    },
                    "searchText": {"type": "string"},
                    "requiresMount": {"type": "boolean"},
                },
                "required": ["intent", "intents", "actorKind", "skeletonProfile", "category", "bodyType", "tags", "loop", "duration", "durationSeconds", "transitionIn", "transitionOut", "energy", "locomotion", "rootMotion", "speedMetersPerSecond", "direction", "gait", "transition", "aliases", "quality", "searchText", "requiresMount"],
            },
        },
        "required": ["title", "description", "summary", "tags", "asset_styles", "asset_types", "categories", "quality_notes", "animation"],
    }
    user_text = _animation_metadata_prompt(model, extra_context=extra_context)
    if _transport(provider) == "a2a":
        parts = [{"kind": "text", "text": user_text + "\n\nReturn JSON matching this schema:\n" + json.dumps(schema, sort_keys=True)}]
        image_part = _a2a_image_part(model)
        if image_part:
            parts.append(image_part)
        payload = _post_json(
            _base_url(provider),
            {
                "jsonrpc": "2.0",
                "id": f"animation-enrichment-{uuid.uuid4()}",
                "method": os.environ.get("HYADES_A2A_METHOD", "message/send"),
                "params": {
                    "message": {
                        "role": os.environ.get("HYADES_A2A_ROLE", "user"),
                        "parts": parts,
                        "messageId": f"animation-enrichment-{uuid.uuid4()}",
                    },
                    "metadata": {"model": _model_name(provider)},
                    "configuration": {"acceptedOutputModes": ["application/json", "text/plain"]},
                },
            },
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            provider=provider,
            transport="a2a",
        )
        output_text = _strip_json_fence(_extract_a2a_output(payload))
        task_id = _a2a_task_id(payload)
        if not output_text and task_id:
            payload = _poll_a2a_task(provider, api_key, task_id)
            output_text = _strip_json_fence(_extract_a2a_output(payload or {}))
        parsed = _parse_json_object(output_text, provider=provider, transport="a2a", label="animation enrichment provider")
        enriched = _normalize_animation_metadata(parsed, model, provider=provider, transport="a2a", response_id=payload.get("id") if isinstance(payload, dict) else None)
        enriched["vision_frame"] = bool(image_part)
        enriched["preview_video_available"] = bool(model.preview_file_id)
        return enriched

    image = _image_part(model) if _openai_transport_supports_image_parts(provider) else None
    user_content = [{"type": "text", "text": user_text}, image] if image else user_text
    body = {
        "model": _model_name(provider),
        "messages": [
            {"role": "system", "content": "You classify VRMA avatar animation clips for fast deterministic runtime intent matching. Return JSON only."},
            {"role": "user", "content": user_content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "animation_enrichment", "strict": True, "schema": schema},
        },
    }
    payload = _post_json(
        _request_url(_base_url(provider)),
        body,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        provider=provider,
        transport="openai",
    )
    parsed = _parse_json_object(_strip_json_fence(_extract_chat_output(payload)), provider=provider, transport="openai", label="animation enrichment provider")
    enriched = _normalize_animation_metadata(parsed, model, provider=provider, transport="openai", response_id=payload.get("id"))
    enriched["vision_frame"] = bool(image)
    enriched["preview_video_available"] = bool(model.preview_file_id)
    return enriched


def enrich_animation_clip(model, extra_context=None):
    enriched = _ai_animation_metadata(model, extra_context=extra_context)
    if enriched is None:
        enriched = _heuristic_animation_metadata(model, extra_context=extra_context)
        enriched["vision_frame"] = False
        enriched["preview_video_available"] = bool(model.preview_file_id)
    return _normalize_animation_metadata(enriched, model, provider=enriched.get("provider"), transport=enriched.get("transport"), response_id=enriched.get("response_id"))


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
        "specific details regarding its exact appearance",
        "optimal use cases cannot be confirmed",
        "as no preview thumbnail is available",
        "no thumbnail is available",
        "baseline for rendering",
        "foundational component",
        "starting point for further 3d sculpting",
    )
    return not lowered.strip() or any(fragment in lowered for fragment in generic_fragments)


def _generic_title(title):
    lowered = (title or "").strip().lower()
    if not lowered:
        return True
    return _contains_any(
        lowered,
        (
            "unknown",
            "ai-generated 3d model",
            "generated 3d model",
            "pixal3d",
            "3d model",
            "untitled",
            "asset",
            "hyades",
        ),
    )


def _vision_subject_metadata(text):
    lowered = (text or "").lower()
    if _contains_any(lowered, ("signpost", "sign post", "wooden sign", "signboard", "sign board")):
        tags = ["signpost", "wooden-sign", "signboard", "wood", "fantasy-prop", "environment-prop"]
        if _contains_any(lowered, ("cartoon", "stylized", "low-poly", "low poly")):
            tags.extend(["stylized", "low-poly"])
        if _contains_any(lowered, ("grass", "rocks", "foliage", "base")):
            tags.extend(["grass-base", "rocks"])
        return {
            "title": "Stylized Wooden Signpost",
            "asset_category": "environment",
            "tags": tags,
            "asset_styles": ["stylized", "fantasy", "low-poly"],
            "asset_types": ["decorative-prop", "low-poly"],
            "description": (
                "A stylized wooden signpost prop with a blank signboard, simple wood material, "
                "and a small grassy base with rocks and foliage. Suitable for fantasy, cartoon, "
                "or low-poly game environments that need customizable directional signs, markers, "
                "or scene dressing."
            ),
            "summary": "Stylized wooden signpost prop for fantasy or low-poly game scenes.",
        }
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
            "asset_types": ["decorative-prop"],
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
        if asset_type in {"static", "static-mesh"}:
            continue
        if asset_type not in normalized:
            normalized.append(asset_type)

    category_terms = {
        "building", "flora", "fauna", "person", "people", "vehicle", "environment",
        "material", "materials", "furniture", "animation", "other", "prop", "props",
        "3d-model", "3d-models",
    }
    normalized = [item for item in normalized if item not in category_terms and item != category]

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
        if _generic_title(enriched.get("title")):
            enriched["title"] = vision_subject["title"]
        if _generic_description(enriched.get("description")):
            enriched["description"] = vision_subject["description"]
            enriched["summary"] = vision_subject["summary"]
        enriched["tags"] = _clean_enrichment_tags((enriched.get("tags") or []) + vision_subject.get("tags", []))
        enriched["asset_styles"] = Model3D.normalize_tags((enriched.get("asset_styles") or []) + vision_subject.get("asset_styles", []))
        enriched["asset_types"] = Model3D.normalize_tags((enriched.get("asset_types") or []) + vision_subject.get("asset_types", []))
        if Model3D.normalize_category(enriched.get("asset_category")) in {None, "", "other", "prop", "props", "3d model", "3d models", "uncategorized"}:
            enriched["asset_category"] = vision_subject["asset_category"]

    category = Model3D.normalize_category(enriched.get("asset_category"))
    generic_categories = {None, "", "other", "prop", "props", "3d model", "3d models", "uncategorized"}
    subject_category_rules = [
        ("building", ("building", "house", "cottage", "tower", "castle", "temple", "hut", "cabin", "wall", "roof", "architecture", "timber-framed", "half-timbered", "tudor", "cupola", "balcony", "gazebo", "pavilion", "shed", "shop")),
        ("flora", ("flower", "flowers", "floral", "bouquet", "bloom", "blooms", "leaf", "leaves", "stem", "stems", "plant", "plants", "tree", "trees", "grass", "moss", "vine", "vines", "bush", "shrub", "fern", "mushroom", "berries", "foliage")),
        ("fauna", ("animal", "creature", "bird", "fish", "insect", "horse", "cat", "dog", "wolf", "dragon", "cow", "cattle", "deer", "butterfly", "bee", "bear", "fox", "rabbit", "mouse", "turtle", "frog", "snake", "lizard", "crab", "bird", "eagle")),
        ("person", ("person", "people", "human", "humanoid", "avatar", "vrm", "man", "woman", "girl", "boy", "male", "female", "warrior", "mage", "knight", "soldier", "villager")),
        ("furniture", ("furniture", "chair", "table", "desk", "bench", "sofa", "couch", "bed", "stool", "shelf", "cabinet", "dresser", "bookcase")),
        ("vehicle", ("vehicle", "car", "truck", "ship", "boat", "aircraft", "spaceship", "wagon", "cart", "rowboat", "canoe", "bicycle", "motorcycle")),
        ("environment", ("terrain", "landscape", "scene", "environment", "diorama", "level", "bridge", "fountain", "lantern", "lamp", "torch", "signpost", "sign", "path", "road", "rock", "rocks", "boulder", "water-feature", "water feature", "well", "fence", "gate", "arch", "ruin", "statue")),
    ]
    category_scores = [(candidate, _keyword_score(text, words)) for candidate, words in subject_category_rules]
    best_category, best_score = max(category_scores, key=lambda item: item[1])
    current_score = next((score for candidate, score in category_scores if candidate == category), 0)
    protected_category = Model3D.normalize_category(vision_subject.get("asset_category")) if vision_subject else None
    if protected_category:
        enriched["asset_category"] = protected_category
        category = protected_category
    elif best_score > 0 and best_category != category and best_category in {"building", "flora", "fauna", "person", "furniture", "vehicle", "environment"}:
        enriched["asset_category"] = best_category
        category = best_category
    elif best_score > 0 and (
        category in generic_categories
        or (best_category != category and best_score >= max(2, current_score + 2))
    ):
        enriched["asset_category"] = best_category
        category = best_category
    elif category in {"material", "materials"} and best_score > 0:
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
    enriched["asset_types"] = _resolve_type_conflicts(asset_types, category, text)

    title = str(enriched.get("title") or "").strip()
    generic_title = _generic_title(title)
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


def _parse_json_object(output_text, provider=None, transport=None, label="AI provider"):
    candidate = _extract_json_object_text(output_text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as error:
        detail = _compact_response_detail(output_text)
        if transport:
            label = f"{label}/{transport}"
        raise RuntimeError(
            f"{label} returned non-JSON output: {error.msg}. "
            f"Output: {detail or 'empty response'}"
        ) from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} returned JSON {type(parsed).__name__}, expected object.")
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
    return _image_part_from_bytes(stored, "image/webp")


def _image_part_from_bytes(stored, content_type="image/webp"):
    if not stored:
        return None
    max_bytes = int(os.environ.get("AI_AUTOTAG_MAX_IMAGE_BYTES", str(2 * 1024 * 1024)))
    if len(stored) > max_bytes:
        return None
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
    return _a2a_image_part_from_bytes(stored, "thumbnail.webp", "image/webp")


def _a2a_image_part_from_bytes(stored, name="thumbnail.webp", content_type="image/webp"):
    if not stored:
        return None
    max_bytes = int(os.environ.get("AI_AUTOTAG_MAX_IMAGE_BYTES", str(2 * 1024 * 1024)))
    if len(stored) > max_bytes:
        return None
    return {
        "kind": "file",
        "file": {
            "bytes": base64.b64encode(stored).decode("ascii"),
            "name": name,
            "mimeType": content_type,
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
        "Identify only the visible catalog facts for this 3D asset preview: subject, style, materials, "
        "colors, and likely buyer search terms. Do not discuss whether it is static, rigged, animated, "
        "emissive, or a light emitter.",
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
            TAGGER_SYSTEM_PROMPT + " " + FAB_LISTING_GUIDANCE + "\n\n"
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


AUTORIG_MARKER_KEYS = [
    "groin", "chest", "neck", "chin",
    "shoulderL", "shoulderR", "elbowL", "elbowR", "wristL", "wristR",
    "hipL", "hipR", "kneeL", "kneeR", "ankleL", "ankleR", "toeL", "toeR",
]


def _autorig_marker_schema():
    point_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "x": {"type": "number", "minimum": 0, "maximum": 1},
            "y": {"type": "number", "minimum": 0, "maximum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "visible": {"type": "boolean"},
        },
        "required": ["x", "y", "confidence", "visible"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "markers": {
                "type": "object",
                "additionalProperties": False,
                "properties": {key: point_schema for key in AUTORIG_MARKER_KEYS},
                "required": AUTORIG_MARKER_KEYS,
            },
            "facing": {"type": "string", "enum": ["front", "back", "side", "uncertain"]},
            "pose": {"type": "string", "enum": ["t-pose", "a-pose", "wide-stance", "posed", "unknown"]},
            "asymmetry": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "notes": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        },
        "required": ["markers", "facing", "pose", "asymmetry", "confidence", "warnings", "notes"],
    }


def _autorig_marker_prompt(model, view="front"):
    view = view if view in {"front", "profile"} else "front"
    view_instruction = (
        "This is a FRONT view. Return x/y for all body landmarks visible or inferable from the front silhouette. "
        "Focus on anatomical left/right and joint centers."
        if view == "front"
        else
        "This is a PROFILE/SIDE view. Return x/y for depth-critical landmarks only when visible or inferable: "
        "chest, neck, chin, elbowL/R, wristL/R, kneeL/R, ankleL/R, toeL/R. The client will preserve front-view x "
        "and use this profile image to correct y/depth. You may still include other markers at low confidence."
    )
    return (
        "You are placing 2D rigging landmarks for a humanoid 3D character from a rendered model view. "
        "Return concise JSON only. Coordinates are normalized image coordinates: x=0 left edge, x=1 right edge, "
        "y=0 top edge, y=1 bottom edge. Use the avatar's anatomical left/right, not the viewer's left/right. "
        "Place only visible or strongly inferable humanoid landmarks. For uncertain hidden landmarks, estimate from "
        "symmetry and set confidence below 0.55. Do not include prose outside JSON.\n\n"
        "Also classify whether the visible pose is close to a T-pose or A-pose. If arms or legs are strongly posed, "
        "crossed, hidden, very wide, bent, or visibly asymmetric, set pose to posed or wide-stance, set asymmetry to "
        "medium/high as appropriate, and add a warning that auto-rigging will need manual correction or a cleaner "
        "front/profile pass. Wide-legged fairies, dancing poses, crossed legs, one-arm poses, and non-mirrored "
        "silhouettes should not be treated as clean T/A-pose sources.\n\n"
        + view_instruction + "\n\n"
        "Required marker meanings:\n"
        "- groin: pelvis center between hip sockets.\n"
        "- chest: upper torso center around sternum.\n"
        "- neck: neck base, below jaw/head.\n"
        "- chin: lower face/head anchor, not top of hair.\n"
        "- shoulderL/R: avatar left/right shoulder sockets.\n"
        "- elbowL/R, wristL/R: avatar left/right arm joints.\n"
        "- hipL/R, kneeL/R, ankleL/R, toeL/R: avatar left/right leg joints and toe/foot direction points.\n\n"
        "Asset context: "
        + json.dumps({
            "name": model.name,
            "filename": model.original_filename,
            "format": model.file_format,
            "description": model.description,
            "view": view,
        }, sort_keys=True)
    )


def _normalize_marker_suggestions(parsed):
    raw = parsed.get("markers") if isinstance(parsed, dict) else {}
    markers = {}
    if isinstance(raw, dict):
        for key in AUTORIG_MARKER_KEYS:
            point = raw.get(key)
            if not isinstance(point, dict):
                continue
            try:
                x = max(0.0, min(1.0, float(point.get("x"))))
                y = max(0.0, min(1.0, float(point.get("y"))))
                confidence = max(0.0, min(1.0, float(point.get("confidence", 0.5))))
            except (TypeError, ValueError):
                continue
            markers[key] = {
                "x": x,
                "y": y,
                "confidence": confidence,
                "visible": bool(point.get("visible", True)),
            }
    notes = parsed.get("notes") if isinstance(parsed.get("notes"), list) else []
    warnings = parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []
    pose = parsed.get("pose") if parsed.get("pose") in {"t-pose", "a-pose", "wide-stance", "posed", "unknown"} else "unknown"
    asymmetry = parsed.get("asymmetry") if parsed.get("asymmetry") in {"low", "medium", "high", "unknown"} else "unknown"
    if pose not in {"t-pose", "a-pose"} and not warnings:
        warnings = ["Pose is not close to a clean T-pose or A-pose; review AI markers before building the rig."]
    if asymmetry in {"medium", "high"} and not any("asym" in str(warning).lower() for warning in warnings):
        warnings.append("Visible silhouette is asymmetric; mirrored marker placement may need manual correction.")
    return {
        "markers": markers,
        "facing": parsed.get("facing") if parsed.get("facing") in {"front", "back", "side", "uncertain"} else "uncertain",
        "pose": pose,
        "asymmetry": asymmetry,
        "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.5) or 0.5))),
        "warnings": [str(warning)[:180] for warning in warnings[:4]],
        "notes": [str(note)[:160] for note in notes[:4]],
    }


def suggest_autorig_markers(model, image_bytes=None, image_mime="image/webp", view="front"):
    provider = _provider()
    api_key = _api_key()
    if not api_key:
        return None
    image_bytes = image_bytes or _thumbnail_bytes(model)
    image_mime = image_mime or "image/webp"
    view = view if view in {"front", "profile"} else "front"
    if not image_bytes:
        raise RuntimeError("Auto-rig marker suggestion requires a saved thumbnail.")

    schema = _autorig_marker_schema()
    user_text = _autorig_marker_prompt(model, view=view)

    if _transport(provider) == "a2a":
        image_part = _a2a_image_part_from_bytes(image_bytes, f"autorig-{view}.webp", image_mime)
        if not image_part:
            raise RuntimeError("Auto-rig marker suggestion requires an image-capable thumbnail.")
        text_part = {"kind": "text", "text": user_text + "\n\nReturn JSON matching this schema:\n" + json.dumps(schema, sort_keys=True)}
        payload = _post_json(
            _base_url(provider),
            {
                "jsonrpc": "2.0",
                "id": f"autorig-markers-{uuid.uuid4()}",
                "method": os.environ.get("HYADES_A2A_METHOD", "message/send"),
                "params": {
                    "message": {
                        "role": os.environ.get("HYADES_A2A_ROLE", "user"),
                        "parts": [text_part, image_part],
                        "messageId": f"autorig-markers-{uuid.uuid4()}",
                    },
                    "metadata": {"model": _model_name(provider)},
                    "configuration": {"acceptedOutputModes": ["application/json", "text/plain"]},
                },
            },
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            provider=provider,
            transport="a2a",
        )
        output_text = _strip_json_fence(_extract_a2a_output(payload))
        task_id = _a2a_task_id(payload)
        if not output_text and task_id:
            payload = _poll_a2a_task(provider, api_key, task_id)
            output_text = _strip_json_fence(_extract_a2a_output(payload or {}))
        parsed = _parse_json_object(output_text, provider=provider, transport="a2a", label="auto-rig marker provider")
        result = _normalize_marker_suggestions(parsed)
        result.update({
            "provider": provider,
            "transport": "a2a",
            "model": _model_name(provider),
            "view": view,
            "response_id": payload.get("id") if isinstance(payload, dict) else None,
        })
        return result

    image = _image_part_from_bytes(image_bytes, image_mime) if _openai_transport_supports_image_parts(provider) else None
    if not image:
        raise RuntimeError("Auto-rig marker suggestion requires image input support.")
    base_messages = [
        {
            "role": "system",
            "content": "You are a precise humanoid rigging assistant. Return JSON only.",
        },
        {"role": "user", "content": [{"type": "text", "text": user_text}, image]},
    ]
    strict_body = {
        "model": _model_name(provider),
        "messages": base_messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "autorig_marker_suggestions",
                "strict": True,
                "schema": schema,
            },
        },
    }
    request_url = _request_url(_base_url(provider))
    try:
        payload = _post_json(
            request_url,
            strict_body,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            provider=provider,
            transport="openai",
        )
        transport = "openai"
    except RuntimeError as error:
        detail = str(error).lower()
        schema_rejected = (
            "response_format" in detail
            or "json_schema" in detail
            or "schema" in detail
            or "strict" in detail
        )
        if not schema_rejected or not _env_bool("AI_AUTORIG_RETRY_SCHEMALESS", True):
            raise
        fallback_messages = [
            base_messages[0],
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            user_text
                            + "\n\nReturn JSON matching this schema exactly:\n"
                            + json.dumps(schema, sort_keys=True)
                        ),
                    },
                    image,
                ],
            },
        ]
        payload = _post_json(
            request_url,
            {"model": _model_name(provider), "messages": fallback_messages},
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            provider=provider,
            transport="openai-schemaless",
        )
        transport = "openai-schemaless"
    parsed = _parse_json_object(_extract_chat_output(payload), provider=provider, transport=transport, label="auto-rig marker provider")
    result = _normalize_marker_suggestions(parsed)
    result.update({
        "provider": provider,
        "transport": transport,
        "model": _model_name(provider),
        "view": view,
        "response_id": payload.get("id") if isinstance(payload, dict) else None,
    })
    return result


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
                "maxItems": 10,
                "description": "Discoverability tags useful to game designers and 3D artists. Use concrete subject, style, material, genre, category, and use-case tags. Do not include file formats, generator names, or generic pipeline/source tags.",
            },
            "title": {
                "type": "string",
                "description": "A Fab listing title under 80 characters based on the visible subject and style, not the filename, generator, provider, or generic phrases like AI-generated 3D model.",
            },
            "asset_category": {
                "type": "string",
                "description": "One broad what-it-is bucket for filtering. Use this taxonomy: fauna for all animals/creatures; flora for all plants/trees/flowers/bushes/grass/mushrooms; building for all buildings/architecture; furniture for furniture; person for people/humanoids/avatars; vehicle for vehicles; environment for outdoor non-living scene objects such as bridges, fountains, lanterns, signs, rocks, terrain, paths, and water features; do not use material as a category; prop only when no specific bucket applies.",
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
                "description": "Non-runtime marketplace/use traits such as game-ready, modular, decorative-prop, pbr, tileable, high-poly, low-poly, kitbash, environment-piece. Do not include static, rigged, animated, light-emitter, emissive, glowing, vrm, or optimized; those are derived from file/runtime metadata.",
            },
            "description": {
                "type": "string",
                "description": "Buyer-facing Fab product description for a 3D listing. Start with what the asset is, then describe style/materials and practical scene/use cases. Avoid AI pipeline provenance, unsupported technical specs, analysis headings, and static/rigged/animated/emissive commentary.",
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
        "field_contract": {
            "title": "Short product title only.",
            "description": "Polished buyer-facing copy only. No analysis sections or runtime debates.",
            "tags": "Up to 10 lowercase search tags.",
            "asset_category": "One broad subject bucket.",
            "asset_styles": "Aesthetic/genre/medium labels.",
            "asset_types": "Marketplace/use traits only, never static, rigged, animated, emissive, light-emitter, optimized, or file format.",
        },
    }
    vision_mcp = _zai_mcp_visual_context_result(model, provider, api_key)
    vision_mcp_analysis = vision_mcp.get("analysis")
    if vision_mcp_analysis:
        prompt["vision_mcp_analysis"] = vision_mcp_analysis
        prompt["metadata_instruction"] = (
            "Use vision_mcp_analysis as the primary source for visible subject, materials, style, colors, "
            "and buyer search terms. Avoid generic generation-pipeline wording such as baseline mesh, "
            "background prop, sculpting base, or retopology unless those details are visibly supported. "
            "Always fill asset_category and asset_styles from the visual analysis, and fill asset_types only for "
            "non-structural traits. For example, "
            "flowers/plants/leaves should use asset_category flora even when the object is also decorative; "
            "animals should use fauna; buildings should use building; furniture should use furniture; "
            "human or humanoid avatars should use person; bridges, fountains, lanterns, signs, terrain pieces, "
            "and other outdoor non-living scene objects should use environment; do not use material as a category, "
            "even for texture, shader, tileable surface, or material-map assets. Keep material information in tags, "
            "asset_types, and description copy instead. "
            "watercolor or hand-painted looks belong in asset_styles; do not report static, rigged, or animated "
            "or emissive/light behavior from vision because those are derived outside auto-tagging. "
            "The title should name the visible asset, for example Watercolor "
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
        + CATEGORY_TAXONOMY_GUIDANCE + " "
        "Prefer concrete visible or file-derived details over generic filler. "
        "Write the title as a concise product/catalog name for the visible subject and style. "
            "Do not leave asset_category or asset_styles empty when visual analysis provides evidence. "
            "Use asset_category for the broad subject bucket, asset_styles for aesthetic/genre/medium, "
            "and asset_types for non-structural technical/use traits. Use buyer search terminology, "
            "but avoid keyword stuffing. Do not mention thumbnail availability, AI generation, provider names, "
            "or pipeline details. Do not invent technical details that are not directly supplied. "
            "Do not discuss or classify static state, rigging, animation, emissive materials, or light emitting behavior. "
            "Return each field directly; do not put the visual-analysis transcript into description or summary. "
            "If visual analysis is unavailable, say only what can be inferred from the filename and asset fields.\n\n"
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
                    "content": TAGGER_SYSTEM_PROMPT + " " + FAB_LISTING_GUIDANCE,
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
    enriched["asset_types"] = [
        value for value in _resolve_type_conflicts(enriched.get("asset_types", []), enriched["asset_category"], cleanup_text)
        if value not in {"static", "static-mesh", "rigged", "animated", "light-emitter", "emissive", "glowing", "vrm", "optimized"}
    ]
    enriched["runtime_metadata"] = {}
    enriched["tags"] = _clean_enrichment_tags(enriched.get("tags", []))
    enriched["description"] = (enriched.get("description") or "").strip()
    enriched["summary"] = (enriched.get("summary") or "").strip()
    enriched.setdefault("categories", [])
    enriched.setdefault("quality_notes", [])
    return enriched
