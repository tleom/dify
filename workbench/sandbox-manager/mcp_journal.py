"""Bounded MCP results with durable, non-expiring execution tombstones."""

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager

UNKNOWN = {
    "error": "此前 MCP 请求已受理，结果未知或已过保留期；请核实外部状态，同一请求不会重复执行",
    "uncertain": True,
}


class Journal:
    def __init__(
        self,
        root,
        *,
        ttl=86400,
        owner_bytes=64 * 1024 * 1024,
        total_bytes=256 * 1024 * 1024,
        max_records=100_000,
    ):
        self.root, self.path = root, root / "mcp-journal.sqlite3"
        self.ttl, self.owner_bytes, self.total_bytes, self.max_records = (
            ttl,
            owner_bytes,
            total_bytes,
            max_records,
        )
        self.guard = threading.RLock()
        with self.connection() as db:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                digest TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
                created REAL NOT NULL, finished REAL, result BLOB
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS calls_owner ON calls(owner, finished)"
            )

    @contextmanager
    def connection(self):
        with self.guard, closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            yield db

    @staticmethod
    def identity(owner, payload):
        request = payload.get("request_key")
        if not isinstance(request, str) or not request or len(request) > 400:
            raise ValueError("MCP invocation requires an execution-scoped request key")
        digest = hashlib.sha256((owner + ":" + request).encode()).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return digest, fingerprint

    def begin(self, owner, payload):
        """Reserve before side effects; a lost reply or process cannot execute twice."""
        digest, fingerprint = self.identity(owner, payload)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            row = db.execute(
                "SELECT fingerprint, result FROM calls WHERE digest=?", (digest,)
            ).fetchone()
            # Preserve old file journals when upgrading a manager with existing data.
            legacy = self.root / ("mcp-" + digest)
            if row is None and legacy.exists():
                value = self._legacy(legacy)
                row = (value["fingerprint"], None)
            if row is not None:
                if row[0] != fingerprint:
                    return digest, {"error": "MCP 请求标识已被不同参数使用"}
                return digest, json.loads(row[1]) if row[1] is not None else dict(
                    UNKNOWN
                )
            if (
                db.execute("SELECT count(*) FROM calls").fetchone()[0]
                >= self.max_records
            ):
                return digest, {
                    "error": "MCP 执行记录已达到容量上限，本次操作未执行，请联系管理员归档"
                }
            db.execute(
                "INSERT INTO calls(digest, owner, fingerprint, created) VALUES(?,?,?,?)",
                (digest, owner, fingerprint, time.time()),
            )
        return digest, None

    def finish(self, digest, output):
        data = json.dumps(output, ensure_ascii=False).encode()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner FROM calls WHERE digest=?", (digest,)
            ).fetchone()
            if row is None:
                raise ValueError("Missing MCP execution reservation")
            owner = row[0]
            self._expire(db)
            if len(data) <= min(self.owner_bytes, self.total_bytes):
                self._trim(db, self.owner_bytes - len(data), owner)
                self._trim(db, self.total_bytes - len(data))
                db.execute(
                    "UPDATE calls SET finished=?, result=? WHERE digest=?",
                    (time.time(), data, digest),
                )
            else:
                db.execute(
                    "UPDATE calls SET finished=? WHERE digest=?", (time.time(), digest)
                )

    def _expire(self, db):
        db.execute(
            "UPDATE calls SET result=NULL WHERE result IS NOT NULL AND finished<?",
            (time.time() - self.ttl,),
        )

    @staticmethod
    def _trim(db, budget, owner=None):
        where, parameters = (
            (" AND owner=?", (owner,)) if owner is not None else ("", ())
        )
        size = db.execute(
            "SELECT coalesce(sum(length(result)),0) FROM calls WHERE result IS NOT NULL"
            + where,
            parameters,
        ).fetchone()[0]
        if size <= budget:
            return
        rows = db.execute(
            "SELECT digest, length(result) FROM calls WHERE result IS NOT NULL"
            + where
            + " ORDER BY finished, digest",
            parameters,
        )
        for digest, length in rows:
            db.execute("UPDATE calls SET result=NULL WHERE digest=?", (digest,))
            size -= length
            if size <= budget:
                break

    @staticmethod
    def _legacy(path):
        # Old writers put the fingerprint first. Compact results without loading
        # an arbitrarily large legacy payload, while retaining replay protection.
        with path.open("rb") as stream:
            prefix = stream.read(256)
        match = re.match(rb'\{\s*"fingerprint"\s*:\s*"([a-f0-9]{64})"', prefix)
        if match is None:
            raise ValueError("Invalid legacy MCP journal; refusing to replay")
        return {"fingerprint": match[1].decode()}

    def prune(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            self._trim(db, self.total_bytes)
            owners = db.execute(
                "SELECT DISTINCT owner FROM calls WHERE result IS NOT NULL"
            ).fetchall()
            for (owner,) in owners:
                self._trim(db, self.owner_bytes, owner)
        with self.connection() as db:
            db.execute("PRAGMA incremental_vacuum(2048)")
        # Legacy tombstones remain readable; only their large result bodies go.
        compacted = 0
        for path in self.root.glob("mcp-*"):
            if (
                re.fullmatch(r"mcp-[a-f0-9]{64}", path.name)
                and path.stat().st_size > 100
            ):
                value = self._legacy(path)
                temporary = path.with_suffix(".compact")
                temporary.write_text(json.dumps(value))
                temporary.replace(path)
                compacted += 1
                if compacted >= 100:
                    break
