"""Tests for ``GET /api/path-complete`` — the composer's ``./`` path completion.

The load-bearing behaviour here is containment: the endpoint takes a caller
relative directory and joins it onto an allow-listed project root, so ``../``
runs and symlinks are the whole risk surface and each has its own test.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_path_complete


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/path-complete", api_path_complete)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


@pytest.fixture()
def project(tmp_path):
    """A project root with one subdirectory, one file, and one dot file."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("x")
    (root / "src" / "appdata").mkdir()
    (root / "readme.md").write_text("hi")
    (root / ".env").write_text("SECRET=1")
    return root


async def _get(known: str, **params) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(known))) as client:
        resp = await client.get("/api/path-complete", params=params)
        return resp.status, await resp.json()


def _names(payload: dict) -> list[str]:
    return [r["name"] for r in payload["results"]]


class TestPathComplete:
    @pytest.mark.asyncio
    async def test_missing_path_is_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/path-complete?dir=./")
            assert resp.status == 400
            assert (await resp.json())["code"] == "path_required"

    @pytest.mark.asyncio
    async def test_unknown_project_is_403(self, tmp_path, project, mock_sel):
        other = tmp_path / "other"
        other.mkdir()
        status, data = await _get(str(project), path=str(other), dir="./")
        assert status == 403
        assert data["code"] == "unknown_project_dir"

    @pytest.mark.asyncio
    async def test_lists_the_project_root_for_a_bare_dot_slash(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        # Directories first, then alphabetical.
        assert _names(data) == ["src", "readme.md"]
        assert data["results"][0]["kind"] == "dir"
        assert data["root"] == os.path.realpath(str(project))

    @pytest.mark.asyncio
    async def test_lists_a_subdirectory(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./src/")
        assert status == 200
        assert _names(data) == ["appdata", "app.ts"]

    @pytest.mark.asyncio
    async def test_prefix_narrows_case_insensitively(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./", q="READ")
        assert status == 200
        assert _names(data) == ["readme.md"]

    @pytest.mark.asyncio
    async def test_dot_entries_are_hidden_until_the_dot_is_typed(self, project, mock_sel):
        _, listed = await _get(str(project), path=str(project), dir="./")
        assert ".env" not in _names(listed)
        _, asked = await _get(str(project), path=str(project), dir="./", q=".")
        assert ".env" in _names(asked)

    @pytest.mark.asyncio
    async def test_a_parent_run_that_escapes_the_project_returns_no_results(
        self, tmp_path, project, mock_sel
    ):
        """`../` may not leave the root: the sibling directory exists and is not
        sensitive, so an unchecked join would have listed it."""
        sibling = tmp_path / "outside"
        sibling.mkdir()
        (sibling / "secrets.txt").write_text("x")
        status, data = await _get(str(project), path=str(project), dir="../outside/")
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_a_parent_run_inside_the_project_still_resolves(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./src/../")
        assert status == 200
        assert _names(data) == ["src", "readme.md"]

    @pytest.mark.asyncio
    async def test_a_leading_parent_run_that_comes_back_inside_lists_entries(
        self, project, mock_sel
    ):
        """`../` is refused as a destination, never as a shape.

        Going up and back down into the same project is what a shell does and what
        the containment rule allows, so a token whose LEADING `../` run re-enters
        the project lists that directory.
        """
        status, data = await _get(
            str(project), path=str(project), dir=f"../{project.name}/src/"
        )
        assert status == 200
        assert _names(data) == ["appdata", "app.ts"]

    @pytest.mark.asyncio
    async def test_an_absolute_dir_is_not_honoured(self, tmp_path, project, mock_sel):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secrets.txt").write_text("x")
        status, data = await _get(str(project), path=str(project), dir=str(outside))
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_a_symlink_out_of_the_project_is_refused(self, tmp_path, project, mock_sel):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secrets.txt").write_text("x")
        (project / "link").symlink_to(outside, target_is_directory=True)
        status, data = await _get(str(project), path=str(project), dir="./link/")
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_an_entry_that_points_out_of_the_project_is_not_offered(
        self, tmp_path, project, mock_sel
    ):
        """Containment applies to what is OFFERED, not only to what is listed.

        A link inside the project aimed outside it would complete to a path that
        leaves the project, so it is dropped on its resolved path -- the same
        check the directory itself passed.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (project / "escape").symlink_to(outside, target_is_directory=True)
        status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert "escape" not in _names(data)
        assert _names(data) == ["src", "readme.md"]

    @pytest.mark.asyncio
    async def test_a_directory_swapped_for_a_link_after_validation_is_refused(
        self, tmp_path, project, mock_sel, monkeypatch
    ):
        """The TOCTOU window between resolving the directory and reading it.

        Everything before the read validates a PATH, and a same-UID writer -- an
        agent working in this very project -- can rename a component and plant a
        link at its name in between. The race is driven deterministically here by
        performing the swap at the moment the directory is opened: the open must
        refuse the link rather than list what it points at.
        """
        from kiro_crew.dashboard.handlers import files as files_mod

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("x")
        real_pin = files_mod.platform_compat.pin_directory
        src = os.path.realpath(str(project / "src"))

        def racing_pin(path):
            if str(path) == src and os.path.isdir(src):
                os.rename(src, str(project / "moved"))
                os.symlink(str(outside), src, target_is_directory=True)
            return real_pin(path)

        monkeypatch.setattr(files_mod.platform_compat, "pin_directory", racing_pin)
        status, data = await _get(str(project), path=str(project), dir="./src/")
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_a_missing_directory_is_an_empty_listing(self, project, mock_sel):
        status, data = await _get(str(project), path=str(project), dir="./nope/")
        assert status == 200
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_a_nul_byte_in_the_token_is_an_empty_listing_not_a_500(
        self, project, mock_sel
    ):
        """A NUL never occurs in a real path, and the resolver raises ValueError
        (not OSError) for one, so it is screened at the boundary."""
        for params in ({"dir": "./\x00"}, {"dir": "./", "q": "a\x00"}):
            status, data = await _get(str(project), path=str(project), **params)
            assert status == 200
            assert data["results"] == []

    @pytest.mark.asyncio
    async def test_a_sensitive_project_root_is_403(self, project, mock_sel):
        from kiro_crew.dashboard.handlers import files as files_mod

        with patch.object(files_mod, "is_sensitive_path", lambda p: True):
            status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 403
        assert data["code"] == "access_denied"

    @pytest.mark.asyncio
    async def test_a_sensitive_entry_is_not_offered(self, project, mock_sel):
        from kiro_crew.dashboard.handlers import files as files_mod

        secret = os.path.realpath(str(project / "readme.md"))
        with patch.object(files_mod, "is_sensitive_path", lambda p: p == secret):
            status, data = await _get(str(project), path=str(project), dir="./")
        assert status == 200
        assert _names(data) == ["src"]

    @pytest.mark.asyncio
    async def test_the_result_set_is_capped(self, tmp_path, mock_sel):
        from kiro_crew.dashboard.handlers import files as files_mod

        root = tmp_path / "many"
        root.mkdir()
        for i in range(files_mod._PATH_COMPLETE_MAX_ENTRIES + 10):
            (root / f"f{i:03d}.txt").write_text("x")
        status, data = await _get(str(root), path=str(root), dir="./")
        assert status == 200
        assert len(data["results"]) == files_mod._PATH_COMPLETE_MAX_ENTRIES
