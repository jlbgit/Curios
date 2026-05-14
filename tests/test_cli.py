"""Tests for the unified ``curios`` CLI (`curios.install.main` / argv routing)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from curios.config import SCHEMA_VERSION, import_slug_for_project
from tests.conftest import make_chroma_collection, topic_meta_false

pytestmark = pytest.mark.cli


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    from curios import install

    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as e:
        install.main()
    code = e.value.code
    return 0 if code is None else int(code)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/home/u/.local/bin/curios index --session-hook", True),
        ("/home/u/.local/bin/curios-index --session-hook", True),
        ("echo hello", False),
        ("", False),
    ],
)
def test_is_curios_session_hook(command: str, expected: bool) -> None:
    from curios.install import _is_curios_session_hook

    assert _is_curios_session_hook(command) is expected


def test_cli_help_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(monkeypatch, ["curios", "--help"]) == 0
    out = capsys.readouterr().out
    assert "COMMAND" in out or "index" in out


def test_cli_index_rebuild_rejects_project(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        _run_main(monkeypatch, ["curios", "index", "--rebuild", "--project", "X"])
        == 1
    )
    assert "cannot combine" in capsys.readouterr().err


def test_cli_index_rebuild_rejects_dry_run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(monkeypatch, ["curios", "index", "--rebuild", "--dry-run"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_cli_index_rebuild_rejects_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    assert _run_main(monkeypatch, ["curios", "index", "--rebuild", "--file", str(f)]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_cli_prune_before_requires_project(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(monkeypatch, ["curios", "prune", "--before", "2020-01-01"]) == 1
    assert "--project" in capsys.readouterr().err


def test_cli_export_no_transcripts(curios_data_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    arch = curios_data_env / "empty.tar.gz"
    assert _run_main(monkeypatch, ["curios", "export", str(arch)]) == 1
    assert "no transcripts" in capsys.readouterr().err


def test_cli_import_missing_archive(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(monkeypatch, ["curios", "import", "/no/such/archive.tar.gz"]) == 1
    assert "missing archive" in capsys.readouterr().err


def test_cli_export_import_round_trip(curios_data_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    proj_base = curios_data_env / "projects"
    slug = "acme-widget"
    agent = proj_base / slug / "agent-transcripts"
    agent.mkdir(parents=True)
    cid = "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"
    tpath = agent / f"{cid}.jsonl"
    tpath.write_text(
        '{"role":"user","message":{"content":"hello"}}\n'
        '{"role":"assistant","message":{"content":"world"}}\n',
        encoding="utf-8",
    )

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    make_chroma_collection(chroma_path)

    arch = curios_data_env / "pack.tar.gz"
    assert _run_main(monkeypatch, ["curios", "export", str(arch)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert arch.is_file()

    imp_slug = import_slug_for_project("ImportedPack")
    dest = proj_base / imp_slug / "agent-transcripts" / f"{cid}.jsonl"
    if dest.exists():
        dest.unlink()

    assert (
        _run_main(
            monkeypatch,
            ["curios", "import", str(arch), "--project", "ImportedPack"],
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "imported" in out
    assert dest.is_file()


def test_cli_verify_runs(curios_data_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from curios import bm25

    chroma_path = curios_data_env / "curios_data" / "chromadb"
    os.chmod(chroma_path, 0o700)
    coll = make_chroma_collection(chroma_path)
    rel = "slug/agent-transcripts/x.jsonl"
    meta = {
        "project": "S",
        "conversation_id": "cid",
        "depth": "standard",
        "novelty": "novel",
        "source_mtime": 1,
        "source_rel_path": rel,
        "exchange_count": 2,
        "chunk_index": 0,
        **topic_meta_false(),
    }
    coll.upsert(ids=["id1"], documents=["hello"], metadatas=[meta])
    schema_path = curios_data_env / "curios_data" / "schema_version.json"
    schema_path.write_text(json.dumps({"version": SCHEMA_VERSION}), encoding="utf-8")
    bm25.insert_many([("id1", "hello", "S")])

    x = curios_data_env / "projects" / "slug" / "agent-transcripts" / "x.jsonl"
    x.parent.mkdir(parents=True)
    x.write_text('{"role":"user","message":{"content":"hi"}}\n', encoding="utf-8")

    assert _run_main(monkeypatch, ["curios", "verify"]) == 0
