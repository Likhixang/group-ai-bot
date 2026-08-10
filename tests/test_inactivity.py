"""Unit tests for inactivity tracking / warning / remind2 / kick helpers + soft ban."""
import os
import importlib.util
import sqlite3
import time
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "bot_inactivity_test",
    os.path.join(os.path.dirname(__file__), "..", "bot.py"),
)
assert SPEC is not None and SPEC.loader is not None
bot = importlib.util.module_from_spec(SPEC)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
SPEC.loader.exec_module(bot)


@pytest.fixture()
def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(bot, "MEMORY_DB_PATH", str(db_path))
    bot._init_memory_db()
    return str(db_path)


def test_touch_and_load_inactive(activity_db):
    now = int(time.time())
    old = now - 31 * 24 * 3600
    bot._touch_user_activity(-100, 1, "Old User", ts=old)
    bot._touch_user_activity(-100, 2, "Fresh User", ts=now)

    threshold = now - 30 * 24 * 3600
    rows = bot._load_inactive_users(threshold, limit=10)
    assert len(rows) == 1
    assert rows[0][1] == 1


def test_warn_once_then_skip_until_activity(activity_db):
    now = int(time.time())
    old = now - 40 * 24 * 3600
    bot._touch_user_activity(-100, 7, "Silent", ts=old)
    threshold = now - 30 * 24 * 3600
    assert len(bot._load_inactive_users(threshold, 10)) == 1

    bot._mark_inactivity_warned(-100, 7, ts=now)
    assert bot._load_inactive_users(threshold, 10) == []

    bot._touch_user_activity(-100, 7, "Silent", ts=now)
    assert bot._load_inactive_users(now - 30 * 24 * 3600, 10) == []


def test_remind2_requires_prior_warn_and_45_days(activity_db):
    now = int(time.time())
    old = now - 50 * 24 * 3600
    bot._touch_user_activity(-100, 8, "Ghost", ts=old)
    remind2_threshold = now - 45 * 24 * 3600
    assert bot._load_inactivity_remind2_candidates(remind2_threshold, 10) == []

    bot._mark_inactivity_warned(-100, 8, ts=now - 10 * 24 * 3600)
    rows = bot._load_inactivity_remind2_candidates(remind2_threshold, 10)
    assert len(rows) == 1
    assert rows[0][1] == 8

    bot._mark_inactivity_remind2(-100, 8, ts=now)
    assert bot._load_inactivity_remind2_candidates(remind2_threshold, 10) == []
    assert bot._load_inactivity_remind2(-100, 8) is not None


def test_kick_warn_requires_remind2_and_60_days(activity_db):
    now = int(time.time())
    old = now - 65 * 24 * 3600
    bot._touch_user_activity(-100, 20, "LongGone", ts=old)
    kick_threshold = now - 60 * 24 * 3600

    # not second-reminded yet
    assert bot._load_inactivity_kick_warn_candidates(kick_threshold, 10) == []

    bot._mark_inactivity_warned(-100, 20, ts=now - 30 * 24 * 3600)
    bot._mark_inactivity_remind2(-100, 20, ts=now - 15 * 24 * 3600)
    rows = bot._load_inactivity_kick_warn_candidates(kick_threshold, 10)
    assert len(rows) == 1
    assert rows[0][1] == 20

    deadline = now + 3 * 24 * 3600
    bot._mark_inactivity_kick_warned(-100, 20, warned_at=now, deadline_at=deadline)
    assert bot._load_inactivity_kick_warn_candidates(kick_threshold, 10) == []
    assert bot._load_inactivity_kick_due(now, 10) == []
    assert bot._load_inactivity_kick_due(deadline, 10)[0][1] == 20


def test_clear_markers_cancels_kick_deadline(activity_db):
    now = int(time.time())
    bot._touch_user_activity(-100, 21, "X", ts=now - 70 * 24 * 3600)
    bot._mark_inactivity_warned(-100, 21, ts=now - 35 * 24 * 3600)
    bot._mark_inactivity_remind2(-100, 21, ts=now - 20 * 24 * 3600)
    bot._mark_inactivity_kick_warned(
        -100, 21, warned_at=now - 1, deadline_at=now + 100
    )
    assert bot._load_inactivity_kick_due(now + 200, 10)

    bot._clear_inactivity_markers(-100, 21, "X", ts=now)
    assert bot._load_inactivity_remind2(-100, 21) is None
    assert bot._load_inactivity_kick_due(now + 10**9, 10) == []


def test_clear_remind2_restarts_clock(activity_db):
    now = int(time.time())
    bot._touch_user_activity(-100, 9, "Muted", ts=now - 50 * 24 * 3600)
    bot._mark_inactivity_warned(-100, 9, ts=now - 15 * 24 * 3600)
    bot._mark_inactivity_remind2(-100, 9, ts=now - 1)
    bot._clear_inactivity_markers(-100, 9, "Muted", ts=now)
    assert bot._load_inactivity_remind2(-100, 9) is None
    assert bot._load_inactive_users(now - 30 * 24 * 3600, 10) == []


def test_remind2_user_not_in_first_warn_list(activity_db):
    now = int(time.time())
    bot._touch_user_activity(-100, 11, "X", ts=now - 60 * 24 * 3600)
    bot._mark_inactivity_warned(-100, 11, ts=now - 20 * 24 * 3600)
    bot._mark_inactivity_remind2(-100, 11, ts=now)
    assert bot._load_inactive_users(now - 30 * 24 * 3600, 10) == []


def test_remove_user_activity(activity_db):
    bot._touch_user_activity(-1, 9, "X", ts=1)
    bot._remove_user_activity(-1, 9)
    assert bot._load_inactive_users(10**12, 10) == []


def test_message_formats_clickable():
    mention = bot._html_user_mention(12345, "张三")
    warn = f"📣 {mention} 你已一个月未发言"
    remind2 = f"📣 {mention} 你已一个半月未发言"
    kick_warn = f"⚠️ {mention} 你已经两个月未发言，将在三天后移出群"
    kick_done = f"👋 {mention} 因长期未发言，已移出群组。"
    for text in (warn, remind2, kick_warn, kick_done):
        assert 'href="tg://user?id=12345"' in text


def test_schema_has_kick_columns(activity_db):
    cols = {
        r[1]
        for r in sqlite3.connect(activity_db).execute("PRAGMA table_info(user_activity)")
    }
    assert {"muted_at", "kick_warned_at", "kick_deadline_at", "last_warned_at"} <= cols


def test_user_display_name_helpers():
    user = SimpleNamespace(full_name="A B", username="ab", id=1)
    assert bot._user_display_name(user) == "A B"


def test_soft_ban_permanent_and_timed(activity_db):
    now = int(time.time())
    # permanent
    bot._save_ban_record(-100, 42, "P", 0, 1, 2)
    active = bot._load_active_ban(-100, 42)
    assert active is not None
    assert active[3] == 0
    assert bot._is_soft_ban_active(0, now=now)
    # permanent must NOT expire via scheduler
    assert bot._load_expired_bans() == []

    # timed active
    bot._save_ban_record(-100, 43, "T", now + 600, 3, 4)
    assert bot._load_active_ban(-100, 43) is not None
    assert bot._load_expired_bans() == []

    # timed expired
    bot._save_ban_record(-100, 44, "E", now - 10, 5, 6)
    assert bot._load_active_ban(-100, 44) is None
    expired = bot._load_expired_bans()
    assert any(r[1] == 44 for r in expired)


def test_soft_ban_allow_all(activity_db):
    now = int(time.time())
    bot._save_ban_record(-200, 1, "A", 0, 0, 0)
    bot._save_ban_record(-200, 2, "B", now + 100, 0, 0)
    bot._save_ban_record(-201, 3, "C", 0, 0, 0)  # other chat
    cleared = bot._clear_all_bans_in_chat(-200)
    assert {u for _, u, *_ in cleared} == {1, 2}
    assert bot._load_active_ban(-200, 1) is None
    assert bot._load_active_ban(-200, 2) is None
    assert bot._load_active_ban(-201, 3) is not None


def test_compat_aliases_exist():
    assert bot._load_inactivity_mute_candidates is bot._load_inactivity_remind2_candidates
    assert bot._mark_inactivity_muted is bot._mark_inactivity_remind2
    assert bot._load_inactivity_mute is bot._load_inactivity_remind2
    assert bot._clear_inactivity_mute is bot._clear_inactivity_markers
    assert bot.INACTIVITY_MUTE_DAYS == bot.INACTIVITY_REMIND2_DAYS


def test_format_soft_ban_remaining():
    now = int(time.time())
    assert bot._format_soft_ban_remaining(0, now=now) == "永久"
    assert "秒" in bot._format_soft_ban_remaining(now + 45, now=now)
    assert "分" in bot._format_soft_ban_remaining(now + 10 * 60, now=now)
    assert "小时" in bot._format_soft_ban_remaining(now + 3 * 3600, now=now)


def test_soft_ban_protected_users():
    """只有 bot 与超管受保护；普通用户/群管理 ID 不在保护名单。"""
    assert bot._is_soft_ban_protected_user(bot.SUPER_ADMIN_ID) is True
    # BOT_ID 启动前可能是 None；模拟已启动
    old = bot.BOT_ID
    try:
        bot.BOT_ID = 999001
        assert bot._is_soft_ban_protected_user(999001) is True
        assert bot._is_soft_ban_protected_user(123456) is False
        # 任意群管理 ID 不等于超管时不应保护
        assert bot._is_soft_ban_protected_user(bot.SUPER_ADMIN_ID + 1) is False
    finally:
        bot.BOT_ID = old


def test_soft_ban_notice_state_is_separate_from_ban_record(activity_db):
    """A repeated-message notice needs one mutable message id per chat/user."""
    bot._save_soft_ban_notice(-100, 42, 9001, 123456)
    assert bot._load_soft_ban_notice(-100, 42) == (9001, 123456)

    # overwrite with new message_id
    bot._save_soft_ban_notice(-100, 42, 9002, 123457)
    assert bot._load_soft_ban_notice(-100, 42) == (9002, 123457)

    bot._remove_soft_ban_notice(-100, 42)
    assert bot._load_soft_ban_notice(-100, 42) is None


def test_soft_ban_notice_independent_per_user(activity_db):
    """Different users in same chat have independent notice records."""
    bot._save_soft_ban_notice(-100, 1, 100, 1000)
    bot._save_soft_ban_notice(-100, 2, 200, 1001)

    assert bot._load_soft_ban_notice(-100, 1) == (100, 1000)
    assert bot._load_soft_ban_notice(-100, 2) == (200, 1001)

    # same user in different chat
    bot._save_soft_ban_notice(-200, 1, 300, 1002)
    assert bot._load_soft_ban_notice(-100, 1) == (100, 1000)
    assert bot._load_soft_ban_notice(-200, 1) == (300, 1002)
