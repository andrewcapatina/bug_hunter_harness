"""Profile-driven model client — BUG_HUNTER_SPEC.md §3.

The model is a PROFILE (config), not code. Swapping DeepSeek-V4-Flash for
another local model or a frontier model is editing the profile — endpoint,
model id (or "auto" discovery), sampling policy, context window, response
format, reasoning flag. Batch sizing derives from `context_window_tokens`
(batch.py), never a hardcoded char budget.

Keeps the model-family robustness both bots learned the hard way: guided JSON,
tolerant extraction (skip reasoning, strip fences, balanced-brace scan), and
per-batch isolation so one batch's failure never sinks the run.

Transport is two module-level hooks (`_http_post`/`_http_get`) so tests inject
responses with no live endpoint.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class ModelError(Exception):
    pass


@dataclass
class ModelProfile:
    endpoint: str                                  # OpenAI-compatible /v1 base
    model: str = "auto"                            # id, or "auto" -> /v1/models
    context_window_tokens: int = 32_000            # batch sizing derives from this
    max_output_tokens: int = 8_192
    request_timeout_s: float = 600.0
    sampling: object = "server_default"            # "server_default" | {temperature,...}
    sampling_incompatible: list = field(default_factory=list)  # params a lane 400s on
    response_format: str = "json_object"           # json_object | json_schema | none
    reasoning_model: bool = True                    # strips </think>, sampling policy
    api_key: str | None = None                      # frontier models; local vLLM = None

    @classmethod
    def from_config(cls, d: dict) -> "ModelProfile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ChatStats:
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    elapsed_s: float
    finish_reason: str | None
    attempt: int


@dataclass
class BatchResult:
    label: str
    ok: bool
    data: dict | None
    stats: ChatStats | None
    error: str | None


# --------------------------------------------------------------------------- #
# Transport hooks (patched in tests).
# --------------------------------------------------------------------------- #
def _http_post(url: str, payload: dict, timeout: float, headers: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Model discovery (cached per endpoint).
# --------------------------------------------------------------------------- #
_MODEL_CACHE: dict[str, str] = {}


def resolve_model(profile: ModelProfile) -> str:
    if profile.model and profile.model != "auto":
        return profile.model
    base = profile.endpoint.rstrip("/")
    if base in _MODEL_CACHE:
        return _MODEL_CACHE[base]
    resp = _http_get(base + "/models")
    data = resp.get("data") or []
    if not data:
        raise ModelError(f"no models served at {base}/models")
    model_id = data[0]["id"]
    _MODEL_CACHE[base] = model_id
    return model_id


def clear_model_cache() -> None:
    _MODEL_CACHE.clear()


# --------------------------------------------------------------------------- #
# JSON extraction — tolerant of reasoning prose, fences, surrounding text.
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _first_json_object(text: str) -> str | None:
    """First balanced {...} object, respecting string literals so braces inside
    strings don't miscount."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> dict:
    if not text or not text.strip():
        raise ModelError("empty model response")
    t = text
    if "</think>" in t:                      # reasoning models leak their trace
        t = t.rsplit("</think>", 1)[-1]
    t = t.strip()
    for candidate in (t, _FENCE.sub("", t).strip(), _first_json_object(t)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ModelError(f"no JSON object in response: {text[:200]!r}")


# --------------------------------------------------------------------------- #
# Chat.
# --------------------------------------------------------------------------- #
def _build_payload(profile, model, system, user, json_schema) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "max_tokens": profile.max_output_tokens,
    }
    if profile.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif profile.response_format == "json_schema" and json_schema is not None:
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": json_schema}
    if isinstance(profile.sampling, dict):
        for k, v in profile.sampling.items():
            if k not in profile.sampling_incompatible:
                payload[k] = v
    return payload


def chat(profile: ModelProfile, system: str, user: str, *,
         json_schema: dict | None = None, attempts: int = 2):
    """One structured-JSON turn. Returns (parsed_dict, ChatStats). One
    corrective retry by default (network blip or unparseable JSON)."""
    model = resolve_model(profile)
    url = profile.endpoint.rstrip("/") + "/chat/completions"
    payload = _build_payload(profile, model, system, user, json_schema)
    headers = {"Authorization": f"Bearer {profile.api_key}"} if profile.api_key else {}
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        t0 = time.monotonic()
        try:
            resp = _http_post(url, payload, profile.request_timeout_s, headers)
            choice = resp["choices"][0]
            parsed = extract_json(choice["message"]["content"])
            usage = resp.get("usage") or {}
            return parsed, ChatStats(
                model=model,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                elapsed_s=round(time.monotonic() - t0, 2),
                finish_reason=choice.get("finish_reason"),
                attempt=attempt)
        except (ModelError, KeyError, IndexError, ValueError,
                urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
    raise ModelError(f"chat failed after {attempts} attempts: {last}")


def chat_batch(profile: ModelProfile, items, *,
               json_schema: dict | None = None) -> list[BatchResult]:
    """Run each (label, system, user) with per-batch isolation — a failing
    batch becomes an ok=False result, never an exception that sinks the run."""
    results: list[BatchResult] = []
    for label, system, user in items:
        try:
            data, stats = chat(profile, system, user, json_schema=json_schema)
            results.append(BatchResult(label, True, data, stats, None))
        except Exception as e:  # noqa: BLE001 — isolation is the whole point
            results.append(BatchResult(label, False, None, None, str(e)))
    return results
