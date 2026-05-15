"""Tests for curios.install (Cursor integration, not full ``curios`` argv routing)."""

from __future__ import annotations

import json
import stat
import sys
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
    monkeypatch.setattr(install, "CLAUDE_HOME", tmp_path / "no_claude_home")
    assert install.cmd_check() == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "All Curios deployment files are up to date" in out


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
    monkeypatch.setattr(install, "CLAUDE_HOME", tmp_path / "no_claude_home")
    assert install.cmd_check() == 1
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


def test_claude_staleness_report_ok_after_claude_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    ch = tmp_path / ".claude"
    ch.mkdir()
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("curios-server", "curios"):
        f = fake_bin / name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)

    monkeypatch.setattr(install, "CLAUDE_HOME", ch)
    monkeypatch.setattr(install, "CLAUDE_JSON_PATH", cj)
    monkeypatch.setattr(install, "CLAUDE_SETTINGS_PATH", ch / "settings.json")
    monkeypatch.setattr(install, "_resolve_binary", lambda name: str(fake_bin / name))
    monkeypatch.setattr(install, "_try_which", lambda name: str(fake_bin / name))

    assert install.cmd_claude_install(claude_home=ch, claude_json=cj) == 0
    report = install.claude_staleness_report(claude_home=ch, claude_json=cj, claude_settings=ch / "settings.json")
    by_label = {label: stale for label, _p, stale in report}
    assert by_label["claude.json MCP curios"] is False
    assert by_label["CLAUDE.md Curios section"] is False
    assert by_label["settings.json SessionEnd hook"] is False
    assert by_label["skills/curios-install/SKILL.md"] is False
    assert by_label["skills/curios-keyword-discovery/SKILL.md"] is False


def test_cmd_claude_install_merges_claude_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    ch = tmp_path / ".claude"
    ch.mkdir()
    cj = tmp_path / "claude.json"
    existing = "# My project\n\nKeep this.\n"
    (ch / "CLAUDE.md").write_text(existing, encoding="utf-8")
    monkeypatch.setattr(install, "CLAUDE_HOME", ch)
    monkeypatch.setattr(install, "CLAUDE_JSON_PATH", cj)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    assert install.cmd_claude_install(claude_home=ch, claude_json=cj) == 0
    cfg = json.loads(cj.read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["curios"]["command"] == "/fake/bin/curios-server"
    md = (ch / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Keep this." in md
    assert "<!-- BEGIN CURIOS -->" in md
    assert "curios_recap" in md

    settings = json.loads((ch / "settings.json").read_text(encoding="utf-8"))
    hook_groups = settings["hooks"]["SessionEnd"]
    assert len(hook_groups) == 1
    handler = hook_groups[0]["hooks"][0]
    assert handler["command"] == "/fake/bin/curios index --session-hook"
    assert handler["type"] == "command"

    assert (ch / "skills" / "curios-install" / "SKILL.md").is_file()
    assert (ch / "skills" / "curios-keyword-discovery" / "SKILL.md").is_file()


def test_cmd_claude_uninstall_removes_section_and_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    ch = tmp_path / ".claude"
    ch.mkdir()
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {"curios": {"command": "/x"}, "x": {"command": "y"}}}), encoding="utf-8")
    snippet = install._package_claude_append()
    (ch / "CLAUDE.md").write_text("intro\n\n" + install._claude_markdown_block(snippet) + "outro\n", encoding="utf-8")
    settings = {
        "effortLevel": "medium",
        "hooks": {
            "SessionEnd": [{"hooks": [{"type": "command", "command": "/fake/bin/curios index --session-hook", "timeout": 10}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo keep"}]}],
        },
    }
    cs = ch / "settings.json"
    cs.write_text(json.dumps(settings), encoding="utf-8")
    for _pkg, rel in install._CLAUDE_SKILL_DEPLOYMENTS:
        skill_path = ch / rel
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("s", encoding="utf-8")

    monkeypatch.setattr(install, "CLAUDE_HOME", ch)
    monkeypatch.setattr(install, "CLAUDE_JSON_PATH", cj)
    assert install.cmd_claude_uninstall(claude_home=ch, claude_json=cj, claude_settings=cs) == 0
    cfg = json.loads(cj.read_text(encoding="utf-8"))
    assert "curios" not in cfg["mcpServers"]
    assert "x" in cfg["mcpServers"]
    md = (ch / "CLAUDE.md").read_text(encoding="utf-8")
    assert "intro" in md and "outro" in md
    assert "<!-- BEGIN CURIOS -->" not in md
    after = json.loads(cs.read_text(encoding="utf-8"))
    assert "SessionEnd" not in after.get("hooks", {})
    assert after["hooks"]["Stop"] == settings["hooks"]["Stop"]
    assert after["effortLevel"] == "medium"
    for _pkg, rel in install._CLAUDE_SKILL_DEPLOYMENTS:
        assert not (ch / Path(rel).parent).exists()


def test_cmd_install_rejects_bad_ide(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from curios import install

    assert install.cmd_install("vscode") == 1
    assert "IDE must be" in capsys.readouterr().err


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


def test_cmd_cursor_install_corrupt_mcp_json_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    (cursor_home / "mcp.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    with pytest.raises(SystemExit) as exc:
        install.cmd_cursor_install()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "invalid JSON" in err
    assert "mcp.json" in err


def test_staleness_report_propagates_permission_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    def boom(_name: str) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(install, "_package_text", boom)
    with pytest.raises(PermissionError):
        install.staleness_report(tmp_path / "home")


def test_merge_claude_md_invalid_markers_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from curios import install

    snippet = install._package_claude_append()
    bad = (
        "x\n"
        + install._CURIOS_CLAUDE_BLOCK_BEGIN
        + "\nold\n"
        + install._CURIOS_CLAUDE_BLOCK_END
        + "\n"
        + install._CURIOS_CLAUDE_BLOCK_BEGIN
        + "\n"
        + install._CURIOS_CLAUDE_BLOCK_END
    )
    out = install._merge_claude_md(bad, snippet)
    assert "invalid Curios markers" in capsys.readouterr().err
    assert out.count(install._CURIOS_CLAUDE_BLOCK_BEGIN) >= 2


def test_cmd_cursor_install_dry_run_writes_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    assert install.cmd_cursor_install(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert not (cursor_home / "mcp.json").exists()
    assert not (cursor_home / "hooks.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="owner-only chmod not applied on Windows")
def test_cmd_cursor_install_sets_mcp_json_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    assert install.cmd_cursor_install() == 0
    mode = (cursor_home / "mcp.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_cmd_cursor_install_second_run_no_bak_when_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    cursor_home = tmp_path / ".cursor"
    cursor_home.mkdir()
    monkeypatch.setattr(install, "CURSOR_HOME", cursor_home)
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    assert install.cmd_cursor_install() == 0
    assert install.cmd_cursor_install() == 0
    # Content unchanged on second run — no backup should be created.
    assert not (cursor_home / "mcp.json.bak").is_file()


def test_save_json_creates_bak_when_content_changes(
    tmp_path: Path,
) -> None:
    from curios import install

    path = tmp_path / "f.json"
    install._save_json(path, {"a": 1})
    assert not (tmp_path / "f.json.bak").is_file()
    install._save_json(path, {"a": 2})
    assert (tmp_path / "f.json.bak").is_file()
    assert json.loads((tmp_path / "f.json.bak").read_text()) == {"a": 1}


def test_cmd_claude_install_preserves_existing_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    ch = tmp_path / ".claude"
    ch.mkdir()
    cj = tmp_path / "claude.json"
    cs = ch / "settings.json"
    existing = {"effortLevel": "high", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    cs.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/fake/bin/{name}")

    assert install.cmd_claude_install(claude_home=ch, claude_json=cj, claude_settings=cs) == 0
    settings = json.loads(cs.read_text(encoding="utf-8"))
    assert settings["effortLevel"] == "high"
    assert settings["hooks"]["Stop"] == existing["hooks"]["Stop"]
    assert len(settings["hooks"]["SessionEnd"]) == 1


def test_cmd_claude_install_replaces_legacy_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from curios import install

    ch = tmp_path / ".claude"
    ch.mkdir()
    cj = tmp_path / "claude.json"
    cs = ch / "settings.json"
    old = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "/old/curios-index --session-hook", "timeout": 5}]}]}}
    cs.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(install, "_resolve_binary", lambda name: f"/new/bin/{name}")

    assert install.cmd_claude_install(claude_home=ch, claude_json=cj, claude_settings=cs) == 0
    settings = json.loads(cs.read_text(encoding="utf-8"))
    groups = settings["hooks"]["SessionEnd"]
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == "/new/bin/curios index --session-hook"


def test_cmd_claude_uninstall_idempotent_without_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from curios import install

    ch = tmp_path / ".claude"
    ch.mkdir()
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    cs = ch / "settings.json"
    cs.write_text(json.dumps({"effortLevel": "low"}), encoding="utf-8")
    monkeypatch.setattr(install, "CLAUDE_HOME", ch)
    monkeypatch.setattr(install, "CLAUDE_JSON_PATH", cj)
    assert install.cmd_claude_uninstall(claude_home=ch, claude_json=cj, claude_settings=cs) == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_resolve_binary_raises_curios_install_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from curios import install

    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(install.CuriosInstallError, match="not found on PATH"):
        install._resolve_binary("curios-server")


def test_cmd_uninstall_none_removes_both_ides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """curios uninstall (no IDE arg) cleans up Cursor and Claude Code when both dirs exist."""
    from curios import install

    ch = tmp_path / ".cursor"
    ch.mkdir()
    cl = tmp_path / ".claude"
    cl.mkdir()
    cj = tmp_path / "claude.json"

    snippet = install._package_claude_append()
    (cl / "CLAUDE.md").write_text(install._claude_markdown_block(snippet), encoding="utf-8")
    cj.write_text(json.dumps({"mcpServers": {"curios": {"command": "/x"}}}), encoding="utf-8")
    cs = cl / "settings.json"
    cs.write_text(
        json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "/x/curios index --session-hook", "timeout": 10}]}]}}),
        encoding="utf-8",
    )
    (ch / "mcp.json").write_text(json.dumps({"mcpServers": {"curios": {"command": "/y"}}}), encoding="utf-8")
    (ch / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {"sessionEnd": [{"command": "/y/curios index --session-hook", "timeout": 10}]}}), encoding="utf-8")

    monkeypatch.setattr(install, "CURSOR_HOME", ch)
    monkeypatch.setattr(install, "CLAUDE_HOME", cl)
    monkeypatch.setattr(install, "CLAUDE_JSON_PATH", cj)

    assert install.cmd_uninstall(None) == 0

    cursor_mcp = json.loads((ch / "mcp.json").read_text(encoding="utf-8"))
    assert "curios" not in cursor_mcp.get("mcpServers", {})
    claude_mcp = json.loads(cj.read_text(encoding="utf-8"))
    assert "curios" not in claude_mcp.get("mcpServers", {})
    # CLAUDE.md had only the Curios block so it gets deleted entirely.
    claude_md = cl / "CLAUDE.md"
    assert not claude_md.exists() or "<!-- BEGIN CURIOS -->" not in claude_md.read_text(encoding="utf-8")


def test_cmd_uninstall_skips_missing_cursor_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from curios import install

    monkeypatch.setattr(install, "CURSOR_HOME", tmp_path / "no_cursor")
    monkeypatch.setattr(install, "CLAUDE_HOME", tmp_path / "no_claude")

    assert install.cmd_uninstall(None) == 0
    out = capsys.readouterr().out
    assert "skipping" in out
