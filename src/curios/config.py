import base64
import binascii
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

# ── Chunking ────────────────────────────────────────────────
# Controls how conversation exchanges are split into embeddable chunks.
# Smaller CHUNK_SIZE → more chunks, finer retrieval but more DB overhead.
# Larger CHUNK_SIZE → fewer chunks, coarser retrieval granularity.
CHUNK_SIZE = 800
MIN_CHUNK_SIZE = 30  # chunks below this char count are discarded
MAX_CHUNK_CHARS = 10_000  # hard cap on any single chunk

# ── Depth classification ────────────────────────────────────
# Conversations with fewer user messages than this are marked "shallow".
# Shallow conversations are excluded by default from search and recap.
SHALLOW_THRESHOLD = 2

# ── Novelty detection ───────────────────────────────────────
# During indexing, each chunk is compared against existing chunks in the
# same project. If cosine similarity exceeds NOVELTY_THRESHOLD, the chunk
# is labelled "incremental" (semantically redundant). Otherwise "novel".
# Higher threshold → stricter dedup → fewer incremental chunks.
NOVELTY_THRESHOLD = 0.92
# How many nearest neighbors to check when evaluating novelty.
NOVELTY_N_RESULTS = 8

# ── Topic scoring ───────────────────────────────────────────
# Each chunk's user+assistant text is scanned for keyword hits.
# User text hits are multiplied by USER_WEIGHT (higher → user phrasing
# matters more than assistant boilerplate for topic assignment).
USER_WEIGHT = 2
# Minimum total weighted hits required to assign a topic to a chunk.
# Topics with many keywords (architecture: 17, decisions: 20) naturally
# accumulate more hits, so a higher default avoids over-tagging.
TOPIC_MIN_HITS_DEFAULT = 4
# Per-topic overrides for high-signal topics with fewer keywords.
TOPIC_MIN_HITS: dict[str, int] = {
    "preferences": 2,
    "open_issues": 2,
    "ideas": 2,
}

# ── Search ranking ──────────────────────────────────────────
# Distance multiplier applied to "incremental" chunks during search.
# Values > 1.0 push redundant content lower in results.
INCREMENTAL_PENALTY = 1.15
# Distance multiplier for decision-tagged chunks when the query itself
# contains decision-related keywords. Values < 1.0 boost them higher.
DECISION_BOOST = 0.82
# Over-fetch multiplier: raw results fetched = n_results * this factor.
# Higher → better reranking quality but slower queries.
SEARCH_OVERFETCH_FACTOR = 8
# Max characters returned per result in search and recap responses.
SEARCH_MAX_TEXT = 8_000
RECAP_PREVIEW_MAX = 600

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

CURIOS_IMPORT_SLUG_PREFIX = "curios-import-"


def import_slug_for_project(project: str) -> str:
    raw = project.encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{CURIOS_IMPORT_SLUG_PREFIX}{b64}"


_B64URL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def project_name_from_import_slug(slug: str) -> str | None:
    if not slug.startswith(CURIOS_IMPORT_SLUG_PREFIX):
        return None
    suffix = slug[len(CURIOS_IMPORT_SLUG_PREFIX) :]
    if not suffix or not all(c in _B64URL_CHARS for c in suffix):
        return None
    try:
        pad = "=" * (-len(suffix) % 4)
        decoded = base64.urlsafe_b64decode(suffix + pad)
        name = decoded.decode("utf-8")
        return name if name else None
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None

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

    imported = project_name_from_import_slug(slug)
    if imported is not None:
        return imported

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
