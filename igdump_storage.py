from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("igdump")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_comment_cache(path: Path, username: str) -> tuple[list[dict[str, Any]], set[str]]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("username"), str) or payload["username"].lower() != username.lower():
        if payload:
            logger.warning("Кеш данных без владельца или от другого профиля не используется; данные будут собраны заново.")
        return [], set()
    records = payload.get("records", payload.get("comments", []))
    completed = payload.get("completed_posts", [])
    if not isinstance(records, list) or not isinstance(completed, list):
        return [], set()
    return [record for record in records if isinstance(record, dict)], {code for code in completed if isinstance(code, str)}


def save_comment_cache(path: Path, username: str, records: list[dict[str, Any]], completed: set[str]) -> None:
    write_json(path, {"version": 3, "username": username, "records": records,
                      "completed_posts": sorted(completed), "updated_at": datetime.now(UTC).isoformat()})


class LikerStore:
    """One copy per account, one unique account/post pair; SQL aggregation."""

    def __init__(self, path: Path, username: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT, full_name TEXT, avatar TEXT);
          CREATE TABLE IF NOT EXISTS likes(post TEXT, user_id TEXT, PRIMARY KEY(post,user_id));
          CREATE TABLE IF NOT EXISTS posts(code TEXT PRIMARY KEY, state TEXT, expected INTEGER, cursor TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS refresh_backup(post TEXT,user_id TEXT,PRIMARY KEY(post,user_id));
        """)
        owner = self.db.execute("SELECT value FROM meta WHERE key='username'").fetchone()
        if owner and owner[0].lower() != username.lower():
            self.close()
            raise ValueError("База лайкеров принадлежит другому профилю; выберите другую папку.")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('username',?)", (username,))
        self.db.execute("INSERT OR IGNORE INTO likes SELECT post,user_id FROM refresh_backup")
        self.db.execute("UPDATE posts SET state='failed' WHERE code IN (SELECT post FROM refresh_backup)")
        self.db.execute("DELETE FROM refresh_backup")
        self.db.commit()
        self.username = username

    def close(self) -> None:
        self.db.close()

    def migrate(self, path: Path) -> None:
        if self.db.execute("SELECT 1 FROM meta WHERE key='migrated'").fetchone():
            return
        records, completed = load_comment_cache(path, self.username)
        with self.db:
            for record in records:
                self._add(record["post_shortcode"], {"pk": record.get("user_id") or record.get("comment_id") or record["username"], **record})
            for row in self.db.execute("SELECT DISTINCT post FROM likes").fetchall():
                self.db.execute("INSERT OR IGNORE INTO posts(code,state) VALUES(?,?)", (row[0], "complete" if row[0] in completed else "partial"))
            for code in completed:
                self.db.execute("INSERT OR IGNORE INTO posts(code,state) VALUES(?, 'complete')", (code,))
            self.db.execute("INSERT INTO meta VALUES('migrated','1')")

    def _add(self, code: str, user: dict[str, Any]) -> None:
        key = str(user.get("pk") or user.get("id") or user.get("user_id") or user.get("username") or "")
        if not key:
            return
        self.db.execute("INSERT INTO users VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name,avatar=excluded.avatar",
                        (key, user.get("username", "unknown"), user.get("full_name", ""), user.get("profile_pic_url", "")))
        self.db.execute("INSERT OR IGNORE INTO likes VALUES(?,?)", (code, key))

    def add_page(self, code: str, users: list[dict[str, Any]], expected: int | None, cursor: str | None) -> None:
        with self.db:
            for user in users:
                self._add(code, user)
            self.db.execute("INSERT INTO posts VALUES(?, 'collecting', ?, ?, NULL) ON CONFLICT(code) DO UPDATE SET expected=COALESCE(excluded.expected,posts.expected),cursor=excluded.cursor,state='collecting'", (code, expected, cursor))

    def state(self, code: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM posts WHERE code=?", (code,)).fetchone()
        return dict(row) if row else {"state": "pending", "cursor": None, "expected": None}

    def count(self, code: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM likes WHERE post=?", (code,)).fetchone()[0]

    def mark(self, code: str, state: str, expected: int | None = None, error: str | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO posts VALUES(?,?,?,NULL,?) ON CONFLICT(code) DO UPDATE SET state=excluded.state,expected=COALESCE(excluded.expected,posts.expected),error=excluded.error", (code, state, expected, error))

    def reset(self, code: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO refresh_backup SELECT post,user_id FROM likes WHERE post=?", (code,))
            self.db.execute("DELETE FROM likes WHERE post=?", (code,))
            self.db.execute("DELETE FROM posts WHERE code=?", (code,))

    def restore_refresh(self) -> None:
        """Keep previous records as well as new pages when a refresh fails."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO likes SELECT post,user_id FROM refresh_backup")

    def finish_refresh(self, code: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM refresh_backup WHERE post=?", (code,))

    def select(self, codes: set[str]) -> None:
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS selected(code TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM selected")
        self.db.executemany("INSERT INTO selected VALUES(?)", ((code,) for code in codes))

    def rows(self):
        query = "SELECT l.post AS post_shortcode,u.id AS user_id,u.username,u.full_name FROM likes l JOIN users u ON u.id=l.user_id JOIN selected s ON s.code=l.post ORDER BY l.post,u.username"
        for row in self.db.execute(query):
            yield dict(row)

    def stats(self) -> dict[str, Any]:
        people = [dict(row) for row in self.db.execute("""SELECT u.username,u.full_name,u.avatar AS profile_pic_url,COUNT(*) AS count,
          0 AS likes_received,0 AS last_comment_ts,COUNT(*) AS posts_commented
          FROM likes l JOIN users u ON u.id=l.user_id JOIN selected s ON s.code=l.post
          GROUP BY u.id ORDER BY count DESC,u.username""")]
        posts = [dict(row) for row in self.db.execute("""SELECT s.code AS shortcode,COALESCE(p.expected,COUNT(l.user_id)) AS count,
          COUNT(l.user_id) AS collected,p.expected IS NULL AS estimated
          FROM selected s LEFT JOIN posts p ON p.code=s.code LEFT JOIN likes l ON l.post=s.code
          GROUP BY s.code ORDER BY count DESC,s.code LIMIT 10""")]
        total = self.db.execute("SELECT COUNT(*) FROM likes l JOIN selected s ON s.code=l.post").fetchone()[0]
        post_count = self.db.execute("SELECT COUNT(*) FROM selected").fetchone()[0]
        with_likes = self.db.execute("SELECT COUNT(DISTINCT l.post) FROM likes l JOIN selected s ON s.code=l.post").fetchone()[0]
        return {"total_comments": total, "unique_commenters": len(people), "posts_with_comments": with_likes,
                "total_posts": post_count, "avg_per_post": round(total / max(post_count, 1), 1),
                "participants": people, "top_commenters": people[:30], "top_posts": posts}
