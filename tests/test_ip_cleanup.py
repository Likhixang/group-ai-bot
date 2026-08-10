import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AI_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
bot = importlib.import_module("bot")


def _private_update(text=None):
    user = SimpleNamespace(id=bot.SUPER_ADMIN_ID)
    msg = SimpleNamespace(
        text=text,
        message_id=100,
        chat_id=200,
        from_user=user,
    )
    chat = SimpleNamespace(type=bot.ChatType.PRIVATE, id=200)
    return SimpleNamespace(effective_message=msg, effective_chat=chat)


def test_ip_cmd_schedules_command_and_reply_cleanup_for_all_outcomes(monkeypatch):
    calls = []

    async def fake_reply_and_cleanup(msg, context, text, delay_seconds, **kwargs):
        calls.append((text, delay_seconds, kwargs))

    monkeypatch.setattr(bot, "_reply_and_cleanup", fake_reply_and_cleanup)

    asyncio.run(bot.ip_cmd(_private_update("/ip"), SimpleNamespace(args=[])))
    asyncio.run(
        bot.ip_cmd(
            _private_update("/ip invalid"),
            SimpleNamespace(args=["invalid"]),
        )
    )

    monkeypatch.setattr(bot, "check_ip", lambda ip: {"ip": ip})
    monkeypatch.setattr(bot, "format_report", lambda result: "report")
    asyncio.run(
        bot.ip_cmd(
            _private_update("/ip 8.8.8.8"),
            SimpleNamespace(args=["8.8.8.8"]),
        )
    )

    def raise_check_ip(ip):
        raise RuntimeError("test failure")

    monkeypatch.setattr(bot, "check_ip", raise_check_ip)
    asyncio.run(
        bot.ip_cmd(
            _private_update("/ip 1.1.1.1"),
            SimpleNamespace(args=["1.1.1.1"]),
        )
    )

    assert len(calls) == 4
    assert all(delay_seconds == 30 for _, delay_seconds, _ in calls)
