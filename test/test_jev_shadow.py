"""Jev shadow sidecar: default-deny, redacted session-summary only, no side effects."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping as AbcMapping
from pathlib import Path

from kiro_crew.jev import (
    ALLOWLISTED_SIGNALS,
    SIGNAL_SESSION_SUMMARY,
    JevShadowRequest,
    enabled_from_mapping,
    evaluate_shadow,
)
from kiro_crew.jev.shadow import (
    REASON_DISABLED,
    REASON_MALFORMED_PAYLOAD,
    REASON_OVERSIZE,
    REASON_RAW_CONTENT,
    REASON_UNKNOWN_SIGNAL,
    REASON_UNREDACTED_SECRET,
    SHADOW_ACTION_NONE,
    SHADOW_BACKEND_LOCAL,
    STATUS_DISABLED,
    STATUS_REJECTED,
    STATUS_SHADOWED,
)

_UNIQUE_MARKER = "UNIQUE_JEV_SHADOW_PAYLOAD_MARKER_7f3c"


def _summary_payload(**overrides):
    payload = {
        "intents": [
            {
                "title": "Ship the shadow sidecar",
                "initial_intent": "Add a local Jev seam",
                "progress": ["typed request and receipt"],
                "next_steps": [
                    {"what": "Keep it shadow-only", "why": "no live routing", "expect": "none"}
                ],
                "ranges": [[1, 2]],
                "status": "active",
                "verified": None,
                "state": "in-progress",
                "last_touched_turn": 2,
                "origin_turn": 1,
            }
        ],
        "constraints": ["do not call a provider"],
    }
    payload.update(overrides)
    return payload


def _request(*, enabled=False, signal=SIGNAL_SESSION_SUMMARY, payload=None):
    return JevShadowRequest(
        signal=signal,
        payload=_summary_payload() if payload is None else payload,
        enabled=enabled,
    )


class TestDefaultDeny:
    def test_request_defaults_to_disabled(self):
        request = JevShadowRequest(signal=SIGNAL_SESSION_SUMMARY, payload=_summary_payload())
        assert request.enabled is False

    def test_disabled_request_is_denied_even_with_a_valid_payload(self):
        receipt = evaluate_shadow(_request(enabled=False))
        assert receipt.accepted is False
        assert receipt.status == STATUS_DISABLED
        assert receipt.reason == REASON_DISABLED
        assert receipt.result is None

    def test_omitted_enabled_flag_is_deny(self):
        receipt = evaluate_shadow(_request())
        assert receipt.status == STATUS_DISABLED
        assert receipt.accepted is False

    def test_truthy_non_true_enabled_is_still_deny(self):
        request = JevShadowRequest(
            signal=SIGNAL_SESSION_SUMMARY,
            payload=_summary_payload(),
            enabled="true",  # type: ignore[arg-type]
        )
        receipt = evaluate_shadow(request)
        assert receipt.status == STATUS_DISABLED

    def test_disabled_path_does_not_inspect_raw_payload(self):
        """Default-off must not reject-for-content: that would process raw chat."""
        receipt = evaluate_shadow(
            _request(
                enabled=False,
                payload={"messages": [{"role": "user", "content": "secret chat"}]},
            )
        )
        assert receipt.status == STATUS_DISABLED
        assert receipt.reason == REASON_DISABLED

    def test_enabled_from_mapping_defaults_to_false(self):
        assert enabled_from_mapping(None) is False
        assert enabled_from_mapping({}) is False
        assert enabled_from_mapping({"jev": {}}) is False
        assert enabled_from_mapping({"jev": {"shadow_enabled": False}}) is False
        assert enabled_from_mapping({"jev": {"shadow_enabled": "true"}}) is False
        assert enabled_from_mapping({"jev": {"shadow_enabled": 1}}) is False
        assert enabled_from_mapping({"decisions": {"preview": True}}) is False

    def test_enabled_from_mapping_requires_exact_true(self):
        assert enabled_from_mapping({"jev": {"shadow_enabled": True}}) is True


class TestMalformedAndRawRejection:
    def test_unknown_signal_is_rejected(self):
        receipt = evaluate_shadow(_request(enabled=True, signal="skills.select"))
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason == REASON_UNKNOWN_SIGNAL
        assert receipt.signal == ""
        assert receipt.result is None

    def test_only_session_summary_is_allowlisted(self):
        assert ALLOWLISTED_SIGNALS == frozenset({SIGNAL_SESSION_SUMMARY})

    def test_missing_intents_is_malformed(self):
        receipt = evaluate_shadow(_request(enabled=True, payload={"constraints": []}))
        assert receipt.reason == REASON_MALFORMED_PAYLOAD
        assert receipt.status == STATUS_REJECTED

    def test_empty_intents_is_malformed(self):
        receipt = evaluate_shadow(_request(enabled=True, payload={"intents": []}))
        assert receipt.reason == REASON_MALFORMED_PAYLOAD

    def test_unknown_top_level_key_is_malformed(self):
        payload = _summary_payload()
        payload["extra"] = "nope"
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.reason == REASON_MALFORMED_PAYLOAD

    def test_generation_envelope_keys_are_admitted(self):
        payload = _summary_payload()
        payload.update(
            {
                "generated_at": 1.5,
                "user_turns": 3,
                "last_activity": "2026-08-10T10:00:00+00:00",
                "sig": 1760000000.5,
                "gen": 3,
            }
        )
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.status == STATUS_SHADOWED
        assert receipt.accepted is True

    def test_nested_stowaway_under_envelope_key_is_malformed(self):
        payload = _summary_payload()
        payload["generated_at"] = {"hidden": "nope"}
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.reason == REASON_MALFORMED_PAYLOAD
        assert receipt.accepted is False

    def test_raw_messages_are_rejected(self):
        receipt = evaluate_shadow(
            _request(
                enabled=True,
                payload={"messages": [{"role": "user", "content": "hello from chat"}]},
            )
        )
        assert receipt.reason == REASON_RAW_CONTENT
        assert receipt.accepted is False
        assert receipt.result is None

    def test_memory_document_is_rejected(self):
        receipt = evaluate_shadow(
            _request(
                enabled=True,
                payload={"memory": {"preferences": "likes rust"}, "intents": []},
            )
        )
        assert receipt.reason == REASON_RAW_CONTENT

    def test_intent_with_transcript_fields_is_rejected(self):
        payload = _summary_payload()
        payload["intents"][0]["content"] = "user said do the thing"
        payload["intents"][0]["role"] = "user"
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.reason == REASON_RAW_CONTENT

    def test_unredacted_secret_is_rejected(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        payload = _summary_payload()
        payload["intents"][0]["title"] = f"rotate {secret}"
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.reason == REASON_UNREDACTED_SECRET
        assert receipt.accepted is False
        dumped = str(receipt.to_dict())
        assert secret not in dumped

    def test_oversize_title_is_rejected(self):
        payload = _summary_payload()
        payload["intents"][0]["title"] = "x" * 4001
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.reason == REASON_OVERSIZE

    def test_non_dict_payload_is_malformed(self):
        receipt = evaluate_shadow(
            JevShadowRequest(signal=SIGNAL_SESSION_SUMMARY, payload=[], enabled=True)
        )
        assert receipt.reason == REASON_MALFORMED_PAYLOAD


class TestBoundedRedactedReceipt:
    def test_valid_redacted_summary_returns_local_shadow(self):
        receipt = evaluate_shadow(_request(enabled=True))
        assert receipt.accepted is True
        assert receipt.status == STATUS_SHADOWED
        assert receipt.signal == SIGNAL_SESSION_SUMMARY
        assert receipt.result is not None
        assert receipt.result["mode"] == "shadow"
        assert receipt.result["action"] == SHADOW_ACTION_NONE
        assert receipt.result["backend"] == SHADOW_BACKEND_LOCAL
        assert receipt.result["intent_count"] == 1
        assert receipt.result["constraint_count"] == 1
        assert len(receipt.result["fingerprint"]) == 64

    def test_receipt_does_not_echo_payload_strings(self):
        payload = _summary_payload()
        payload["intents"][0]["title"] = _UNIQUE_MARKER
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.accepted is True
        dumped = str(receipt.to_dict())
        assert _UNIQUE_MARKER not in dumped
        assert "Ship the shadow sidecar" not in dumped

    def test_receipt_result_keys_are_bounded(self):
        receipt = evaluate_shadow(_request(enabled=True))
        assert receipt.result is not None
        assert set(receipt.result) == {
            "mode",
            "action",
            "backend",
            "signal",
            "fingerprint",
            "intent_count",
            "constraint_count",
        }

    def test_same_payload_is_deterministic(self):
        payload = _summary_payload()
        first = evaluate_shadow(_request(enabled=True, payload=payload))
        second = evaluate_shadow(_request(enabled=True, payload=payload))
        assert first == second
        assert first.result == second.result

    def test_key_order_does_not_change_fingerprint(self):
        a = _summary_payload()
        b = {
            "constraints": a["constraints"],
            "intents": a["intents"],
        }
        first = evaluate_shadow(_request(enabled=True, payload=a))
        second = evaluate_shadow(_request(enabled=True, payload=b))
        assert first.result is not None and second.result is not None
        assert first.result["fingerprint"] == second.result["fingerprint"]


class TestNoSideEffects:
    def test_does_not_write_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
        evaluate_shadow(_request(enabled=True))
        evaluate_shadow(_request(enabled=True, payload={"messages": [{"role": "user"}]}))
        after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
        assert after == before

    def test_does_not_import_action_or_provider_modules(self):
        before = set(sys.modules)
        evaluate_shadow(_request(enabled=True))
        leaked = set(sys.modules) - before
        blocked = (
            "kiro_crew.memory",
            "kiro_crew.learn",
            "kiro_crew.cron",
            "kiro_crew.knowledge",
            "kiro_crew.acp.client",
            "kiro_crew.providers",
            "httpx",
            "openai",
        )
        for name in blocked:
            assert name not in leaked, f"shadow path imported {name}"

    def test_module_source_has_no_network_client(self):
        source = (
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("src", "kiro_crew", "jev", "shadow.py")
            .read_text(encoding="utf-8")
        )
        for needle in (
            "import httpx",
            "import aiohttp",
            "import urllib",
            "import requests",
            "import socket",
            "import openai",
            "from httpx",
            "from aiohttp",
            "from urllib",
            "from requests",
            "from socket",
            "from openai",
        ):
            assert needle not in source

    def test_action_is_always_none(self):
        receipt = evaluate_shadow(_request(enabled=True))
        assert receipt.result is not None
        assert receipt.result["action"] == "none"

    def test_logs_do_not_contain_payload(self, caplog):
        payload = _summary_payload()
        payload["intents"][0]["title"] = _UNIQUE_MARKER
        with caplog.at_level(logging.DEBUG):
            evaluate_shadow(_request(enabled=True, payload=payload))
            evaluate_shadow(
                _request(
                    enabled=True,
                    payload={"messages": [{"role": "user", "content": _UNIQUE_MARKER}]},
                )
            )
        assert _UNIQUE_MARKER not in caplog.text


class _InfiniteMapping(AbcMapping):
    """Untrusted Mapping whose keys never end."""

    def __getitem__(self, key):
        return "x"

    def __iter__(self):
        i = 0
        while True:
            yield str(i)
            i += 1

    def __len__(self):
        return 10**9

    def keys(self):
        return self

    def items(self):
        for key in self:
            yield key, self[key]

    def values(self):
        while True:
            yield "x"

    def get(self, key, default=None):
        return default


class TestUntrustedPayloadBounds:
    def test_cyclic_mapping_is_malformed_not_a_crash(self):
        payload = {}
        payload["intents"] = [payload]
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason == REASON_MALFORMED_PAYLOAD
        assert receipt.result is None

    def test_cyclic_list_under_intent_is_malformed_not_a_crash(self):
        payload = _summary_payload()
        cycle: list = []
        cycle.append(cycle)
        payload["intents"][0]["progress"] = cycle
        receipt = evaluate_shadow(_request(enabled=True, payload=payload))
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason in {REASON_MALFORMED_PAYLOAD, REASON_OVERSIZE}
        assert receipt.result is None

    def test_deeply_nested_mapping_is_oversize_not_a_crash(self):
        nested: dict = {"title": "ok"}
        for _ in range(64):
            nested = {"title": "ok", "progress": [nested]}
        receipt = evaluate_shadow(_request(enabled=True, payload={"intents": [nested]}))
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason in {REASON_MALFORMED_PAYLOAD, REASON_OVERSIZE}

    def test_infinite_mapping_keys_are_oversize_not_a_hang(self):
        receipt = evaluate_shadow(_request(enabled=True, payload=_InfiniteMapping()))
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason in {REASON_MALFORMED_PAYLOAD, REASON_OVERSIZE}
        assert receipt.result is None
