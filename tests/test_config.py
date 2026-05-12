"""Tests for curios.config: paths, redaction, slugs, keywords, env overrides."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from curios.config import (
    conversation_id_from_path,
    extract_project_name,
    import_slug_for_project,
    project_name_from_import_slug,
    redact_secrets,
    transcript_relative_path,
)

pytestmark = pytest.mark.config


def test_redact_expanded_patterns():
    long_ant = "sk-ant-api03-" + "a" * 25
    long_glpat = "glpat-" + "a" * 25
    samples = [
        f"prefix {long_ant} suffix",
        f"key={long_glpat}",
        "tok=xoxb-1234567890abcdefghij",
        "AIza" + "a" * 35,
        "https q=github_pat_aaaaaaaaaaaaaaaaaaaa usage",
        "aws_secret_access_key=ABCDEFGHIJKLMNOPQRSTUVWXYZabcd1234EFGH",
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJ0aGluZyJ9.abcdefghsigned",
    ]
    for text in samples:
        out = redact_secrets(text)
        assert "[REDACTED]" in out, (text, out)


def test_redact_openai_still_covered():
    t = "sk-abcdefghijklmnopqrst"
    assert redact_secrets(t) == "[REDACTED]"


def test_redact_pem_azure_heroku_prose():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAabcd\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert "[REDACTED]" in redact_secrets(pem)

    azure = (
        "DefaultEndpointsProtocol=https;AccountName=x;AccountKey="
        + "A" * 44
        + ";EndpointSuffix=core.windows.net"
    )
    assert "[REDACTED]" in redact_secrets(azure)

    heroku = "HEROKU_API_KEY=01234567-89ab-cdef-0123-456789abcdef"
    assert "[REDACTED]" in redact_secrets(heroku)

    prose = 'password is "supersecret"'
    out = redact_secrets(prose)
    assert "[REDACTED]" in out
    assert "supersecret" not in out


def test_extract_project_name_multi_segment():
    base = Path("/x/projects/foo-bar-baz-qux/agent-transcripts/stem.jsonl")
    name = extract_project_name(base)
    assert name == "QUX"


def test_extract_project_name_cursor_slug_strips_home_prefix(monkeypatch):
    """Cursor `.cursor/projects/<slug>` layout: strip user path prefix from slug; keep final label."""
    import curios.config as cfg

    monkeypatch.setattr(cfg, "HOME", Path("/home/alice"))
    tr = Path(
        "/home/alice/.cursor/projects/"
        "home-alice-work-docs-AcmePrototype/agent-transcripts/c.jsonl"
    )
    assert extract_project_name(tr) == "AcmePrototype"


def test_extract_project_name_cursor_slug_apps_tree(monkeypatch):
    import curios.config as cfg

    monkeypatch.setattr(cfg, "HOME", Path("/home/alice"))
    tr = Path(
        "/home/alice/.cursor/projects/"
        "home-alice-apps-WidgetToolkit/agent-transcripts/a.jsonl"
    )
    assert extract_project_name(tr) == "WidgetToolkit"


def test_import_slug_round_trip():
    name = "My Project / Foo"
    slug = import_slug_for_project(name)
    assert slug.startswith("curios-import-")
    assert project_name_from_import_slug(slug) == name


def test_project_name_from_import_slug_invalid():
    assert project_name_from_import_slug("not-import") is None
    assert project_name_from_import_slug("curios-import!!!") is None


def test_conversation_id_from_path_uuid_stem():
    p = Path("/a/b/550e8400-e29b-41d4-a716-446655440000.jsonl")
    assert conversation_id_from_path(p) == "550e8400-e29b-41d4-a716-446655440000"


def test_conversation_id_from_path_non_uuid():
    p = Path("/a/b/my-chat.jsonl")
    assert conversation_id_from_path(p) == "my-chat"


def test_transcript_relative_path_under_base(monkeypatch, tmp_path):
    base = tmp_path / "projects"
    base.mkdir()
    tr = base / "slug" / "agent-transcripts" / "x.jsonl"
    tr.parent.mkdir(parents=True)
    tr.touch()
    monkeypatch.setattr("curios.config.TRANSCRIPTS_BASE", base)
    rel = transcript_relative_path(tr)
    assert rel.replace("\\", "/") == "slug/agent-transcripts/x.jsonl"


def test_transcript_relative_path_outside_base(monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere" / "f.jsonl"
    outside.parent.mkdir(parents=True)
    outside.touch()
    monkeypatch.setattr("curios.config.TRANSCRIPTS_BASE", tmp_path / "projects")
    rel = transcript_relative_path(outside)
    assert str(outside.resolve()) in rel or rel.endswith("f.jsonl")


def test_get_topic_keywords_merges_custom(monkeypatch, tmp_path):
    import curios.config as cfg

    custom = tmp_path / "custom_keywords.json"
    custom.write_text(
        json.dumps({"decisions": ["xyzzy_unique_kw"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "CUSTOM_KEYWORDS_PATH", custom)
    cfg.get_topic_keywords.cache_clear()
    try:
        kws = cfg.get_topic_keywords()
        assert "xyzzy_unique_kw" in kws["decisions"]
    finally:
        cfg.get_topic_keywords.cache_clear()


def test_get_compiled_topic_patterns_has_all_topics():
    import curios.config as cfg

    cfg.get_topic_keywords.cache_clear()
    cfg.get_compiled_topic_patterns.cache_clear()
    try:
        pats = cfg.get_compiled_topic_patterns()
        for t in cfg.ALL_TOPICS:
            assert t in pats
            assert isinstance(pats[t], tuple)
    finally:
        cfg.get_topic_keywords.cache_clear()
        cfg.get_compiled_topic_patterns.cache_clear()


def test_config_env_overrides_reload(monkeypatch):
    import curios.config as cfg

    try:
        monkeypatch.setenv("CURIOS_CHUNK_SIZE", "900")
        monkeypatch.setenv("CURIOS_RRF_K", "42")
        monkeypatch.setenv("CURIOS_DECISION_BOOST", "0.75")
        monkeypatch.setenv("CURIOS_NOVELTY_THRESHOLD", "0.88")
        monkeypatch.setenv("CURIOS_BM25_MAX_TERMS", "30")
        importlib.reload(cfg)
        assert cfg.CHUNK_SIZE == 900
        assert cfg.CHUNK_HARD_SPLIT_OVERLAP == max(1, 900 // 10)
        assert cfg.RRF_K == 42
        assert cfg.DECISION_BOOST == 0.75
        assert cfg.NOVELTY_THRESHOLD == 0.88
        assert cfg.BM25_MAX_TERMS == 30
    finally:
        for name in (
            "CURIOS_CHUNK_SIZE",
            "CURIOS_RRF_K",
            "CURIOS_DECISION_BOOST",
            "CURIOS_NOVELTY_THRESHOLD",
            "CURIOS_BM25_MAX_TERMS",
        ):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(cfg)


def test_keyword_languages_en_removes_spanish(monkeypatch):
    import curios.config as cfg

    try:
        monkeypatch.setenv("CURIOS_KEYWORD_LANGUAGES", "en")
        importlib.reload(cfg)
        assert cfg.KEYWORD_LANGUAGES == frozenset({"en"})
        assert "decidimos" not in cfg.TOPIC_KEYWORDS["decisions"]
        assert "decided" in cfg.TOPIC_KEYWORDS["decisions"]
    finally:
        monkeypatch.delenv("CURIOS_KEYWORD_LANGUAGES", raising=False)
        importlib.reload(cfg)
        assert "decidimos" in cfg.TOPIC_KEYWORDS["decisions"]
