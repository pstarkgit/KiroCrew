"""Tests for the local-only jev.shadow_enabled config section."""

from __future__ import annotations

import json
from dataclasses import asdict

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import JevConfig

_ABSENT = object()


class TestJevDefaults:
    def test_disabled_on_a_fresh_config(self):
        assert KiroCrewConfig().jev.shadow_enabled is False

    def test_every_field_carries_ui_metadata(self):
        for f in JevConfig.__dataclass_fields__.values():
            meta = f.metadata.get("x-meta") or f.metadata
            assert meta, f"{f.name} has no metadata"


class TestJevParsing:
    @staticmethod
    def _load(tmp_path, monkeypatch, section):
        cfgp = tmp_path / "config.json"
        payload = {"agent": {"provider": "acp"}}
        if section is not _ABSENT:
            payload["jev"] = section
        cfgp.write_text(json.dumps(payload))
        monkeypatch.setattr(L, "config_path", lambda: cfgp)
        monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
        return KiroCrewConfig.load()

    def test_load_reads_exact_true(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"shadow_enabled": True})
        assert cfg.jev.shadow_enabled is True

    def test_string_true_is_deny(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"shadow_enabled": "true"})
        assert cfg.jev.shadow_enabled is False

    def test_one_is_deny(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"shadow_enabled": 1})
        assert cfg.jev.shadow_enabled is False

    def test_absent_section_yields_defaults(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, _ABSENT)
        assert cfg.jev == JevConfig()

    def test_non_dict_section_degrades_to_defaults(self, tmp_path, monkeypatch):
        for junk in ("nope", 7, [], None):
            cfg = self._load(tmp_path, monkeypatch, junk)
            assert cfg.jev == JevConfig()

    def test_section_is_serialized_so_save_round_trips(self):
        assert "jev" in KiroCrewConfig().to_dict()
        assert KiroCrewConfig().to_dict()["jev"]["shadow_enabled"] is False

    def test_round_trips_through_to_dict(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"shadow_enabled": True})
        assert cfg.to_dict()["jev"] == asdict(cfg.jev)

    def test_section_is_known_not_an_unknown_passthrough(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"shadow_enabled": True})
        assert isinstance(cfg.jev, JevConfig)
        assert "jev" not in cfg._extra_sections

    def test_section_is_registered_in_the_config_schema(self):
        from kiro_crew.config import schema

        paths = {e.path for e in schema.SCHEMA_REGISTRY}
        assert "jev" in paths
        assert "jev.shadow_enabled" in paths

    def test_not_on_the_dashboard_settings_surface(self):
        from pathlib import Path

        website_src = Path(__file__).resolve().parents[1] / "website" / "src"
        for path in website_src.rglob("*"):
            if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            assert "jev.shadow_enabled" not in text, path
            assert "shadow_enabled" not in text, path
