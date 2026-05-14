"""Tests for curios.install (Cursor integration, not full ``curios`` argv routing)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.cli


def test_staleness_report_all_match_package(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from curios import install

    home = tmp_path / "cursor_home"
    home.mkdir()
    for pkg_name, rel in install._CURSOR_DEPLOYMENTS:
        text = install._package_text(pkg_name)
        dest = home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

    report = install.staleness_report(home)
    assert len(report) == 3
    assert all(not stale for _, _, stale in report)


def test_staleness_report_missing_deployed_is_stale(tmp_path: Path) -> None:
    from curios import install

    home = tmp_path / "empty"
    home.mkdir()
    report = install.staleness_report(home)
    assert any(stale for _, _, stale in report)


def test_cmd_cursor_check_ok_when_files_match_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    home = tmp_path / "cursor_home"
    home.mkdir()
    for pkg_name, rel in install._CURSOR_DEPLOYMENTS:
        text = install._package_text(pkg_name)
        dest = home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

    monkeypatch.setattr(install, "CURSOR_HOME", home)
    assert install.cmd_cursor_check() == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "All Cursor files are up to date" in out


def test_cmd_cursor_check_fails_when_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    home = tmp_path / "cursor_home"
    home.mkdir()
    for pkg_name, rel in install._CURSOR_DEPLOYMENTS:
        text = install._package_text(pkg_name)
        dest = home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    (home / "rules" / "curios.mdc").write_text("stale-content-not-from-package", encoding="utf-8")

    monkeypatch.setattr(install, "CURSOR_HOME", home)
    assert install.cmd_cursor_check() == 1
    assert "STALE" in capsys.readouterr().out


def test_cmd_cursor_install_writes_mcp_hooks_rules_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    assert install.cmd_cursor_install() == 0
    assert "Done" in capsys.readouterr().out

    mcp = json.loads((cursor_home / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["curios"]["command"] == "/fake/bin/curios-server"

    hooks = json.loads((cursor_home / "hooks.json").read_text(encoding="utf-8"))
    session_end = hooks["hooks"]["sessionEnd"]
    assert len(session_end) >= 1
    assert "/fake/bin/curios index --session-hook" in session_end[0]["command"]

    assert (cursor_home / "rules" / "curios.mdc").is_file()
    assert (cursor_home / "skills" / "curios-install" / "SKILL.md").is_file()
    assert (cursor_home / "skills" / "curios-keyword-discovery" / "SKILL.md").is_file()


def test_cmd_cursor_install_replaces_legacy_curios_index_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    (cursor_home / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    legacy = {
        "version": 1,
        "hooks": {
            "sessionEnd": [
                {"command": "/old/curios-index --session-hook", "timeout": 10},
                {"command": "echo other", "timeout": 5},
            ]
        },
    }
    (cursor_home / "hooks.json").write_text(json.dumps(legacy), encoding="utf-8")

    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")
    assert install.cmd_cursor_install() == 0

    hooks = json.loads((cursor_home / "hooks.json").read_text(encoding="utf-8"))
    se = hooks["hooks"]["sessionEnd"]
    assert len(se) == 2
    assert "curios index --session-hook" in se[0]["command"]
    assert se[1]["command"] == "echo other"


def test_cmd_cursor_uninstall_removes_curios_and_preserves_other_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    (cursor_home / "mcp.json").write_text(
        json.dumps({"mcpServers": {"curios": {"command": "/x"}, "other": {"command": "y"}}}),
        encoding="utf-8",
    )
    hooks = {
        "version": 1,
        "hooks": {
            "sessionEnd": [
                {"command": "/fake/bin/curios index --session-hook", "timeout": 10},
                {"command": "echo keep", "timeout": 5},
            ]
        },
    }
    (cursor_home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    (cursor_home / "rules" / "curios.mdc").parent.mkdir(parents=True)
    (cursor_home / "rules" / "curios.mdc").write_text("x", encoding="utf-8")
    skill = cursor_home / "skills" / "curios-install" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("s", encoding="utf-8")

    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    assert install.cmd_cursor_uninstall() == 0

    mcp = json.loads((cursor_home / "mcp.json").read_text(encoding="utf-8"))
    assert "curios" not in mcp["mcpServers"]
    assert "other" in mcp["mcpServers"]

    hooks_after = json.loads((cursor_home / "hooks.json").read_text(encoding="utf-8"))
    assert len(hooks_after["hooks"]["sessionEnd"]) == 1
    assert hooks_after["hooks"]["sessionEnd"][0]["command"] == "echo keep"

    assert not (cursor_home / "rules" / "curios.mdc").exists()
    assert "removed" in capsys.readouterr().out


def test_cmd_cursor_uninstall_idempotent_when_already_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    (cursor_home / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    (cursor_home / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {}}), encoding="utf-8")

    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    assert install.cmd_cursor_uninstall() == 0
