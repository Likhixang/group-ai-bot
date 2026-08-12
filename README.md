[English](README.md) | [中文](README.zh-CN.md)

# Group AI Bot (Docker Compose)

A Telegram group/channel bot that connects to an OpenAI-compatible API.

## 1. Environment Variables

Copy the template:

```bash
cp .env.example .env
```

Edit `.env` and fill in:

- `TELEGRAM_BOT_TOKEN`
- `AI_BASE_URL`
- `AI_API_KEY`
- (optional) `AI_MODEL`
- (optional) `OAI_MODEL`
- (optional) `IMAGE_MODEL`
- (optional) `IMAGE_EDIT_MODEL`

- (optional) `MEMORY_MAX_TURNS`

- (optional) `SUPER_ADMIN_ID`
- (optional) `MEMORY_DB_PATH`
- (optional) `ALLOWED_GROUP_IDS`
- (optional) `ALLOWED_CHAT_USERNAMES`
- (optional) `ALLOWED_TOPIC_IDS`

- (optional) `TIMED_TOPIC_CHAT_ID`
- (optional) `TIMED_TOPIC_ID`
- (optional) `TIMED_TOPIC_TZ`
- (optional) `TIMED_OPEN_HOUR`
- (optional) `TIMED_CLOSE_HOUR`
- (optional) `PIN_TARGET_CHAT_ID`
- (optional) `PIN_TARGET_TOPIC_ID`
- (optional) `PIN_TZ`
- (optional) `PIN_HOUR`
- (optional) `PIN_MINUTE`
- (optional) `DEFAULT_DAILY_PIN_TEXT`

## 2. Start

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f group-ai-bot
```

## 3. Usage in Groups

- `grok your question` (fast text model: `grok-4-1-fast`)
- `grokt your question` (deep-thinking text: `grok-4-1-thinking`)
- `oai your question` or `/oai your question` (GPT model: `gpt-5.5`)
- `img your prompt` or `/img your prompt` — generate an image with `gpt-image-2`
- Reply to a bot-generated text message to continue the text context (no prefix needed)
- Reply to a bot-generated image with edit instructions to modify that image
- `edit your instructions` or `/edit your instructions` — edit the replied image; also works when uploading a photo with the edit instruction as caption
- Reply to an image with `/av`, or send an image with `/av` as its caption — search AVScan for matching codes; after the global cleanup TTL, the source image, command, and result are all deleted
- `/new` — clear your personal chat context
- `/new_group` — clear the group chat memory (super-admin only)
- `/pin content` — update the daily pinned message in the target topic (super-admin only)
- Reply to a message with native formatting (bold/italic/link/code…) and send `/pin` to pin it with formatting preserved; bare `/pin` re-posts the stored content (formatting included)
- Configure per-topic strategies (e.g. topic `1`: safe-for-work, topic `45807`: default style)
- Configure a topic to auto-enable/disable on a schedule (e.g. Beijing time 23:00–05:00 open, closed otherwise)
- Configure a target topic for automated daily pinned messages (default: Beijing time 00:01)
- Group chat memory records recent conversation (default 50 items, oldest evicted automatically)

## 4. Persistent Memory

- Uses SQLite by default: `/data/memory.db`
- `docker compose` mounts a persistent volume; memory survives container restarts

## 5. Security

- Do not commit `.env` to git.
- Keep `AI_API_KEY` and `TELEGRAM_BOT_TOKEN` only in environment variables.
