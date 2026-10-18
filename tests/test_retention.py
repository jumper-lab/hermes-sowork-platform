"""Real SQLite cleanup: replay barriers, unresolved work and upgrade-in-place."""

import pytest

from sowork.state import HISTORY_SECONDS, Store


def add(store, mid, status="complete", stamp=1):
    raw = {"id": "event-" + mid, "data": {"message": {"id": mid}}, "createdAt": stamp}
    store.ingest("cursor-" + mid, raw, "accepted", now=stamp)
    if status == "uncertain":
        store.claim(mid); store.recover_claims()
    elif status == "claimed":
        store.claim(mid)
    elif status != "queued":
        store.finish(mid, status)
        store.db.execute("UPDATE inbox SET finished=? WHERE message_id=?", (stamp, mid))
        store.db.commit()
    return raw


def test_cleanup_reclaims_payloads_but_cannot_replay_input_send_or_upload(tmp_path):
    store = Store(tmp_path / "state.db", "install:sub")
    raw = add(store, "old")
    store.prepare_messages("old", 0, "dm", [{"text": "sensitive payload"}])
    store.sent("old:0:0", {"id": "receipt"})
    store.upload("old:0:upload", "fingerprint")
    store.uploaded("old:0:upload", {"id": "file"})
    store.db.execute("UPDATE outbox SET created=1,delivered=1");store.db.commit()
    store._set_meta("cursor", "unchanged");store.db.commit()
    assert store.prune(now=HISTORY_SECONDS + 2) == {"turns": 1, "events": 1}
    assert store.cursor == "unchanged"
    for table in ("inbox", "outbox", "uploads", "outbox_batches", "events"):
        assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert store.prune(now=HISTORY_SECONDS + 3)["turns"] == 0
    store.close()
    store = Store(tmp_path / "state.db", "install:sub")
    assert not store.ingest("cursor-replayed", raw, "accepted")
    raw["id"] = "another-envelope-for-same-input"
    assert not store.ingest("cursor-replayed-2", raw, "accepted")
    assert not store.queued()
    for action in (lambda: store.prepare_messages("old", 0, "dm", [{"text": "changed"}]),
                   lambda: store.prepare_send("old:0:0", "old", "dm", {"text": "changed"}),
                   lambda: store.upload("old:0:upload", "fingerprint")):
        with pytest.raises(ValueError, match="reconciliation"):
            action()
    store.close()


@pytest.mark.parametrize("status", ["queued", "claimed", "uncertain", "failed"])
def test_unresolved_inputs_and_their_event_ids_survive_cleanup(tmp_path, status):
    store = Store(tmp_path / "state.db", "install:sub")
    add(store, "review", status)
    assert store.prune(now=HISTORY_SECONDS + 2)["turns"] == 0
    assert store.summary()["inbox"] == {status: 1}
    assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    store.close()


@pytest.mark.parametrize("protected", ["pending", "visual", "unfinished_upload", "recent", "thread"])
def test_cleanup_preserves_related_work_and_thread_routes(tmp_path, protected):
    store = Store(tmp_path / "state.db", "install:sub")
    add(store, "keep")
    if protected == "pending":store.prepare_messages("keep", 0, "dm", [{"text": "pending"}])
    if protected == "visual":store.begin_visual("keep")
    if protected == "unfinished_upload":store.upload("keep:0:upload", "fingerprint")
    if protected == "recent":
        store.db.execute("UPDATE inbox SET finished=?", (HISTORY_SECONDS,));store.db.commit()
    if protected == "thread":store.remember_thread({"id": "root", "channel": {"id": "dm"}, "text": "root"})
    result = store.prune(now=HISTORY_SECONDS + 2)
    assert result["turns"] == (1 if protected == "thread" else 0)
    if protected == "thread":assert store.thread("root", "dm")["id"] == "root"
    store.close()


def test_bounded_cleanup_and_status_index_survive_existing_database(tmp_path):
    store = Store(tmp_path / "state.db", "install:sub")
    for i in range(12):add(store, str(i))
    # Simulate the old database schema; upgrade must retain data and cursor.
    store.db.execute("DROP INDEX inbox_status");store.db.execute("DROP INDEX outbox_status")
    cursor = store.cursor;store.close()
    store = Store(tmp_path / "state.db", "install:sub")
    assert store.cursor == cursor and store.summary()["inbox"] == {"complete": 12}
    assert store.prune(now=HISTORY_SECONDS + 2, limit=3)["turns"] == 3
    assert store.summary()["inbox"] == {"complete": 9}
    add(store, "new", "queued", HISTORY_SECONDS)
    assert [e["data"]["message"]["id"] for e in store.queued()] == ["new"]
    plan = store.db.execute("EXPLAIN QUERY PLAN SELECT payload FROM inbox WHERE status='queued' ORDER BY rowid").fetchall()
    assert any("SEARCH" in row[3] for row in plan), [tuple(row) for row in plan]
    store.close()


def test_receiver_observation_runs_cleanup_at_most_hourly(tmp_path):
    store = Store(tmp_path / "state.db", "install:sub")
    add(store, "old")
    now = HISTORY_SECONDS + 2
    store.observed(now=now)
    assert store.summary()["inbox"] == {}
    assert float(store.get_meta("observed_at")) == now
    assert float(store.get_meta("last_cleanup")) == now
    store.observed(now=now + 1)
    assert float(store.get_meta("last_cleanup")) == now
    store.observed(now=now + 3600)
    assert float(store.get_meta("last_cleanup")) == now + 3600
    store.close()


def test_proactive_turn_retirement_blocks_new_sequences_without_an_inbox_row(tmp_path):
    store = Store(tmp_path / "state.db", "install:sub")
    store.prepare_messages("proactive_run", "text", "dm", [{"text": "scheduled"}])
    store.sent("proactive_run:text:0", {"id": "receipt"})
    store.db.execute("UPDATE outbox SET created=1,delivered=1");store.db.commit()
    assert store.prune(now=HISTORY_SECONDS + 2)["turns"] == 1
    with pytest.raises(ValueError, match="reconciliation"):
        store.prepare_messages("proactive_run", "new_sequence", "dm", [{"text": "changed"}])
    with pytest.raises(ValueError, match="reconciliation"):
        store.upload("proactive_run:file_new:upload", "new fingerprint")
    store.close()


def test_cleanup_failure_rolls_back_payload_removal_and_replay_barriers(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.db", "install:sub")
    raw = add(store, "atomic")
    retire = store._retire
    def interrupted(kind, identities):
        retire(kind, identities)
        if kind == "upload":raise RuntimeError("interrupted cleanup")
    monkeypatch.setattr(store, "_retire", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        store.prune(now=HISTORY_SECONDS + 2)
    assert store.summary()["inbox"] == {"complete": 1}
    assert store.db.execute("SELECT count(*) FROM retired").fetchone()[0] == 0
    assert not store.ingest("duplicate", raw, "accepted")
    store.close()


@pytest.mark.parametrize("protected", ["pending", "visual", "unfinished_upload", "failed"])
def test_protected_work_does_not_starve_cleanup_batch(tmp_path, protected):
    store = Store(tmp_path / "state.db", "install:sub")
    add(store, "a-protected", "failed" if protected == "failed" else "complete")
    add(store, "z-eligible")
    if protected == "pending":store.prepare_messages("a-protected", 0, "dm", [{"text": "pending"}])
    if protected == "visual":store.begin_visual("a-protected")
    if protected == "unfinished_upload":store.upload("a-protected:0:upload", "fingerprint")
    if protected == "failed":
        store.prepare_messages("a-protected", 0, "dm", [{"text": "failure notice"}])
        store.sent("a-protected:0:0", {"id": "receipt"})
        store.db.execute("UPDATE outbox SET created=1,delivered=1");store.db.commit()
    assert store.prune(now=HISTORY_SECONDS + 2, limit=1)["turns"] == 1
    assert store.db.execute("SELECT message_id FROM inbox").fetchone()[0] == "a-protected"
    store.close()
