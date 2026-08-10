"""Verify managed-pin formatting survives a save->load->send round-trip.

The pin feature carries Telegram MessageEntity objects so native formatting
(bold/italic/link/code...) is preserved. This test exercises the
serialize/deserialize helpers and the DB layer without a live bot.
"""
import json
import os
import sqlite3
import tempfile

import pytest

# Load bot module without triggering the full application startup.
import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "bot_pin_test", os.path.join(os.path.dirname(__file__), "..", "bot.py")
)
bot = importlib.util.module_from_spec(SPEC)
# Provide a dummy token so module-level constants don't blow up on import.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
SPEC.loader.exec_module(bot)

from telegram import MessageEntity


def _sample_entities():
    return [
        MessageEntity(type="bold", offset=0, length=4),
        MessageEntity(type="text_link", offset=5, length=8, url="https://khixang.cc"),
        MessageEntity(type="code", offset=14, length=6),
    ]


def test_serialize_entities_roundtrip():
    ents = _sample_entities()
    raw = bot._serialize_entities(ents)
    assert raw is not None
    # JSON must be storable; dicts must survive a dump/load
    loaded = json.loads(raw)
    assert len(loaded) == 3
    assert loaded[0]["type"] == "bold"

    restored = bot._deserialize_entities(raw)
    assert restored is not None
    assert len(restored) == 3
    assert all(isinstance(e, MessageEntity) for e in restored)
    assert restored[1].url == "https://khixang.cc"
    # Re-serialize restored entities -> identical content
    assert json.loads(bot._serialize_entities(restored)) == loaded


def test_serialize_entities_empty_returns_none():
    assert bot._serialize_entities(None) is None
    assert bot._serialize_entities([]) is None
    assert bot._deserialize_entities(None) is None
    assert bot._deserialize_entities("") is None


def test_db_pin_entities_roundtrip(tmp_path):
    db_path = tmp_path / "memory.db"
    bot.MEMORY_DB_PATH = str(db_path)

    # Ensure schema (with migration column) is created.
    import sqlite3 as _sql

    conn = _sql.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS managed_pins (
            chat_id INTEGER NOT NULL,
            topic_id TEXT NOT NULL,
            pin_text TEXT NOT NULL,
            message_id INTEGER,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, topic_id)
        )"""
    )
    try:
        conn.execute("ALTER TABLE managed_pins ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE managed_pins ADD COLUMN pin_entities TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

    # Save a pin with formatting
    ents = _sample_entities()
    bot._save_managed_pin(100, "1", "**bold** link code", 999, entities=ents)
    row = bot._load_managed_pin(100, "1")
    assert row["entities"] is not None
    restored = bot._deserialize_entities(row["entities"])
    assert len(restored) == 3
    assert restored[1].url == "https://khixang.cc"

    # Update without entities — must clear the column (not keep stale formatting)
    bot._save_managed_pin(100, "1", "plain text", 1000, entities=None)
    row2 = bot._load_managed_pin(100, "1")
    assert row2["entities"] is None
