"""
=========================================================
VK SUGGEST AUTOPOSTER — новый отдельный бот
=========================================================

Что делает:
  - Раз в сутки (по расписанию POST_TIMES) берёт 1 запись из
    предложки сообщества (wall.get, filter=suggests)
  - Отправляет прикреплённый скрин в Groq (vision-модель) —
    она пишет текст поста и решает, публиковать ли скрин
  - Публикует на стену от имени сообщества, с упоминанием
    автора (from_id самой предложки)
  - Удаляет запись из предложки

  - Также отвечает на комментарии под постами (через Callback API):
    когда кто-то комментирует пост в сообществе, бот генерирует
    короткий ответ через Groq и оставляет его как комментарий

Как запустить на Replit:
  1. Создай новый Repl (Python)
  2. Положи этот файл как main.py
  3. В Secrets (замочек слева) добавь:
       VK_TOKEN            — токен сообщества с правами wall, photos
       VK_GROUP_ID         — числовой ID сообщества (без минуса)
       GROQ_API_KEY        — ключ Groq
       VK_CONFIRMATION_CODE — код подтверждения (см. ниже)
       VK_GROUP_SECRET      — секретный ключ Callback API (см. ниже)
  4. В requirements (или через Shell: pip install flask groq requests)
  5. Запусти — бот поднимет веб-сервер и в фоне будет публиковать
     по расписанию + слушать комментарии

Настройка ответов на комментарии (Callback API):
  1. Настройки сообщества → Работа с API → Callback API
  2. Добавь Callback-сервер: URL = https://<твой-репл>.replit.dev/vk/callback
     (Replit даёт публичный URL автоматически)
  3. VK покажет "Строку подтверждения" — скопируй её в секрет
     VK_CONFIRMATION_CODE
  4. Там же есть "Секретный ключ" — скопируй его в VK_GROUP_SECRET
     (или собственный придумай и вставь в поле в VK)
  5. Во вкладке "Типы событий" включи:
       Комментарии на стене → Добавление
  6. Сохрани — VK отправит проверочный запрос, бот должен ответить
     кодом подтверждения автоматически (сервер уже это умеет)

POST_TIMES — список времени публикаций (24ч, локальное время сервера).
По умолчанию 2 раза в день: 12:00 и 20:00.
"""

import os
import time
import json
import threading
from datetime import datetime

import requests
from flask import Flask, request
from groq import Groq


# =========================================================
# CONFIG
# =========================================================

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
GROUP_ID = int(os.environ.get("VK_GROUP_ID", "0") or 0)

VK_CONFIRMATION_CODE = os.environ.get("VK_CONFIRMATION_CODE", "").strip()
VK_GROUP_SECRET = os.environ.get("VK_GROUP_SECRET", "").strip()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
# Vision-модель Groq (понимает изображения, до 5 картинок за запрос)
GROQ_VISION_MODEL = os.environ.get(
    "GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"
)
# Обычная текстовая модель Groq — для ответов на комментарии
GROQ_TEXT_MODEL = os.environ.get(
    "GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"
)

# Время публикаций в сутки
POST_TIMES = ["12:00", "20:00"]

# ТЕСТОВЫЙ РЕЖИМ: если в Render/Replit задать переменную TEST_MODE=1,
# бот игнорирует POST_TIMES и публикует раз в TEST_INTERVAL_MINUTES минут.
# Чтобы вернуть обычный режим — просто удали TEST_MODE (или поставь 0).
TEST_MODE = os.environ.get("TEST_MODE", "0").strip() == "1"
TEST_INTERVAL_MINUTES = int(os.environ.get("TEST_INTERVAL_MINUTES", "10") or 10)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Чтобы не отвечать дважды на одно и то же событие Callback API
processed_events = set()
processed_events_lock = threading.Lock()


# =========================================================
# VK HELPERS
# =========================================================

def vk_call(method, **params):
    params["access_token"] = VK_TOKEN
    params["v"] = VK_VERSION
    r = requests.post(f"{VK_API}/{method}", data=params, timeout=20)
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["response"]


def get_suggested_posts(count=1, offset=0):
    resp = vk_call(
        "wall.get",
        owner_id=-GROUP_ID,
        filter="suggests",
        count=count,
        offset=offset,
    )
    return resp.get("items", [])


def get_biggest_photo_url(photo):
    sizes = photo.get("sizes", [])
    if not sizes:
        return None
    biggest = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
    return biggest.get("url")


def get_user_mention(user_id):
    """VK-упоминание автора предложки: [id123|Имя Фамилия]."""
    if not user_id or user_id <= 0:
        return "аноним"
    try:
        resp = vk_call("users.get", user_ids=user_id)
        if resp:
            u = resp[0]
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
            return f"[id{user_id}|{name}]"
    except Exception as e:
        print("users.get error:", e, flush=True)
    return f"[id{user_id}|автор]"


def _safe_delete(post_id):
    try:
        vk_call("wall.delete", owner_id=-GROUP_ID, post_id=post_id)
    except Exception as e:
        print("wall.delete error:", e, flush=True)


# =========================================================
# AI: АНАЛИЗ СКРИНА (Groq vision)
# =========================================================

PROMPT = (
    "Это скриншот из мобильной игры World of Tanks Blitz, "
    "присланный подписчиком паблика в предложку. "
    "Напиши короткий пост для игрового паблика ВКонтакте "
    "(1–3 предложения), живым разговорным тоном, можно с эмодзи, "
    "по существу того, что видно на скрине (результат боя, урон, "
    "техника, достижение и т.п.). Не придумывай цифры и детали, "
    "которых не видно на изображении. Если скрин не по теме игры, "
    "нерелевантен, это спам, мем или что-то не для паблика — "
    "верни is_relevant=false. "
    'Ответь СТРОГО в формате JSON без markdown и пояснений: '
    '{"is_relevant": true, "text": "..."}'
)


def analyze_screenshot(photo_url):
    fallback = {"is_relevant": True, "text": "Новый скрин от подписчика! 🔥"}

    if not groq_client or not photo_url:
        return fallback

    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_url", "image_url": {"url": photo_url}},
                    ],
                }
            ],
            max_tokens=220,
            temperature=0.7,
        )
        raw = completion.choices[0].message.content.strip()
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

        parsed = json.loads(raw)
        text = (parsed.get("text") or "").strip()
        if not text:
            return fallback

        return {
            "is_relevant": bool(parsed.get("is_relevant", True)),
            "text": text,
        }
    except Exception as e:
        print("analyze_screenshot error:", e, flush=True)
        return fallback


# =========================================================
# ПУБЛИКАЦИЯ ОДНОГО ПОСТА ИЗ ПРЕДЛОЖКИ
# =========================================================

def publish_next_suggested():
    if not VK_TOKEN or not GROUP_ID:
        print("VK_TOKEN / VK_GROUP_ID не заданы.", flush=True)
        return

    try:
        items = get_suggested_posts(count=1)
    except Exception as e:
        print("get_suggested_posts error:", e, flush=True)
        return

    if not items:
        print("Предложка пуста.", flush=True)
        return

    post = items[0]
    post_id = post["id"]
    author_id = post.get("from_id")

    photo = next(
        (a["photo"] for a in post.get("attachments", []) if a.get("type") == "photo"),
        None,
    )

    if not photo:
        _safe_delete(post_id)
        print(f"Пост {post_id} без фото — убран из очереди.", flush=True)
        return

    photo_url = get_biggest_photo_url(photo)
    ai_result = analyze_screenshot(photo_url)

    if not ai_result["is_relevant"]:
        _safe_delete(post_id)
        print(f"Пост {post_id} отклонён AI как нерелевантный.", flush=True)
        return

    attachment_str = f"photo{photo['owner_id']}_{photo['id']}"
    mention = get_user_mention(author_id)
    message = f"{ai_result['text']}\n\nПрислал: {mention}"

    try:
        vk_call(
            "wall.post",
            owner_id=-GROUP_ID,
            from_group=1,
            message=message,
            attachments=attachment_str,
        )
        _safe_delete(post_id)
        print(f"Опубликован пост {post_id}, автор {mention}", flush=True)
    except Exception as e:
        print("wall.post error:", e, flush=True)


# =========================================================
# AI: ОТВЕТ НА КОММЕНТАРИЙ (текстовая модель Groq)
# =========================================================

COMMENT_SYSTEM_PROMPT = (
    "Ты — живой участник геймерского паблика ВКонтакте про "
    "World of Tanks Blitz. Под постами со скринами игроков "
    "иногда отвечаешь на комментарии людей. Пиши коротко "
    "(1 предложение, максимум 2), живым разговорным тоном, "
    "можно с эмодзи, по-дружески. Не будь занудным и не пиши "
    "как бот/техподдержка. Если комментарий — просто эмоция "
    "или мем без вопроса, можно отреагировать коротко и легко. "
    "Не отвечай, если комментарий явно не требует ответа "
    "(в этом случае верни пустую строку)."
)


def generate_comment_reply(comment_text):
    if not groq_client or not comment_text:
        return ""

    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_TEXT_MODEL,
            messages=[
                {"role": "system", "content": COMMENT_SYSTEM_PROMPT},
                {"role": "user", "content": comment_text},
            ],
            max_tokens=120,
            temperature=0.8,
        )
        reply = completion.choices[0].message.content.strip()
        return reply
    except Exception as e:
        print("generate_comment_reply error:", e, flush=True)
        return ""


def handle_wall_reply_new(event_object):
    """Обрабатывает новый комментарий под постом на стене сообщества."""
    comment_id = event_object.get("id")
    post_id = event_object.get("post_id")
    from_id = event_object.get("from_id")
    text = (event_object.get("text") or "").strip()

    # не отвечаем сами себе / другим ботам сообщества
    if not from_id or from_id < 0:
        return

    if not text:
        return

    reply = generate_comment_reply(text)
    if not reply:
        print(f"Комментарий {comment_id}: решили не отвечать.", flush=True)
        return

    try:
        vk_call(
            "wall.createComment",
            owner_id=-GROUP_ID,
            post_id=post_id,
            reply_to_comment=comment_id,
            from_group=1,
            message=reply,
        )
        print(f"Ответили на комментарий {comment_id}: {reply}", flush=True)
    except Exception as e:
        print("wall.createComment error:", e, flush=True)


# =========================================================
# ФОНОВЫЙ ЦИКЛ ПО РАСПИСАНИЮ
# =========================================================

def autoposter_loop():
    if TEST_MODE:
        print(
            f"⚠️ ТЕСТОВЫЙ РЕЖИМ включён — публикация каждые "
            f"{TEST_INTERVAL_MINUTES} мин. Не забудь выключить "
            f"(убрать TEST_MODE) после проверки!",
            flush=True,
        )
        while True:
            publish_next_suggested()
            time.sleep(TEST_INTERVAL_MINUTES * 60)
        return

    posted_today = set()
    last_day = None

    print(f"Автопостер запущен. Слоты: {POST_TIMES}", flush=True)

    while True:
        now = datetime.now()
        today = now.date()

        if today != last_day:
            posted_today = set()
            last_day = today

        current_slot = now.strftime("%H:%M")

        for slot in POST_TIMES:
            if current_slot == slot and slot not in posted_today:
                publish_next_suggested()
                posted_today.add(slot)

        time.sleep(30)


# =========================================================
# FLASK (keep-alive для Replit)
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "VK suggest autoposter работает"


@app.route("/vk/callback", methods=["POST"])
def vk_callback():
    data = request.get_json(force=True, silent=True) or {}

    # подтверждение сервера при подключении Callback API
    if data.get("type") == "confirmation":
        return VK_CONFIRMATION_CODE

    # проверка секретного ключа (если он задан в настройках VK)
    if VK_GROUP_SECRET and data.get("secret") != VK_GROUP_SECRET:
        return "ok"

    event_id = data.get("event_id")
    if event_id:
        with processed_events_lock:
            if event_id in processed_events:
                return "ok"
            processed_events.add(event_id)
            # чтобы множество не росло бесконечно
            if len(processed_events) > 5000:
                processed_events.clear()

    event_type = data.get("type")
    obj = data.get("object", {})

    try:
        if event_type == "wall_reply_new":
            handle_wall_reply_new(obj)
    except Exception as e:
        print("vk_callback handler error:", e, flush=True)

    return "ok"


if __name__ == "__main__":
    threading.Thread(target=autoposter_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
