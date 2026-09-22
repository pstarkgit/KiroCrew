"""Bounded, local-only Jev shadow seam for one redacted session-summary signal.

This is a typed entry point, not a product integration. It does not call a
provider, write a log of the payload, or drive skills, memory, the knowledge
graph, routing, or cron. The Decisions (Jev) settings card stays honest: this
module does not publish a ``decisions`` config section and does not imply a
live model.

Default is deny. Only an exact ``enabled=True`` on the request (or
``jev.shadow_enabled is True`` via :func:`enabled_from_mapping`) runs the
shadow path, and even then the only legal input is an allowlisted, already
redacted session-summary payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from kiro_crew.session_summary import (
    STATE_DONE,
    STATE_DROPPED,
    STATE_IN_PROGRESS,
    STATE_NEEDS_YOU,
    redact_payload,
)

logger = logging.getLogger(__name__)

SIGNAL_SESSION_SUMMARY = "session_summary"
ALLOWLISTED_SIGNALS = frozenset({SIGNAL_SESSION_SUMMARY})

STATUS_DISABLED = "disabled"
STATUS_REJECTED = "rejected"
STATUS_SHADOWED = "shadowed"

REASON_DISABLED = "disabled"
REASON_UNKNOWN_SIGNAL = "unknown_signal"
REASON_MALFORMED_PAYLOAD = "malformed_payload"
REASON_RAW_CONTENT = "raw_content"
REASON_UNREDACTED_SECRET = "unredacted_secret"
REASON_OVERSIZE = "oversize"

SHADOW_MODE = "shadow"
SHADOW_ACTION_NONE = "none"
SHADOW_BACKEND_LOCAL = "local"

# Stored session-summary payloads from ``normalize_payload`` plus the two
# list containers. Anything else is either raw transcript/memory or unknown.
_SESSION_SUMMARY_TOP_KEYS = frozenset({"intents", "constraints"})
_INTENT_KEYS = frozenset(
    {
        "title",
        "initial_intent",
        "progress",
        "next_steps",
        "ranges",
        "status",
        "verified",
        "state",
        "last_touched_turn",
        "origin_turn",
    }
)
_NEXT_STEP_KEYS = frozenset({"what", "why", "expect"})
_PROGRESS_STATES = frozenset({"active", "completed", "abandoned"})
_PANEL_STATES = frozenset({STATE_DONE, STATE_NEEDS_YOU, STATE_IN_PROGRESS, STATE_DROPPED})

# Keys that mean the caller handed over chat, memory, or secret material
# instead of the redacted summary shape. Matched case-insensitively.
_FORBIDDEN_KEYS = frozenset(
    {
        "messages",
        "message",
        "content",
        "chat",
        "transcript",
        "records",
        "memory",
        "lessons",
        "preferences",
        "secrets",
        "secret",
        "token",
        "api_key",
        "password",
        "authorization",
        "cookie",
        "history",
        "raw",
        "text",
        "prompt",
        "system",
        "role",
    }
)

_MAX_INTENTS = 50
_MAX_CONSTRAINTS = 50
_MAX_STRING_CHARS = 4000
_MAX_PROGRESS_ITEMS = 50
_MAX_NEXT_STEPS = 20
_MAX_RANGES = 20
_MAX_PAYLOAD_BYTES = 64_000

_RECEIPT_RESULT_KEYS = frozenset(
    {
        "mode",
        "action",
        "backend",
        "signal",
        "fingerprint",
        "intent_count",
        "constraint_count",
    }
)


def enabled_from_mapping(config: object) -> bool:
    """Read the explicit enable flag from a mapping.

    Only ``config["jev"]["shadow_enabled"] is True`` enables. A missing
    section, a non-mapping, ``"true"``, ``1``, or any other value is deny.
    This helper does not read ``decisions.preview``: that flag belongs to a
    settings card this seam does not implement.
    """
    if not isinstance(config, Mapping):
        return False
    section = config.get("jev")
    if not isinstance(section, Mapping):
        return False
    return section.get("shadow_enabled") is True


@dataclass(frozen=True)
class JevShadowRequest:
    """Local-only shadow request. ``enabled`` defaults to deny."""

    signal: str
    payload: Mapping[str, Any]
    enabled: bool = False


@dataclass(frozen=True)
class JevShadowReceipt:
    """Bounded receipt. Never echoes the payload or any of its strings."""

    accepted: bool
    status: str
    signal: str
    reason: str
    result: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "accepted": self.accepted,
            "status": self.status,
            "signal": self.signal,
            "reason": self.reason,
        }
        if self.result is not None:
            out["result"] = dict(self.result)
        return out


def evaluate_shadow(request: JevShadowRequest) -> JevShadowReceipt:
    """Admit one allowlisted redacted session-summary signal, or deny.

    Disabled requests return before the payload is inspected, so a default-off
    caller cannot leak raw content through this function's logs or receipt.
    """
    signal = request.signal if request.signal in ALLOWLISTED_SIGNALS else ""
    if request.enabled is not True:
        return _receipt(
            accepted=False,
            status=STATUS_DISABLED,
            signal=signal,
            reason=REASON_DISABLED,
        )
    if request.signal not in ALLOWLISTED_SIGNALS:
        logger.debug("jev shadow rejected: %s", REASON_UNKNOWN_SIGNAL)
        return _receipt(
            accepted=False,
            status=STATUS_REJECTED,
            signal="",
            reason=REASON_UNKNOWN_SIGNAL,
        )
    reason = _validate_session_summary_payload(request.payload)
    if reason is not None:
        logger.debug("jev shadow rejected: %s", reason)
        return _receipt(
            accepted=False,
            status=STATUS_REJECTED,
            signal=SIGNAL_SESSION_SUMMARY,
            reason=reason,
        )
    result = _local_shadow_result(request.payload)
    logger.debug("jev shadow accepted: %s", STATUS_SHADOWED)
    return _receipt(
        accepted=True,
        status=STATUS_SHADOWED,
        signal=SIGNAL_SESSION_SUMMARY,
        reason=STATUS_SHADOWED,
        result=result,
    )


def _receipt(
    *,
    accepted: bool,
    status: str,
    signal: str,
    reason: str,
    result: Mapping[str, Any] | None = None,
) -> JevShadowReceipt:
    if result is not None:
        extra = set(result) - _RECEIPT_RESULT_KEYS
        if extra:
            raise ValueError(f"shadow result keys outside the receipt allowlist: {extra}")
    return JevShadowReceipt(
        accepted=accepted,
        status=status,
        signal=signal,
        reason=reason,
        result=result,
    )


def _validate_session_summary_payload(payload: object) -> str | None:
    if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
        return REASON_MALFORMED_PAYLOAD
    if _has_forbidden_key(payload):
        return REASON_RAW_CONTENT
    extra = set(payload) - _SESSION_SUMMARY_TOP_KEYS
    if extra:
        return REASON_MALFORMED_PAYLOAD
    intents = payload.get("intents")
    constraints = payload.get("constraints", [])
    if not isinstance(intents, list) or not intents:
        return REASON_MALFORMED_PAYLOAD
    if not isinstance(constraints, list):
        return REASON_MALFORMED_PAYLOAD
    if len(intents) > _MAX_INTENTS or len(constraints) > _MAX_CONSTRAINTS:
        return REASON_OVERSIZE
    for intent in intents:
        intent_reason = _validate_intent(intent)
        if intent_reason is not None:
            return intent_reason
    for item in constraints:
        if not isinstance(item, str):
            return REASON_MALFORMED_PAYLOAD
        bound = _bounded_string(item)
        if bound is not None:
            return bound
    encoded = _canonical_json(payload)
    if encoded is None:
        return REASON_MALFORMED_PAYLOAD
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        return REASON_OVERSIZE
    if _looks_like_raw_chat(payload):
        return REASON_RAW_CONTENT
    if redact_payload(dict(payload)) != _plain(payload):
        return REASON_UNREDACTED_SECRET
    return None


def _validate_intent(intent: object) -> str | None:
    if not isinstance(intent, Mapping):
        return REASON_MALFORMED_PAYLOAD
    if _has_forbidden_key(intent):
        return REASON_RAW_CONTENT
    extra = set(intent) - _INTENT_KEYS
    if extra:
        return REASON_MALFORMED_PAYLOAD
    title = intent.get("title")
    if not isinstance(title, str) or not title.strip():
        return REASON_MALFORMED_PAYLOAD
    for key in ("title", "initial_intent"):
        if key in intent:
            if not isinstance(intent[key], str):
                return REASON_MALFORMED_PAYLOAD
            bound = _bounded_string(intent[key])
            if bound is not None:
                return bound
    if "status" in intent and intent["status"] not in _PROGRESS_STATES:
        return REASON_MALFORMED_PAYLOAD
    if "state" in intent and intent["state"] not in _PANEL_STATES:
        return REASON_MALFORMED_PAYLOAD
    if "verified" in intent:
        verified = intent["verified"]
        if verified is not None and not isinstance(verified, bool):
            return REASON_MALFORMED_PAYLOAD
    if "last_touched_turn" in intent and not _is_int(intent["last_touched_turn"]):
        return REASON_MALFORMED_PAYLOAD
    if "origin_turn" in intent:
        origin = intent["origin_turn"]
        if origin is not None and not _is_int(origin):
            return REASON_MALFORMED_PAYLOAD
    if "progress" in intent:
        progress = intent["progress"]
        if not isinstance(progress, list) or len(progress) > _MAX_PROGRESS_ITEMS:
            return REASON_OVERSIZE if isinstance(progress, list) else REASON_MALFORMED_PAYLOAD
        for item in progress:
            if not isinstance(item, str):
                return REASON_MALFORMED_PAYLOAD
            bound = _bounded_string(item)
            if bound is not None:
                return bound
    if "next_steps" in intent:
        steps = intent["next_steps"]
        if not isinstance(steps, list) or len(steps) > _MAX_NEXT_STEPS:
            return REASON_OVERSIZE if isinstance(steps, list) else REASON_MALFORMED_PAYLOAD
        for step in steps:
            step_reason = _validate_next_step(step)
            if step_reason is not None:
                return step_reason
    if "ranges" in intent:
        ranges = intent["ranges"]
        if not isinstance(ranges, list) or len(ranges) > _MAX_RANGES:
            return REASON_OVERSIZE if isinstance(ranges, list) else REASON_MALFORMED_PAYLOAD
        for item in ranges:
            if not isinstance(item, list) or len(item) != 2:
                return REASON_MALFORMED_PAYLOAD
            start, end = item
            if not _is_int(start) or not _is_int(end) or start < 1 or end < start:
                return REASON_MALFORMED_PAYLOAD
    return None


def _validate_next_step(step: object) -> str | None:
    if not isinstance(step, Mapping):
        return REASON_MALFORMED_PAYLOAD
    if _has_forbidden_key(step):
        return REASON_RAW_CONTENT
    extra = set(step) - _NEXT_STEP_KEYS
    if extra:
        return REASON_MALFORMED_PAYLOAD
    what = step.get("what")
    if not isinstance(what, str) or not what.strip():
        return REASON_MALFORMED_PAYLOAD
    for key in _NEXT_STEP_KEYS:
        if key in step:
            if not isinstance(step[key], str):
                return REASON_MALFORMED_PAYLOAD
            bound = _bounded_string(step[key])
            if bound is not None:
                return bound
    return None


def _bounded_string(value: str) -> str | None:
    if len(value) > _MAX_STRING_CHARS:
        return REASON_OVERSIZE
    return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                return True
            if _has_forbidden_key(nested):
                return True
    elif isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


def _looks_like_raw_chat(value: object) -> bool:
    """Reject transcript-shaped lists even if they sat under an allowed key."""
    if isinstance(value, list):
        if value and all(
            isinstance(item, Mapping) and "role" in item and "content" in item for item in value
        ):
            return True
        return any(_looks_like_raw_chat(item) for item in value)
    if isinstance(value, Mapping):
        return any(_looks_like_raw_chat(item) for item in value.values())
    return False


def _plain(value: object) -> Any:
    """JSON-comparable copy so redaction equality does not depend on Mapping type."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes | None:
    try:
        return json.dumps(
            _plain(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _local_shadow_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic local observation. ``action`` is always none."""
    encoded = _canonical_json(payload)
    assert encoded is not None  # validation already canonicalized
    digest = hashlib.sha256(encoded).hexdigest()
    intents = payload.get("intents")
    constraints = payload.get("constraints", [])
    intent_count = len(intents) if isinstance(intents, list) else 0
    constraint_count = len(constraints) if isinstance(constraints, list) else 0
    return {
        "mode": SHADOW_MODE,
        "action": SHADOW_ACTION_NONE,
        "backend": SHADOW_BACKEND_LOCAL,
        "signal": SIGNAL_SESSION_SUMMARY,
        "fingerprint": digest,
        "intent_count": intent_count,
        "constraint_count": constraint_count,
    }
