import asyncio
import base64
import importlib
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AI_API_KEY", "test-key")
os.environ.setdefault("AI_BASE_URL", "http://127.0.0.1:8090/v1")
os.environ.setdefault("DS_MODEL", "deepseek-v4-flash")
os.environ.setdefault("GROK_MODEL", "grok-4.6")
os.environ.setdefault("AI_THINKING_MODEL", "deepseek-v4-pro")
os.environ.setdefault("OAI_MODEL", "gpt-5.5")
os.environ.setdefault("IMAGE_MODEL", "gpt-image-2")
os.environ.setdefault("IMAGE_EDIT_MODEL", "gpt-image-2")
os.environ.setdefault("GROK_IMAGE_MODEL", "grok-imagine-image-2.0")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
bot = importlib.import_module("bot")


def test_clean_image_prompt_removes_img_and_edit_prefixes():
    assert bot._clean_image_prompt("img 画一只猫") == "画一只猫"
    assert bot._clean_image_prompt("/img 画一只猫") == "画一只猫"
    assert bot._clean_image_prompt("/gkimg 画一只猫") == "画一只猫"
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


def test_clean_prompt_and_select_text_model_for_gm():
    assert bot.GEMINI_MODEL == "gemini-3.8-flash"
    assert bot._clean_prompt("gm 你好") == "你好"
    assert bot._clean_prompt("/gm 你好") == "你好"
    assert bot._clean_prompt("gm") == ""
    assert bot._clean_prompt("/gm") == ""
    assert bot._select_text_model("gm 你好") == bot.GEMINI_MODEL
    assert bot._select_text_model("/gm 你好") == bot.GEMINI_MODEL
    assert bot._select_text_model("gm") == bot.GEMINI_MODEL
    assert bot._select_text_model("/gm") == bot.GEMINI_MODEL
    assert bot._is_text_ai_prefix("gm 测试") is True
    assert bot._is_text_ai_prefix("/gm 测试") is True
    assert bot._is_text_ai_prefix("gm") is True
    assert bot._is_text_ai_prefix("/gm") is True
    assert bot._is_text_ai_prefix("gmx 测试") is False


def test_clean_prompt_and_select_text_model_for_lm():
    assert bot.LUMO_MODEL == "lumo-2.0-max"
    assert bot._clean_prompt("lm 你好") == "你好"
    assert bot._clean_prompt("/lm 你好") == "你好"
    assert bot._clean_prompt("lm") == ""
    assert bot._clean_prompt("/lm") == ""
    assert bot._select_text_model("lm 你好") == bot.LUMO_MODEL
    assert bot._select_text_model("/lm 你好") == bot.LUMO_MODEL
    assert bot._select_text_model("lm") == bot.LUMO_MODEL
    assert bot._select_text_model("/lm") == bot.LUMO_MODEL
    assert bot._is_text_ai_prefix("lm 测试") is True
    assert bot._is_text_ai_prefix("/lm 测试") is True
    assert bot._is_text_ai_prefix("lm") is True
    assert bot._is_text_ai_prefix("/lm") is True
    assert bot._is_text_ai_prefix("lmx 测试") is False


def test_image_request_detection_handles_prefixes_and_commands():
    assert bot._is_image_generation_request("img 赛博朋克城市") is True
    assert bot._is_image_generation_request("/img 赛博朋克城市") is True
    assert bot._is_image_generation_request("/gkimg 赛博朋克城市") is True
    assert bot._is_image_generation_request("/gkimg@any_bot 赛博朋克城市") is True
    assert bot._is_grok_image_generation_request("/gkimg 赛博朋克城市") is True
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

    assert bot._normalize_fourhoi_slug("abc-000") == "abc-000"
    assert bot._normalize_fourhoi_slug("ABC_000") == "abc-000"
    for invalid in ("ABC", "abc 000", "abc-000;evil", "../ABC000"):
        assert bot._normalize_fourhoi_slug(invalid) is None

    title, cover_url, actresses = bot._r18dev_cover_from_payload(
        {
            "title": "<unsafe & title>",
            "actresses": [{"name": "Yui Hatano"}, {"name": "  "}, {"name": "Yui Hatano"}],
            "images": {
                "jacket_image": {
                    "large2": "https://pics.dmm.co.jp/digital/video/demo/demopl.jpg"
                }
            },
        }
    )
    assert title == "<unsafe & title>"
    assert cover_url.endswith("demopl.jpg")
    assert actresses == ["Yui Hatano"]
    assert "&lt;unsafe &amp; title&gt;" in bot._format_av_cover_caption(
        "ABC000", title
    )
    assert "Fourhoi" in bot._format_av_cover_caption("ABC000", title)
    assert "R18.dev" in bot._format_av_cover_caption("ABC000", title, "R18.dev")
    caption_with_actress = bot._format_av_cover_caption(
        "ABC000", title, "R18.dev", ["Yui Hatano", "<evil>"]
    )
    assert "主演：Yui Hatano、&lt;evil&gt;" in caption_with_actress

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

    assert bot._validate_fourhoi_cover_url(
        "https://fourhoi.com/abc-000/cover.jpg"
    ) == "https://fourhoi.com/abc-000/cover.jpg"
    for unsafe_url in (
        "http://fourhoi.com/abc-000/cover.jpg",
        "https://evil.example/abc-000/cover.jpg",
        "https://fourhoi.com/abc-000/cover.jpg?x=1",
        "https://fourhoi.com/abc-000/poster.jpg",
        "https://user@fourhoi.com/abc-000/cover.jpg",
    ):
        try:
            bot._validate_fourhoi_cover_url(unsafe_url)
        except bot.FourhoiError:
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
        assert text == "🖼 正在查询封面..."
        return Status()

    async def fake_lookup_fourhoi(slug):
        assert slug == "abc-000"
        return "", "https://fourhoi.com/abc-000/cover.jpg"

    async def fake_download_fourhoi(cover_url):
        assert cover_url.endswith("cover.jpg")
        return b"jpeg-bytes"

    async def fake_lookup_r18dev(dvd_id):
        # Fourhoi hit: R18.dev is probed only for caption enrichment, and it
        # may fail without taking the delivery down.
        assert dvd_id == "ABC000"
        raise bot.R18DevNotFoundError("r18dev has no jacket for abc-000")

    async def fake_download_r18dev(cover_url):
        raise AssertionError("fourhoi success must not download from r18dev")

    async def fake_reply_photo(*, photo, caption, parse_mode, **kwargs):
        assert photo.name == "cover.jpg"
        assert photo.read() == b"jpeg-bytes"
        assert "&lt;unsafe &amp; title&gt;" in caption or "Fourhoi" in caption
        assert "主演：" not in caption  # enrichment failed → no actress row
        assert parse_mode == bot.ParseMode.HTML
        return SimpleNamespace(message_id=102)

    async def fake_delete_message(**kwargs):
        calls["deleted"].append(kwargs)

    def fake_schedule(context, chat_id, *messages):
        calls["cleanup"].append((chat_id, [getattr(m, "message_id", None) for m in messages]))

    monkeypatch.setattr(bot, "_reply_text_and_track", fake_reply_text)
    monkeypatch.setattr(bot, "_lookup_fourhoi_cover", fake_lookup_fourhoi)
    monkeypatch.setattr(bot, "_download_fourhoi_cover", fake_download_fourhoi)
    monkeypatch.setattr(bot, "_lookup_r18dev_cover", fake_lookup_r18dev)
    monkeypatch.setattr(bot, "_download_r18dev_cover", fake_download_r18dev)
    monkeypatch.setattr(bot, "_schedule_av_cleanup", fake_schedule)

    msg = SimpleNamespace(message_id=100, reply_photo=fake_reply_photo)
    chat = SimpleNamespace(id=-100123)
    context = SimpleNamespace(bot=SimpleNamespace(delete_message=fake_delete_message))
    asyncio.run(bot._av_cover_cmd(msg, chat, context, "abc-000"))

    assert calls["deleted"] == [{"chat_id": -100123, "message_id": 101}]
    assert calls["cleanup"] == [(-100123, [100, 101, 102])]


def test_av_cover_command_falls_back_to_r18dev_when_fourhoi_misses(monkeypatch):
    calls = {"deleted": [], "cleanup": []}

    class Status:
        chat_id = -100123
        message_id = 101

        async def edit_text(self, text, **kwargs):
            raise AssertionError(f"unexpected error status: {text}")

    async def fake_reply_text(msg, text, **kwargs):
        assert text == "🖼 正在查询封面..."
        return Status()

    async def fake_lookup_fourhoi(slug):
        raise bot.FourhoiNotFoundError("fourhoi has no cover for abc-000")

    async def fake_download_fourhoi(cover_url):
        raise AssertionError("fallback should not download from fourhoi")

    async def fake_lookup_r18dev(dvd_id):
        assert dvd_id == "ABC000"
        return (
            "<unsafe & title>",
            "https://pics.dmm.co.jp/digital/video/demo/demopl.jpg",
            ["Yui Hatano"],
        )

    async def fake_download_r18dev(cover_url):
        assert cover_url.endswith("demopl.jpg")
        return b"jpeg-bytes"

    async def fake_reply_photo(*, photo, caption, parse_mode, **kwargs):
        assert photo.name == "cover.jpg"
        assert photo.read() == b"jpeg-bytes"
        assert "R18.dev" in caption
        assert "&lt;unsafe &amp; title&gt;" in caption
        assert parse_mode == bot.ParseMode.HTML
        return SimpleNamespace(message_id=102)

    async def fake_delete_message(**kwargs):
        calls["deleted"].append(kwargs)

    def fake_schedule(context, chat_id, *messages):
        calls["cleanup"].append((chat_id, [getattr(m, "message_id", None) for m in messages]))

    monkeypatch.setattr(bot, "_reply_text_and_track", fake_reply_text)
    monkeypatch.setattr(bot, "_lookup_fourhoi_cover", fake_lookup_fourhoi)
    monkeypatch.setattr(bot, "_download_fourhoi_cover", fake_download_fourhoi)
    monkeypatch.setattr(bot, "_lookup_r18dev_cover", fake_lookup_r18dev)
    monkeypatch.setattr(bot, "_download_r18dev_cover", fake_download_r18dev)
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


def test_parse_bing_image_candidates_prefers_originals_and_dedupes():
    html = (
        '<a m="{&quot;murl&quot;:&quot;https://cdn.example.com/a.jpg&quot;,&quot;turl&quot;:&quot;https://t1.example.com/a.jpg&quot;}"></a>'
        '<a m="{&quot;murl&quot;:&quot;https://cdn.example.com/a.jpg&quot;}"></a>'
        '<a m="{&quot;murl&quot;:&quot;https://img.example.com/b.jpg?a=1&amp;b=2&quot;}"></a>'
        '<a m="{&quot;murl&quot;:&quot;ftp://bad.example/c.jpg&quot;}"></a>'
        '<a m="{&quot;turl&quot;:&quot;https://cdn.example.com/a.jpg&quot;}"></a>'
        '<a m="{&quot;turl&quot;:&quot;https://t2.example.com/x.jpg&quot;}"></a>'
    )
    assert bot._parse_bing_image_candidates(html) == [
        "https://cdn.example.com/a.jpg",
        "https://img.example.com/b.jpg?a=1&b=2",
        "https://t1.example.com/a.jpg",
        "https://t2.example.com/x.jpg",
    ]
    assert bot._parse_bing_image_candidates("") == []
    assert bot._parse_bing_image_candidates(None) == []


def test_find_reference_image_skips_failed_candidates(monkeypatch):
    async def fake_search(query):
        assert query == "柴犬"
        return [
            "https://bad.example/1.png",
            "https://ok.example/2.jpg",
            "https://never.example/3.jpg",
        ]

    calls = []

    async def fake_download(url):
        calls.append(url)
        if "ok" not in url:
            raise RuntimeError("HTTP 403")
        return b"jpeg-bytes"

    monkeypatch.setattr(bot, "_search_reference_images", fake_search)
    monkeypatch.setattr(bot, "_download_reference_image", fake_download)

    assert asyncio.run(bot._find_reference_image("柴犬")) == b"jpeg-bytes"
    assert calls == ["https://bad.example/1.png", "https://ok.example/2.jpg"]


def test_find_reference_image_falls_back_when_search_or_downloads_fail(monkeypatch):
    async def failing_search(query):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(bot, "_search_reference_images", failing_search)
    assert asyncio.run(bot._find_reference_image("x")) is None

    async def empty_search(query):
        return []

    monkeypatch.setattr(bot, "_search_reference_images", empty_search)
    assert asyncio.run(bot._find_reference_image("x")) is None

    async def some_search(query):
        return ["https://bad.example/1.png"]

    async def failing_download(url):
        raise RuntimeError("nope")

    monkeypatch.setattr(bot, "_search_reference_images", some_search)
    monkeypatch.setattr(bot, "_download_reference_image", failing_download)
    assert asyncio.run(bot._find_reference_image("x")) is None


def test_reference_generation_and_edit_use_shared_edits_pipeline(monkeypatch):
    captured = []

    async def fake_post(prompt, image_bytes, *, model, log_tag):
        captured.append((prompt, image_bytes, model, log_tag))
        return b"final-image"

    monkeypatch.setattr(bot, "_post_image_edits", fake_post)

    assert asyncio.run(
        bot._generate_image_with_reference("画一只猫", b"ref", model="grok-image")
    ) == b"final-image"
    assert captured[-1] == (
        bot.IMAGE_REFERENCE_GUIDE + "画一只猫",
        b"ref",
        "grok-image",
        "image reference",
    )

    assert asyncio.run(bot._edit_image("改成夜景", b"src")) == b"final-image"
    assert captured[-1] == (
        bot.IMAGE_EDIT_GUIDE + "改成夜景",
        b"src",
        bot.IMAGE_EDIT_MODEL,
        "image edit",
    )


def test_post_image_edits_builds_multipart_request(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, files=None, data=None):
            captured.update(url=url, headers=headers, files=files, data=data)
            return FakeResponse()

    monkeypatch.setattr(bot, "httpx", SimpleNamespace(AsyncClient=FakeClient))
    monkeypatch.setattr(bot, "AI_BASE_URL", "https://api.example/v1")

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"payload"
    out = asyncio.run(bot._post_image_edits("hello", png_bytes, model="m1", log_tag="tag"))
    assert out == b"png-bytes"
    assert captured["url"] == "https://api.example/v1/images/edits"
    assert captured["data"] == {"model": "m1", "prompt": "hello", "response_format": "b64_json"}
    name, blob, mime = captured["files"]["image"]
    assert name == "input.png" and blob == png_bytes and mime == "image/png"

    jpg_bytes = b"\xff\xd8\xff\xe0" + b"payload"
    asyncio.run(bot._post_image_edits("hi", jpg_bytes, model="m2", log_tag="tag"))
    assert captured["files"]["image"][0] == "input.jpg"
    assert captured["files"]["image"][2] == "image/jpeg"


def test_generate_image_uses_requested_model(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(bot.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(bot, "AI_BASE_URL", "https://api.example/v1")

    out = asyncio.run(
        bot._generate_image("画一只猫", model="grok-imagine-image-2.0")
    )

    assert out == b"png-bytes"
    assert captured["url"] == "https://api.example/v1/images/generations"
    assert captured["json"] == {
        "model": "grok-imagine-image-2.0",
        "prompt": "画一只猫",
        "n": 1,
        "size": "1024x1024",
    }


def test_generate_grok_image_uses_chat_api_and_rewrites_loopback_media(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, *, payload=None, content=b""):
            self.status_code = 200
            self._payload = payload
            self.content = content
            self.text = ""

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(("post", url, headers, json))
            return FakeResponse(
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": "![image](http://127.0.0.1:8000/v1/media/images/img_test)"
                            }
                        }
                    ]
                }
            )

        async def get(self, url):
            calls.append(("get", url))
            return FakeResponse(content=b"grok-image")

    monkeypatch.setattr(bot.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(bot, "AI_BASE_URL", "https://api.example/v1")
    monkeypatch.setattr(bot, "GROK_MEDIA_BASE_URL", "http://grok2api:8000")

    out = asyncio.run(bot._generate_grok_image("画一只猫"))

    assert out == b"grok-image"
    assert calls[1][0:2] == ("post", "https://api.example/v1/chat/completions")
    assert calls[1][3] == {
        "model": "grok-imagine-image-2.0",
        "messages": [{"role": "user", "content": "画一只猫"}],
        "stream": False,
    }
    assert calls[2] == (
        "get",
        "http://grok2api:8000/v1/media/images/img_test",
    )


def test_create_grok_video_task_uses_axonhub_contract(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"request_id": "video-test"}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(bot.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(bot, "AI_BASE_URL", "https://api.example/v1")
    monkeypatch.setattr(bot, "VIDEO_MODEL", "grok-imagine-video-1.5")

    result = asyncio.run(bot._create_video_task("让云层缓慢移动"))

    assert result == "video-test"
    assert captured["url"] == "https://api.example/v1/videos/generations"
    assert captured["json"] == {
        "model": "grok-imagine-video-1.5",
        "prompt": "让云层缓慢移动",
        "duration": bot.VIDEO_DURATION,
        "aspect_ratio": bot.VIDEO_ASPECT_RATIO,
        "resolution": bot.VIDEO_RESOLUTION,
    }


JAVDB_ACTOR_SEARCH_HTML = """
<div class="box actor-box">
  <a href="/actors/Av2e" title="三上悠亜, 三上悠亞, 鬼头桃菜">
    <figure class="image"><img class="avatar" src="https://c0.jdbstatic.com/avatars/av2e.jpg" /></figure>
    <strong>三上悠亜</strong>
  </a>
</div>
<div class="box actor-box">
  <a href="/actors/xxxx" title="三上悠亜(無碼)">
    <figure class="image"><img class="avatar" src="https://c0.jdbstatic.com/avatars/xxxx.jpg" /></figure>
    <strong>三上悠亜</strong>
    <span class="badge">無碼</span>
  </a>
</div>
"""

JAVDB_ACTOR_PAGE_HTML = """
<div class="video-title"><strong>OFJE-712</strong> some title</div>
<div class="video-title"><strong>OFJE-659</strong> another</div>
<div class="video-title"><strong>OFJE-712</strong> duplicate</div>
"""

JAVDATABASE_SEARCH_HTML = """
<a class="nav-link" href="https://www.javdatabase.com/idols/yui-hatano/">Yui Hatano</a>
<a href="https://www.javdatabase.com/idols/yoko-nagisano/">Yoko Nagisano</a>
"""

JAVDATABASE_IDOL_HTML = """
<div class="facetwp-template">
  <div class="col-md-3">
    <div class="movie-cover-thumb"><a href="https://www.javdatabase.com/movies/mird-277/"><img src="https://www.javdatabase.com/covers/thumb/mi/mird00277ps.webp" /></a></div>
    <p class="display-6 pcard"><a href="https://www.javdatabase.com/movies/mird-277/" class="cut-text">MIRD-277 </a></p>
    <div class="mt-auto"><a href="https://www.javdatabase.com/movies/mird-277/" class="cut-text">MOODYZ Bus Tour Title Here</a><br/>2026-03-13</div>
  </div>
  <div class="col-md-3">
    <div class="movie-cover-thumb"><a href="https://www.javdatabase.com/movies/pred-884/"><img src="https://www.javdatabase.com/covers/thumb/pr/pred00884ps.webp" /></a></div>
    <p class="display-6 pcard"><a href="https://www.javdatabase.com/movies/pred-884/" class="cut-text">PRED-884 </a></p>
    <div class="mt-auto"><a href="https://www.javdatabase.com/movies/pred-884/" class="cut-text">Graduation Title</a><br/>2026-08-14</div>
  </div>
</div>
"""


def test_parse_javdb_actor_search_prefers_exact_and_censored():
    assert bot._parse_javdb_actor_search(JAVDB_ACTOR_SEARCH_HTML, "三上悠亜") == (
        "/actors/Av2e",
        "三上悠亜",
    )
    # 无码 box 有 badge，但匹配完全时仍应选中前者（censored 优先）
    assert bot._parse_javdb_actor_search("", "三上悠亜") is None


def test_parse_javdb_actor_codes_dedupes_and_limits():
    assert bot._parse_javdb_actor_codes(JAVDB_ACTOR_PAGE_HTML, 5) == [
        "OFJE-712",
        "OFJE-659",
    ]
    assert bot._parse_javdb_actor_codes("", 5) == []


def test_parse_javdatabase_actor_slug():
    assert bot._parse_javdatabase_actor_slug(JAVDATABASE_SEARCH_HTML, "Yui Hatano") == "yui-hatano"
    assert bot._parse_javdatabase_actor_slug(JAVDATABASE_SEARCH_HTML, "Other") == "yui-hatano"
    assert bot._parse_javdatabase_actor_slug("", "Yui Hatano") is None


def test_parse_javdatabase_top_movies():
    movies = bot._parse_javdatabase_top_movies(JAVDATABASE_IDOL_HTML, 10)
    assert len(movies) == 2
    assert movies[0]["code"] == "MIRD-277"
    assert movies[0]["title"] == "MOODYZ Bus Tour Title Here"
    assert movies[0]["date"] == "2026-03-13"
    assert movies[0]["cover"].endswith("mird00277ps.webp")
    assert movies[1]["code"] == "PRED-884"
    assert movies[1]["date"] == "2026-08-14"


def test_format_av_actor_top_escapes_and_orders():
    movies = [
        {"code": "MIRD-277", "title": "MOODYZ <unsafe>", "date": "2026-03-13", "actor_display": "三上悠亜", "actor_romaji": "Yua Mikami"},
        {"code": "PRED-884", "title": "PRED-884", "date": "", "actor_display": "三上悠亜", "actor_romaji": "Yua Mikami"},
    ]
    text = bot._format_av_actor_top(movies)
    assert text.startswith("🎬 三上悠亜 最热门作品")
    assert "1. <code>MIRD-277</code>（2026-03-13） MOODYZ &lt;unsafe&gt;" in text
    assert "2. <code>PRED-884</code>" in text
    assert bot._format_av_actor_top([]) == "🔎 没有找到该演员的作品。"


def test_av_actor_route_handles_cjk_name(monkeypatch):
    seen = []

    async def fake_actor(msg, chat, context, name):
        seen.append((msg.text, chat.id, name))

    async def fake_cover(msg, chat, context, code):
        raise AssertionError("CJK name must not route to cover lookup")

    monkeypatch.setattr(bot, "_is_allowed_chat", lambda chat: True)
    monkeypatch.setattr(bot, "_is_allowed_topic", lambda msg: True)
    monkeypatch.setattr(bot, "_av_actor_cmd", fake_actor)
    monkeypatch.setattr(bot, "_av_cover_cmd", fake_cover)

    msg = SimpleNamespace(
        text="/av 三上悠亜",
        caption=None,
        message_id=10,
        from_user=SimpleNamespace(id=1),
        reply_to_message=None,
    )
    chat = SimpleNamespace(id=-100123, type=bot.ChatType.SUPERGROUP)
    asyncio.run(
        bot.av_cmd(
            SimpleNamespace(effective_message=msg, effective_chat=chat),
            SimpleNamespace(),
        )
    )
    assert seen == [("/av 三上悠亜", -100123, "三上悠亜")]


def test_av_actor_lookup_pipeline_uses_three_sources(monkeypatch):
    """javdb 查演员页 → r18.dev 桥接英文名 → javdatabase 收藏排序。"""
    calls = {"javdb": [], "r18": [], "jd": []}

    async def fake_lookup_javdb_actor(name):
        calls["javdb"].append(name)
        return "/actors/Av2e", "三上悠亜"

    async def fake_lookup_javdb_actor_codes(path, limit):
        calls["javdb"].append(path)
        return ["OFJE-712", "OFJE-659"]

    async def fake_lookup_r18dev(dvd_id):
        calls["r18"].append(dvd_id)
        return "", "https://pics.dmm.co.jp/digital/video/demo/demopl.jpg", ["Yua Mikami"]

    async def fake_lookup_javdatabase_actor_slug(romaji):
        calls["jd"].append(romaji)
        assert romaji == "Yua Mikami"
        return "yua-mikami"

    async def fake_lookup_javdatabase_top_movies(slug, limit):
        calls["jd"].append(slug)
        return [
            {"code": "MIRD-277", "title": "Bus Tour", "date": "2026-03-13", "cover": ""},
            {"code": "PRED-884", "title": "Graduation", "date": "2026-08-14", "cover": ""},
        ]

    monkeypatch.setattr(bot, "_lookup_javdb_actor", fake_lookup_javdb_actor)
    monkeypatch.setattr(bot, "_lookup_javdb_actor_codes", fake_lookup_javdb_actor_codes)
    monkeypatch.setattr(bot, "_lookup_r18dev_cover", fake_lookup_r18dev)
    monkeypatch.setattr(bot, "_lookup_javdatabase_actor_slug", fake_lookup_javdatabase_actor_slug)
    monkeypatch.setattr(bot, "_lookup_javdatabase_top_movies", fake_lookup_javdatabase_top_movies)

    movies = asyncio.run(bot._lookup_actor_top_videos("三上悠亜"))
    assert len(movies) == 2
    assert movies[0]["actor_display"] == "三上悠亜"
    assert movies[0]["actor_romaji"] == "Yua Mikami"
    assert calls == {
        "javdb": ["三上悠亜", "/actors/Av2e"],
        "r18": ["OFJE-712", "OFJE-659"],
        "jd": ["Yua Mikami", "yua-mikami"],
    }
