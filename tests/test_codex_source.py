"""Regression tests for complete source posts and source-only alert links."""
import json
from types import SimpleNamespace

import httpx
import pytest

from test_codex_reset import bot

SOURCE = "https://x.com/thsottiaux/status/2107676072871600470"
FULL = "We shipped four things.\n\nTherefore ... the reset has been processed. Enjoy!"


def page(text=FULL, canonical=SOURCE, body_source=SOURCE):
    return (
        f'<link rel="canonical" href="{canonical}">'
        '<script>const data={bodyText:' + json.dumps(text)
        + ',canonicalPath:' + json.dumps("/thsottiaux/status/2107676072871600470")
        + ',canonicalUrl:' + json.dumps(body_source) + '};</script>'
    )


def test_complete_post_parser_uses_focal_body_not_quoted_text():
    html = page() + '<script>const quote={text:"Unrelated quoted post"};</script>'
    assert bot._parse_codex_source_post(html, SOURCE) == FULL


@pytest.mark.parametrize("html", [
    page(canonical="https://x.com/thsottiaux/status/2107575657014468879"),
    page(body_source="https://x.com/thsottiaux/status/2107575657014468879"),
    page(text="This is truncated…"),
    '<html>Log in to X</html>',
])
def test_complete_post_parser_rejects_wrong_or_incomplete_page(html):
    with pytest.raises(ValueError):
        bot._parse_codex_source_post(html, SOURCE)


@pytest.mark.asyncio
async def test_source_fetch_retries_and_returns_complete_text(monkeypatch):
    calls = []
    delays = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(503 if len(calls) == 1 else 200, text=page())

    real_client = httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    assert await bot._fetch_codex_source_post(SOURCE) == FULL
    assert calls == [SOURCE, SOURCE]
    assert delays == [2]


@pytest.mark.asyncio
async def test_source_fetch_failure_does_not_fall_back_to_truncated_api(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="Log in")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(bot.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)
    ))

    async def sleep(delay):
        pass

    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    with pytest.raises(RuntimeError):
        await bot._prepare_codex_reset_alert({"link": SOURCE, "description": "broken"})
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_preparation_translates_full_source_without_body_urls(monkeypatch):
    async def fetch(url):
        assert url == SOURCE
        return FULL + " https://t.co/quote"

    async def translate(text):
        assert text == FULL
        return "因此，重置已完成。祝使用愉快！ https://t.co/quote"

    monkeypatch.setattr(bot, "_fetch_codex_source_post", fetch)
    monkeypatch.setattr(bot, "_translate_codex_reset_text", translate)
    original = {"link": SOURCE, "description": "Therefore ... the reset has been https://t.co/quote"}
    result = await bot._prepare_codex_reset_alert(original)
    assert result["description"] == "因此，重置已完成。祝使用愉快！"
    assert result["link"] == SOURCE
    assert original["description"].endswith("https://t.co/quote")
    formatted = bot._format_codex_reset_alert(result)
    assert formatted.count("https://") == 1
    assert SOURCE in formatted


@pytest.mark.asyncio
async def test_translation_and_formatting_do_not_silently_cut_at_1200(monkeypatch):
    text = "完整正文。" * 260

    async def ask(*args, **kwargs):
        return text

    monkeypatch.setattr(bot, "_ask_ai_once", ask)
    assert await bot._translate_codex_reset_text("Full announcement") == text
    formatted = bot._format_codex_reset_alert({"description": text, "link": SOURCE})
    assert text in formatted


def test_formatter_removes_body_urls_but_keeps_source():
    formatted = bot._format_codex_reset_alert({
        "description": "重置已完成。 https://t.co/quote", "link": SOURCE
    })
    assert "t.co" not in formatted
    assert SOURCE in formatted


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/status/1234567890", "https://x.com.evil.test/a/status/1234567890", "https://x.com/thsottiaux/status/1234567890?x=1"])
async def test_source_fetch_rejects_untrusted_urls(url):
    with pytest.raises(ValueError):
        await bot._fetch_codex_source_post(url)


@pytest.mark.asyncio
async def test_publish_validates_before_unpin_or_send(monkeypatch):
    async def prepare(item):
        raise RuntimeError("source incomplete")

    monkeypatch.setattr(bot, "_prepare_codex_reset_alert", prepare)
    with pytest.raises(RuntimeError, match="source incomplete"):
        await bot._publish_codex_reset_alert(SimpleNamespace(bot=object()), {"link": SOURCE})


@pytest.mark.asyncio
async def test_preparation_rejects_overlong_alert_before_publish(monkeypatch):
    async def translate(text):
        return "完" * 4096

    async def fetch(url):
        return FULL

    monkeypatch.setattr(bot, "_translate_codex_reset_text", translate)
    monkeypatch.setattr(bot, "_fetch_codex_source_post", fetch)
    with pytest.raises(ValueError, match="Telegram message limit"):
        await bot._prepare_codex_reset_alert({"link": SOURCE, "description": "source"})


@pytest.mark.asyncio
async def test_failed_source_publish_clears_etag_for_next_poll(monkeypatch):
    states = []
    monkeypatch.setattr(bot, "CODEX_RESET_ENABLED", True)
    monkeypatch.setattr(bot, "PIN_TARGET_CHAT_ID", -100123)
    monkeypatch.setattr(bot, "PIN_TARGET_TOPIC_ID", "1")
    monkeypatch.setattr(bot, "_load_codex_state", lambda key: "1")
    monkeypatch.setattr(bot, "_save_codex_state", lambda key, value: states.append((key, value)))
    monkeypatch.setattr(bot, "_record_codex_observation", lambda *args, **kwargs: True)
    monkeypatch.setattr(bot, "_has_active_codex_reset_alert", lambda: False)

    async def due(application):
        pass

    async def api():
        return {"data": {"latest_reset": {"id": "2107676072871600470", "source": {"url": SOURCE}}}}

    async def publish(application, item):
        raise RuntimeError("source unavailable")

    async def stop(delay):
        raise bot.asyncio.CancelledError()

    monkeypatch.setattr(bot, "_check_due_codex_reset_alerts", due)
    monkeypatch.setattr(bot, "_fetch_codex_reset_api", api)
    monkeypatch.setattr(bot, "_publish_codex_reset_alert", publish)
    monkeypatch.setattr(bot.asyncio, "sleep", stop)
    with pytest.raises(bot.asyncio.CancelledError):
        await bot._codex_reset_scheduler_loop(object())
    assert states == [(bot.CODEX_RESET_API_ETAG_STATE_KEY, "")]


def test_url_removal_preserves_adjacent_chinese_sentences():
    assert bot._strip_codex_body_urls(
        "详情：https://t.co/quote。重置已完成。祝使用愉快！"
    ) == "详情：。重置已完成。祝使用愉快！"


@pytest.mark.asyncio
async def test_missing_source_cannot_publish_api_preview():
    with pytest.raises(ValueError, match="HTTPS X post"):
        await bot._prepare_codex_reset_alert({"description": "API short preview"})


@pytest.mark.asyncio
async def test_active_pin_deferral_refetches_and_publishes_next_poll(monkeypatch):
    state = {bot.CODEX_RESET_API_ETAG_STATE_KEY: ""}
    polls = []
    published = []
    monkeypatch.setattr(bot, "CODEX_RESET_ENABLED", True)
    monkeypatch.setattr(bot, "PIN_TARGET_CHAT_ID", -100123)
    monkeypatch.setattr(bot, "PIN_TARGET_TOPIC_ID", "1")
    monkeypatch.setattr(bot, "_load_codex_state", lambda key: "1")
    monkeypatch.setattr(bot, "_save_codex_state", lambda key, value: state.update({key: value}))
    monkeypatch.setattr(bot, "_record_codex_observation", lambda *args, **kwargs: True)
    monkeypatch.setattr(bot, "_has_active_codex_reset_alert", lambda: len(polls) == 1)

    async def due(application):
        pass

    async def api():
        polls.append(state[bot.CODEX_RESET_API_ETAG_STATE_KEY])
        if polls[-1]:
            return None  # Emulate HTTP 304 if the old ETag was not cleared.
        state[bot.CODEX_RESET_API_ETAG_STATE_KEY] = "v1"
        return {"data": {"latest_reset": {"id": "2107676072871600470", "source": {"url": SOURCE}}}}

    async def publish(application, item):
        published.append(item["source_event_id"])

    async def stop(delay):
        if len(polls) >= 2:
            raise bot.asyncio.CancelledError()

    monkeypatch.setattr(bot, "_check_due_codex_reset_alerts", due)
    monkeypatch.setattr(bot, "_fetch_codex_reset_api", api)
    monkeypatch.setattr(bot, "_publish_codex_reset_alert", publish)
    monkeypatch.setattr(bot.asyncio, "sleep", stop)
    with pytest.raises(bot.asyncio.CancelledError):
        await bot._codex_reset_scheduler_loop(object())
    assert polls == ["", ""]
    assert published == ["2107676072871600470"]


