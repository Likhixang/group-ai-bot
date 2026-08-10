"""Verify ban/allow notification user mentions are clickable HTML links."""
import os
import importlib.util
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "bot_mention_test", os.path.join(os.path.dirname(__file__), "..", "bot.py")
)
bot = importlib.util.module_from_spec(SPEC)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
SPEC.loader.exec_module(bot)


def test_html_user_mention_basic():
    html = bot._html_user_mention(8733956596, "Khixang Li")
    assert html == '<a href="tg://user?id=8733956596">Khixang Li</a>'


def test_html_user_mention_escapes_special_chars():
    html = bot._html_user_mention(1, 'A <B> & "C"')
    assert html == '<a href="tg://user?id=1">A &lt;B&gt; &amp; &quot;C&quot;</a>'


def test_html_user_mention_fallback_name():
    html = bot._html_user_mention(42, None)
    assert html == '<a href="tg://user?id=42">用户42</a>'
    html2 = bot._html_user_mention(42, "   ")
    assert html2 == '<a href="tg://user?id=42">用户42</a>'


def test_user_display_name_prefers_full_name():
    user = SimpleNamespace(full_name="Full Name", username="uname", id=9)
    assert bot._user_display_name(user) == "Full Name"


def test_user_display_name_falls_back_to_username():
    user = SimpleNamespace(full_name="", username="uname", id=9)
    assert bot._user_display_name(user) == "uname"


def test_user_display_name_falls_back_to_id():
    user = SimpleNamespace(full_name="", username="", id=99)
    assert bot._user_display_name(user) == "用户99"
    assert bot._user_display_name(None, fallback_id=7) == "用户7"


def test_ban_message_format_contains_clickable_mention():
    mention = bot._html_user_mention(123, "某某人")
    text = f"🔇 已禁言 {mention} 999999999 分钟。"
    assert 'href="tg://user?id=123"' in text
    assert ">某某人<" in text
    assert "999999999 分钟" in text


def test_allow_and_release_message_formats():
    mention = bot._html_user_mention(456, "恢复用户")
    allow_text = f"🔈 已提前解除 {mention} 的禁言，请谨言慎行！"
    release_text = f"🔊 已恢复 {mention} 发言，请谨言慎行！"
    assert 'href="tg://user?id=456"' in allow_text
    assert 'href="tg://user?id=456"' in release_text
