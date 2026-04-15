import os
import re
from pathlib import Path

CURSOR_HOME = Path(os.environ.get("CURIOS_CURSOR_HOME", Path.home() / ".cursor"))
CURIOS_DATA = Path(os.environ.get("CURIOS_DATA", Path.home() / ".local" / "share" / "curios"))

CHROMADB_PATH = CURIOS_DATA / "chromadb"
TRANSCRIPTS_BASE = CURSOR_HOME / "projects"
PREFERENCES_PATH = CURIOS_DATA / "preferences.md"
LOCK_PATH = CURIOS_DATA / ".index.lock"
SCHEMA_STATE_PATH = CURIOS_DATA / "schema_version.json"

COLLECTION_NAME = "curios"
SENTINEL_COLLECTION_NAME = "curios_sentinels"

SCHEMA_VERSION = 3
CHUNK_SIZE = 800
MIN_CHUNK_SIZE = 30
MAX_CHUNK_CHARS = 10_000
SHALLOW_THRESHOLD = 2
NOVELTY_THRESHOLD = 0.92
TOPIC_MIN_HITS_DEFAULT = 4

TOPIC_MIN_HITS: dict[str, int] = {
    "preferences": 2,
    "open_issues": 2,
    "ideas": 2,
}

HOME = Path.home()

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "decisions": (
        "decided",
        "chose",
        "went with",
        "let's go with",
        "went for",
        "the call is",
        "trade-off",
        "tradeoff",
        "instead of",
        "approach",
        "strategy",
        "decision",
        "we will",
        "we'll use",
        "agreed",
        "conclusion",
        "rationale",
        "decidimos",
        "vamos con",
        "la decisión",
    ),
    "architecture": (
        "architecture",
        "arquitectura",
        "design",
        "pattern",
        "module",
        "service",
        "schema",
        "component",
        "layer",
        "interface",
        "dependency",
        "coupling",
        "bounded context",
        "stack",
        "pipeline",
        "endpoint",
        "middleware",
    ),
    "planning": (
        "plan",
        "roadmap",
        "milestone",
        "sprint",
        "scope",
        "requirement",
        "requisito",
        "deadline",
        "timeline",
        "backlog",
        "epic",
        "deliverable",
        "entregable",
        "capítulo",
        "sección",
    ),
    "problems": (
        "bug",
        "error",
        "crash",
        "broken",
        "workaround",
        "root cause",
        "fix",
        "regression",
        "exception",
        "stack trace",
        "fails",
        "overengineering",
        "overkill",
        "too complex",
        "too complicated",
        "too heavy",
    ),
    "preferences": (
        "i prefer",
        "i'd rather",
        "i like to",
        "i want",
        "i always",
        "i never",
        "always use",
        "never use",
        "always do",
        "never do",
        "don't add",
        "no need for",
        "keep it simple",
        "my convention",
        "my style",
        "my rule",
        "our team uses",
        "prefiero",
        "por favor no",
        "me gusta",
    ),
    "ideas": (
        "what if",
        "what about",
        "maybe we could",
        "we could",
        "nice to have",
        "nice-to-have",
        "worth exploring",
        "future",
        "prototype",
        "experiment",
        "brainstorm",
        "spike",
        "explore",
        "might be worth",
    ),
    "open_issues": (
        "todo",
        "fixme",
        "still need to",
        "haven't yet",
        "pending",
        "not yet implemented",
        "follow-up",
        "open question",
        "blocked",
    ),
    "general": (),
}

PROJECT_NAME_OVERRIDES: dict[str, str] = {}

REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "[REDACTED]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"password\s*[:=]\s*\S+", re.I), "[REDACTED]"),
    (re.compile(r"secret\s*[:=]\s*\S+", re.I), "[REDACTED]"),
    (re.compile(r"token\s*[:=]\s*\S+", re.I), "[REDACTED]"),
)


def redact_secrets(text: str) -> str:
    out = text
    for pattern, repl in REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _slug_segments(slug: str) -> list[str]:
    return [s for s in slug.split("-") if s]


def extract_project_name(transcript_path: Path) -> str:
    resolved = transcript_path.resolve()
    parts = resolved.parts
    slug = ""
    try:
        idx = parts.index("projects")
        slug = parts[idx + 1]
    except (ValueError, IndexError):
        return "unknown"

    if slug in PROJECT_NAME_OVERRIDES:
        return PROJECT_NAME_OVERRIDES[slug]

    segments = _slug_segments(slug)
    if not segments:
        return "unknown"

    home_name = HOME.name.lower()
    while segments and segments[0].lower() == home_name:
        segments = segments[1:]

    skip = {"home", "users", "documents", "documentos", "applications", "apps", "projects", "workspace", "code", "src", "git", "gitlab", "github", "dev"}
    meaningful = [s for s in segments if s.lower() not in skip and not s.isdigit()]
    if not meaningful:
        meaningful = [s for s in segments if not s.isdigit()] or segments
    pick = meaningful[-1]
    return pick.upper() if pick.islower() and len(pick) <= 4 else pick


def transcript_relative_path(transcript_path: Path) -> str:
    try:
        return str(transcript_path.resolve().relative_to(TRANSCRIPTS_BASE.resolve()))
    except ValueError:
        return str(transcript_path.resolve())


def conversation_id_from_path(transcript_path: Path) -> str:
    stem = transcript_path.stem
    if len(stem) == 36 and stem.count("-") == 4:
        return stem
    return stem
