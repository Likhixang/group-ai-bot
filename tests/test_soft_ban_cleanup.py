"""Regression tests for soft-ban reminder lifetime and Telegram API failures."""
import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:TEST")
os.environ.setdefault("AI_API_KEY", "test")
SPEC = importlib.util.spec_from_file_location(
    "bot_soft_ban_cleanup_test", Path(__file__).resolve().parents[1] / "bot.py"
)
assert SPEC is not None and SPEC.loader is not None
bot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(bot, "NOTICE_DELETE_TTL", 7)
    monkeypatch.setattr(bot, "_is_allowed_chat", lambda _: True)
    monkeypatch.setattr(bot, "_is_soft_ban_protected_user", lambda _: False)
    monkeypatch.setattr(bot, "_SOFT_BAN_LAST_NOTICE", {})
    monkeypatch.setattr(bot, "_SOFT_BAN_NOTICE_DELETE_TASKS", {})
    bot._init_memory_db()
    bot._save_ban_record(-100, 42, "User", 0)
    clock = SimpleNamespace(now=1000)
    monkeypatch.setattr(bot.time, "time", lambda: clock.now)
    user = SimpleNamespace(id=42, full_name="User", is_bot=False)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100, type=bot.ChatType.SUPERGROUP),
        effective_message=SimpleNamespace(message_id=77, from_user=user),
    )
    tasks = []

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    api = SimpleNamespace(
        delete_message=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
        edit_message_text=AsyncMock(),
    )
    context = SimpleNamespace(bot=api, application=SimpleNamespace(create_task=create_task))
    return update, context, clock, tasks


async def enforce(update, context):
    with pytest.raises(bot.ApplicationHandlerStop):
        await bot.enforce_soft_ban(update, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_error", [None, BadRequest("Message is not modified")])
async def test_edits_keep_original_global_ttl_and_do_not_replace_notice(scenario, monkeypatch, edit_error):
    update, context, clock, tasks = scenario
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    delays = []

    async def sleep(delay):
        delays.append(delay)
        sleep_started.set()
        await release_sleep.wait()

    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    try:
        await enforce(update, context)
        await sleep_started.wait()
        clock.now += 3
        context.bot.edit_message_text.side_effect = edit_error
        await enforce(update, context)
        assert len(tasks) == 1
        assert not tasks[0].cancelled()
        context.bot.send_message.assert_awaited_once()
        assert bot._load_soft_ban_notice(-100, 42) == (88, 1000)
        assert delays == [7]
    finally:
        release_sleep.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert call(chat_id=-100, message_id=88) in context.bot.delete_message.await_args_list
    assert bot._load_soft_ban_notice(-100, 42) is None
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS == {}


@pytest.mark.asyncio
async def test_failed_edit_does_not_cancel_cleanup_of_old_notice(scenario, monkeypatch):
    update, context, clock, tasks = scenario
    gate = asyncio.Event()
    async def sleep(_):
        await gate.wait()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    try:
        await enforce(update, context)
        clock.now += 3
        context.bot.edit_message_text.side_effect = NetworkError("temporary failure")
        await enforce(update, context)
        assert len(tasks) == 1
        context.bot.send_message.assert_awaited_once()
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert call(chat_id=-100, message_id=88) in context.bot.delete_message.await_args_list


@pytest.mark.asyncio
async def test_replacement_does_not_cancel_old_notice_cleanup(scenario, monkeypatch):
    update, context, clock, tasks = scenario
    gate = asyncio.Event()
    async def sleep(_):
        await gate.wait()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    try:
        await enforce(update, context)
        clock.now += 3
        context.bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
        context.bot.send_message.return_value = SimpleNamespace(message_id=89)
        await enforce(update, context)
        assert len(tasks) == 2
        assert not tasks[0].cancelling()
        assert bot._load_soft_ban_notice(-100, 42)[0] == 89
    finally:
        gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    for mid in (88, 89):
        assert call(chat_id=-100, message_id=mid) in context.bot.delete_message.await_args_list
    assert bot._load_soft_ban_notice(-100, 42) is None
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS == {}


@pytest.mark.asyncio
async def test_persisted_notice_rearms_only_remaining_ttl(scenario, monkeypatch):
    update, context, clock, tasks = scenario
    bot._save_soft_ban_notice(-100, 42, 88, clock.now - 5)
    context.bot.edit_message_text.side_effect = BadRequest("Message is not modified")
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    await enforce(update, context)
    await asyncio.gather(*tasks)
    sleep.assert_awaited_once_with(2)
    context.bot.send_message.assert_not_awaited()
    assert bot._load_soft_ban_notice(-100, 42) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error,delay", [(NetworkError("temporary"), 1), (RetryAfter(4), 4)])
async def test_cleanup_retries_transient_delete_errors(scenario, monkeypatch, error, delay):
    update, context, clock, tasks = scenario
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    await enforce(update, context)
    context.bot.delete_message.side_effect = [error, True]
    await asyncio.gather(*tasks)
    assert sleep.await_args_list == [call(7), call(delay)]
    assert context.bot.delete_message.await_args_list[-2:] == [
        call(chat_id=-100, message_id=88), call(chat_id=-100, message_id=88),
    ]
    assert bot._load_soft_ban_notice(-100, 42) is None


@pytest.mark.asyncio
async def test_permanent_delete_failure_is_logged_and_record_retained(scenario, monkeypatch, caplog):
    update, context, clock, tasks = scenario
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    await enforce(update, context)
    context.bot.delete_message.side_effect = BadRequest("Message can't be deleted")
    await asyncio.gather(*tasks)
    assert "soft_ban_notice_delete failed" in caplog.text
    assert bot._load_soft_ban_notice(-100, 42)[0] == 88
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS == {}


@pytest.mark.asyncio
async def test_concurrent_sends_each_keep_their_own_cleanup(scenario, monkeypatch):
    update, context, clock, tasks = scenario
    send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def sleep(_):
        await release_cleanup.wait()

    async def send_message(**_):
        if not send_started.is_set():
            send_started.set()
            await release_first_send.wait()
            return SimpleNamespace(message_id=88)
        return SimpleNamespace(message_id=89)

    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    context.bot.send_message.side_effect = send_message
    first = asyncio.create_task(enforce(update, context))
    try:
        await send_started.wait()
        clock.now += 3
        await enforce(update, context)
        release_first_send.set()
        await first
        assert len(tasks) == 2
    finally:
        release_first_send.set()
        await first
        release_cleanup.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    for mid in (88, 89):
        assert call(chat_id=-100, message_id=mid) in context.bot.delete_message.await_args_list
    assert bot._load_soft_ban_notice(-100, 42) is None
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS == {}


@pytest.mark.asyncio
async def test_old_cleanup_cannot_remove_new_notice_state(scenario, monkeypatch):
    _, context, clock, _ = scenario
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    bot._save_soft_ban_notice(-100, 42, 89, clock.now)
    newer_task = asyncio.get_running_loop().create_future()
    bot._SOFT_BAN_NOTICE_DELETE_TASKS["-100:42"] = newer_task
    await bot._delete_soft_ban_notice_later(context, -100, 42, 88, 0)
    assert bot._load_soft_ban_notice(-100, 42)[0] == 89
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS["-100:42"] is newer_task
    newer_task.cancel()


@pytest.mark.asyncio
async def test_delete_retry_exhaustion_keeps_record_and_logs(scenario, monkeypatch, caplog):
    update, context, _, tasks = scenario
    sleep = AsyncMock()
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    await enforce(update, context)
    context.bot.delete_message.side_effect = NetworkError("unavailable")
    context.bot.delete_message.reset_mock()
    await asyncio.gather(*tasks)
    assert context.bot.delete_message.await_count == 3
    assert sleep.await_args_list == [call(7), call(1), call(2)]
    assert "soft_ban_notice_delete failed" in caplog.text
    assert bot._load_soft_ban_notice(-100, 42)[0] == 88
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS == {}


@pytest.mark.asyncio
async def test_already_deleted_notice_clears_state(scenario, monkeypatch):
    update, context, _, tasks = scenario
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    await enforce(update, context)
    context.bot.delete_message.side_effect = BadRequest("Message to delete not found")
    await asyncio.gather(*tasks)
    assert bot._load_soft_ban_notice(-100, 42) is None
    assert bot._SOFT_BAN_NOTICE_DELETE_TASKS == {}
