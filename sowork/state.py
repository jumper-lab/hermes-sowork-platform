"""Transactional inbox, opaque stream cursor and immutable outbox bodies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

RETENTION_SECONDS = 72 * 3600
HISTORY_SECONDS = 30 * 24 * 3600
CLEANUP_INTERVAL = 3600


class ReconciliationRequired(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path, scope: str):
        self.path = path
        self._next_cleanup = 0
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("State database cannot be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, message_id TEXT, log_id TEXT NOT NULL,
                verdict TEXT NOT NULL, received REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                message_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                status TEXT NOT NULL, received REAL NOT NULL, finished REAL);
            CREATE TABLE IF NOT EXISTS outbox (
                logical_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                body TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, receipt TEXT, created REAL NOT NULL, delivered REAL);
            CREATE TABLE IF NOT EXISTS uploads (
                logical_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                attachment TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS visuals (
                turn_id TEXT PRIMARY KEY, message_id TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox_batches (
                logical_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS inbox_status ON inbox(status);
            CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status);
            CREATE INDEX IF NOT EXISTS inbox_finished ON inbox(finished);
            CREATE INDEX IF NOT EXISTS events_received ON events(received);
            CREATE INDEX IF NOT EXISTS outbox_turn ON outbox(turn_id);
            CREATE TABLE IF NOT EXISTS retired (digest TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS threads (
                root_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, parent TEXT NOT NULL);
        """)
        old_scope = self.get_meta("scope")
        if old_scope and old_scope != scope:
            self.close()
            raise ValueError("State belongs to a different installation/subscription")
        with self.db:
            self._set_meta("scope", scope)

    def close(self):
        self.db.close()

    def _begin_write(self):
        # Retirement and replay checks must see one serialized snapshot, including
        # when a standalone worker shares this database with the live receiver.
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @property
    def cursor(self):
        return self.get_meta("cursor")

    def observed(self, now=None):
        with self.db:
            self._set_meta("observed_at", now if now is not None else time.time())
        stamp = time.time() if now is None else now
        if stamp >= self._next_cleanup:
            self.prune(now=stamp)

    @staticmethod
    def _digest(kind, identity):
        return hashlib.sha256((kind + "|" + identity).encode()).hexdigest()

    def _retired(self, kind, identity):
        return self.db.execute("SELECT 1 FROM retired WHERE digest=?",
                               (self._digest(kind, identity),)).fetchone() is not None

    def _retire(self, kind, identities):
        self.db.executemany("INSERT OR IGNORE INTO retired VALUES (?)",
                            ((self._digest(kind, identity),) for identity in identities))

    def _require_active(self, kind, identity):
        if self._retired(kind, identity):
            raise ValueError("Delivery history was retired; operator reconciliation required")

    def prune(self, *, now=None, limit=200):
        """Bound retained payloads, preserving compact replay barriers and unresolved work.

        Digests are intentionally retained. Deleting them could repeat tools or an
        old proactive delivery after the API's idempotency window has expired.
        No VACUUM or cursor changes; SQLite reuses freed pages during normal work.
        """
        stamp = time.time() if now is None else now
        cutoff = stamp - HISTORY_SECONDS
        with self.db:
            self._set_meta("last_cleanup", stamp)  # Acquire the writer lock before selecting.
            turns = [r[0] for r in self.db.execute("""
                WITH candidates(turn_id) AS (
                    SELECT message_id FROM inbox WHERE status IN ('complete','cancelled','rejected')
                        AND finished < ?
                    UNION
                    SELECT turn_id FROM outbox GROUP BY turn_id
                        HAVING max(created) < ? AND max(COALESCE(delivered,?)) < ?
                )
                SELECT c.turn_id FROM candidates c
                WHERE NOT EXISTS (SELECT 1 FROM inbox i WHERE i.message_id=c.turn_id
                    AND (i.status NOT IN ('complete','cancelled','rejected') OR i.finished IS NULL OR i.finished>=?))
                AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.turn_id=c.turn_id
                    AND (o.status!='sent' OR o.delivered IS NULL OR o.delivered>=?))
                AND NOT EXISTS (SELECT 1 FROM visuals v WHERE v.turn_id=c.turn_id)
                AND NOT EXISTS (SELECT 1 FROM uploads u WHERE u.attachment IS NULL
                    AND substr(u.logical_id,1,length(c.turn_id)+1)=c.turn_id||':')
                LIMIT ?""", (cutoff, cutoff, stamp, cutoff, cutoff, cutoff, limit))]
            removed = 0
            for turn in turns:
                prefix = turn + ":"
                uploads = [r[0] for r in self.db.execute(
                    "SELECT logical_id FROM uploads WHERE substr(logical_id,1,?)=?", (len(prefix), prefix))]
                sends = [r[0] for r in self.db.execute("SELECT logical_id FROM outbox WHERE turn_id=?", (turn,))]
                batches = [r[0] for r in self.db.execute(
                    "SELECT logical_id FROM outbox_batches WHERE substr(logical_id,1,?)=?", (len(prefix), prefix))]
                for kind, identities in (("input", [turn]), ("send", sends), ("batch", batches), ("upload", uploads)):
                    self._retire(kind, identities)
                self.db.execute("DELETE FROM inbox WHERE message_id=?", (turn,))
                self.db.execute("DELETE FROM outbox WHERE turn_id=?", (turn,))
                self.db.executemany("DELETE FROM uploads WHERE logical_id=?", ((key,) for key in uploads))
                self.db.executemany("DELETE FROM outbox_batches WHERE logical_id=?", ((key,) for key in batches))
                removed += 1
            events = [r[0] for r in self.db.execute("""SELECT event_id FROM events e WHERE received<?
                AND NOT EXISTS (SELECT 1 FROM inbox i WHERE i.message_id=e.message_id)
                ORDER BY received LIMIT ?""", (cutoff, limit * 10))]
            self._retire("event", events)
            self.db.executemany("DELETE FROM events WHERE event_id=?", ((key,) for key in events))
        self._next_cleanup = stamp + CLEANUP_INTERVAL
        return {"turns": removed, "events": len(events)}

    def check_retention(self, now=None):
        observed = self.get_meta("observed_at")
        if observed and (time.time() if now is None else now) - float(observed) >= RETENTION_SECONDS:
            raise ReconciliationRequired("Mailbox retention gap: reconcile explicitly before resuming")

    def ingest(self, log_id, event, verdict, *, now=None):
        """Commit the admitted payload AND cursor together, before dispatch."""
        stamp = time.time() if now is None else now
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id or not isinstance(log_id, str) or not log_id:
            raise ValueError("Event and log IDs are required")
        data = event.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        message_id = message.get("id") if isinstance(message, dict) else None
        admitted = False
        with self.db:
            self._begin_write()
            inserted = not self._retired("event", event_id) and self.db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                (event_id, message_id, log_id, verdict, stamp),
            ).rowcount
            if inserted and verdict == "accepted" and not self._retired("input", message_id or ""):
                if not isinstance(message_id, str) or not message_id:
                    raise ValueError("Admitted event needs a message ID")
                admitted = bool(self.db.execute(
                    "INSERT OR IGNORE INTO inbox VALUES (?,?,'queued',?,NULL)",
                    (message_id, json.dumps(event, separators=(",", ":")), stamp),
                ).rowcount)
            self._set_meta("cursor", log_id)
            self._set_meta("observed_at", stamp)
        return admitted

    def queued(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM inbox WHERE status='queued' ORDER BY rowid"
        )]

    def claim(self, message_id):
        # The durable barrier is BEFORE the gateway/agent invocation. A process
        # dying after this point may have executed tools, so never replay it.
        with self.db:
            return bool(self.db.execute(
                "UPDATE inbox SET status='claimed' WHERE message_id=? AND status='queued'",
                (message_id,),
            ).rowcount)

    def finish(self, message_id, status):
        if status not in {"complete", "failed", "cancelled", "rejected"}:
            raise ValueError("Invalid completion status")
        with self.db:
            self.db.execute("UPDATE inbox SET status=?,finished=? WHERE message_id=?", (status, time.time(), message_id))

    def recover_claims(self):
        with self.db:
            return self.db.execute("UPDATE inbox SET status='uncertain' WHERE status='claimed'").rowcount

    def prepare_send(self, logical_id, turn_id, channel_id, body):
        with self.db:
            self._begin_write()
            return self._prepare_send(logical_id, turn_id, channel_id, body)

    def prepare_response(self, turn_id, sequence, channel_id, chunks):
        return self.prepare_messages(turn_id, sequence, channel_id, [{"text": chunk} for chunk in chunks])

    def prepare_messages(self, turn_id, sequence, channel_id, bodies):
        with self.db:
            self._begin_write()
            self._require_active("input", turn_id)
            fingerprint = hashlib.sha256(json.dumps([channel_id, bodies], sort_keys=True).encode()).hexdigest()
            batch = f"{turn_id}:{sequence}"
            self._require_active("batch", batch)
            self.db.execute("INSERT OR IGNORE INTO outbox_batches VALUES (?,?)", (batch, fingerprint))
            if self.db.execute("SELECT fingerprint FROM outbox_batches WHERE logical_id=?", (batch,)).fetchone()[0] != fingerprint:
                raise ValueError("A logical delivery cannot change its payload or chunk count")
            return [self._prepare_send(f"{turn_id}:{sequence}:{i}", turn_id, channel_id, body) for i, body in enumerate(bodies)]

    def upload(self, logical_id, fingerprint):
        with self.db:
            self._begin_write()
            self._require_active("input", logical_id.rsplit(":", 2)[0])
            self._require_active("upload", logical_id)
            self.db.execute("INSERT OR IGNORE INTO uploads VALUES (?,?,NULL,?)", (logical_id, fingerprint, time.time()))
        row = self.db.execute("SELECT * FROM uploads WHERE logical_id=?", (logical_id,)).fetchone()
        if row["fingerprint"] != fingerprint:
            raise ValueError("A logical upload cannot change its file or destination")
        return json.loads(row["attachment"]) if row["attachment"] else None

    def uploaded(self, logical_id, attachment):
        with self.db:
            self.db.execute("UPDATE uploads SET attachment=? WHERE logical_id=?", (json.dumps(attachment, sort_keys=True), logical_id))

    def upload_created(self, logical_id):
        return self.db.execute("SELECT created FROM uploads WHERE logical_id=?", (logical_id,)).fetchone()[0]

    def thread(self, root_id, channel_id):
        row = self.db.execute("SELECT parent FROM threads WHERE root_id=? AND channel_id=?", (root_id, channel_id)).fetchone()
        return json.loads(row[0]) if row else None

    def remember_thread(self, parent):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?)",
                            (parent["id"], parent["channel"]["id"], json.dumps(parent, separators=(",", ":"))))

    def begin_visual(self, turn_id):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO visuals VALUES (?,NULL,?)", (turn_id, time.time()))

    def remember_visual(self, turn_id, message_id):
        with self.db:
            self.db.execute("UPDATE visuals SET message_id=? WHERE turn_id=?", (message_id, turn_id))

    def forget_visual(self, turn_id):
        with self.db:
            self.db.execute("DELETE FROM visuals WHERE turn_id=?", (turn_id,))

    def unfinished_visuals(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM visuals")]

    def _prepare_send(self, logical_id, turn_id, channel_id, body):
        self._require_active("input", turn_id)
        self._require_active("send", logical_id)
        serialized = json.dumps(body, sort_keys=True, separators=(",", ":"))
        key = "hermes_" + hashlib.sha256((self.get_meta("scope") + "|" + logical_id).encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,'pending',NULL,?,NULL)",
            (logical_id, turn_id, channel_id, serialized, key, time.time()),
        )
        row = self.db.execute("SELECT * FROM outbox WHERE logical_id=?", (logical_id,)).fetchone()
        if row["body"] != serialized or row["channel_id"] != channel_id or row["turn_id"] != turn_id:
            raise ValueError("A logical send cannot change body or destination")
        return dict(row)

    def sent(self, logical_id, receipt):
        with self.db:
            self.db.execute(
                "UPDATE outbox SET status='sent',receipt=?,delivered=? WHERE logical_id=?",
                (json.dumps(receipt, separators=(",", ":")), time.time(), logical_id),
            )

    def pending_sends(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM outbox WHERE status='pending' ORDER BY rowid")]

    def summary(self):
        return {table: {row[0]: row[1] for row in self.db.execute(
            f"SELECT status,COUNT(*) FROM {table} GROUP BY status"
        )} for table in ("inbox", "outbox")}
