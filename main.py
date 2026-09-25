"""
=========================================================
VK MEDIA BOT — «глаза и уши» для основного бота
=========================================================

Что делает:
  - Получает события message_new из VK Callback API.
  - Если в сообщении голосовое или фото, разбирает его через
    СВОЙ ключ Groq (Whisper для голоса, vision для картинок).
  - Кладёт результат в таблицу media_inbox в Supabase:
    VK ID, имя, чат, подпись, текст или описание.
  - В чат НИЧЕГО не пишет. Отвечает основной бот.

ENV (Render -> Environment):

  VK_CONFIRMATION_CODE   строка подтверждения ЭТОГО Callback-сервера
  VK_GROUP_SECRET        secret key этого сервера (если задан в VK)
  GROQ_API_KEY           ключ Groq этого бота
  SUPABASE_URL           тот же, что у основного бота
  SUPABASE_SECRET_KEY    тот же, что у основного бота

  Необязательно:
  VK_TOKEN               токен сообщества: чтобы сохранять имя автора
  GROQ_VISION_MODEL      по умолчанию qwen/qwen3.8-27b
  GROQ_WHISPER_MODEL     по умолчанию whisper-large-v3-turbo
  MEDIA_DAILY_LIMIT      сколько вложений в день (по умолчанию 100)
  CHAT_MAP               перевод номеров чатов: "2000000001:2000000003"
                         (номер у второго бота : номер у основного),
                         несколько пар через запятую
"""

import os
import re
import time
import base64
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, request
from groq import Groq


MEDIA_BOT_VERSION = "M1.2"


# =========================================================
# CONFIG
# =========================================================

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()

VK_CONFIRMATION_CODE = os.environ.get(
    "VK_CONFIRMATION_CODE", ""
).strip()

VK_GROUP_SECRET = os.environ.get(
    "VK_GROUP_SECRET", ""
).strip()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

GROQ_VISION_MODEL = os.environ.get(
    "GROQ_VISION_MODEL", "qwen/qwen3.8-27b"
).strip()

GROQ_WHISPER_MODEL = os.environ.get(
    "GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"
).strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()

SUPABASE_SECRET_KEY = (
    os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    or os.environ.get("SUPABASE_KEY", "").strip()
)

if SUPABASE_URL and not SUPABASE_URL.startswith(
    ("http://", "https://")
):
    SUPABASE_URL = "https://" + SUPABASE_URL

SUPABASE_URL = SUPABASE_URL.rstrip("/")

INBOX_TABLE = "media_inbox"

try:
    MEDIA_DAILY_LIMIT = int(
        os.environ.get("MEDIA_DAILY_LIMIT", "100") or 100
    )
except ValueError:
    MEDIA_DAILY_LIMIT = 100

MEDIA_MAX_VOICE_SECONDS = 120
MEDIA_MAX_IMAGE_BYTES = 2_800_000
MEDIA_MAX_AUDIO_BYTES = 20_000_000
MEDIA_BLOCK_SECONDS = 30 * 60
MAX_RESULT_CHARS = 600


# Перевод номеров чатов. У разных сообществ один и тот же чат
# имеет разный peer_id, поэтому второй бот переводит свой номер
# в номер, под которым чат знает основной бот.
# Формат: CHAT_MAP=2000000001:2000000003,2000000002:2000000005
CHAT_MAP = {}

for _pair in os.environ.get("CHAT_MAP", "").split(","):
    if ":" not in _pair:
        continue

    _src, _dst = _pair.split(":", 1)

    try:
        CHAT_MAP[int(_src.strip())] = int(_dst.strip())
    except ValueError:
        print(f"CHAT_MAP: не понял пару '{_pair}'", flush=True)


def map_chat_id(peer_id):
    peer_id = int(peer_id)
    return CHAT_MAP.get(peer_id, peer_id)


groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)

app = Flask(__name__)

media_lock = threading.Lock()
media_state = {"day": "", "count": 0, "blocked_until": 0.0}

seen_events = {}
seen_lock = threading.Lock()

user_names = {}


# =========================================================
# PROMPT
# =========================================================

MEDIA_VISION_PROMPT = (
    "Ты помогаешь боту игрового чата про Tanks Blitz "
    "(World of Tanks Blitz). "
    "Если картинка НЕ связана с Tanks Blitz или World of Tanks "
    "(обычное фото, мем, другая игра, реклама и т.п.), "
    "ответь ровно одним словом: НЕТ. "
    "Если связана, опиши по-русски кратко (до 350 символов, "
    "без списков и без вступления), что на ней. "
    "Результат боя: победа или поражение, урон, заблокированный урон, "
    "уничтожено, ник и клан, дата. "
    "СТЕПЕНЬ БОЯ: посмотри на ряд иконок-медалей вверху экрана "
    "результата боя. Среди них ищи значок в виде щита/короны с крупной "
    "буквой M — это значит игрок получил степень «Мастер». Если вместо "
    "буквы M на таком же по форме значке видна цифра 1, 2 или 3 — это "
    "«1 степень», «2 степень» или «3 степень» соответственно. Если такого "
    "значка (буква M или цифра 1/2/3 на щите-короне) в ряду нет — "
    "значит степень не присвоена, так и напиши, не выдумывай. "
    "Магазин или предложения: названия танков и наборов, цены, скидки. "
    "Награды и контейнеры: что выпало и сколько. "
    "Профиль и статистика: бои, процент побед, средний урон. "
    "Гараж или рендер танка: название танка, только если оно написано "
    "на картинке или ты абсолютно уверен, иначе просто опиши танк "
    "и обстановку. "
    "Цифры и ники переписывай точно. Ничего не выдумывай."
)


# =========================================================
# SUPABASE (REST)
# =========================================================

def sb_headers(extra=None):
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
    }

    if extra:
        headers.update(extra)

    return headers


def sb_insert_row(row):
    """Вставка строки. Дубли по event_key молча игнорируются."""
    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/{INBOX_TABLE}",
        params={"on_conflict": "event_key"},
        headers=sb_headers({
            "Prefer": "resolution=ignore-duplicates,return=minimal"
        }),
        json=row,
        timeout=20,
    )

    if response.status_code >= 300:
        raise RuntimeError(
            f"Supabase {response.status_code}: {response.text[:300]}"
        )


def sb_insert_with_retry(row, attempts=3):
    last_error = None

    for attempt in range(attempts):
        try:
            sb_insert_row(row)
            return True
        except Exception as e:
            last_error = e
            time.sleep(2 * (attempt + 1))

    print(f"SUPABASE insert failed: {last_error}", flush=True)

    return False


# =========================================================
# VK NAME (необязательно)
# =========================================================

def get_vk_user_name(user_id):
    if not VK_TOKEN or not user_id:
        return None

    cached = user_names.get(str(user_id))

    if cached and time.time() - cached[0] < 24 * 60 * 60:
        return cached[1]

    try:
        data = requests.get(
            f"{VK_API}/users.get",
            params={
                "access_token": VK_TOKEN,
                "v": VK_VERSION,
                "user_ids": user_id,
            },
            timeout=10,
        ).json()

        users = data.get("response") or []

        if not users:
            return None

        user = users[0]

        name = (
            f"{user.get('first_name', '').strip()} "
            f"{user.get('last_name', '').strip()}"
        ).strip()

        if name:
            user_names[str(user_id)] = (time.time(), name)

        return name or None

    except Exception as e:
        print("VK name error:", e, flush=True)
        return None


# =========================================================
# LIMITS
# =========================================================

def media_available():
    """Есть ли ключ, не на паузе ли он и не выбран ли дневной лимит."""
    if not groq_client:
        return False

    now = time.time()

    with media_lock:
        if now < media_state["blocked_until"]:
            return False

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if media_state["day"] != today:
            media_state["day"] = today
            media_state["count"] = 0

        if media_state["count"] >= MEDIA_DAILY_LIMIT:
            return False

        media_state["count"] += 1

    return True


def media_note_error(error):
    """Если лимит Groq кончился, ставим бота на паузу на 30 минут."""
    low = str(error).lower()

    if any(x in low for x in ("429", "rate", "quota", "limit")):
        with media_lock:
            media_state["blocked_until"] = (
                time.time() + MEDIA_BLOCK_SECONDS
            )

        print(
            "MEDIA: лимит Groq, пауза 30 минут",
            flush=True
        )


# =========================================================
# ВЛОЖЕНИЯ VK
# =========================================================

def media_download(url, max_bytes):
    with requests.get(url, timeout=30, stream=True) as resp:
        resp.raise_for_status()

        chunks = []
        size = 0

        for chunk in resp.iter_content(65536):
            size += len(chunk)

            if size > max_bytes:
                raise ValueError("файл слишком большой")

            chunks.append(chunk)

    return b"".join(chunks)


def vk_pick_photo_url(photo):
    sizes = [
        x for x in (photo.get("sizes") or [])
        if x.get("url")
    ]

    if not sizes:
        return None

    def width(item):
        return int(item.get("width") or 0)

    fitting = [x for x in sizes if width(x) <= 1280]

    best = (
        max(fitting, key=width)
        if fitting
        else min(sizes, key=width)
    )

    return best["url"]


def vk_find_media(message):
    """
    Возвращает (kind, url, duration).
    kind = 'voice' | 'image' | None.
    Если вложение есть, а ссылки нет, url = None
    (строка всё равно будет записана, чтобы не потерять подпись).
    """
    for att in (message.get("attachments") or []):
        kind = att.get("type")

        if kind == "audio_message":
            data = att.get("audio_message") or {}

            return (
                "voice",
                data.get("link_mp3") or data.get("link_ogg"),
                int(data.get("duration") or 0),
            )

        if kind == "photo":
            return (
                "image",
                vk_pick_photo_url(att.get("photo") or {}),
                0,
            )

    return None, None, 0


# =========================================================
# GROQ: ГОЛОС И КАРТИНКИ
# =========================================================

def media_transcribe(data, filename):
    result = groq_client.audio.transcriptions.create(
        file=(filename, data),
        model=GROQ_WHISPER_MODEL,
        language="ru",
        temperature=0,
    )

    text = getattr(result, "text", None)

    if text is None and isinstance(result, str):
        text = result

    return (text or "").strip()


def media_describe_image(data):
    """Описание картинки. Пустая строка = не про Tanks Blitz."""
    mime = (
        "image/png"
        if data[:8] == b"\x89PNG\r\n\x1a\n"
        else "image/jpeg"
    )

    b64 = base64.b64encode(data).decode("ascii")

    request_body = dict(
        model=GROQ_VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": MEDIA_VISION_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64}"
                    },
                },
            ],
        }],
        max_tokens=800,
        temperature=0.3,
    )

    try:
        # Пробуем без «размышлений»: быстрее и дешевле по токенам.
        completion = groq_client.chat.completions.create(
            reasoning_effort="none",
            **request_body
        )
    except Exception as e:
        low = str(e).lower()

        if any(
            x in low
            for x in ("reasoning", "400", "invalid", "unsupported")
        ):
            completion = groq_client.chat.completions.create(
                **request_body
            )
        else:
            raise

    raw = completion.choices[0].message.content or ""

    raw = re.sub(
        r"<think>.*?</think>",
        "",
        raw,
        flags=re.DOTALL
    ).strip()

    # Модель ответила «НЕТ» (картинка не про Tanks Blitz).
    if re.match(r"^\W*нет\b", raw, flags=re.IGNORECASE):
        return ""

    return raw


def media_to_text(kind, url, duration=0):
    """Голос -> текст, картинка -> описание. При любой ошибке ''."""
    if not url:
        return ""

    if kind == "voice" and duration > MEDIA_MAX_VOICE_SECONDS:
        print(
            f"MEDIA [voice] слишком длинное ({duration} c), пропуск",
            flush=True
        )
        return ""

    if not media_available():
        print("MEDIA: недоступно (лимит или нет ключа)", flush=True)
        return ""

    try:
        if kind == "voice":
            data = media_download(url, MEDIA_MAX_AUDIO_BYTES)

            name = (
                "voice.ogg"
                if ".ogg" in url.split("?")[0]
                else "voice.mp3"
            )

            text = media_transcribe(data, name)

        else:
            data = media_download(url, MEDIA_MAX_IMAGE_BYTES)
            text = media_describe_image(data)

        text = re.sub(r"\s+", " ", text).strip()

        if not text:
            print(
                f"MEDIA [{kind}] пропущено "
                "(не про Tanks Blitz или пусто)",
                flush=True
            )
        else:
            print(
                f"MEDIA [{kind}] OK: {text[:120]}",
                flush=True
            )

        return text[:MAX_RESULT_CHARS]

    except Exception as e:
        print(f"MEDIA [{kind}] ERROR: {e}", flush=True)
        media_note_error(e)
        return ""


# =========================================================
# ОБРАБОТКА СОБЫТИЯ
# =========================================================

def process_event(data):
    obj = data.get("object") or {}
    message = obj.get("message") or {}

    peer_id = message.get("peer_id")
    sender_id = message.get("from_id") or message.get("user_id")

    if not peer_id or not sender_id:
        return

    # Сообщения от сообществ (других ботов) не трогаем.
    if int(sender_id) < 0:
        return

    # Личные сообщения основной бот тоже игнорирует.
    if int(peer_id) == int(sender_id):
        return

    kind, url, duration = vk_find_media(message)

    if not kind:
        return

    caption = (message.get("text") or "").strip()

    reply = message.get("reply_message") or {}
    reply_from_id = reply.get("from_id")

    result = media_to_text(kind, url, duration)

    event_id = data.get("event_id")
    cmid = message.get("conversation_message_id")

    event_key = (
        f"vk:{event_id}"
        if event_id
        else f"vk:{peer_id}:{cmid}"
    )

    chat_id = map_chat_id(peer_id)

    row = {
        "event_key": event_key,
        "chat_id": chat_id,
        "sender_id": int(sender_id),
        "sender_name": get_vk_user_name(sender_id),
        "message_id": int(cmid) if cmid is not None else None,
        "reply_from_id": (
            int(reply_from_id)
            if reply_from_id is not None
            else None
        ),
        "kind": kind,
        "caption": caption or None,
        "result": result,
        "status": "new",
    }

    if sb_insert_with_retry(row):
        print(
            f"INBOX +1: {kind} от {sender_id} "
            f"в чате {peer_id} -> {chat_id} "
            f"({len(result)} симв.)",
            flush=True
        )


def safe_process(data):
    try:
        process_event(data)
    except Exception as e:
        print("process_event error:", e, flush=True)


def already_seen(event_id):
    if not event_id:
        return False

    now = time.time()

    with seen_lock:
        for key in list(seen_events):
            if now - seen_events[key] > 30 * 60:
                seen_events.pop(key, None)

        if event_id in seen_events:
            return True

        seen_events[event_id] = now

        if len(seen_events) > 2000:
            oldest = min(seen_events, key=seen_events.get)
            seen_events.pop(oldest, None)

    return False


# =========================================================
# FLASK
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return {
        "status": "ok",
        "bot": "VK media bot (eyes and ears)",
        "version": MEDIA_BOT_VERSION,
        "groq": bool(groq_client),
        "supabase": bool(SUPABASE_URL and SUPABASE_SECRET_KEY),
        "vision_model": GROQ_VISION_MODEL,
        "whisper_model": GROQ_WHISPER_MODEL,
        "chat_map": {str(k): v for k, v in CHAT_MAP.items()},
    }, 200


@app.route("/callback", methods=["POST"])
def callback():
    data = request.get_json(force=True, silent=True) or {}

    event_type = data.get("type")

    if event_type == "confirmation":
        return VK_CONFIRMATION_CODE

    if (
        VK_GROUP_SECRET
        and data.get("secret") != VK_GROUP_SECRET
    ):
        return "invalid secret", 403

    if event_type != "message_new":
        return "ok"

    if already_seen(data.get("event_id")):
        return "ok"

    # Сразу отвечаем VK «ok», а тяжёлую работу делаем в фоне:
    # так VK не будет слать повторы, пока бот просыпается.
    threading.Thread(
        target=safe_process,
        args=(data,),
        daemon=True
    ).start()

    return "ok"


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    print("========================================", flush=True)
    print(f"👀 VK MEDIA BOT {MEDIA_BOT_VERSION}", flush=True)
    print(f"🔑 Groq key: {'YES' if groq_client else 'NO'}", flush=True)
    print(f"🖼 vision: {GROQ_VISION_MODEL}", flush=True)
    print(f"🎤 whisper: {GROQ_WHISPER_MODEL}", flush=True)
    print(
        "🗄 Supabase: "
        f"{'YES' if SUPABASE_URL and SUPABASE_SECRET_KEY else 'NO'}",
        flush=True
    )
    print(
        f"👤 VK_TOKEN (имена): {'YES' if VK_TOKEN else 'NO'}",
        flush=True
    )
    print(f"📊 Лимит вложений в день: {MEDIA_DAILY_LIMIT}", flush=True)
    print(f"🔀 CHAT_MAP: {CHAT_MAP if CHAT_MAP else 'нет'}", flush=True)
    print("========================================", flush=True)

    port = int(os.environ.get("PORT", 5000))

    app.run(host="0.0.0.0", port=port)
