"""Removed/hidden designs in own collections, and next-sync info."""

from __future__ import annotations

from datetime import datetime, timedelta


def _remote(db, design_count, ids):
    db.replace_remote_collections(
        [
            {
                "collection_id": 5,
                "title": "Favorites",
                "slug": "f",
                "design_count": design_count,
                "is_default": True,
                "design_ids": ids,
            }
        ]
    )


def _have(db, *design_ids):
    for d in design_ids:
        db.insert_model(
            design_id=d,
            profile_id=None,
            title="T",
            slug="t",
            url="u",
            filename=f"{d}.3mf",
            file_path=f"/x/{d}.3mf",
            file_size=1,
        )


def test_hidden_designs_dont_block_all_downloaded(db):
    # MakerWorld says 5 designs, but only 3 are still listed.
    _remote(db, 5, [1, 2, 3])
    _have(db, 1, 2, 3)
    row = db.remote_collections()[0]
    assert (row["hidden_count"], row["available_count"]) == (2, 3)
    assert row["downloaded_count"] == 3
    assert row["downloaded"] is True


def test_partial_with_hidden(db):
    _remote(db, 5, [1, 2, 3])
    _have(db, 1)
    row = db.remote_collections()[0]
    assert row["downloaded"] is False
    assert (row["downloaded_count"], row["available_count"]) == (1, 3)


def test_no_ids_means_no_hidden_guess(db):
    _remote(db, 4, [])  # listing not paged (yet): don't claim 4 hidden
    row = db.remote_collections()[0]
    assert (row["hidden_count"], row["available_count"]) == (0, 4)


def test_capped_ids_not_treated_as_hidden(db):
    _remote(db, 1500, list(range(1, 1001)))  # pager cap reached
    assert db.remote_collections()[0]["hidden_count"] == 0


def test_collections_route_next_sync(app_client):
    import app.routes as routes

    client, database, _ = app_client
    manager = routes.manager  # the app's lifespan wires its own manager
    database.upsert_collection(10, "Synced", "u", 60)
    database.upsert_collection(11, "Never", "u", 60)
    database.record_sync(10, "ok", 0)
    rows = {c["collection_id"]: c for c in client.get("/api/collections").json()}

    last = datetime.fromisoformat(database.get_collection(10)["last_sync_at"])
    assert datetime.fromisoformat(rows[10]["next_sync_at"]) == last + timedelta(
        minutes=60
    )
    assert rows[11]["next_sync_at"] is None
    assert rows[10]["syncing"] is False

    manager._syncing.add(10)
    try:
        rows = {c["collection_id"]: c for c in client.get("/api/collections").json()}
    finally:
        manager._syncing.discard(10)
    assert rows[10]["syncing"] is True
