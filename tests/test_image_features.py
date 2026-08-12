import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AI_API_KEY", "test-key")
os.environ.setdefault("AI_BASE_URL", "http://127.0.0.1:8090/v1")
os.environ.setdefault("DS_MODEL", "deepseek-v4-flash")
os.environ.setdefault("GROK_MODEL", "grok-4.5")
os.environ.setdefault("AI_THINKING_MODEL", "deepseek-v4-pro")
os.environ.setdefault("OAI_MODEL", "gpt-5.5")
os.environ.setdefault("IMAGE_MODEL", "gpt-image-2")
os.environ.setdefault("IMAGE_EDIT_MODEL", "gpt-image-2")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
bot = importlib.import_module("bot")


def test_clean_image_prompt_removes_img_and_edit_prefixes():
    assert bot._clean_image_prompt("img 画一只猫") == "画一只猫"
    assert bot._clean_image_prompt("/img 画一只猫") == "画一只猫"
    assert bot._clean_image_prompt("edit 改成夜景") == "改成夜景"
    assert bot._clean_image_prompt("/edit 改成夜景") == "改成夜景"


def test_clean_prompt_and_select_text_model_for_ds_and_gk():
    assert bot._clean_prompt("ds 你好") == "你好"
    assert bot._clean_prompt("/ds 你好") == "你好"
    assert bot._clean_prompt("gk 你好") == "你好"
    assert bot._clean_prompt("/gk 你好") == "你好"
    assert bot._select_text_model("ds 你好") == bot.DS_MODEL
    assert bot._select_text_model("/ds 你好") == bot.DS_MODEL
    assert bot._select_text_model("gk 你好") == bot.GROK_MODEL
    assert bot._select_text_model("/gk 你好") == bot.GROK_MODEL
    assert bot._is_text_ai_prefix("gk 测试") is True
    assert bot._is_text_ai_prefix("/gk 测试") is True


def test_image_request_detection_handles_prefixes_and_commands():
    assert bot._is_image_generation_request("img 赛博朋克城市") is True
    assert bot._is_image_generation_request("/img 赛博朋克城市") is True
    assert bot._is_image_generation_request("ds img 赛博朋克城市") is False
    assert bot._is_image_edit_request("edit 改成水彩") is True
    assert bot._is_image_edit_request("/edit 改成水彩") is True
    assert bot._is_image_edit_request("ds edit 改成水彩") is False


def test_reply_to_bot_photo_is_image_edit_target():
    bot.BOT_ID = 123
    replied = SimpleNamespace(
        from_user=SimpleNamespace(id=123, username="any_bot"),
        photo=[SimpleNamespace(file_id="file-1")],
        document=None,
    )
    msg = SimpleNamespace(reply_to_message=replied)
    assert bot._reply_image_target(msg) == "file-1"


def test_user_uploaded_photo_with_edit_caption_is_image_edit_target():
    msg = SimpleNamespace(
        reply_to_message=None,
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
        document=None,
    )
    assert bot._message_image_target(msg) == "large"


def test_image_prompt_web_search_decision_for_current_or_specific_subjects():
    assert bot._should_web_search_image_prompt("img 画一下最新的 Tesla Roadster 外观") is True
    assert bot._should_web_search_image_prompt("img 画一张 iPhone 18 发布会海报") is True
    assert bot._should_web_search_image_prompt("img 画一只白猫在月光下喝茶") is False
    assert bot._should_web_search_image_prompt("img 梵高风格的向日葵") is False


def test_av_cover_parsing_and_r18dev_dmm_allow_list(monkeypatch):
    monkeypatch.setattr(bot, "BOT_USERNAME", "any_bot")

    assert bot._av_cover_argument(
        SimpleNamespace(text="/av abc-000", caption=None)
    ) == (True, "abc-000")
    assert bot._av_cover_argument(
        SimpleNamespace(text="/av@any_bot ABC000", caption=None)
    ) == (True, "ABC000")
    assert bot._av_cover_argument(
        SimpleNamespace(text="/av@other_bot ABC000", caption=None)
    ) == (False, None)
    assert bot._av_cover_argument(
        SimpleNamespace(text=None, caption="/av ABC000")
    ) == (True, "ABC000")

    assert bot._normalize_av_cover_code("abc-000") == "ABC000"
    assert bot._normalize_av_cover_code("ABC_000") == "ABC000"
    for invalid in ("ABC", "abc 000", "abc-000;evil", "../ABC000"):
        assert bot._normalize_av_cover_code(invalid) is None

    title, cover_url = bot._r18dev_cover_from_payload(
        {
            "title": "<unsafe & title>",
            "images": {
                "jacket_image": {
                    "large2": "https://pics.dmm.co.jp/digital/video/demo/demopl.jpg"
                }
            },
        }
    )
    assert title == "<unsafe & title>"
    assert cover_url.endswith("demopl.jpg")
    assert "&lt;unsafe &amp; title&gt;" in bot._format_r18dev_cover_caption(
        "ABC000", title
    )

    for unsafe_url in (
        "http://pics.dmm.co.jp/digital/video/demo/demopl.jpg",
        "https://evil.example/digital/video/demo/demopl.jpg",
        "https://pics.dmm.co.jp/not-digital/video/demopl.jpg",
        "https://pics.dmm.co.jp/digital/video/demo/demopl.jpg?redirect=evil",
        "https://user@pics.dmm.co.jp/digital/video/demo/demopl.jpg",
    ):
        try:
            bot._r18dev_cover_from_payload(
                {"images": {"jacket_image": {"large2": unsafe_url}}}
            )
        except bot.R18DevError:
            pass
        else:
            raise AssertionError(unsafe_url)


def test_av_code_route_does_not_access_replied_image(monkeypatch):
    seen = []

    async def fake_cover(msg, chat, context, code):
        seen.append((msg.text, msg.caption, chat.id, code))

    def fail_if_image_is_read(msg):
        raise AssertionError("code lookup must not inspect a replied image")

    monkeypatch.setattr(bot, "_is_allowed_chat", lambda chat: True)
    monkeypatch.setattr(bot, "_is_allowed_topic", lambda msg: True)
    monkeypatch.setattr(bot, "_av_cover_cmd", fake_cover)
    monkeypatch.setattr(bot, "_avscan_image_source", fail_if_image_is_read)

    chat = SimpleNamespace(id=-100123, type=bot.ChatType.SUPERGROUP)
    for text, caption in (("/av ABC-000", None), (None, "/av ABC-000")):
        msg = SimpleNamespace(
            text=text,
            caption=caption,
            message_id=10,
            from_user=SimpleNamespace(id=1),
            reply_to_message=SimpleNamespace(
                photo=[SimpleNamespace(file_id="unrelated")]
            ),
        )
        asyncio.run(
            bot.av_cmd(
                SimpleNamespace(effective_message=msg, effective_chat=chat),
                SimpleNamespace(),
            )
        )
    assert seen == [
        ("/av ABC-000", None, -100123, "ABC-000"),
        (None, "/av ABC-000", -100123, "ABC-000"),
    ]


def test_av_cover_command_sends_jacket_and_schedules_cleanup(monkeypatch):
    calls = {"deleted": [], "cleanup": []}

    class Status:
        chat_id = -100123
        message_id = 101

        async def edit_text(self, text, **kwargs):
            raise AssertionError(f"unexpected error status: {text}")

    async def fake_reply_text(msg, text, **kwargs):
        assert text == "🖼 正在查询 R18.dev 封面..."
        return Status()

    async def fake_lookup(dvd_id):
        assert dvd_id == "ABC000"
        return "<unsafe & title>", "https://pics.dmm.co.jp/digital/video/demo/demopl.jpg"

    async def fake_download(cover_url):
        assert cover_url.endswith("demopl.jpg")
        return b"jpeg-bytes"

    async def fake_reply_photo(*, photo, caption, parse_mode):
        assert photo.name == "cover.jpg"
        assert photo.read() == b"jpeg-bytes"
        assert "&lt;unsafe &amp; title&gt;" in caption
        assert parse_mode == bot.ParseMode.HTML
        return SimpleNamespace(message_id=102)

    async def fake_delete_message(**kwargs):
        calls["deleted"].append(kwargs)

    def fake_schedule(context, chat_id, *messages):
        calls["cleanup"].append((chat_id, [getattr(m, "message_id", None) for m in messages]))

    monkeypatch.setattr(bot, "_reply_text_and_track", fake_reply_text)
    monkeypatch.setattr(bot, "_lookup_r18dev_cover", fake_lookup)
    monkeypatch.setattr(bot, "_download_r18dev_cover", fake_download)
    monkeypatch.setattr(bot, "_schedule_av_cleanup", fake_schedule)

    msg = SimpleNamespace(message_id=100, reply_photo=fake_reply_photo)
    chat = SimpleNamespace(id=-100123)
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=fake_delete_message))
    asyncio.run(bot._av_cover_cmd(msg, chat, context, "abc-000"))

    assert calls["deleted"] == [{"chat_id": -100123, "message_id": 101}]
    assert calls["cleanup"] == [(-100123, [100, 101, 102])]


def test_plain_av_image_caption_stays_on_avscan_path(monkeypatch):
    calls = {"download": [], "upload": [], "edits": [], "cleanup": []}

    class Status:
        message_id = 101

        async def edit_text(self, text, **kwargs):
            calls["edits"].append((text, kwargs))

    async def fake_reply_text(msg, text, **kwargs):
        assert text == "🔎 AVScan 检索中..."
        return Status()

    async def fake_download_telegram(context, file_id, *, max_bytes):
        calls["download"].append((file_id, max_bytes))
        return b"source"

    async def fake_search(upload):
        calls["upload"].append(upload)
        return {"results": []}

    def fake_source(msg):
        return "image-file", msg

    def fake_schedule(context, chat_id, *messages):
        calls["cleanup"].append((chat_id, [getattr(m, "message_id", None) for m in messages]))

    monkeypatch.setattr(bot, "_is_allowed_chat", lambda chat: True)
    monkeypatch.setattr(bot, "_is_allowed_topic", lambda msg: True)
    monkeypatch.setattr(bot, "_reply_text_and_track", fake_reply_text)
    monkeypatch.setattr(bot, "_avscan_image_source", fake_source)
    monkeypatch.setattr(bot, "_download_telegram_file", fake_download_telegram)
    monkeypatch.setattr(bot, "_prepare_avscan_image", lambda source: b"upload")
    monkeypatch.setattr(bot, "_search_avscan", fake_search)
    monkeypatch.setattr(bot, "_format_avscan_results", lambda payload: "result")
    monkeypatch.setattr(bot, "_schedule_av_cleanup", fake_schedule)

    msg = SimpleNamespace(
        text=None,
        caption="/av",
        message_id=100,
        from_user=SimpleNamespace(id=1),
    )
    chat = SimpleNamespace(id=-100123, type=bot.ChatType.SUPERGROUP)
    asyncio.run(
        bot.av_cmd(
            SimpleNamespace(effective_message=msg, effective_chat=chat),
            SimpleNamespace(),
        )
    )

    assert calls["download"] == [("image-file", bot.AVSCAN_MAX_SOURCE_BYTES)]
    assert calls["upload"] == [b"upload"]
    assert calls["edits"] == [("result", {"parse_mode": bot.ParseMode.HTML, "disable_web_page_preview": True})]
    assert calls["cleanup"] == [(-100123, [100, 100, 101])]


def test_enrich_image_prompt_with_web_context_includes_real_search_data():
    prompt = "画最新的 Tesla Roadster"
    web_context = "WEB_SEARCH_CONTEXT\nResult 1:\nTitle: Tesla Roadster 2026\nURL: https://example.com\nContent: updated body shape and LED strip"
    enriched = bot._enrich_image_prompt_with_web_context(prompt, web_context)
    assert prompt in enriched
    assert "updated body shape" in enriched
    assert "真实参考资料" in enriched
