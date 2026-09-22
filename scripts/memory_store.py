#!/usr/bin/env python3
"""
memory_store.py — Shared LanceDB backend for the agent's long-term memory.

Every memory script (memory_ingest / memory_recall / memory_ask /
memory_inspect) talks to the store through this module. It owns:

  * the on-disk location and table schema
  * the fastembed embedding model (BAAI/bge-small-en-v1.5, 384-dim)
  * chunk -> row conversion, including the deterministic row id used for
    idempotent re-ingest
  * hybrid (vector + BM25 full-text) search and chronological timeline reads

Storage layout::

    /agent/memory/long_term_memory.lancedb/    <- lancedb.connect() URI
        memories.lance/                        <- the single table

This is a library, not a CLI. Both `lancedb` and `fastembed` are imported
defensively so the memory scripts stay importable (and unit-testable) on
runtimes where they are not installed.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pyarrow as pa
except ImportError:  # pragma: no cover - pyarrow ships with lancedb
    pa = None

try:
    import lancedb
except ImportError:
    lancedb = None

# fastembed is imported lazily inside _embedder() rather than at module scope:
# constructing it is cheap but importing onnxruntime is not, and the read-only
# paths (timeline, inspect) never need an embedding at all.
fastembed = None


# ── Location & model ────────────────────────────────────────────────────────

MEMORY = Path("/agent/memory")
DEFAULT_DB = MEMORY / "long_term_memory.lancedb"
TABLE_NAME = "memories"

# Local embedding model — ONNX via fastembed, no PyTorch, no network after the
# first download. Matches the model the previous backend used, so recall
# quality is unchanged. EMBED_DIM must agree with the model: memory_inspect
# verifies it against the actual vector column width and exits 2 on mismatch.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

# Must be identical on the index and on every query: LanceDB defaults queries to
# l2 while the ANN index is built with whatever is configured here, and a
# mismatch silently degrades ranking. bge vectors are unit-normalised, so cosine
# and l2 rank identically — cosine is chosen because its distance is bounded,
# which keeps the similarity conversion in _score_of well defined.
DISTANCE_TYPE = "cosine"

# Rows are written in batches of this size so peak memory during a rebuild is
# bounded by one batch of embeddings rather than the whole corpus.
BUILD_BATCH_SIZE = 50

# Below this row count LanceDB brute-forces the vector scan, which is both
# faster and more accurate than an approximate index. IVF_PQ also needs a
# few thousand rows to train a useful codebook.
VECTOR_INDEX_MIN_ROWS = 5000

# Rows appended after an index build are searchable immediately (LanceDB
# flat-scans the unindexed tail) but that scan slows down as the tail grows.
# Rebuild once this many rows sit outside the index.
REINDEX_THRESHOLD = 200

# LanceDB is copy-on-write: every write produces a new table version plus new
# files, and superseded files stay on disk until they are pruned. Left alone
# that dwarfs the actual data — 100 cycles of small appends measured at 306
# files / 1.0 MB for 100 rows.
#
# Compaction has two knobs and getting their relationship wrong is far worse
# than doing nothing at all. Measured over the same 100 cycles:
#
#   no maintenance                        306 files    1.0 MB
#   compact, retention > write cadence   2027 files   11.3 MB   <- 10x WORSE
#   compact, retention < interval          51 files    0.3 MB
#
# The middle row is the trap: compaction rewrites every fragment, but nothing
# older than the retention window can be pruned, so each pass leaves the
# previous copy behind. Compaction is only ever worth doing when it can also
# prune — hence the two constants below must satisfy
# COMPACT_RETENTION << COMPACT_MIN_INTERVAL.
#
# The primary trigger is write churn: each cycle-close costs one version (two
# when it replaces an existing row), so this fires roughly every 20 cycles,
# which is also LanceDB's own "every ~20 data-modification operations" advice.
COMPACT_VERSION_THRESHOLD = 20

# History kept at each compaction. This is a concurrency guard, not a feature:
# there is no time-travel use case here (the store is rebuildable from the
# source JSON), but a reader that resolved a version whose files then get
# pruned mid-query fails with "Not found". Readers take milliseconds, so two
# minutes is a ~10,000x margin. Setting this to 0 measured an 86% failure rate
# across 1000 concurrent reads — do not.
COMPACT_RETENTION = timedelta(minutes=2)

# Floor on the wall-clock spacing between compactions, tracked by a stamp file
# so it holds across the detached per-cycle flush processes. At normal heartbeat
# cadence 20 cycles take far longer than this, so the version threshold above is
# what actually fires; this only guards the pathological case of many writes in
# quick succession, where compacting inside the retention window would inflate
# the store instead of shrinking it.
COMPACT_MIN_INTERVAL = timedelta(minutes=30)

# bge-small truncates input at 512 tokens (~2 KB of English), so anything longer
# than this is embedded only up to the cut and the remainder is invisible to
# vector search. Long documents are split into overlapping pieces instead; the
# overlap keeps a sentence that straddles a boundary retrievable from both.
MAX_CHUNK_CHARS = 1500
CHUNK_OVERLAP_CHARS = 150


def _require_lancedb():
    """Fail fast with a clear error when lancedb is unavailable."""
    if lancedb is None or pa is None:
        print(
            "ERROR: lancedb not installed (not available on this runtime). "
            "Install with: uv sync  (or seed/install_memory_deps.sh)",
            file=sys.stderr,
        )
        sys.exit(1)


def _cache_dir():
    """Where fastembed keeps the downloaded ONNX weights.

    Pinned inside the agent tree on the container so the ~130 MB model
    survives across restarts; falls back to fastembed's own default
    (~/.cache) elsewhere so dev machines behave normally.
    """
    env = os.environ.get("FASTEMBED_CACHE_PATH")
    if env:
        return env
    agent_root = MEMORY.parent
    if agent_root.is_dir():
        return str(agent_root / ".cache" / "fastembed")
    return None


_EMBEDDER = None
# Guards construction only. cycle_start fans recall() out across a thread pool,
# so without this every worker in a cold process would load its own ~130 MB copy
# of the ONNX model.
_EMBEDDER_LOCK = threading.Lock()


def _embedder():
    """Return the process-wide fastembed model, constructing it on first use."""
    if _EMBEDDER is not None:
        return _EMBEDDER
    with _EMBEDDER_LOCK:
        return _build_embedder()


def _build_embedder():
    """Construct the embedder. Callers must hold _EMBEDDER_LOCK."""
    global _EMBEDDER, fastembed
    if _EMBEDDER is not None:
        return _EMBEDDER
    if fastembed is None:
        try:
            import fastembed as _fe

            fastembed = _fe
        except ImportError:
            print(
                "ERROR: fastembed not installed (not available on this runtime). "
                "Install with: uv sync  (or seed/install_memory_deps.sh)",
                file=sys.stderr,
            )
            sys.exit(1)
    kwargs = {}
    cache = _cache_dir()
    if cache:
        kwargs["cache_dir"] = cache
    _EMBEDDER = fastembed.TextEmbedding(model_name=EMBED_MODEL, **kwargs)
    return _EMBEDDER


def embed_texts(texts: list) -> list:
    """Embed documents. Returns one plain float list per input text.

    This and :func:`embed_query` are the two seams the test-suite
    monkeypatches, so no test ever triggers a model download.
    """
    if not texts:
        return []
    return [list(map(float, v)) for v in _embedder().embed(list(texts))]


def embed_query(text: str) -> list:
    """Embed a search query.

    bge models expect an instruction prefix on the query side but not on the
    document side; fastembed's ``query_embed`` applies it.
    """
    return [list(map(float, v)) for v in _embedder().query_embed([text])][0]


# ── Schema ──────────────────────────────────────────────────────────────────


def schema():
    """Arrow schema for the memories table.

    Columns that are filtered on (source / cycle / date / ts / label) are real
    columns so LanceDB's SQL ``where`` can use them. Everything else from the
    chunk's free-form metadata dict is kept as a JSON string, which avoids
    schema migrations every time a transformer adds a key.
    """
    _require_lancedb()
    return pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("title", pa.string()),
            pa.field("label", pa.string()),
            pa.field("text", pa.string()),
            pa.field("tags", pa.list_(pa.string())),
            pa.field("source", pa.string()),
            pa.field("cycle", pa.int32()),
            pa.field("date", pa.string()),
            pa.field("ts", pa.int64()),
            pa.field("metadata", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
        ]
    )


# ── Chunk -> row ────────────────────────────────────────────────────────────

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def to_unix(value) -> int:
    """Best-effort conversion of a chunk timestamp into unix seconds.

    Accepts ISO-8601 strings (with or without a timezone or a time part) and
    raw ints. Falls back to "now" so a record with an unparseable date still
    lands somewhere sensible on the timeline instead of at the epoch.
    """
    if isinstance(value, bool):
        value = None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass
        m = _ISO_DATE.match(text)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                return int(dt.timestamp())
            except ValueError:
                pass
    return int(datetime.now(timezone.utc).timestamp())


def _to_int(value):
    """Coerce a metadata value to int, or None when it isn't numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    return None


def row_id(chunk: dict) -> str:
    """Deterministic id for a chunk.

    Two ingests of the same source record produce the same id, which is what
    makes append idempotent: :func:`add_chunks` deletes matching ids before
    inserting. Derived from the identifying metadata plus the full text so an
    edited record is treated as new rather than silently shadowing the old one.
    """
    meta = chunk.get("metadata") or {}
    parts = [
        str(meta.get("source") or ""),
        str(meta.get("id") or ""),
        str(meta.get("cycle") or ""),
        str(chunk.get("title") or ""),
        str(chunk.get("text") or ""),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def chunk_to_row(chunk: dict, vector: list) -> dict:
    """Convert an ingest chunk plus its embedding into a table row."""
    meta = dict(chunk.get("metadata") or {})
    raw_date = meta.get("date") or ""
    return {
        "id": row_id(chunk),
        "title": str(chunk.get("title") or ""),
        "label": str(chunk.get("label") or ""),
        "text": str(chunk.get("text") or ""),
        "tags": [str(t) for t in (chunk.get("tags") or [])],
        "source": str(meta.get("source") or ""),
        "cycle": _to_int(meta.get("cycle")),
        "date": str(raw_date)[:10],
        "ts": to_unix(raw_date),
        "metadata": json.dumps(meta, ensure_ascii=False, sort_keys=True),
        "vector": vector,
    }


def split_text(
    text: str, max_chars: int = MAX_CHUNK_CHARS, overlap: int = CHUNK_OVERLAP_CHARS
) -> list:
    """Split long text into embedding-sized pieces, preferring paragraph breaks.

    Returns ``[text]`` unchanged when it already fits, so short records — which
    is every journal and inbox entry — are untouched.
    """
    text = text or ""
    if len(text) <= max_chars:
        return [text] if text else []

    pieces: list = []
    for para in text.split("\n\n"):
        if len(para) <= max_chars:
            if para.strip():
                pieces.append(para)
            continue
        # A single oversized paragraph still has to be cut; step by
        # max_chars - overlap so consecutive pieces share a tail.
        step = max(max_chars - overlap, 1)
        for start in range(0, len(para), step):
            window = para[start : start + max_chars]
            if window.strip():
                pieces.append(window)
            if start + max_chars >= len(para):
                break

    # Re-pack adjacent paragraphs so we don't emit a row per one-line paragraph.
    packed: list = []
    buffer = ""
    for piece in pieces:
        candidate = f"{buffer}\n\n{piece}" if buffer else piece
        if len(candidate) <= max_chars:
            buffer = candidate
        else:
            if buffer:
                packed.append(buffer)
            buffer = piece
    if buffer:
        packed.append(buffer)
    return packed


def rows_from_chunks(chunks: list) -> list:
    """Embed a batch of chunks and return table rows, in input order."""
    chunks = list(chunks)
    if not chunks:
        return []
    vectors = embed_texts([str(c.get("text") or "") for c in chunks])
    return [chunk_to_row(c, v) for c, v in zip(chunks, vectors)]


def row_to_dict(row: dict) -> dict:
    """Normalise a raw LanceDB row into the shape the CLIs print.

    Decodes the JSON metadata column back into a dict and drops LanceDB's
    internal score columns after copying the relevant one into ``score``.
    """
    out = {
        "id": row.get("id") or "",
        "title": row.get("title") or "",
        "label": row.get("label") or "",
        "text": row.get("text") or "",
        "tags": list(row.get("tags") or []),
        "source": row.get("source") or "",
        "cycle": row.get("cycle"),
        "date": row.get("date") or "",
        "ts": row.get("ts"),
    }
    try:
        out["metadata"] = json.loads(row.get("metadata") or "{}")
    except (TypeError, ValueError):
        out["metadata"] = {}
    return out


# ── Connect / open / index ──────────────────────────────────────────────────


def connect(db_path):
    """Open (creating if needed) the LanceDB database directory."""
    _require_lancedb()
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(path))


def create_table(db_path):
    """Create an empty memories table, replacing any existing one."""
    db = connect(db_path)
    return db.create_table(TABLE_NAME, schema=schema(), mode="overwrite")


def open_table(db_path):
    """Open the memories table for read or write.

    Returns None only when the store genuinely isn't there yet, so callers can
    treat that as "run --build first" or a benign no-op. Any other failure
    (corrupt manifest, permissions, lock contention) propagates: silently
    reporting those as an empty memory would let a cycle discard its journal
    entry while printing a success marker.
    """
    _require_lancedb()
    path = Path(db_path)
    if not path.exists():
        return None
    db = lancedb.connect(str(path))
    try:
        return db.open_table(TABLE_NAME)
    except (ValueError, FileNotFoundError):
        # LanceDB raises ValueError("Table 'x' was not found") for a database
        # directory that exists but holds no such table.
        return None


def has_fts_index(tbl) -> bool:
    """True when the full-text index over `text` exists."""
    try:
        return any(getattr(i, "index_type", "") == "FTS" for i in tbl.list_indices())
    except Exception:
        return False


def ensure_indexes(tbl, quiet: bool = True) -> None:
    """Create/refresh the search indexes. Safe to call repeatedly.

    The FTS index is always built — hybrid search cannot run without it. The
    vector index is only built once the table is large enough to benefit; below
    :data:`VECTOR_INDEX_MIN_ROWS` LanceDB's exhaustive scan is both faster and
    exact.
    """
    from lancedb.index import FTS, IvfPq

    try:
        tbl.create_index("text", config=FTS(), replace=True)
    except Exception as e:
        if not quiet:
            print(f"WARN: FTS index build failed: {e}", file=sys.stderr)

    try:
        rows = tbl.count_rows()
    except Exception:
        rows = 0
    if rows >= VECTOR_INDEX_MIN_ROWS:
        try:
            tbl.create_index(
                "vector", config=IvfPq(distance_type=DISTANCE_TYPE), replace=True
            )
        except Exception as e:
            if not quiet:
                print(f"WARN: vector index build failed: {e}", file=sys.stderr)


def unindexed_rows(tbl) -> int:
    """How many rows the full-text index has not absorbed yet."""
    try:
        for idx in tbl.list_indices():
            if getattr(idx, "index_type", "") == "FTS":
                stats = tbl.index_stats(idx.name)
                return int(getattr(stats, "num_unindexed_rows", 0) or 0)
    except Exception:
        pass
    return 0


def version_count(tbl) -> int:
    """Retained table versions — a direct measure of un-reclaimed write churn."""
    try:
        return len(tbl.list_versions())
    except Exception:
        return 0


def compact(tbl, *, retention=None, quiet: bool = True) -> bool:
    """Merge small fragments and physically drop superseded versions.

    ``retention`` must be passed through to ``optimize``: its default is 7 days,
    which reclaims nothing for a store written a few rows at a time — and a
    compaction that cannot prune actively inflates the store. Defaults to
    :data:`COMPACT_RETENTION`, read at call time so it stays overridable.
    """
    if retention is None:
        retention = COMPACT_RETENTION
    try:
        tbl.optimize(cleanup_older_than=retention)
        _touch_compact_stamp(tbl)
        return True
    except Exception as e:
        if not quiet:
            print(f"WARN: compaction failed: {e}", file=sys.stderr)
        return False


def compact_stamp_path(db_path) -> Path:
    """Marker file recording when a store was last compacted.

    Kept as a hidden sibling of the database directory rather than inside it,
    so a ``--build`` swap (which replaces the directory wholesale) doesn't take
    the history with it.
    """
    db = Path(db_path)
    return db.parent / f".{db.name}.compact"


def _compact_stamp(tbl) -> Path | None:
    """Stamp path for an open table, or None if it isn't local."""
    uri = getattr(tbl, "uri", None)
    if not uri or "://" in str(uri):
        return None  # remote/object-store table: no local stamp to keep
    # tbl.uri points at <db>/<table>.lance; the store is its parent.
    return compact_stamp_path(Path(str(uri)).parent)


def clear_compact_stamp(db_path) -> None:
    """Drop a store's compaction stamp. Best-effort."""
    try:
        compact_stamp_path(db_path).unlink(missing_ok=True)
    except OSError:
        pass


def mark_compacted(db_path) -> None:
    """Record a compaction against a store path. Best-effort.

    A missing stamp only means the next write compacts sooner than necessary,
    so failures here are not worth surfacing.
    """
    try:
        stamp = compact_stamp_path(db_path)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    except OSError:
        pass


def _touch_compact_stamp(tbl) -> None:
    stamp = _compact_stamp(tbl)
    if stamp is not None:
        mark_compacted(Path(str(getattr(tbl, "uri"))).parent)


def _compaction_is_due(tbl) -> bool:
    """True when enough wall-clock time has passed since the last compaction.

    Rate-limiting is what makes pruning effective: each pass can only reclaim
    files older than COMPACT_RETENTION, so passes have to be spaced further
    apart than that to have anything to reclaim.
    """
    stamp = _compact_stamp(tbl)
    if stamp is None:
        return True
    try:
        last = datetime.fromtimestamp(stamp.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return True  # never compacted
    return datetime.now(timezone.utc) - last >= COMPACT_MIN_INTERVAL


def maintain(tbl, *, quiet: bool = True) -> bool:
    """Amortised upkeep after a write: reindex the tail, reclaim dead files.

    Two independent concerns, measured separately:

    * unindexed rows — rows appended after the last index build are still
      searchable (LanceDB flat-scans the tail) but slow the query down as they
      accumulate;
    * write churn — copy-on-write leaves superseded files on disk, and that is
      what actually makes the store balloon.

    Compaction is additionally rate-limited by wall clock, because running it
    too often makes the store *bigger*: nothing inside the retention window can
    be pruned, so a pass that runs before the window has elapsed just rewrites
    every fragment and keeps both copies.

    Returns True when anything ran.
    """
    ran = False
    if unindexed_rows(tbl) >= REINDEX_THRESHOLD:
        ensure_indexes(tbl, quiet=quiet)
        ran = True
    if version_count(tbl) >= COMPACT_VERSION_THRESHOLD and _compaction_is_due(tbl):
        compact(tbl, quiet=quiet)
        ran = True
    return ran


# ── Writes ──────────────────────────────────────────────────────────────────


def _id_in_clause(ids) -> str:
    """SQL predicate matching a set of row ids.

    Row ids are 32 hex characters by construction, so they need no escaping.
    """
    return "id IN ({})".format(", ".join(f"'{i}'" for i in sorted(ids)))


def _existing_ids(tbl, ids) -> set:
    """Which of `ids` are already stored. Falls back to "assume all" on error,
    so a probe failure degrades into the old unconditional-delete behaviour
    rather than risking a duplicate row.
    """
    ids = set(ids)
    if not ids:
        return set()
    try:
        rows = (
            tbl.search()
            .select(["id"])
            .where(_id_in_clause(ids))
            .limit(len(ids))
            .to_list()
        )
    except Exception:
        return ids
    return {r["id"] for r in rows}


def add_chunks(tbl, chunks: list, *, quiet: bool = True, dedup: bool = True) -> tuple:
    """Embed and insert chunks, replacing any rows with the same id.

    Returns ``(ok, fail)``. The delete-then-add makes re-ingesting the same
    source record a no-op rather than a duplicate, so callers no longer have
    to reason about whether a record was already flushed.

    ``dedup=False`` skips the delete for callers that already know the table
    cannot contain these ids (the rebuild path, which starts from an empty
    staging table and tracks seen ids itself) — the delete is a full scan, so
    skipping it keeps a rebuild linear in the number of chunks.
    """
    chunks = [c for c in (chunks or []) if c]
    if not chunks:
        return (0, 0)
    try:
        rows = rows_from_chunks(chunks)
    except SystemExit:
        raise
    except Exception as e:
        if not quiet:
            print(f"WARN: embedding failed: {e}", file=sys.stderr)
        return (0, len(chunks))

    # Guard against the same chunk appearing twice within one batch, which
    # would reintroduce the duplicate the delete below is meant to remove.
    seen = set()
    unique = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        unique.append(r)

    if dedup:
        # Only issue the delete when something would actually be replaced.
        # An unconditional delete is a write: it bumps the table version and
        # leaves a deletion file behind even when it matches nothing, which
        # doubles the version churn on the common all-new-rows path.
        colliding = _existing_ids(tbl, seen)
        if colliding:
            try:
                tbl.delete(_id_in_clause(colliding))
            except Exception as e:
                if not quiet:
                    print(f"WARN: dedup delete failed: {e}", file=sys.stderr)

    try:
        tbl.add(unique)
    except Exception as e:
        if not quiet:
            print(f"WARN: insert failed: {e}", file=sys.stderr)
        return (0, len(unique))
    return (len(unique), 0)


# ── Reads ───────────────────────────────────────────────────────────────────


def ts_clause(since=None, until=None) -> str:
    """Build the SQL predicate for a `ts` range, or "" when unbounded."""
    parts = []
    if since is not None:
        parts.append(f"ts >= {int(since)}")
    if until is not None:
        parts.append(f"ts <= {int(until)}")
    return " AND ".join(parts)


def _score_of(row: dict):
    """Pull whichever relevance column the query type produced.

    Hybrid gives ``_relevance_score`` (higher is better), pure FTS gives
    ``_score`` (higher is better) and pure vector gives ``_distance`` (lower is
    better) — normalise all three to "higher is better", non-negative.

    Cosine distance runs 0..2, so the naive ``1 - distance`` goes negative for
    anything less similar than orthogonal. Clamping matters: a negative top
    score would make every relative score in :func:`search` collapse to 0 and
    silently drop the whole result set through the ``min_score`` filter.
    """
    for key in ("_relevance_score", "_score"):
        if row.get(key) is not None:
            return max(0.0, float(row[key]))
    if row.get("_distance") is not None:
        return max(0.0, 1.0 - float(row["_distance"]))
    return 0.0


def search(tbl, query: str, k: int = 5, since=None, until=None, min_score: float = 0.0):
    """Hybrid (vector + BM25) search over the memories table.

    ``min_score`` is applied *relative to the best hit* rather than on an
    absolute scale: hybrid results are reciprocal-rank-fusion scores whose
    magnitude depends on the result-set size, so an absolute cut-off would
    behave unpredictably. 0.0 disables the filter and returns the top ``k``.

    Returns a list of dicts from :func:`row_to_dict`, each with a ``score``
    key normalised to 0..1, ordered best-first.
    """
    query = (query or "").strip()
    if not query:
        return []

    clause = ts_clause(since, until)
    vector = embed_query(query)

    def _run(hybrid: bool):
        if hybrid:
            q = tbl.search(query_type="hybrid").vector(vector).text(query)
        else:
            q = tbl.search(vector)
        # Pin the metric explicitly: LanceDB defaults queries to l2 regardless
        # of what the ANN index was built with, so leaving it unset would make
        # the query and the index disagree once the table is large enough to
        # have one.
        q = q.distance_type(DISTANCE_TYPE)
        if clause:
            q = q.where(clause, prefilter=True)
        return q.limit(k).to_list()

    try:
        raw = _run(hybrid=True)
    except Exception:
        # No FTS index yet (table built but never sealed) — vector-only still
        # returns useful results, so degrade instead of failing the query.
        raw = _run(hybrid=False)

    scored = [(row, _score_of(row)) for row in raw]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    top = scored[0][1] if scored else 0.0

    results = []
    for row, score in scored:
        relative = (score / top) if top > 0 else 0.0
        if min_score > 0 and relative < min_score:
            continue
        item = row_to_dict(row)
        item["score"] = round(relative, 4)
        results.append(item)
    return results


TIMELINE_COLUMNS = [
    "id",
    "title",
    "label",
    "text",
    "tags",
    "source",
    "date",
    "ts",
    "metadata",
]


def timeline(tbl, limit: int = 20, since=None, until=None):
    """Return rows newest-first, optionally bounded by a `ts` range.

    The ordering has to happen inside the query: an unordered LanceDB scan
    returns rows in storage order, so applying ``limit`` first would page in
    the *oldest* rows and then sort only those.

    One query returns everything the caller needs — unlike the previous
    backend, timeline rows already carry title and tags, so there is no
    per-entry lookup.
    """
    q = tbl.search().select(TIMELINE_COLUMNS)
    clause = ts_clause(since, until)
    if clause:
        q = q.where(clause)
    q = q.order_by([{"column_name": "ts", "ascending": False}])
    rows = q.limit(max(int(limit), 1)).to_list()
    return [row_to_dict(r) for r in rows]


# ── Rebuild support ─────────────────────────────────────────────────────────


def staging_paths(db_path):
    """Return the (staging, backup) sibling directories used by --build."""
    path = Path(db_path)
    return (
        path.with_name(path.name + ".rebuild"),
        path.with_name(path.name + ".backup"),
    )


def promote_staging(db_path) -> None:
    """Swap a freshly-built staging database into the canonical location.

    The canonical database stays open and queryable by other processes for the
    whole rebuild; only these two renames make it unavailable, and only for the
    instant between them. Directory renames are atomic on POSIX as long as the
    destination does not exist, hence the rmtree of any previous backup.
    """
    path = Path(db_path)
    staging, backup = staging_paths(path)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if path.exists():
        os.replace(path, backup)
    os.replace(staging, path)
    # The staging store is gone; don't leave its stamp behind as litter.
    clear_compact_stamp(staging)


def dir_size(path) -> int:
    """Total bytes under a path — LanceDB stores a directory, not a file."""
    p = Path(path)
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def vector_dimension(tbl):
    """Actual width of the stored vector column, or None if unavailable.

    memory_inspect compares this against EMBED_DIM: a mismatch means the table
    was built with a different embedding model and every query is meaningless
    until it is rebuilt.
    """
    try:
        field = tbl.schema.field("vector")
        return int(field.type.list_size)
    except Exception:
        return None
