"""
=========================================================
VK SUGGEST AUTOPOSTER — OAuth версия
=========================================================

Что делает:
  - Раз в сутки (по расписанию POST_TIMES) берёт 1 запись из
    предложки сообщества через wall.get filter=suggests
  - wall.get выполняется пользовательским VK OAuth токеном
  - Публикация выполняется токеном сообщества
  - Отправляет скрин в Groq Vision
  - Публикует текст от имени сообщества
  - Удаляет запись из предложки
  - Отвечает на комментарии через Callback API

=========================================================
ENV
=========================================================

Обязательные:

VK_TOKEN
    Токен сообщества.

VK_GROUP_ID
    ID сообщества без минуса.

GROQ_API_KEY
    Ключ Groq.

VK_CONFIRMATION_CODE
    Код подтверждения Callback API.

VK_GROUP_SECRET
    Секрет Callback API.

Для OAuth:

VK_CLIENT_ID
    ID приложения VK.

VK_CLIENT_SECRET
    Защищённый ключ приложения VK.

VK_REDIRECT_URI
    Например:
    https://твой-сервис.onrender.com/vk/oauth/callback

После запуска открыть:

https://твой-сервис.onrender.com/vk/login

После авторизации VK перенаправит обратно на сервер,
сервер сам обменяет code на пользовательский access_token.

=========================================================
"""

import os
import time
import json
import threading
import secrets
from datetime import datetime
from urllib.parse import urlencode

import requests
from flask import Flask, request, redirect
from groq import Groq


# =========================================================
# CONFIG
# =========================================================

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"

# ---------------------------------------------------------
# Токен сообщества
# ---------------------------------------------------------

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()

# ---------------------------------------------------------
# VK OAuth
# ---------------------------------------------------------

VK_CLIENT_ID = os.environ.get("VK_CLIENT_ID", "").strip()
VK_CLIENT_SECRET = os.environ.get("VK_CLIENT_SECRET", "").strip()
VK_REDIRECT_URI = os.environ.get("VK_REDIRECT_URI", "").strip()

# Пользовательский токен.
#
# Если он уже есть в Render Environment Variables,
# бот сможет использовать его сразу.
#
# После OAuth токен также записывается в память процесса.
VK_USER_TOKEN = os.environ.get("VK_USER_TOKEN", "").strip()

# Защита OAuth state
oauth_state = None
oauth_state_lock = threading.Lock()

# ---------------------------------------------------------
# Сообщество
# ---------------------------------------------------------

GROUP_ID = int(os.environ.get("VK_GROUP_ID", "0") or 0)

VK_CONFIRMATION_CODE = os.environ.get(
    "VK_CONFIRMATION_CODE", ""
).strip()

VK_GROUP_SECRET = os.environ.get(
    "VK_GROUP_SECRET", ""
).strip()

# ---------------------------------------------------------
# Groq
# ---------------------------------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

GROQ_VISION_MODEL = os.environ.get(
    "GROQ_VISION_MODEL",
    "meta-llama/llama-4-scout-17b-16e-instruct",
)

GROQ_TEXT_MODEL = os.environ.get(
    "GROQ_TEXT_MODEL",
    "llama-3.3-70b-versatile",
)

# ---------------------------------------------------------
# Расписание
# ---------------------------------------------------------

POST_TIMES = ["12:00", "20:00"]

# ---------------------------------------------------------
# Тестовый режим
# ---------------------------------------------------------

TEST_MODE = (
    os.environ.get("TEST_MODE", "0").strip() == "1"
)

TEST_INTERVAL_MINUTES = int(
    os.environ.get(
        "TEST_INTERVAL_MINUTES",
        "10",
    )
    or 10
)

# ---------------------------------------------------------
# Groq client
# ---------------------------------------------------------

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)

# ---------------------------------------------------------
# Callback protection
# ---------------------------------------------------------

processed_events = set()
processed_events_lock = threading.Lock()


# =========================================================
# VK HELPERS
# =========================================================

def vk_call(method, token=None, **params):
    """
    Универсальный вызов VK API.

    По умолчанию используется токен сообщества.

    Для методов, которым нужен пользовательский токен,
    можно передать:

        token=VK_USER_TOKEN
    """

    access_token = token or VK_TOKEN

    if not access_token:
        raise RuntimeError(
            "VK access_token не задан."
        )

    params["access_token"] = access_token
    params["v"] = VK_VERSION

    r = requests.post(
        f"{VK_API}/{method}",
        data=params,
        timeout=20,
    )

    r.raise_for_status()

    data = r.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    return data["response"]


# =========================================================
# VK SUGGESTS
# =========================================================

def get_suggested_posts(count=1, offset=0):
    """
    Получение предложки.

    ВАЖНО:
    wall.get filter=suggests выполняем пользовательским
    OAuth токеном.
    """

    if not VK_USER_TOKEN:
        raise RuntimeError(
            "VK_USER_TOKEN отсутствует. "
            "Открой /vk/login и пройди авторизацию VK."
        )

    resp = vk_call(
        "wall.get",
        token=VK_USER_TOKEN,
        owner_id=-GROUP_ID,
        filter="suggests",
        count=count,
        offset=offset,
    )

    return resp.get("items", [])


# =========================================================
# PHOTOS
# =========================================================

def get_biggest_photo_url(photo):
    sizes = photo.get("sizes", [])

    if not sizes:
        return None

    biggest = max(
        sizes,
        key=lambda s:
        s.get("width", 0) * s.get("height", 0)
    )

    return biggest.get("url")


# =========================================================
# USER MENTION
# =========================================================

def get_user_mention(user_id):
    """
    VK-упоминание автора предложки:
    [id123|Имя Фамилия]
    """

    if not user_id or user_id <= 0:
        return "аноним"

    try:
        resp = vk_call(
            "users.get",
            user_ids=user_id,
        )

        if resp:
            u = resp[0]

            name = (
                f"{u.get('first_name', '')} "
                f"{u.get('last_name', '')}"
            ).strip()

            return f"[id{user_id}|{name}]"

    except Exception as e:
        print(
            "users.get error:",
            e,
            flush=True,
        )

    return f"[id{user_id}|автор]"


# =========================================================
# DELETE SUGGEST
# =========================================================

def _safe_delete(post_id):
    """
    Удаление записи из предложки.

    Здесь используется токен сообщества.
    """

    try:
        vk_call(
            "wall.delete",
            owner_id=-GROUP_ID,
            post_id=post_id,
        )

    except Exception as e:
        print(
            "wall.delete error:",
            e,
            flush=True,
        )


# =========================================================
# AI — SCREENSHOT
# =========================================================

PROMPT = (
    "Это скриншот из мобильной игры World of Tanks Blitz, "
    "присланный подписчиком паблика в предложку. "
    "Напиши короткий пост для игрового паблика ВКонтакте "
    "(1–3 предложения), живым разговорным тоном, можно с "
    "эмодзи, по существу того, что видно на скрине "
    "(результат боя, урон, техника, достижение и т.п.). "
    "Не придумывай цифры и детали, которых нет на "
    "изображении. Если скрин не по теме игры, нерелевантен, "
    "это спам, мем или что-то не для паблика — "
    "верни is_relevant=false. "
    'Ответь СТРОГО в формате JSON без markdown и пояснений: '
    '{"is_relevant": true, "text": "..."}'
)


def analyze_screenshot(photo_url):
    fallback = {
        "is_relevant": True,
        "text": "Новый скрин от подписчика! 🔥",
    }

    if not groq_client or not photo_url:
        return fallback

    try:
        completion = (
            groq_client.chat.completions.create(
                model=GROQ_VISION_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": PROMPT,
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": photo_url
                                },
                            },
                        ],
                    }
                ],
                max_tokens=220,
                temperature=0.7,
            )
        )

        raw = (
            completion
            .choices[0]
            .message
            .content
            .strip()
        )

        raw = raw.strip("`")

        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

        parsed = json.loads(raw)

        text = (
            parsed.get("text") or ""
        ).strip()

        if not text:
            return fallback

        return {
            "is_relevant": bool(
                parsed.get(
                    "is_relevant",
                    True,
                )
            ),
            "text": text,
        }

    except Exception as e:
        print(
            "analyze_screenshot error:",
            e,
            flush=True,
        )

        return fallback


# =========================================================
# PUBLISH ONE SUGGESTED POST
# =========================================================

def publish_next_suggested():

    if not VK_TOKEN or not GROUP_ID:
        print(
            "VK_TOKEN / VK_GROUP_ID не заданы.",
            flush=True,
        )
        return

    # -----------------------------------------------------
    # Проверяем пользовательский токен
    # -----------------------------------------------------

    if not VK_USER_TOKEN:
        print(
            "⚠️ VK_USER_TOKEN отсутствует.",
            flush=True,
        )

        print(
            "Открой /vk/login для авторизации.",
            flush=True,
        )

        return

    # -----------------------------------------------------
    # Получаем предложку
    # -----------------------------------------------------

    try:

        items = get_suggested_posts(
            count=1
        )

    except Exception as e:

        print(
            "get_suggested_posts error:",
            e,
            flush=True,
        )

        return

    if not items:

        print(
            "Предложка пуста.",
            flush=True,
        )

        return

    post = items[0]

    post_id = post["id"]
    author_id = post.get("from_id")

    # -----------------------------------------------------
    # Ищем фото
    # -----------------------------------------------------

    photo = next(
        (
            a["photo"]
            for a in post.get(
                "attachments",
                []
            )
            if a.get("type") == "photo"
        ),
        None,
    )

    if not photo:

        _safe_delete(post_id)

        print(
            f"Пост {post_id} без фото — "
            f"убран из очереди.",
            flush=True,
        )

        return

    photo_url = get_biggest_photo_url(photo)

    # -----------------------------------------------------
    # AI
    # -----------------------------------------------------

    ai_result = analyze_screenshot(
        photo_url
    )

    if not ai_result["is_relevant"]:

        _safe_delete(post_id)

        print(
            f"Пост {post_id} отклонён AI "
            f"как нерелевантный.",
            flush=True,
        )

        return

    # -----------------------------------------------------
    # Формируем публикацию
    # -----------------------------------------------------

    attachment_str = (
        f"photo"
        f"{photo['owner_id']}_"
        f"{photo['id']}"
    )

    mention = get_user_mention(
        author_id
    )

    message = (
        f"{ai_result['text']}\n\n"
        f"Прислал: {mention}"
    )

    # -----------------------------------------------------
    # Публикуем от имени сообщества
    # -----------------------------------------------------

    try:

        vk_call(
            "wall.post",
            owner_id=-GROUP_ID,
            from_group=1,
            message=message,
            attachments=attachment_str,
        )

        # После успешной публикации удаляем
        # исходную запись из предложки.

        _safe_delete(post_id)

        print(
            f"Опубликован пост {post_id}, "
            f"автор {mention}",
            flush=True,
        )

    except Exception as e:

        print(
            "wall.post error:",
            e,
            flush=True,
        )


# =========================================================
# AI — COMMENTS
# =========================================================

COMMENT_SYSTEM_PROMPT = (
    "Ты — живой участник геймерского паблика ВКонтакте "
    "про World of Tanks Blitz. Под постами со скринами "
    "игроков иногда отвечаешь на комментарии людей. "
    "Пиши коротко (1 предложение, максимум 2), живым "
    "разговорным тоном, можно с эмодзи, по-дружески. "
    "Не будь занудным и не пиши как бот/техподдержка. "
    "Если комментарий — просто эмоция или мем без вопроса, "
    "можно отреагировать коротко и легко. "
    "Не отвечай, если комментарий явно не требует ответа "
    "(в этом случае верни пустую строку)."
)


def generate_comment_reply(comment_text):

    if not groq_client or not comment_text:
        return ""

    try:

        completion = (
            groq_client.chat.completions.create(
                model=GROQ_TEXT_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content":
                            COMMENT_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content":
                            comment_text,
                    },
                ],
                max_tokens=120,
                temperature=0.8,
            )
        )

        reply = (
            completion
            .choices[0]
            .message
            .content
            .strip()
        )

        return reply

    except Exception as e:

        print(
            "generate_comment_reply error:",
            e,
            flush=True,
        )

        return ""


def handle_wall_reply_new(event_object):

    comment_id = event_object.get("id")
    post_id = event_object.get("post_id")
    from_id = event_object.get("from_id")

    text = (
        event_object.get("text") or ""
    ).strip()

    # Не отвечаем сообществам / ботам
    if not from_id or from_id < 0:
        return

    if not text:
        return

    reply = generate_comment_reply(
        text
    )

    if not reply:

        print(
            f"Комментарий {comment_id}: "
            f"решили не отвечать.",
            flush=True,
        )

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

        print(
            f"Ответили на комментарий "
            f"{comment_id}: {reply}",
            flush=True,
        )

    except Exception as e:

        print(
            "wall.createComment error:",
            e,
            flush=True,
        )


# =========================================================
# AUTPOSTER LOOP
# =========================================================

def autoposter_loop():

    if TEST_MODE:

        print(
            f"⚠️ ТЕСТОВЫЙ РЕЖИМ включён — "
            f"публикация каждые "
            f"{TEST_INTERVAL_MINUTES} мин.",
            flush=True,
        )

        while True:

            publish_next_suggested()

            time.sleep(
                TEST_INTERVAL_MINUTES * 60
            )

        return

    posted_today = set()
    last_day = None

    print(
        f"Автопостер запущен. "
        f"Слоты: {POST_TIMES}",
        flush=True,
    )

    while True:

        now = datetime.now()
        today = now.date()

        if today != last_day:

            posted_today = set()
            last_day = today

        current_slot = now.strftime(
            "%H:%M"
        )

        for slot in POST_TIMES:

            if (
                current_slot == slot
                and slot not in posted_today
            ):

                publish_next_suggested()

                posted_today.add(slot)

        time.sleep(30)


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    oauth_status = (
        "авторизован"
        if VK_USER_TOKEN
        else "НЕ авторизован"
    )

    return (
        "VK suggest autoposter работает.<br>"
        f"VK OAuth: <b>{oauth_status}</b><br>"
        "<br>"
        '<a href="/vk/login">'
        "Авторизоваться через VK"
        "</a>"
    )


# =========================================================
# VK OAUTH LOGIN
# =========================================================

@app.route("/vk/login")
def vk_login():

    global oauth_state

    if not VK_CLIENT_ID:
        return (
            "Ошибка: VK_CLIENT_ID не задан.",
            500,
        )

    if not VK_REDIRECT_URI:
        return (
            "Ошибка: VK_REDIRECT_URI не задан.",
            500,
        )

    # Генерируем одноразовый state
    new_state = secrets.token_urlsafe(32)

    with oauth_state_lock:
        oauth_state = new_state

    params = {
        "client_id": VK_CLIENT_ID,
        "redirect_uri": VK_REDIRECT_URI,
        "response_type": "code",
        "scope": "wall,photos",
        "state": new_state,
    }

    auth_url = (
        "https://oauth.vk.com/authorize?"
        + urlencode(params)
    )

    return redirect(auth_url)


# =========================================================
# VK OAUTH CALLBACK
# =========================================================

@app.route("/vk/oauth/callback")
def vk_oauth_callback():

    global VK_USER_TOKEN
    global oauth_state

    # -----------------------------------------------------
    # VK может вернуть ошибку
    # -----------------------------------------------------

    error = request.args.get("error")

    if error:

        error_description = request.args.get(
            "error_description",
            "",
        )

        return (
            "VK OAuth error: "
            f"{error}<br>"
            f"{error_description}",
            400,
        )

    # -----------------------------------------------------
    # Получаем code
    # -----------------------------------------------------

    code = request.args.get("code")

    if not code:

        return (
            "VK OAuth: code отсутствует.",
            400,
        )

    # -----------------------------------------------------
    # Проверяем state
    # -----------------------------------------------------

    state = request.args.get("state")

    with oauth_state_lock:

        expected_state = oauth_state

        # state одноразовый
        oauth_state = None

    if (
        not state
        or not expected_state
        or state != expected_state
    ):

        return (
            "VK OAuth: неверный state.",
            400,
        )

    # -----------------------------------------------------
    # Проверяем настройки
    # -----------------------------------------------------

    if not VK_CLIENT_ID:
        return (
            "VK_CLIENT_ID не задан.",
            500,
        )

    if not VK_CLIENT_SECRET:
        return (
            "VK_CLIENT_SECRET не задан.",
            500,
        )

    if not VK_REDIRECT_URI:
        return (
            "VK_REDIRECT_URI не задан.",
            500,
        )

    # -----------------------------------------------------
    # Обмениваем code на access_token
    # -----------------------------------------------------

    try:

        response = requests.get(
            "https://oauth.vk.com/access_token",
            params={
                "client_id":
                    VK_CLIENT_ID,

                "client_secret":
                    VK_CLIENT_SECRET,

                "redirect_uri":
                    VK_REDIRECT_URI,

                "code":
                    code,
            },
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        if "error" in data:

            return (
                "Ошибка получения VK access_token:<br>"
                f"<pre>{json.dumps(data, "
                "ensure_ascii=False, indent=2)}</pre>",
                400,
            )

        access_token = (
            data.get("access_token")
            or ""
        ).strip()

        if not access_token:

            return (
                "VK не вернул access_token.<br>"
                f"<pre>{json.dumps(data, "
                "ensure_ascii=False, indent=2)}</pre>",
                400,
            )

        # -------------------------------------------------
        # Сохраняем токен в памяти процесса
        # -------------------------------------------------

        VK_USER_TOKEN = access_token

        print(
            "✅ VK OAuth: пользовательский "
            "access_token получен.",
            flush=True,
        )

        # Не выводим сам токен в логах.

        return (
            "<h2>VK авторизация успешна ✅</h2>"
            "<p>"
            "Пользовательский VK API токен получен."
            "</p>"
            "<p>"
            "Теперь бот может использовать "
            "<b>wall.get filter=suggests</b>."
            "</p>"
            "<p>"
            "Можно закрыть эту страницу."
            "</p>"
        )

    except Exception as e:

        print(
            "VK OAuth callback error:",
            e,
            flush=True,
        )

        return (
            "Ошибка VK OAuth:<br>"
            f"<pre>{e}</pre>",
            500,
        )


# =========================================================
# VK CALLBACK API
# =========================================================

@app.route(
    "/vk/callback",
    methods=["POST"],
)
def vk_callback():

    data = (
        request.get_json(
            force=True,
            silent=True,
        )
        or {}
    )

    # -----------------------------------------------------
    # Подтверждение Callback API
    # -----------------------------------------------------

    if data.get("type") == "confirmation":

        return VK_CONFIRMATION_CODE

    # -----------------------------------------------------
    # Проверяем secret
    # -----------------------------------------------------

    if (
        VK_GROUP_SECRET
        and data.get("secret") != VK_GROUP_SECRET
    ):

        return "ok"

    # -----------------------------------------------------
    # Защита от повторной обработки
    # -----------------------------------------------------

    event_id = data.get("event_id")

    if event_id:

        with processed_events_lock:

            if event_id in processed_events:

                return "ok"

            processed_events.add(
                event_id
            )

            if len(processed_events) > 5000:

                processed_events.clear()

    event_type = data.get("type")

    obj = data.get(
        "object",
        {},
    )

    # -----------------------------------------------------
    # Обработка события
    # -----------------------------------------------------

    try:

        if event_type == "wall_reply_new":

            handle_wall_reply_new(obj)

    except Exception as e:

        print(
            "vk_callback handler error:",
            e,
            flush=True,
        )

    return "ok"


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=autoposter_loop,
        daemon=True,
    ).start()

    port = int(
        os.environ.get(
            "PORT",
            5000,
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
