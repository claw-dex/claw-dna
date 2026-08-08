"""Tests for scripts/memory_store.py — the LanceDB backend shared by every
memory script."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

# These suites exercise a real LanceDB store. uv only resolves lancedb for
# the Linux container, so on a dev machine the dependency is simply absent.
pytest.importorskip("lancedb")

import memory_store as store


@pytest.fixture
def db(tmp_path):
    return tmp_path / "long_term_memory.lancedb"


def _chunk(title="Cycle 1: fixed the scheduler", text="repair the cron parser", **meta):
    metadata = {"source": "journal", "cycle": "1", "date": "2026-01-01T10:00:00Z"}
    metadata.update(meta)
    return {
        "title": title,
        "label": "evolve",
        "text": text,
        "tags": ["journal", "cycle:1"],
        "metadata": metadata,
    }


# ── Timestamp coercion ──────────────────────────────────────────────────────


def test_to_unix_iso_with_offset():
    assert store.to_unix("2026-01-01T00:00:00+00:00") == 1767225600


def test_to_unix_iso_with_z():
    assert store.to_unix("2026-01-01T00:00:00Z") == 1767225600


def test_to_unix_naive_is_treated_as_utc():
    assert store.to_unix("2026-01-01T00:00:00") == 1767225600


def test_to_unix_date_only():
    assert store.to_unix("2026-01-01") == 1767225600


def test_to_unix_passes_through_ints():
    assert store.to_unix(1700000000) == 1700000000


def test_to_unix_unparseable_falls_back_to_now():
    # Unparseable dates land at "now" rather than the epoch, so a bad record
    # doesn't silently sort to the bottom of every timeline.
    assert store.to_unix("not a date") > 1_700_000_000
    assert store.to_unix("") > 1_700_000_000
    assert store.to_unix(None) > 1_700_000_000


# ── Row construction ────────────────────────────────────────────────────────


def test_row_id_is_deterministic():
    assert store.row_id(_chunk()) == store.row_id(_chunk())


def test_row_id_changes_with_text():
    assert store.row_id(_chunk(text="a")) != store.row_id(_chunk(text="b"))


def test_row_id_changes_with_source():
    assert store.row_id(_chunk()) != store.row_id(_chunk(source="inbox"))


def test_chunk_to_row_promotes_filter_columns():
    row = store.chunk_to_row(_chunk(), [0.0] * store.EMBED_DIM)
    assert row["source"] == "journal"
    assert row["cycle"] == 1
    assert row["date"] == "2026-01-01"
    assert row["ts"] == 1767261600
    assert json.loads(row["metadata"])["cycle"] == "1"


def test_chunk_to_row_handles_missing_cycle():
    row = store.chunk_to_row(
        {"title": "t", "text": "x", "metadata": {"source": "inbox"}},
        [0.0] * store.EMBED_DIM,
    )
    assert row["cycle"] is None
    assert row["date"] == ""


def test_chunk_to_row_rejects_bool_cycle():
    row = store.chunk_to_row(
        {"title": "t", "text": "x", "metadata": {"cycle": True}},
        [0.0] * store.EMBED_DIM,
    )
    assert row["cycle"] is None


def test_row_to_dict_survives_bad_metadata():
    out = store.row_to_dict({"id": "a", "metadata": "{not json"})
    assert out["metadata"] == {}


# ── Predicates ──────────────────────────────────────────────────────────────


def test_ts_clause_unbounded():
    assert store.ts_clause() == ""


def test_ts_clause_both_bounds():
    assert store.ts_clause(1, 2) == "ts >= 1 AND ts <= 2"


def test_ts_clause_single_bound():
    assert store.ts_clause(since=5) == "ts >= 5"
    assert store.ts_clause(until=9) == "ts <= 9"


# ── Table lifecycle ─────────────────────────────────────────────────────────


def test_open_table_missing_returns_none(db):
    assert store.open_table(db) is None


def test_open_table_without_table_returns_none(db, stub_embeddings):
    store.connect(db)  # database directory exists but holds no table
    assert store.open_table(db) is None


def test_create_table_schema_matches_embed_dim(db, stub_embeddings):
    tbl = store.create_table(db)
    assert store.vector_dimension(tbl) == store.EMBED_DIM


def test_open_table_propagates_unexpected_errors(db, stub_embeddings, monkeypatch):
    """A store that exists but won't open must raise, not look empty.

    Returning None there would let cycle_close report a successful flush while
    discarding the cycle's memories.
    """
    store.create_table(db)

    def _boom(_uri):
        raise PermissionError("locked")

    monkeypatch.setattr(store.lancedb, "connect", _boom)
    with pytest.raises(PermissionError):
        store.open_table(db)


# ── Writes ──────────────────────────────────────────────────────────────────


def test_add_chunks_empty_is_noop(db, stub_embeddings):
    tbl = store.create_table(db)
    assert store.add_chunks(tbl, []) == (0, 0)
    assert store.add_chunks(tbl, None) == (0, 0)


def test_add_chunks_writes_rows(db, stub_embeddings):
    tbl = store.create_table(db)
    assert store.add_chunks(
        tbl, [_chunk(), _chunk(title="Other", text="other body")]
    ) == (2, 0)
    assert tbl.count_rows() == 2


def test_add_chunks_replaces_same_id(db, stub_embeddings):
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    store.add_chunks(tbl, [_chunk()])
    assert tbl.count_rows() == 1


def test_add_chunks_dedupes_within_batch(db, stub_embeddings):
    tbl = store.create_table(db)
    assert store.add_chunks(tbl, [_chunk(), _chunk()]) == (1, 0)
    assert tbl.count_rows() == 1


def test_add_chunks_reports_embedding_failure(db, stub_embeddings, monkeypatch):
    tbl = store.create_table(db)

    def _boom(_texts):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(store, "embed_texts", _boom)
    assert store.add_chunks(tbl, [_chunk()]) == (0, 1)
    assert tbl.count_rows() == 0


# ── Indexes ─────────────────────────────────────────────────────────────────


def test_ensure_indexes_builds_fts(db, stub_embeddings):
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    assert store.has_fts_index(tbl) is False
    store.ensure_indexes(tbl)
    assert store.has_fts_index(tbl) is True


def test_ensure_indexes_skips_vector_index_when_small(db, stub_embeddings):
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    store.ensure_indexes(tbl)
    types = {i.index_type for i in tbl.list_indices()}
    assert types == {"FTS"}


def test_maintain_below_thresholds_is_noop(db, stub_embeddings):
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    store.ensure_indexes(tbl)
    store.add_chunks(tbl, [_chunk(title="Second", text="second body")])
    assert store.unindexed_rows(tbl) == 1
    assert store.maintain(tbl) is False


def test_maintain_absorbs_unindexed_tail(db, stub_embeddings, monkeypatch):
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    store.ensure_indexes(tbl)
    store.add_chunks(tbl, [_chunk(title="Second", text="second body")])
    monkeypatch.setattr(store, "REINDEX_THRESHOLD", 1)
    assert store.maintain(tbl) is True
    assert store.unindexed_rows(tbl) == 0


def test_maintain_compacts_once_churn_accumulates(db, stub_embeddings, monkeypatch):
    tbl = store.create_table(db)
    for i in range(6):
        store.add_chunks(tbl, [_chunk(title=f"C{i}", text=f"body number {i}")])
    monkeypatch.setattr(store, "COMPACT_VERSION_THRESHOLD", 3)
    monkeypatch.setattr(store, "COMPACT_RETENTION", timedelta(0))
    before = store.version_count(tbl)
    assert store.maintain(tbl) is True
    assert store.version_count(tbl) < before
    assert tbl.count_rows() == 6


def test_compaction_is_rate_limited(db, stub_embeddings, monkeypatch):
    """A second compaction must not run until the interval has elapsed.

    This is the guard that keeps compaction from making the store *bigger*:
    a pass that runs inside the retention window can't prune anything, so it
    just rewrites every fragment and keeps both copies.
    """
    tbl = store.create_table(db)
    for i in range(6):
        store.add_chunks(tbl, [_chunk(title=f"C{i}", text=f"body number {i}")])
    monkeypatch.setattr(store, "COMPACT_VERSION_THRESHOLD", 1)
    monkeypatch.setattr(store, "COMPACT_RETENTION", timedelta(0))
    monkeypatch.setattr(store, "COMPACT_MIN_INTERVAL", timedelta(hours=6))

    assert store._compaction_is_due(tbl) is True
    store.maintain(tbl)
    assert store._compaction_is_due(tbl) is False

    monkeypatch.setattr(store, "COMPACT_MIN_INTERVAL", timedelta(0))
    assert store._compaction_is_due(tbl) is True


def test_compact_stamp_survives_a_rebuild_swap(db, stub_embeddings):
    """The stamp is a sibling of the store, not inside it.

    --build replaces the store directory wholesale; a stamp kept inside would
    vanish with it and the next write would compact a store that has nothing
    to reclaim.
    """
    stamp = store.compact_stamp_path(db)
    assert stamp.parent == db.parent
    assert not str(stamp).startswith(str(db) + "/")

    staging, _backup = store.staging_paths(db)
    store.create_table(db)
    store.create_table(staging)
    store.mark_compacted(db)
    assert stamp.exists()
    store.promote_staging(db)
    assert stamp.exists()


def test_compact_reclaims_disk(db, stub_embeddings):
    tbl = store.create_table(db)
    for i in range(15):
        store.add_chunks(tbl, [_chunk(title=f"C{i}", text=f"body number {i}")])
    before_files = sum(len(f) for _, _, f in os.walk(db))
    assert store.compact(tbl, retention=timedelta(0)) is True
    after_files = sum(len(f) for _, _, f in os.walk(db))
    assert after_files < before_files
    assert store.version_count(store.open_table(db)) == 1
    assert store.open_table(db).count_rows() == 15


def test_add_chunks_skips_the_delete_when_nothing_collides(db, stub_embeddings):
    """An unconditional delete is a write — it bumps the version and leaves a
    deletion file even when it matches nothing."""
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    v_after_first = tbl.version

    store.add_chunks(tbl, [_chunk(title="Fresh", text="entirely new body")])
    fresh_cost = tbl.version - v_after_first

    v = tbl.version
    store.add_chunks(tbl, [_chunk()])  # collides with row one
    colliding_cost = tbl.version - v

    assert fresh_cost < colliding_cost


# ── Search ──────────────────────────────────────────────────────────────────


@pytest.fixture
def populated(db, stub_embeddings):
    tbl = store.create_table(db)
    store.add_chunks(
        tbl,
        [
            _chunk(),
            {
                "title": "Inbox message",
                "label": "inbox",
                "text": "please restart the portal when you get a chance",
                "tags": ["inbox"],
                "metadata": {
                    "source": "inbox",
                    "id": "m1",
                    "date": "2026-01-03T09:00:00Z",
                },
            },
        ],
    )
    store.ensure_indexes(tbl)
    return tbl


def test_search_empty_query_returns_nothing(populated):
    assert store.search(populated, "") == []
    assert store.search(populated, "   ") == []


def test_search_top_hit_scores_one(populated):
    results = store.search(populated, "restart the portal", k=5)
    assert results
    assert results[0]["score"] == pytest.approx(1.0)
    assert "portal" in results[0]["text"]


def test_search_scores_are_descending(populated):
    results = store.search(populated, "portal scheduler", k=5)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_search_respects_k(populated):
    assert len(store.search(populated, "portal scheduler", k=1)) == 1


def test_search_min_score_filters(populated):
    assert len(store.search(populated, "portal scheduler", k=5, min_score=1.0)) == 1


def test_search_since_until_filter(populated):
    late = store.search(
        populated, "portal scheduler", k=5, since=store.to_unix("2026-01-02")
    )
    assert all(r["source"] == "inbox" for r in late)
    early = store.search(
        populated, "portal scheduler", k=5, until=store.to_unix("2026-01-02")
    )
    assert all(r["source"] == "journal" for r in early)


def test_search_decodes_metadata(populated):
    results = store.search(populated, "cron parser", k=1)
    assert results[0]["metadata"]["source"] == "journal"


def test_search_without_fts_index_falls_back(db, stub_embeddings):
    tbl = store.create_table(db)
    store.add_chunks(tbl, [_chunk()])
    assert store.has_fts_index(tbl) is False
    assert store.search(tbl, "cron parser", k=5)


# ── Timeline ────────────────────────────────────────────────────────────────


def test_timeline_is_newest_first(populated):
    rows = store.timeline(populated, limit=10)
    stamps = [r["ts"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


def test_timeline_since_filters(populated):
    rows = store.timeline(populated, limit=10, since=store.to_unix("2026-01-02"))
    assert len(rows) == 1
    assert rows[0]["source"] == "inbox"


def test_timeline_carries_title_and_tags(populated):
    rows = store.timeline(populated, limit=10)
    assert all(r["title"] for r in rows)
    assert any(r["tags"] for r in rows)


def test_timeline_limit_is_at_least_one(populated):
    assert len(store.timeline(populated, limit=0)) == 1


# ── Rebuild helpers ─────────────────────────────────────────────────────────


def test_staging_paths_are_siblings(db):
    staging, backup = store.staging_paths(db)
    assert staging.name == db.name + ".rebuild"
    assert backup.name == db.name + ".backup"
    assert staging.parent == db.parent


def test_promote_staging_swaps_and_backs_up(db, stub_embeddings):
    staging, backup = store.staging_paths(db)
    store.add_chunks(store.create_table(db), [_chunk(text="old content")])
    store.add_chunks(store.create_table(staging), [_chunk(text="new content")])

    store.promote_staging(db)

    assert not staging.exists()
    assert backup.is_dir()
    rows = store.open_table(db).search().limit(5).to_list()
    assert rows[0]["text"] == "new content"


def test_promote_staging_replaces_existing_backup(db, stub_embeddings):
    staging, backup = store.staging_paths(db)
    backup.mkdir(parents=True)
    (backup / "OLD_BACKUP").write_text("junk")
    store.create_table(db)
    store.create_table(staging)

    store.promote_staging(db)

    assert not (backup / "OLD_BACKUP").exists()


def test_promote_staging_first_run_has_no_backup(db, stub_embeddings):
    staging, backup = store.staging_paths(db)
    store.create_table(staging)
    store.promote_staging(db)
    assert db.is_dir()
    assert not backup.exists()


def test_dir_size_walks_recursively(tmp_path):
    root = tmp_path / "store"
    (root / "nested").mkdir(parents=True)
    (root / "a").write_text("x" * 10)
    (root / "nested" / "b").write_text("y" * 5)
    assert store.dir_size(root) == 15


def test_dir_size_of_missing_path_is_zero(tmp_path):
    assert store.dir_size(tmp_path / "nope") == 0


def test_dir_size_of_file(tmp_path):
    f = tmp_path / "f"
    f.write_text("abcd")
    assert store.dir_size(f) == 4


# ── Regression coverage for ordering and scoring ────────────────────────────


def test_timeline_pages_the_newest_not_the_first_written(db, stub_embeddings):
    """`limit` must be applied after ordering, not before.

    Rows are inserted oldest-first, so an unordered scan would return the
    oldest k. Asking for fewer rows than exist is the only way this shows up.
    """
    tbl = store.create_table(db)
    for day in range(1, 13):
        store.add_chunks(
            tbl,
            [
                {
                    "title": f"Cycle {day}",
                    "label": "evolve",
                    "text": f"work performed on day {day}",
                    "tags": [f"cycle:{day}"],
                    "metadata": {"source": "journal", "date": f"2026-01-{day:02d}"},
                }
            ],
        )

    rows = store.timeline(tbl, limit=3)
    assert [r["date"] for r in rows] == ["2026-01-12", "2026-01-11", "2026-01-10"]


def test_timeline_since_still_returns_newest_page(db, stub_embeddings):
    tbl = store.create_table(db)
    for day in range(1, 13):
        store.add_chunks(
            tbl,
            [
                {
                    "title": f"Cycle {day}",
                    "label": "evolve",
                    "text": f"work performed on day {day}",
                    "tags": [],
                    "metadata": {"source": "journal", "date": f"2026-01-{day:02d}"},
                }
            ],
        )
    rows = store.timeline(tbl, limit=2, since=store.to_unix("2026-01-05"))
    assert [r["date"] for r in rows] == ["2026-01-12", "2026-01-11"]


def test_score_of_clamps_distant_vectors():
    """A far-away vector must score 0, not a negative number.

    Cosine distance runs 0..2, so an unclamped `1 - distance` would go negative
    and make the top-relative normalisation in search() collapse everything to
    zero — silently emptying every result set that has a min_score filter.
    """
    assert store._score_of({"_distance": 1.9}) == 0.0
    assert store._score_of({"_distance": 0.0}) == 1.0
    assert store._score_of({"_score": -3.0}) == 0.0


def test_score_of_prefers_relevance_over_distance():
    assert store._score_of({"_relevance_score": 0.4, "_distance": 0.1}) == 0.4


# ── Text splitting ──────────────────────────────────────────────────────────


def test_split_text_leaves_short_text_alone():
    assert store.split_text("short enough") == ["short enough"]


def test_split_text_empty():
    assert store.split_text("") == []


def test_split_text_respects_max_chars():
    text = "\n\n".join("para " + "x" * 400 for _ in range(20))
    pieces = store.split_text(text, max_chars=1000, overlap=100)
    assert len(pieces) > 1
    assert all(len(p) <= 1000 for p in pieces)


def test_split_text_splits_one_huge_paragraph():
    pieces = store.split_text("y" * 5000, max_chars=1000, overlap=100)
    assert len(pieces) > 1
    assert all(len(p) <= 1000 for p in pieces)
    # No content is dropped: the pieces overlap, so their total exceeds the input.
    assert sum(len(p) for p in pieces) >= 5000


def test_split_text_keeps_all_content():
    text = "\n\n".join(f"paragraph number {i} with some body text" for i in range(50))
    pieces = store.split_text(text, max_chars=200, overlap=20)
    joined = " ".join(pieces)
    assert "paragraph number 0 " in joined
    assert "paragraph number 49" in joined
