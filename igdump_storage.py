from __future__ import annotations

import json
import logging
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
            logger.warning("Кеш комментариев без владельца или от другого профиля не используется; комментарии будут собраны заново.")
        return [], set()
    records = payload.get("comments", [])
    completed = payload.get("completed_posts", [])
    if not isinstance(records, list) or not isinstance(completed, list):
        return [], set()
    return [record for record in records if isinstance(record, dict)], {code for code in completed if isinstance(code, str)}


def save_comment_cache(path: Path, username: str, records: list[dict[str, Any]], completed: set[str]) -> None:
    write_json(path, {"version": 2, "username": username, "comments": records,
                      "completed_posts": sorted(completed), "updated_at": datetime.now(UTC).isoformat()})


