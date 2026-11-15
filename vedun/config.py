"""Paths, constants, environment settings, the corpus profile and the embedding-model registry."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variables are ``VEDUN_<NAME>``.
ENV_PREFIX = "VEDUN_"


def env(name: str, default: str | None = None) -> str | None:
    """Setting ``name`` (without prefix) from ``VEDUN_<name>``, else ``default``.

    An empty value counts as unset (an unset secret passed through compose arrives as ``""``).
    """
    return os.environ.get(ENV_PREFIX + name) or default


def _env_path(name: str, default: Path) -> Path:
    """Path from setting ``name`` (see :func:`env`) or the default (container-friendly)."""
    v = env(name)
    return Path(v) if v else default


#: Project root: holds the DB, ``md/``, ``figures/`` and ``reports/``.
ROOT = _env_path("ROOT", Path(__file__).resolve().parent.parent)
DB_PATH = _env_path("DB", ROOT / "vedun.db")
MD_DIR = _env_path("MD_DIR", ROOT / "md")
FIG_DIR = _env_path("FIG_DIR", ROOT / "figures")
REPORT_DIR = _env_path("REPORT_DIR", ROOT / "reports")
#: Sidecar state (job history, catalog cache): a separate SQLite file, never the KB database.
STATE_DIR = _env_path("STATE_DIR", ROOT / ".state")
DEFAULT_PDF_DIR = _env_path("PDF_DIR", ROOT / "pdf")

# --- corpus profile ---------------------------------------------------------------------------------------
# Everything that depends on the document collection (how documents are named, numbered and cited, which
# boilerplate lines to drop) lives here with neutral defaults; a collection sets its own values in the JSON
# config file (key in brackets). Code reads these names at call time, never copies the patterns.

#: Document id as it appears in file names and citations [doc_id_pattern]; no capturing groups.
#: File names are ``<id>_<YYYYMM>.pdf`` (edition = year + month).
DOC_ID_PATTERN = r"\d{1,2}-\d{1,3}[A-Z]?"
#: Text before the id in a citation ("Document 3-26") [citation_prefix_pattern]; no capturing groups.
CITATION_PREFIX_PATTERN = r"(?:Doc(?:ument)?\.?)"
#: Whole-line page furniture to drop during conversion, regexes, case-insensitive [boilerplate_patterns].
#: The running header (a line holding only the document id) and "Page N" are always dropped.
BOILERPLATE_PATTERNS: tuple[str, ...] = ()
#: Words after which a number is a cross-reference ("see 2.1.3", "Table 2.4"), not a new clause [clause_ref_words].
CLAUSE_REF_WORDS: tuple[str, ...] = (
    "Section", "Sections", "Clause", "Clauses", "Paragraph", "Table", "Tables", "Figure", "Figures", "Fig.",
    "Appendix", "Annex", "Chapter", "Item", "Step", "Note", "No.",
    "see", "See", "in", "of", "per", "and", "or", "to", "with", "under",
)  # fmt: skip
#: Capitalised tokens after which a number is a value ("2.5 MMBtuh"), not a new clause [value_units].
#: Matched as prefixes; lowercase units (ft, mm, kPa) never qualify anyway: a clause title starts with a capital.
VALUE_UNITS: tuple[str, ...] = ("MMBtu", "Btu", "MW", "MJ", "GJ", "MPa")
#: Documents whose tables are read with Docling's FAST TableFormer instead of ACCURATE [fast_table_documents].
#: ACCURATE decodes table structure cell by cell; page-filling numeric tables can take hours on CPU.
FAST_TABLE_DOCUMENTS: frozenset[str] = frozenset()
#: Name of the collection shown in the API title and the MCP server instructions [corpus_name].
CORPUS_NAME = "Document knowledge base"
#: How a document is named in running text, chunk headers and tool output [doc_label]; field {doc_id}.
#: Part of every embedded chunk: changing it means re-embedding the whole collection.
DOC_LABEL = "{doc_id}"
#: Ready-to-paste citation per language [citation_template]; fields {doc_id} {title} {edition} {locator}.
CITATION_TEMPLATE: dict[str, str] = {
    "pl": "{doc_id} „{title}” ({edition}), {locator}",
    "en": '{doc_id} "{title}" ({edition}), {locator}',
}
#: Locator of a unit per language and kind [locators]; field {id}.
LOCATORS: dict[str, dict[str, str]] = {
    "pl": {"clause": "pkt {id}", "table": "Tab. {id}", "figure": "rys. {id}", "appendix": "zał. {id}"},
    "en": {"clause": "Section {id}", "table": "Table {id}", "figure": "Fig. {id}", "appendix": "Appendix {id}"},
}
#: How the printed edition date is found [edition_label]; None = month and year from the file name.
#: Keys: ``pages`` (first pages to read), ``original`` and ``revision`` (full-line regexes), ``original_in_history``
#: (regex searched on every page when only a revision line is printed; group 1 = the date). ``{MONTH}`` in a regex
#: stands for "<English month> <YYYY>".
EDITION_LABEL: dict | None = None
#: Regexes removed from the PDF metadata title; ``{doc_id}`` = the escaped document id [title_strip_patterns].
TITLE_STRIP_PATTERNS: tuple[str, ...] = ()
#: Unit id of a document's own "what changed" section, quoted verbatim in sync reports [changes_unit_id].
CHANGES_UNIT_ID: str | None = None
#: Python file defining ``SOURCE`` (catalog of documents + download), see :mod:`vedun.catalog` [source_plugin].
#: A relative path is resolved against the config file that names it. None = no remote source.
SOURCE_PLUGIN: Path | None = None
#: Folder names that check-citations never scans, on top of the built-in ones [citation_skip_dirs].
CITATION_SKIP_DIRS: tuple[str, ...] = ()
#: Language of the dashboard: "en" or "pl" [ui_language]; a viewer can switch in the page header.
UI_LANGUAGE = "en"
UI_LANGUAGES = ("en", "pl")
#: Prefix of the MCP tool names: ``<prefix>_lookup``, ``<prefix>_search`` ... [mcp_tool_prefix].
MCP_TOOL_PREFIX = "kb"
_MCP_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
#: The config file that was loaded (after ``extends``), for messages.
CONFIG_PATH: Path | None = None


def _no_groups(name: str, pattern: str) -> str:
    """Validate a regex from the config file: it must compile and have no capturing groups (callers wrap it)."""
    try:
        groups = re.compile(pattern).groups
    except re.error as e:
        raise ValueError(f"config {name}: invalid regex: {e}") from e
    if groups:
        raise ValueError(f"config {name}: use (?:...) instead of capturing groups")
    return pattern


def doc_id_re() -> re.Pattern[str]:
    """Full match of a document id (built on each call: ``load_settings`` may change the pattern; ``re`` caches)."""
    return re.compile(rf"^(?:{DOC_ID_PATTERN})$")


def pdf_name_re() -> re.Pattern[str]:
    """``<id>_<YYYYMM>.pdf``; group 1 = id, group 2 = YYYYMM."""
    return re.compile(rf"^({DOC_ID_PATTERN})_(\d{{6}})\.pdf$")


def pdf_path(source_pdf: str | None, stored_path: str | None = None) -> str | None:
    """Location of a sheet's PDF in THIS environment (host or container).

    The DB stores the absolute path of the machine that ran ``sync`` (``documents.source_path``), which is
    wrong in the other environment. The file name (``documents.source_pdf``) is stable, so the path shown in
    Evidence is derived from the configured PDF folder (``VEDUN_PDF_DIR``) instead.
    """
    return str(DEFAULT_PDF_DIR / source_pdf) if source_pdf else stored_path


# --- chunking -------------------------------------------------------------------------
CHUNK_TOKENS = 380  # target size of a text chunk (approx. tokens)
CHUNK_OVERLAP_TOKENS = 50
WORDS_PER_TOKEN = 0.72  # crude: 1 token ~ 0.72 English words (tokens ~ words * 1.4)

# --- cost guard / retries ---------------------------------------------------------------
MAX_TOTAL_COST_USD = 1.0
CHARS_PER_TOKEN = 3.5  # conservative estimate used before calling the API
IMAGE_TOKENS_ESTIMATE = 1600  # conservative per-image token estimate for cost guard

OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"


@dataclass(frozen=True)
class ModelSpec:
    """One embedding model behind an OpenAI-compatible ``/embeddings`` endpoint (default: OpenRouter)."""

    key: str  # short key, used in table names (vec_<key>, figvec_<key>)
    api_id: str  # model id at the provider
    dim: int  # stored dimension (vectors are truncated + re-normalised to this size)
    supports_text: bool = True
    supports_image: bool = False
    query_template: str = "{q}"  # applied to queries
    doc_template: str = "{d}"  # applied to documents/chunks
    price_text_per_m: float = 0.0  # USD per million tokens
    price_image_per_m: float = 0.0
    batch_size: int = 16
    time_budget_s: int = 20 * 60  # per-run budget for this model
    max_attempts: int = 6
    base_url: str = OPENROUTER_URL  # full URL of the embeddings endpoint
    api_key_env: str = "OPENROUTER_API_KEY"  # environment variable holding the provider key
    notes: str = field(default="", compare=False)


_MODEL_KEY_RE = re.compile(r"^[a-z0-9_]{1,16}$")  # the key becomes part of SQL table names
_MODEL_FIELDS = {f for f in ModelSpec.__dataclass_fields__ if f not in ("key", "notes")}


def _model_from_json(key: str, raw: dict, base: ModelSpec | None) -> ModelSpec:
    """A model defined (or overridden) in the JSON config; unknown fields and unsafe keys are rejected."""
    if not _MODEL_KEY_RE.match(key):
        raise ValueError(f"models: key {key!r} must match {_MODEL_KEY_RE.pattern}")
    if bad := set(raw) - _MODEL_FIELDS:
        raise ValueError(f"models.{key}: unknown field(s) {sorted(bad)}")
    if base is None and not {"api_id", "dim"} <= set(raw):
        raise ValueError(f"models.{key}: a new model needs at least 'api_id' and 'dim'")
    fields = {**({f: getattr(base, f) for f in _MODEL_FIELDS} if base else {}), **raw}
    return ModelSpec(key=key, **fields)


QWEN_INSTRUCT = "Instruct: Given a question, retrieve the document clause that answers it\nQuery:{q}"

MODELS: dict[str, ModelSpec] = {
    # T1: text-only, cheap, 4096-d native; MRL-truncated to 1024.
    "t1": ModelSpec(
        key="t1",
        api_id="qwen/qwen3-embedding-8b",
        dim=1024,
        query_template=QWEN_INSTRUCT,
        price_text_per_m=0.01,
        batch_size=32,
        time_budget_s=20 * 60,
    ),
    # T2 / I2: multimodal (text + page images), free tier, flaky availability.
    "t2": ModelSpec(
        key="t2",
        api_id="nvidia/nemotron-3.5-embed-8b:free",
        dim=1024,
        supports_image=True,
        batch_size=8,
        time_budget_s=20 * 60,
    ),
    # I1: Google multimodal; text queries and images share a vector space.
    "i1": ModelSpec(
        key="i1",
        api_id="google/gemini-embedding-2",
        dim=1536,
        supports_text=True,
        supports_image=True,
        price_text_per_m=0.20,
        price_image_per_m=0.45,
        batch_size=8,
        time_budget_s=20 * 60,
    ),
}

#: Models whose *text chunks* are embedded and searched ("vec_<key>").
#: Only ``i1`` (google/gemini-embedding-2: text AND figures in one vector space) is active; ``t1`` stays in
#: ``MODELS`` (its vectors are still in the DB) but is disabled; ``t2`` is blocked by the account privacy setting.
TEXT_MODEL_KEYS = ["i1"]
#: Models whose *figure images* are embedded and searched ("figvec_<key>").
IMAGE_MODEL_KEYS = ["i1"]
DEFAULT_TEXT_MODEL = "i1"
DEFAULT_IMAGE_MODEL = "i1"


# --- retrieval defaults (overridable via the JSON config file, see load_settings) ------------
RRF_K = 60
W_FTS = 0.4
W_VEC = 0.6
FUSION = "wrrf"  # the only fusion method: weighted Reciprocal Rank Fusion
RERANK_CANDIDATES = 30
#: Reranking is off by default; a profile turns it on ("r3" = Jev/TypeSafe). If the reranker's key is missing or
#: it fails, ``search`` returns the fused order with ``rerank_error`` set. None | "r1" | "r2" | "r3".
DEFAULT_RERANKER: str | None = None

RERANK_URL = "https://openrouter.ai/api/v1/rerank"
RERANK_INSTRUCTION = (
    "Given a question, rank the document clauses that state the applicable requirement, value or condition highest"
)


@dataclass(frozen=True)
class RerankSpec:
    """One reranker backend."""

    key: str
    api_id: str
    backend: str  # "openrouter" | "jev"
    price_per_m: float = 0.0  # USD per million tokens
    max_attempts: int = 4


RERANKERS: dict[str, RerankSpec] = {
    "r1": RerankSpec("r1", "cohere/rerank-5-pro", "openrouter", 0.05),
    "r2": RerankSpec("r2", "qwen/qwen3-reranker-8b", "openrouter", 0.20),
    "r3": RerankSpec("r3", "typesafe/jev", "jev", 0.0),
}


_MONTH_RE = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}"
_TEMPLATE_FIELDS = {"doc_id", "title", "edition", "locator"}
_UNIT_KINDS = {"clause", "table", "figure", "appendix"}


def doc_label(doc_id: str) -> str:
    """A document named as the profile wants it in running text: ``ASC 3-26`` with ``"doc_label": "ASC {doc_id}"``,
    ``3-26`` by default (``doc_label``)."""
    return DOC_LABEL.format(doc_id=doc_id)


def month_regex(pattern: str) -> re.Pattern[str]:
    """Compile a profile regex in which ``{MONTH}`` stands for "<English month> <YYYY>"."""
    return re.compile(pattern.replace("{MONTH}", _MONTH_RE))


def _fields_of(name: str, template: str, allowed: set[str]) -> str:
    """Validate a ``str.format`` template: only ``allowed`` fields, no positional or nested fields."""
    import string

    try:
        used = {f for _t, f, _s, _c in string.Formatter().parse(template) if f is not None}
    except ValueError as e:
        raise ValueError(f"config {name}: {e}") from e
    if bad := used - allowed:
        raise ValueError(f"config {name}: unknown field(s) {sorted(bad)}; allowed {sorted(allowed)}")
    return template


def _read_config(path: Path, seen: tuple[Path, ...] = ()) -> dict:
    """JSON config with ``extends`` (a base file, relative to this one): local keys override the base."""
    import json

    path = path.resolve()
    if path in seen:
        raise ValueError(f"config: 'extends' loop through {path}")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if cfg.get("source_plugin"):  # relative to the file that names it, not to the one that extends it
        cfg["source_plugin"] = str(path.parent / cfg["source_plugin"])
    base = cfg.pop("extends", None)
    if not base:
        return cfg
    return {**_read_config(path.parent / base, (*seen, path)), **cfg}


def load_settings() -> None:
    """Apply overrides from a JSON config file (env ``VEDUN_CONFIG`` or ``<root>/vedun.config.json``).

    ``extends`` names a base file (relative path); local keys override it. Recognised keys: ``w_fts``, ``w_vec``,
    ``rrf_k``, ``rerank_candidates``, ``reranker``, ``rerank_instruction``, ``text_models``, ``image_models``,
    ``default_text_model``, ``default_image_model``, ``models`` (define or override embedding models:
    ``{"<key>": {"api_id", "dim", "base_url", "api_key_env", "query_template", prices…}}``), and the corpus
    profile: ``corpus_name``, ``doc_label``, ``doc_id_pattern``, ``citation_prefix_pattern``, ``citation_template``, ``locators``,
    ``edition_label``, ``title_strip_patterns``, ``changes_unit_id``, ``boilerplate_patterns``, ``clause_ref_words``,
    ``value_units``, ``fast_table_documents``, ``source_plugin``, ``mcp_tool_prefix``, ``citation_skip_dirs``, ``ui_language``.
    """
    global W_FTS, W_VEC, RRF_K, RERANK_CANDIDATES, DEFAULT_RERANKER, RERANK_INSTRUCTION, FAST_TABLE_DOCUMENTS
    global TEXT_MODEL_KEYS, IMAGE_MODEL_KEYS, DEFAULT_TEXT_MODEL, DEFAULT_IMAGE_MODEL
    global DOC_ID_PATTERN, CITATION_PREFIX_PATTERN, BOILERPLATE_PATTERNS, CLAUSE_REF_WORDS, VALUE_UNITS
    global CORPUS_NAME, DOC_LABEL, CITATION_TEMPLATE, LOCATORS, EDITION_LABEL, TITLE_STRIP_PATTERNS, CHANGES_UNIT_ID
    global CONFIG_PATH, SOURCE_PLUGIN, MCP_TOOL_PREFIX, CITATION_SKIP_DIRS, UI_LANGUAGE
    path = _env_path("CONFIG", ROOT / "vedun.config.json")
    if not path.exists():
        return
    cfg = _read_config(path)
    CONFIG_PATH = path
    W_FTS = float(cfg.get("w_fts", W_FTS))
    W_VEC = float(cfg.get("w_vec", W_VEC))
    RRF_K = int(cfg.get("rrf_k", RRF_K))
    RERANK_CANDIDATES = int(cfg.get("rerank_candidates", RERANK_CANDIDATES))
    DEFAULT_RERANKER = cfg.get("reranker", DEFAULT_RERANKER)
    RERANK_INSTRUCTION = str(cfg.get("rerank_instruction", RERANK_INSTRUCTION))
    TEXT_MODEL_KEYS = list(cfg.get("text_models", TEXT_MODEL_KEYS))
    IMAGE_MODEL_KEYS = list(cfg.get("image_models", IMAGE_MODEL_KEYS))
    DEFAULT_TEXT_MODEL = cfg.get("default_text_model", DEFAULT_TEXT_MODEL)
    DEFAULT_IMAGE_MODEL = cfg.get("default_image_model", DEFAULT_IMAGE_MODEL)
    FAST_TABLE_DOCUMENTS = frozenset(cfg.get("fast_table_documents", FAST_TABLE_DOCUMENTS))
    DOC_ID_PATTERN = _no_groups("doc_id_pattern", cfg.get("doc_id_pattern", DOC_ID_PATTERN))
    CITATION_PREFIX_PATTERN = _no_groups(
        "citation_prefix_pattern", cfg.get("citation_prefix_pattern", CITATION_PREFIX_PATTERN)
    )
    BOILERPLATE_PATTERNS = tuple(
        _no_groups("boilerplate_patterns", p) for p in cfg.get("boilerplate_patterns", BOILERPLATE_PATTERNS)
    )
    CLAUSE_REF_WORDS = tuple(cfg.get("clause_ref_words", CLAUSE_REF_WORDS))
    VALUE_UNITS = tuple(cfg.get("value_units", VALUE_UNITS))
    CORPUS_NAME = str(cfg.get("corpus_name", CORPUS_NAME))
    DOC_LABEL = _fields_of("doc_label", cfg.get("doc_label", DOC_LABEL), {"doc_id"})
    CITATION_TEMPLATE = {
        lang: _fields_of(f"citation_template.{lang}", t, _TEMPLATE_FIELDS)
        for lang, t in {**CITATION_TEMPLATE, **(cfg.get("citation_template") or {})}.items()
    }
    LOCATORS = {lang: dict(kinds) for lang, kinds in LOCATORS.items()}
    for lang, kinds in (cfg.get("locators") or {}).items():
        if bad := set(kinds) - _UNIT_KINDS:
            raise ValueError(f"config locators.{lang}: unknown unit kind(s) {sorted(bad)}")
        LOCATORS.setdefault(lang, {}).update(
            {k: _fields_of(f"locators.{lang}.{k}", v, {"id"}) for k, v in kinds.items()}
        )
    if "edition_label" in cfg:
        EDITION_LABEL = cfg["edition_label"]
        for k in ("original", "revision", "original_in_history"):
            if EDITION_LABEL and EDITION_LABEL.get(k):
                try:
                    month_regex(EDITION_LABEL[k])
                except re.error as e:
                    raise ValueError(f"config edition_label.{k}: invalid regex: {e}") from e
    TITLE_STRIP_PATTERNS = tuple(cfg.get("title_strip_patterns", TITLE_STRIP_PATTERNS))
    for t in TITLE_STRIP_PATTERNS:
        try:
            re.compile(t.replace("{doc_id}", "x"))
        except re.error as e:
            raise ValueError(f"config title_strip_patterns: invalid regex {t!r}: {e}") from e
    CHANGES_UNIT_ID = cfg.get("changes_unit_id", CHANGES_UNIT_ID)
    if cfg.get("source_plugin"):
        SOURCE_PLUGIN = Path(cfg["source_plugin"])
        if not SOURCE_PLUGIN.is_file():
            raise ValueError(f"config source_plugin: file not found: {SOURCE_PLUGIN}")
    UI_LANGUAGE = str(cfg.get("ui_language", UI_LANGUAGE))
    if UI_LANGUAGE not in UI_LANGUAGES:
        raise ValueError(f"config ui_language: {UI_LANGUAGE!r} is not one of {UI_LANGUAGES}")
    CITATION_SKIP_DIRS = tuple(str(d) for d in cfg.get("citation_skip_dirs", CITATION_SKIP_DIRS))
    MCP_TOOL_PREFIX = str(cfg.get("mcp_tool_prefix", MCP_TOOL_PREFIX))
    if not _MCP_PREFIX_RE.match(MCP_TOOL_PREFIX):
        raise ValueError(f"config mcp_tool_prefix: {MCP_TOOL_PREFIX!r} must match {_MCP_PREFIX_RE.pattern}")
    for key, raw in (cfg.get("models") or {}).items():
        MODELS[key] = _model_from_json(key, raw, MODELS.get(key))


load_settings()
