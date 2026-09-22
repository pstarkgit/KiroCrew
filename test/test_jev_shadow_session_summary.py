"""Integration: session-summary generation optionally emits a local Jev receipt."""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_summary
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.jev.shadow import (
    REASON_DISABLED,
    REASON_MALFORMED_PAYLOAD,
    REASON_RAW_CONTENT,
    RECEIPT_DIR_NAME,
    STATUS_REJECTED,
    STATUS_SHADOWED,
    evaluate_shadow,
    observe_session_summary,
    shadow_receipt_dir,
)
from kiro_crew.platform_compat import IS_POSIX


def _make_slot(messages=None, memory_mode="default"):
    slot = _ChatSlot("s1")
    slot.messages = messages if messages is not None else []
    slot.memory_mode = memory_mode
    slot._last_stop_reason = "end_turn"
    return slot


def _write_transcript(log, hkey, messages):
    for m in messages:
        log.append(hkey, m["role"], m["content"])


class _FakeState:
    def __init__(self, log):
        self.conversation_log = log
        self.sessions = object()
        self.pushed: list[str] = []
        self.hkey = ""

    def push_session_summary(self, key):
        self.pushed.append(key)

    def flush_slot_now(self, slot):
        if not slot._dirty or not slot.messages:
            return
        gen = slot._dirty_gen
        _save_slot_to_history(self, slot)
        if slot._dirty_gen == gen:
            slot._dirty = False


def _turns(n=3):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"request {i}"})
        out.append({"role": "assistant", "content": f"reply {i}"})
    return out


_GOOD_REPLY = json.dumps(
    {
        "intents": [
            {
                "title": "set up auth",
                "ranges": [[1, 3]],
                "status": "completed",
                "verified": False,
                "initial_intent": "wire up login",
                "progress": ["login works locally"],
                "next_steps": [{"what": "try it in staging", "why": "never run there"}],
            }
        ],
        "constraints": ["restart the worker after a config change"],
    }
)

_UNIQUE = "UNIQUE_JEV_SHADOW_SUMMARY_MARKER_9c1e"


def _cfg(*, shadow=False, **overrides):
    cfg = KiroCrewConfig()
    cfg.session_summary = SessionSummaryConfig(**{"enabled": True, **overrides})
    cfg.jev.shadow_enabled = shadow
    return cfg


def _stub_llm(monkeypatch, reply=_GOOD_REPLY):
    async def fake(*args, **kwargs):
        return reply

    monkeypatch.setattr(chat_summary, "run_bg_oneliner", fake)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    log = ConversationLog(base_dir=tmp_path)
    slot = _make_slot(_turns())
    state = _FakeState(log)
    state.hkey = slot_history_key(slot)
    _write_transcript(log, state.hkey, _turns())
    return state, slot


def _receipt_files():
    directory = shadow_receipt_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


class _BoomPayload(dict):
    def __getitem__(self, key):
        raise AssertionError("disabled path inspected payload")

    def get(self, *args, **kwargs):
        raise AssertionError("disabled path inspected payload")

    def __iter__(self):
        raise AssertionError("disabled path inspected payload")


class TestObserveGate:
    def test_disabled_does_not_inspect_payload_or_write(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        receipt = observe_session_summary(
            enabled=False,
            session_key="s1",
            payload=_BoomPayload(),
        )
        assert receipt is None
        assert _receipt_files() == []

    def test_enabled_writes_one_bounded_receipt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        session_key = "agent:secret-session-id"
        payload = {
            "intents": [{"title": _UNIQUE, "status": "active"}],
            "constraints": ["do not call a provider"],
            "generated_at": 1.5,
            "user_turns": 3,
            "last_activity": "2026-08-10T10:00:00+00:00",
        }
        receipt = observe_session_summary(enabled=True, session_key=session_key, payload=payload)
        assert receipt is not None
        assert receipt.status == STATUS_SHADOWED
        files = _receipt_files()
        assert len(files) == 1
        dumped = files[0].read_text(encoding="utf-8")
        assert _UNIQUE not in dumped
        assert session_key not in dumped
        assert session_key not in files[0].name
        assert "secret-session-id" not in files[0].name
        assert "session_stem" not in dumped
        body = json.loads(dumped)
        assert body["accepted"] is True
        assert body["result"]["action"] == "none"
        assert body["result"]["backend"] == "local"
        assert "intents" not in body
        assert "constraints" not in body
        if IS_POSIX:
            assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
            assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700

    def test_extra_messages_are_rejected_not_sliced_into_acceptance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        payload = {
            "intents": [{"title": _UNIQUE, "status": "active"}],
            "constraints": ["do not call a provider"],
            "messages": [{"role": "user", "content": _UNIQUE}],
        }
        receipt = observe_session_summary(
            enabled=True, session_key="agent:secret-session-id", payload=payload
        )
        assert receipt is not None
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason == REASON_RAW_CONTENT
        files = _receipt_files()
        assert len(files) == 1
        dumped = files[0].read_text(encoding="utf-8")
        assert _UNIQUE not in dumped
        assert "secret-session-id" not in dumped
        assert "secret-session-id" not in files[0].name

    def test_unknown_top_level_field_is_rejected_not_discarded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        payload = {
            "intents": [{"title": _UNIQUE, "status": "active"}],
            "constraints": ["do not call a provider"],
            "notes": "stowaway",
        }
        receipt = observe_session_summary(enabled=True, session_key="s1", payload=payload)
        assert receipt is not None
        assert receipt.accepted is False
        assert receipt.status == STATUS_REJECTED
        assert receipt.reason == REASON_MALFORMED_PAYLOAD

    def test_observe_failure_does_not_log_traceback_or_private_text(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

        class Boom(dict):
            def __iter__(self):
                raise RuntimeError(_UNIQUE)

        with caplog.at_level(logging.DEBUG):
            receipt = observe_session_summary(
                enabled=True,
                session_key="agent:secret-session-id",
                payload=Boom({"intents": []}),
            )
        assert receipt is None
        assert _UNIQUE not in caplog.text
        assert "Traceback" not in caplog.text
        assert "secret-session-id" not in caplog.text
        assert _receipt_files() == []


class TestGeneratePath:
    pytestmark = pytest.mark.asyncio

    async def test_disabled_generation_does_not_call_evaluate_or_write(
        self, env, monkeypatch, tmp_path
    ):
        state, slot = env
        _stub_llm(monkeypatch)
        called = []

        def boom(*args, **kwargs):
            called.append(True)
            raise AssertionError("evaluate_shadow must not run when disabled")

        monkeypatch.setattr(chat_summary, "observe_session_summary", boom)
        monkeypatch.setattr("kiro_crew.jev.shadow.evaluate_shadow", boom)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(shadow=False))
        assert ok is True
        assert called == []
        assert _receipt_files() == []
        stored = state.conversation_log.get_cached_intent_summary(state.hkey)
        assert stored is not None
        assert stored["intents"][0]["title"] == "set up auth"

    async def test_generation_passes_unsliced_payload_to_observe(self, env, monkeypatch, tmp_path):
        state, slot = env
        _stub_llm(monkeypatch)
        seen = []

        def capture(*, enabled, session_key, payload):
            seen.append(payload)
            return None

        monkeypatch.setattr(chat_summary, "observe_session_summary", capture)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(shadow=True))
        assert ok is True
        assert len(seen) == 1
        payload = seen[0]
        assert "intents" in payload
        assert "constraints" in payload
        assert "generated_at" in payload
        assert "user_turns" in payload
        assert "last_activity" in payload
        assert "messages" not in payload

    async def test_enabled_generation_writes_one_local_receipt_and_no_network(
        self, env, monkeypatch, tmp_path
    ):
        state, slot = env
        reply = json.dumps(
            {
                "intents": [
                    {
                        "title": _UNIQUE,
                        "ranges": [[1, 3]],
                        "status": "completed",
                        "verified": False,
                        "initial_intent": "wire up login",
                        "progress": ["login works locally"],
                        "next_steps": [{"what": "try it in staging", "why": "never run there"}],
                    }
                ],
                "constraints": ["restart the worker after a config change"],
            }
        )
        _stub_llm(monkeypatch, reply)

        connects = []

        def forbid_connect(*args, **kwargs):
            connects.append((args, kwargs))
            raise AssertionError("jev shadow must not open a network connection")

        monkeypatch.setattr("socket.socket.connect", forbid_connect, raising=False)

        before_modules = set(sys.modules)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(shadow=True))
        assert ok is True
        assert connects == []
        leaked = set(sys.modules) - before_modules
        for name in (
            "kiro_crew.memory",
            "kiro_crew.learn",
            "kiro_crew.cron",
            "kiro_crew.knowledge",
            "httpx",
            "openai",
        ):
            assert name not in leaked, f"shadow path imported {name}"
        files = _receipt_files()
        assert len(files) == 1
        dumped = files[0].read_text(encoding="utf-8")
        assert _UNIQUE not in dumped
        body = json.loads(dumped)
        assert body["status"] == STATUS_SHADOWED
        assert body["result"]["action"] == "none"
        assert body["result"]["backend"] == "local"
        stored = state.conversation_log.get_cached_intent_summary(state.hkey)
        assert stored is not None
        assert stored["intents"][0]["title"] == _UNIQUE
        assert "jev" not in stored
        assert RECEIPT_DIR_NAME not in str(
            state.conversation_log._intent_summary_cache_path(state.hkey)
        )

    async def test_shadow_failure_is_fail_open_for_summary_fail_closed_for_jev(
        self, env, monkeypatch
    ):
        state, slot = env
        _stub_llm(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("shadow backend exploded")

        monkeypatch.setattr(chat_summary, "observe_session_summary", boom)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(shadow=True))
        assert ok is True
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is not None
        assert _receipt_files() == []

    async def test_inner_evaluate_failure_writes_no_receipt(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch)

        def boom(request):
            raise RuntimeError("evaluate crashed")

        monkeypatch.setattr("kiro_crew.jev.shadow.evaluate_shadow", boom)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(shadow=True))
        assert ok is True
        assert _receipt_files() == []
        assert evaluate_shadow is not boom
        assert REASON_DISABLED == "disabled"


def test_receipt_dir_name_is_local():
    assert RECEIPT_DIR_NAME == "jev-shadow"
    assert not str(Path(RECEIPT_DIR_NAME)).startswith("http")
