import base64
import binascii
import functools
import json
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Windows drive root or POSIX filesystem root as a single path part.
_DRIVE_OR_ROOT_PART = re.compile(r"^([A-Za-z]:[\\/]*|/)$")


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    return float(raw) if raw else default


# ── Paths & directories ─────────────────────────────────────
# Override via env: CURIOS_CURSOR_HOME, CURIOS_DATA.
# Default Cursor home: ~/.cursor on all platforms.
# Default data: Linux/BSD XDG-style; macOS ~/Library/Application Support/curios;
# Windows %LOCALAPPDATA%\\curios.
CURSOR_HOME = Path(os.environ.get("CURIOS_CURSOR_HOME", Path.home() / ".cursor"))


def _default_curios_data_dir() -> Path:
    """Platform-appropriate default for CURIOS_DATA when env is unset."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "curios"
        return Path.home() / "AppData" / "Local" / "curios"
    if sys.platform == "darwin":
        # macOS kernel is Darwin; this branch is all Apple desktop OS releases.
        return Path.home() / "Library" / "Application Support" / "curios"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "curios"
    return Path.home() / ".local" / "share" / "curios"


CURIOS_DATA = Path(
    (os.environ.get("CURIOS_DATA") or "").strip() or _default_curios_data_dir()
)

CHROMADB_PATH = CURIOS_DATA / "chromadb"          # persistent vector store


def set_owner_only_permissions(path: str | Path) -> None:
    """Owner-only mode on Unix (dirs 0o700, files 0o600); no-op on Windows."""
    if sys.platform == "win32":
        return
    p = Path(path)
    try:
        mode = 0o700 if p.is_dir() else 0o600
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_data_dir() -> None:
    """Create CURIOS_DATA with restricted permissions (idempotent)."""
    CURIOS_DATA.mkdir(parents=True, exist_ok=True)
    set_owner_only_permissions(CURIOS_DATA)
TRANSCRIPTS_BASE = CURSOR_HOME / "projects"       # where Cursor writes agent transcripts
PREFERENCES_PATH = CURIOS_DATA / "preferences.md" # user-authored preference notes (future)
CUSTOM_KEYWORDS_PATH = CURIOS_DATA / "custom_keywords.json"     # user topic keyword extensions
PROJECT_OVERRIDES_PATH = CURIOS_DATA / "project_overrides.json" # slug→friendly-name mapping
LOCK_PATH = CURIOS_DATA / ".index.lock"           # flock file for concurrent indexer safety
SCHEMA_STATE_PATH = CURIOS_DATA / "schema_version.json"  # tracks DB schema migrations
INDEX_LOG_PATH = CURIOS_DATA / "index.log"        # stdout/stderr from background indexer
LAST_INDEXED_PATH = CURIOS_DATA / "last_indexed.json"    # timestamp of last successful run
BM25_DB_PATH = CURIOS_DATA / "bm25.db"            # SQLite FTS5 sparse index
SENTINELS_DB_PATH = CURIOS_DATA / "sentinels.db"  # per-file index sentinels + recap conversation cache

# ── ChromaDB collections ────────────────────────────────────
COLLECTION_NAME = "curios"              # main chunk collection (embeddings + metadata)

# Bump to force a full re-index (deletes main collection, wipes bm25/sentinels, rewrites schema_version.json).
SCHEMA_VERSION = 6

# Indexed topic dimensions (boolean metadata topic_<name> per chunk). Excludes "general".
ALL_TOPICS: tuple[str, ...] = (
    "decisions",
    "architecture",
    "learnings",
    "problems",
    "preferences",
    "ideas",
    "open_issues",
)

# ── Chunking ────────────────────────────────────────────────
# Target maximum chunk length in characters. Splits prefer paragraph then
# sentence boundaries before falling back to hard character cuts.
# Smaller → more chunks, finer retrieval granularity, more DB overhead.
# Larger  → fewer chunks, coarser granularity, less overhead.
# Sensible range: 400–1500. Default 800 balances granularity vs. context.
# Override: CURIOS_CHUNK_SIZE.
CHUNK_SIZE = _env_int("CURIOS_CHUNK_SIZE", 800)
# Chunks shorter than this (chars) are discarded as noise. Range: 10–100.
MIN_CHUNK_SIZE = 30
# Absolute hard cap on any single chunk (chars). Safety guard against
# pathological inputs. Should be >> CHUNK_SIZE. Default 10 000.
MAX_CHUNK_CHARS = 10_000
# Char overlap between consecutive pieces from the hard-cut fallback splitter.
# Helps embeddings catch cross-boundary content. Sensible range: 5–20% of CHUNK_SIZE.
CHUNK_HARD_SPLIT_OVERLAP = max(1, CHUNK_SIZE // 10)

# ── Depth classification ────────────────────────────────────
# Conversations with fewer user messages than this are tagged depth="shallow"
# at index time. Shallow conversations are excluded by default from search
# and recap (callers can opt in via include_shallow=True).
# 1 = only truly empty conversations are shallow; 3+ = aggressive filtering.
# Default 2: single-exchange "hello world" chats are excluded.
SHALLOW_THRESHOLD = 2

# ── Novelty detection ───────────────────────────────────────
# At index time each chunk is compared against existing chunks in the same
# project. If cosine similarity to any neighbor exceeds this threshold, the
# chunk is tagged novelty="incremental" (semantically redundant); otherwise
# novelty="novel". strict=True search excludes incremental chunks.
# Higher → stricter dedup, fewer incremental tags, more unique chunks kept.
# Lower  → more aggressive dedup, more chunks marked redundant.
# Sensible range: 0.85–0.96. Default 0.92.
# Override: CURIOS_NOVELTY_THRESHOLD.
NOVELTY_THRESHOLD = _env_float("CURIOS_NOVELTY_THRESHOLD", 0.92)
# Number of nearest neighbors checked per chunk for the novelty comparison.
# Higher catches more potential duplicates but slows indexing.
# Sensible range: 3–15. Default 8.
NOVELTY_N_RESULTS = 8

# ── Stale file detection ────────────────────────────────────
# At catch-up time (MCP tool call), sentinels indexed within this window
# are checked for mtime changes on disk. Files modified since indexing
# are re-indexed with force=True. Larger → catches older stale files
# but stats more rows. Sensible range: 1800–86400. Default 3600 (1 hour).
# Override: CURIOS_STALE_MAX_AGE_S.
STALE_MAX_AGE_S = _env_int("CURIOS_STALE_MAX_AGE_S", 86400) # 1 day
# Seconds between full transcript discovery scans in the MCP server.
# Each MCP tool call drains the queue and checks stale files; full
# discovery re-scans all transcript dirs for anything the hook missed.
# Sensible range: 120–900. Default 300 (5 minutes).
# Override: CURIOS_DISCOVERY_INTERVAL_S.
DISCOVERY_INTERVAL_S = _env_int("CURIOS_DISCOVERY_INTERVAL_S", 300)

# ── Topic scoring ───────────────────────────────────────────
# Each chunk's user+assistant text is scanned for keyword hits from
# TOPIC_KEYWORDS (below). Hits are weighted by role: (user_weight, agent_weight).
# Pairs sum to ~3.0 so topics remain comparable. Asymmetry reflects which
# voice typically originates each topic — e.g. "preferences" is almost always
# user-voiced, "learnings" almost always agent-synthesized.
TOPIC_ROLE_WEIGHTS: dict[str, tuple[float, float]] = {
    "preferences":  (2.7, 0.3),   # user-heavy
    "learnings":    (0.5, 2.5),   # agent-heavy (research/tool output)
    "architecture": (1.0, 2.0),   # agent-leaning (design synthesis)
    "decisions":    (2.0, 1.0),   # user-leaning (explicit choices)
    "problems":     (1.5, 1.5),   # balanced
    "ideas":        (1.5, 1.5),   # balanced
    "open_issues":  (1.5, 1.5),   # balanced
}
# Fallback weights for any topic not listed above.
_DEFAULT_ROLE_WEIGHTS: tuple[float, float] = (2.0, 1.0)
# Weighted score must reach this threshold for a topic tag to be assigned.
# Below this, only the single best-scoring topic (if > 0) is tagged as a
# fallback; truly zero-signal chunks default to "general".
# Sensible range: 1–4. Default 2.
TOPIC_MIN_HITS_DEFAULT = 2
# Per-topic overrides for TOPIC_MIN_HITS_DEFAULT.
TOPIC_MIN_HITS: dict[str, int] = {
    "preferences": 2,
    "open_issues": 2,
    "ideas": 2,
    "learnings": 2,
}

# ── Search ranking ──────────────────────────────────────────
# Max chunks returned from the same conversation in a single search.
# Higher → better recall for long conversations with many relevant exchanges.
# Lower (1–2) → maximises conversation diversity in results.
# Ablation (2026-05-05): raising from 3→unlimited doubled mean contextual
# recall (~0.26→~0.52) with no faithfulness regression.
# Sensible range: 3–20. Default 10.
MAX_CHUNKS_PER_CONV = 10
# Distance multiplier applied to decision-tagged chunks when the query
# itself contains decision-related keywords. < 1.0 boosts, > 1.0 penalises.
# Sensible range: 0.7–1.0. Default 0.82.
# Override: CURIOS_DECISION_BOOST.
DECISION_BOOST = _env_float("CURIOS_DECISION_BOOST", 0.82)
# Default max results when MCP caller omits n_results on curios_search.
# Sensible range: 3–20. Default 5.
SEARCH_DEFAULT_N_RESULTS = 5
# Over-fetch multiplier: raw results fetched = n_results * SEARCH_OVERFETCH_FACTOR.
# Higher → better reranking quality (more candidates to sort) but slower.
# Sensible range: 4–15. Default 8.
SEARCH_OVERFETCH_FACTOR = 8
# Max characters returned per result in search responses. Truncates long
# chunks in the MCP output. Default 8 000 (~2 000 tokens).
SEARCH_MAX_TEXT = 8_000
# Max characters per conversation preview in curios_recap output.
# Default 600 (~150 tokens).
RECAP_PREVIEW_MAX = 600

# ── Search fetch bounds ──────────────────────────────────────
# Raw candidates fetched = max(n_results * SEARCH_OVERFETCH_FACTOR, SEARCH_FETCH_MIN),
# then capped at SEARCH_FETCH_MAX. These bounds apply to the dense vector path.
# Floor on raw candidates. Ensures a useful pool even with small n_results.
# Sensible range: 10–50. Default 24.
SEARCH_FETCH_MIN = 24
# Ceiling on raw candidates. Prevents excessive Chroma queries on large n_results.
# Sensible range: 60–300. Default 120.
SEARCH_FETCH_MAX = 120
# After ranking, the candidate pool is expanded to n_results * this factor
# before slicing to the final n_results. Larger → better ranking accuracy
# at minor CPU cost (no extra DB calls). Sensible range: 2–5. Default 3.
SEARCH_CANDIDATES_FACTOR = 3

# ── Hybrid search (BM25 + dense) ────────────────────────────
# When enabled, curios_search fuses dense vector results with BM25 sparse
# results via Reciprocal Rank Fusion (RRF). Disabled = dense-only.
# Override via env: CURIOS_HYBRID_SEARCH=0 for dense-only baseline.
HYBRID_SEARCH_ENABLED = os.environ.get(
    "CURIOS_HYBRID_SEARCH", "true"
).strip().lower() not in ("0", "false", "no", "off")
# Max query tokens sent to FTS5 MATCH. Longer queries are truncated to this
# many OR-joined terms. Higher → broader sparse recall, risk of noise.
# Sensible range: 10–40. Default 24.
# Override: CURIOS_BM25_MAX_TERMS.
BM25_MAX_TERMS = _env_int("CURIOS_BM25_MAX_TERMS", 24)
# RRF smoothing constant. Lower → top ranks dominate more; higher → flatter
# rank contribution. Standard IR default is 60. Sensible range: 20–100.
# Override: CURIOS_RRF_K.
RRF_K = _env_int("CURIOS_RRF_K", 60)
# Number of BM25 results fetched per search. Higher → better sparse recall
# but more candidates to merge. Sensible range: 20–100. Default 50.
BM25_FETCH_N = 50
# When topic / strict / default depth filters are active, BM25_FETCH_N is
# multiplied by this factor before FTS5 LIMIT. BM25 only filters by project;
# post-filters drop non-matching chunks — over-fetching keeps sparse signal.
# Sensible range: 2–6. Default 4.
BM25_FILTER_OVERFETCH_FACTOR = 4

# ── curios_recap ────────────────────────────────────────────
# Default max recent conversations returned when caller omits n_results.
# Sensible range: 3–15. Default 5.
RECAP_DEFAULT_N_RESULTS = 5
# Hard cap on total chunks scanned when building the recency-ordered recap.
# Protects against very large indices. Sensible range: 1000–20 000. Default 5 000.
RECAP_FETCH_LIMIT = 5_000

# ── curios_related ──────────────────────────────────────────
# Default max related conversations returned when caller omits n_results.
# Sensible range: 3–15. Default 5.
RELATED_DEFAULT_N_RESULTS = 5
# Max chunks loaded from the source conversation for probe selection.
# Higher → more context to pick probes from, but slower on huge conversations.
# Sensible range: 20–100. Default 50.
RELATED_SOURCE_LIMIT = 50
# Number of top-scored source chunks used as ANN probes into other
# conversations. More probes → broader cross-reference recall.
# Sensible range: 1–5. Default 3.
RELATED_PROBE_CHUNKS = 3
# Probe selection scoring weights. Each source chunk gets a score; the top
# RELATED_PROBE_CHUNKS are selected. Higher weight → stronger preference.
RELATED_PROBE_WEIGHT_DEPTH = 1.0   # bonus for non-shallow conversations
RELATED_PROBE_WEIGHT_NOVEL = 0.5   # bonus for novel (non-incremental) chunks
RELATED_PROBE_WEIGHT_FIRST = 0.3   # bonus for the first chunk (conversation opener)
# Raw candidates per probe = min(n_results * this factor, RELATED_FETCH_MAX).
# Higher → broader candidate pool per probe. Sensible range: 3–10. Default 6.
RELATED_OVERFETCH_FACTOR = 6
# Ceiling on candidates fetched per probe. Sensible range: 30–120. Default 60.
RELATED_FETCH_MAX = 60

# ── Multi-query retrieval ───────────────────────────────────
# When enabled AND a topic filter is active, curios_search fires additional
# semantic queries (from FIELD_QUERY_TEMPLATES + a keyword-augmented variant)
# and merges results by best distance. Improves recall for topic-filtered
# searches at the cost of extra Chroma queries.
MULTI_QUERY_ENABLED = True
# Cap on distinct query strings per search (includes the user's primary query).
# Sensible range: 2–6. Default 4.
MULTI_QUERY_MAX_VARIANTS = 4
# Number of top topic keywords appended to form the keyword-augmented variant.
# More → broader keyword coverage, but dilutes semantic focus.
# Sensible range: 3–10. Default 5.
MULTI_QUERY_KW_COUNT = 5

# ── ChromaDB ─────────────────────────────────────────────────
# Distance metric for HNSW index. "cosine" suits normalised sentence embeddings.
# Alternatives: "l2", "ip". Changing requires a full re-index.
CHROMA_HNSW_SPACE = "cosine"
# Dense embedding model. "default" = Chroma's ONNX all-MiniLM-L6-v2 (unchanged behaviour).
# Any other value: HuggingFace model id for SentenceTransformerEmbeddingFunction
# (requires sentence-transformers). Override: CURIOS_EMBEDDING_MODEL.
# After switching models, bump SCHEMA_VERSION or force a full re-index.
EMBEDDING_MODEL = (os.environ.get("CURIOS_EMBEDDING_MODEL") or "default").strip() or "default"
# Retries on transient ChromaDB InternalError (e.g. HNSW race conditions).
# Sensible range: 1–5. Default 2.
CHROMA_RETRY_ATTEMPTS = 2
# Seconds between retries. Default 0.5.
CHROMA_RETRY_DELAY = 0.5
# Page size when iterating all chunks (stats, verify, build-bm25).
# Larger → fewer round-trips, more memory. Sensible range: 500–5000. Default 2000.
CHROMA_ITER_BATCH = 2000
# Batch size for bulk deletes (prune commands).
# Larger → fewer round-trips. Sensible range: 100–2000. Default 500.
CHROMA_DELETE_BATCH = 500


def get_embedding_function():
    """Chroma embedding function for indexing and querying (must be identical)."""
    from chromadb.utils import embedding_functions

    if EMBEDDING_MODEL.lower() == "default":
        return embedding_functions.DefaultEmbeddingFunction()
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL,
    )


# ── CLI display (curios report / status) ─────────────────────
# Character width for horizontal rulers and table formatting.
CLI_RULER_WIDTH = 62
# Max conversations listed in the shallow / fully-incremental sections.
CLI_MAX_LIST_ITEMS = 20

# ── Cursor integration ───────────────────────────────────────
# Timeout in seconds for the sessionEnd hook command in hooks.json.
# If the indexer doesn't finish in this window, Cursor kills the process.
# Sensible range: 5–30. Default 10.
SESSION_HOOK_TIMEOUT = 10

HOME = Path.home()

# ── Topic query templates ────────────────────────────────────
# Additional natural-language queries fired per topic when multi-query retrieval
# is active (MULTI_QUERY_ENABLED + topic filter). Two templates per topic;
# at most MULTI_QUERY_MAX_VARIANTS total queries including the user's original.
FIELD_QUERY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "decisions": (
        "what decisions were made and why, what was the rationale",
        "what did we choose, what approach did we go with",
    ),
    "architecture": (
        "software architecture design patterns components structure",
        "how is the system designed, what are the key modules and layers",
    ),
    "learnings": (
        "what did we learn, research findings, key insights discovered",
        "what does the documentation say, what did analysis reveal",
    ),
    "problems": (
        "bugs errors crashes failures and how they were fixed",
        "what went wrong, root cause analysis, workarounds applied",
    ),
    "preferences": (
        "coding preferences conventions style rules the user prefers",
        "what the user always does or never does, personal rules",
    ),
    "ideas": (
        "ideas for future improvements, brainstormed suggestions",
        "what if we could, worth exploring, possible approaches",
    ),
    "open_issues": (
        "open issues todos unresolved questions still pending",
        "what still needs to be done, remaining work, blockers",
    ),
}

# ── Topic keywords ───────────────────────────────────────────
# Case-insensitive phrases scanned in chunk text for topic scoring.
# English vs Spanish subsets are merged according to KEYWORD_LANGUAGES.
# Extend per-user via CUSTOM_KEYWORDS_PATH (custom_keywords.json).
_kw_lang_raw = (os.environ.get("CURIOS_KEYWORD_LANGUAGES") or "en,es").strip().lower()
KEYWORD_LANGUAGES: frozenset[str] = frozenset(
    x.strip() for x in (_kw_lang_raw or "en,es").split(",") if x.strip()
)

_TOPIC_KW_EN: dict[str, tuple[str, ...]] = {
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
        "sounds good",
        "go ahead",
        "let's do that",
        "that works",
        "approved",
    ),
    "architecture": (
        "architecture",
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
    "learnings": (
        "according to",
        "the paper",
        "the documentation",
        "documentation says",
        "research shows",
        "research suggests",
        "the study",
        "benchmark",
        "i found that",
        "i learned",
        "turns out",
        "it appears that",
        "key finding",
        "the takeaway",
        "in summary",
        "to summarize",
        "based on my analysis",
        "the data shows",
        "results show",
        "web search",
        "search results",
        "the results indicate",
        "measured",
        "observed that",
        "confirmed that",
        "best practice",
        "gotcha",
        "official docs",
        "TIL",
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
        "doesn't work",
        "breaking change",
        "incompatible",
        "performance issue",
        "memory leak",
        "race condition",
        "technical debt",
        "tech debt",
    ),
    "preferences": (
        "i prefer",
        "i'd rather",
        "i'd like",
        "i like to",
        "i want to",
        "i feel",
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
        "my preference",
        "our team uses",
        "our convention",
        "please don't",
        "please avoid",
        "i don't like",
        "i don't want",
    ),
    "ideas": (
        "what if",
        "what about",
        "maybe we could",
        "we could",
        "how about",
        "nice to have",
        "nice-to-have",
        "worth exploring",
        "worth trying",
        "worth considering",
        "future",
        "prototype",
        "experiment",
        "brainstorm",
        "spike",
        "explore",
        "might be worth",
        "could try",
        "idea:",
        "one idea",
        "another idea",
        "alternative approach",
        "an option",
        "possible approach",
        "stretch goal",
        "down the road",
        "longer term",
        "eventually",
    ),
    "open_issues": (
        "todo",
        "fixme",
        "hack",
        "still need to",
        "haven't yet",
        "hasn't been",
        "pending",
        "not yet implemented",
        "not yet done",
        "follow-up",
        "follow up",
        "open question",
        "blocked",
        "needs work",
        "needs fixing",
        "needs attention",
        "needs tightening",
        "not addressed",
        "unresolved",
        "left to do",
        "remaining work",
        "should revisit",
        "revisit",
        "come back to",
        "circle back",
        "defer",
        "deferred",
        "postpone",
        "known issue",
        "known limitation",
        "missing",
        "incomplete",
        "inconsistency",
        "inconsistencies",
        "inconsistent",
        "work in progress",
        "WIP",
        "TBD",
        "to be determined",
        "need to figure out",
        "workaround in place",
        "temporary fix",
        "temp fix",
    ),
    "general": (),
}

_TOPIC_KW_ES: dict[str, tuple[str, ...]] = {
    "decisions": (
        "decidimos",
        "vamos con",
        "la decisión",
        "optamos por",
        "elegimos",
        "nos quedamos con",
        "la opción es",
        "de acuerdo",
        "la conclusión",
        "la estrategia",
        "está decidido",
        "vamos a usar",
        "compromiso",
        "nos decantamos",
    ),
    "architecture": (
        "arquitectura",
        "diseño",
        "patrón",
        "módulo",
        "capa",
        "estructura",
        "flujo",
        "componente",
        "servicio",
        "interfaz",
        "dependencia",
        "acoplamiento",
        "esquema",
        "tubería",
        "punto de acceso",
    ),
    "learnings": (
        "según",
        "la investigación",
        "resulta que",
        "el análisis muestra",
        "los datos muestran",
        "en resumen",
        "encontré que",
        "aprendí que",
        "el resultado",
        "se confirma",
        "la documentación dice",
        "resultado clave",
        "la conclusión es",
        "buena práctica",
        "búsqueda web",
        "resultados de búsqueda",
        "se observó",
        "se midió",
    ),
    "problems": (
        "no funciona",
        "falla",
        "fallo",
        "causa raíz",
        "solución alternativa",
        "demasiado complejo",
        "está roto",
        "regresión",
        "excepción",
        "traza de error",
        "no funciona correctamente",
        "cambio incompatible",
        "deuda técnica",
        "problema de rendimiento",
    ),
    "preferences": (
        "prefiero",
        "me gustaría",
        "por favor no",
        "por favor evita",
        "siempre uso",
        "nunca uses",
        "mi convención",
        "mi preferencia",
        "no me gusta",
        "nuestro equipo",
        "preferiría",
        "siempre hago",
        "nunca hago",
        "mantenlo simple",
        "por favor evitar",
        "no quiero",
        "nuestro equipo usa",
    ),
    "ideas": (
        "qué tal si",
        "podríamos",
        "estaría bien",
        "a futuro",
        "y si",
        "otra idea",
        "una opción",
        "posible enfoque",
        "a largo plazo",
        "estaría bien tener",
        "merece explorar",
        "merece probar",
        "lluvia de ideas",
        "objetivo ambicioso",
        "más adelante",
        "eventualmente",
    ),
    "open_issues": (
        "falta",
        "hace falta",
        "por hacer",
        "aún no",
        "pregunta abierta",
        "bloqueado",
        "sin implementar",
        "sin resolver",
        "pendiente",
        "hay que volver",
        "problema conocido",
        "incompleto",
        "trabajo en curso",
        "por determinar",
        "hay que resolver",
        "necesita atención",
        "necesita arreglo",
        "aplazar",
        "aplazado",
        "posponer",
        "solución temporal",
        "arreglo temporal",
    ),
    "general": (),
}

_TOPIC_KW_REGISTRY: dict[str, dict[str, tuple[str, ...]]] = {
    "en": _TOPIC_KW_EN,
    "es": _TOPIC_KW_ES,
}

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {}
for _topic in (*ALL_TOPICS, "general"):
    _merged_kw: list[str] = []
    for _lang, _kw_dict in _TOPIC_KW_REGISTRY.items():
        if _lang in KEYWORD_LANGUAGES:
            _merged_kw.extend(_kw_dict.get(_topic, ()))
    TOPIC_KEYWORDS[_topic] = tuple(_merged_kw)


@functools.lru_cache(maxsize=1)
def get_topic_keywords() -> dict[str, tuple[str, ...]]:
    """Merge default TOPIC_KEYWORDS with user-specific custom_keywords.json.

    Cached for the process lifetime. After editing custom_keywords.json, restart
    the Curios MCP server to pick up changes; the short-lived indexer always
    starts fresh and sees the latest file.
    """
    if not CUSTOM_KEYWORDS_PATH.exists():
        return TOPIC_KEYWORDS
    try:
        custom: dict[str, list[str]] = json.loads(
            CUSTOM_KEYWORDS_PATH.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load %s: %s", CUSTOM_KEYWORDS_PATH, exc)
        return TOPIC_KEYWORDS
    unknown = set(custom) - set(TOPIC_KEYWORDS)
    if unknown:
        log.warning(
            "custom_keywords.json contains unknown topics (ignored): %s",
            sorted(unknown),
        )
    merged: dict[str, tuple[str, ...]] = {}
    for topic, defaults in TOPIC_KEYWORDS.items():
        extras = custom.get(topic, [])
        existing = set(k.lower() for k in defaults)
        new = tuple(k for k in extras if k.lower() not in existing)
        merged[topic] = defaults + new
    return merged


def _keyword_boundary_pattern(keyword: str) -> re.Pattern[str]:
    """Word-boundary match; omits \\b where the phrase starts/ends with punctuation."""
    k = keyword.strip()
    pre = r"\b" if k[:1].isalnum() or k[:1] == "_" else ""
    post = r"\b" if k[-1:].isalnum() or k[-1:] == "_" else ""
    return re.compile(rf"{pre}{re.escape(k)}{post}", re.IGNORECASE | re.UNICODE)


@functools.lru_cache(maxsize=1)
def get_compiled_topic_patterns() -> dict[str, tuple[re.Pattern[str], ...]]:
    """Per-topic compiled regexes for word-boundary keyword hits.

    Cached with get_topic_keywords(); restart MCP after custom_keywords.json edits.
    """
    kws = get_topic_keywords()
    return {
        topic: tuple(_keyword_boundary_pattern(k) for k in keys if k.strip())
        for topic, keys in kws.items()
    }


def get_project_overrides() -> dict[str, str]:
    """Load user-local slug→project-name overrides from project_overrides.json."""
    if not PROJECT_OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(PROJECT_OVERRIDES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


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

# ── Secret redaction ─────────────────────────────────────────
# Regex patterns applied to all chunk text before indexing. Matches are
# replaced with "[REDACTED]" to prevent secrets leaking into the DB.
# Order matters: list more specific patterns before broad ones (e.g. sk-ant- before sk-).
REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}"), "[REDACTED]"),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "[REDACTED]"),
    (re.compile(
        r"(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY)\s*[:=]\s*[A-Za-z0-9/+=]{40}"
    ), "[REDACTED]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "[REDACTED]"),
    (re.compile(r"github_pat_[a-zA-Z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"glpat-[a-zA-Z0-9\-_]{20,}"), "[REDACTED]"),
    (re.compile(r"xox[bpas]-[a-zA-Z0-9\-]{10,}"), "[REDACTED]"),
    (re.compile(r"AIza[a-zA-Z0-9_\-]{35}"), "[REDACTED]"),
    (re.compile(
        r"eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}"
    ), "[REDACTED]"),
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
            r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(r"(?:AccountKey|SharedAccessKey|sig)=[A-Za-z0-9/+=]{20,}", re.I),
        "[REDACTED]",
    ),
    (
        re.compile(r"(?i)heroku[a-zA-Z_]*[:=]\s*[0-9a-f\-]{36}"),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"(?:password|secret|token|api_key)\s*(?:is|was|will be|:)\s*[\"']?\S+[\"']?",
            re.I,
        ),
        "[REDACTED]",
    ),
    (re.compile(
        r"(?:password|secret|token|api_key|apikey|private_key)\s*[:=]\s*\S+",
        re.I,
    ), "[REDACTED]"),
    (re.compile(
        r"^[A-Z_]*(?:SECRET|PASSWORD|TOKEN|PRIVATE_KEY|API_KEY)[A-Z_]*\s*=\s*\S+",
        re.I | re.M,
    ), "[REDACTED]"),
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

    overrides = get_project_overrides()
    if slug in overrides:
        return overrides[slug]

    imported = project_name_from_import_slug(slug)
    if imported is not None:
        return imported

    segments = _slug_segments(slug)
    if not segments:
        return "unknown"

    home_parts = [
        p.lower()
        for p in HOME.parts
        if not _DRIVE_OR_ROOT_PART.match(p)
    ]
    while segments and home_parts and segments[0].lower() == home_parts[0]:
        segments = segments[1:]
        home_parts = home_parts[1:]

    skip = {"home", "users", "documents", "documentos", "applications", "apps", "projects", "workspace", "code", "src", "git", "gitlab", "github", "dev"}
    meaningful = [s for s in segments if s.lower() not in skip and not s.isdigit()]
    if not meaningful:
        meaningful = [s for s in segments if not s.isdigit()] or segments
    pick = meaningful[-1] if meaningful else slug
    return pick.upper() if pick.islower() and len(pick) <= 4 else pick


def transcript_relative_path(transcript_path: Path) -> str:
    try:
        return str(transcript_path.resolve().relative_to(TRANSCRIPTS_BASE.resolve()))
    except ValueError:
        return str(transcript_path.resolve())


def conversation_id_from_path(transcript_path: Path) -> str:
    return transcript_path.stem
