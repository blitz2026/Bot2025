"""
=========================================================
VK SUGGEST AUTOPOSTER — OAuth версия
=========================================================

Что делает:
  - Берёт записи из предложки сообщества через wall.get
  - wall.get выполняется пользовательским VK OAuth токеном
  - Публикация выполняется токеном сообщества
  - Отправляет скрин в Groq Vision
  - Публикует текст от имени сообщества
  - Удаляет запись из предложки
  - Отвечает на комментарии через Callback API

ENV:

VK_TOKEN
VK_GROUP_ID
GROQ_API_KEY
VK_CONFIRMATION_CODE
VK_GROUP_SECRET

OAuth:

VK_CLIENT_ID
VK_CLIENT_SECRET
VK_REDIRECT_URI
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


VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"

VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()

VK_CLIENT_ID = os.environ.get("VK_CLIENT_ID", "").strip()
VK_CLIENT_SECRET = os.environ.get("VK_CLIENT_SECRET", "").strip()
VK_REDIRECT_URI = os.environ.get("VK_REDIRECT_URI", "").strip()

VK_USER_TOKEN = os.environ.get("VK_USER_TOKEN", "").strip()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

GROUP_ID = int(os.environ.get("VK_GROUP_ID", "0") or 0)

VK_CONFIRMATION_CODE = os.environ.get(
    "VK_CONFIRMATION_CODE",
    ""
).strip()

VK_GROUP_SECRET = os.environ.get(
    "VK_GROUP_SECRET",
    ""
).strip()

GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY",
    ""
).strip()

GROQ_VISION_MODEL = os.environ.get(
    "GROQ_VISION_MODEL",
    "meta-llama/llama-4-scout-17b-16e-instruct",
)

GROQ_TEXT_MODEL = os.environ.get(
    "GROQ_TEXT_MODEL",
    "llama-3.3-70b-versatile",
)

POST_TIMES = ["12:00", "20:00"]

TEST_MODE = (
    os.environ.get("TEST_MODE", "0").strip() == "1"
)

TEST_INTERVAL_MINUTES = int(
    os.environ.get(
        "TEST_INTERVAL_MINUTES",
        "10"
    ) or 10
)

groq_client = (
    Groq(api_key=GROQ_API_KEY)
    if GROQ_API_KEY
    else None
)

oauth_state = None
oauth_state_lock = threading.Lock()

processed_events = set()
processed_events_lock = threading.Lock()


def vk_call(method, token=None, **params):
    access_token = token or VK_TOKEN

    if not access_token:
        raise RuntimeError(
            "VK access_token не задан."
        )

    params["access_token"] = access_token
    params["v"] = VK_VERSION

    response = requests.post(
        f"{VK_API}/{method}",
        data=params,
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    return data["response"]


def _supabase_rest_url(path):
    base = SUPABASE_URL.rstrip("/")

    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]

    return f"{base}/rest/v1/{path}"


def load_token_from_supabase():
    global VK_USER_TOKEN

    if not SUPABASE_URL or not SUPABASE_KEY:
        return

    try:
        response = requests.get(
            _supabase_rest_url("bot_state"),
            params={
                "key": "eq.vk_user_token",
                "select": "value",
            },
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=10,
        )

        response.raise_for_status()

        rows = response.json()

        if rows:
            saved_value = (rows[0].get("value") or "").strip()

            if saved_value:
                VK_USER_TOKEN = saved_value

                print(
                    "✅ VK_USER_TOKEN загружен из Supabase.",
                    flush=True,
                )

    except Exception as e:
        print(
            "Supabase load_token error:",
            e,
            flush=True,
        )


def save_token_to_supabase(token):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return

    try:
        response = requests.post(
            _supabase_rest_url("bot_state"),
            json={
                "key": "vk_user_token",
                "value": token,
            },
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            timeout=10,
        )

        response.raise_for_status()

        print(
            "✅ VK_USER_TOKEN сохранён в Supabase.",
            flush=True,
        )

    except Exception as e:
        print(
            "Supabase save_token error:",
            e,
            flush=True,
        )


def get_suggested_posts(count=1, offset=0):
    if not VK_USER_TOKEN:
        raise RuntimeError(
            "VK_USER_TOKEN отсутствует. "
            "Открой мини-приложение VK и авторизуйся."
        )

    response = vk_call(
        "wall.get",
        token=VK_USER_TOKEN,
        owner_id=-GROUP_ID,
        filter="suggests",
        count=count,
        offset=offset,
    )

    return response.get("items", [])


def get_biggest_photo_url(photo):
    sizes = photo.get("sizes", [])

    if not sizes:
        return None

    biggest = max(
        sizes,
        key=lambda item:
        item.get("width", 0) *
        item.get("height", 0)
    )

    return biggest.get("url")


def get_user_mention(user_id):
    if not user_id or user_id <= 0:
        return "аноним"

    try:
        response = vk_call(
            "users.get",
            user_ids=user_id,
        )

        if response:
            user = response[0]

            name = (
                f"{user.get('first_name', '')} "
                f"{user.get('last_name', '')}"
            ).strip()

            return f"[id{user_id}|{name}]"

    except Exception as e:
        print(
            "users.get error:",
            e,
            flush=True,
        )

    return f"[id{user_id}|автор]"


def _safe_delete(post_id):
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
            groq_client
            .chat
            .completions
            .create(
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
                    True
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


def publish_next_suggested():
    if not VK_TOKEN or not GROUP_ID:
        print(
            "VK_TOKEN / VK_GROUP_ID не заданы.",
            flush=True,
        )
        return

    if not VK_USER_TOKEN:
        print(
            "⚠️ VK_USER_TOKEN отсутствует.",
            flush=True,
        )

        print(
            "Открой мини-приложение VK для авторизации.",
            flush=True,
        )

        return

    try:
        items = get_suggested_posts(count=1)

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

    photo = next(
        (
            attachment["photo"]
            for attachment in post.get(
                "attachments",
                []
            )
            if attachment.get("type") == "photo"
        ),
        None,
    )

    if not photo:
        _safe_delete(post_id)

        print(
            f"Пост {post_id} без фото — "
            "убран из очереди.",
            flush=True,
        )

        return

    photo_url = get_biggest_photo_url(photo)

    ai_result = analyze_screenshot(
        photo_url
    )

    if not ai_result["is_relevant"]:
        _safe_delete(post_id)

        print(
            f"Пост {post_id} отклонён AI "
            "как нерелевантный.",
            flush=True,
        )

        return

    attachment_str = (
        f"photo{photo['owner_id']}_{photo['id']}"
    )

    mention = get_user_mention(
        author_id
    )

    message = (
        f"{ai_result['text']}\n\n"
        f"Прислал: {mention}"
    )

    try:
        vk_call(
            "wall.post",
            owner_id=-GROUP_ID,
            from_group=1,
            message=message,
            attachments=attachment_str,
        )

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
            groq_client
            .chat
            .completions
            .create(
                model=GROQ_TEXT_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": COMMENT_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": comment_text,
                    },
                ],
                max_tokens=120,
                temperature=0.8,
            )
        )

        return (
            completion
            .choices[0]
            .message
            .content
            .strip()
        )

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
            "решили не отвечать.",
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


def autoposter_loop():
    if TEST_MODE:
        print(
            "⚠️ ТЕСТОВЫЙ РЕЖИМ включён — "
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


app = Flask(__name__)


@app.route("/")
def home():
    oauth_status = (
        "авторизован"
        if VK_USER_TOKEN
        else "НЕ авторизован"
    )

    return (
        "VK suggest autoposter работает.<br>"
        f"VK OAuth: <b>{oauth_status}</b><br><br>"
        '<a href="/vk/login">'
        "Авторизоваться через VK (старый способ, "
        "не работает для мини-приложений)"
        "</a><br>"
        '<a href="/vk/miniapp">'
        "Открыть страницу авторизации мини-приложения"
        "</a>"
    )


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


@app.route("/vk/oauth/callback")
def vk_oauth_callback():
    global VK_USER_TOKEN
    global oauth_state

    error = request.args.get(
        "error"
    )

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

    code = request.args.get("code")

    if not code:
        return (
            "VK OAuth: code отсутствует.",
            400,
        )

    state = request.args.get(
        "state"
    )

    with oauth_state_lock:
        expected_state = oauth_state
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

    try:
        response = requests.get(
            "https://oauth.vk.com/access_token",
            params={
                "client_id": VK_CLIENT_ID,
                "client_secret": VK_CLIENT_SECRET,
                "redirect_uri": VK_REDIRECT_URI,
                "code": code,
            },
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        if "error" in data:
            error_json = json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            )

            return (
                "Ошибка получения VK "
                "access_token:<br>"
                "<pre>"
                + error_json
                + "</pre>",
                400,
            )

        access_token = (
            data.get("access_token") or ""
        ).strip()

        if not access_token:
            data_json = json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            )

            return (
                "VK не вернул access_token.<br>"
                "<pre>"
                + data_json
                + "</pre>",
                400,
            )

        VK_USER_TOKEN = access_token

        print(
            "✅ VK OAuth: пользовательский "
            "access_token получен.",
            flush=True,
        )

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
            "<pre>"
            + str(e)
            + "</pre>",
            500,
        )


@app.route("/vk/miniapp")
def vk_miniapp():
    """
    Страница авторизации мини-приложения VK.
    Открывается ВНУТРИ VK (например vk.com/app<ID>).
    Получает пользовательский токен через VK Bridge
    (VKWebAppGetAuthToken) и отправляет его на сервер
    через POST /vk/save-token.
    """

    app_id = VK_CLIENT_ID

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Авторизация БлицСеть</title>
<script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
</head>
<body style="font-family: sans-serif; padding: 20px;">
<h3>Авторизация мини-приложения</h3>
<p id="status">Подключение к VK...</p>

<script>
  var statusEl = document.getElementById('status');

  function setStatus(text) {{
    statusEl.textContent = text;
  }}

  vkBridge.send('VKWebAppInit')
    .then(function () {{
      setStatus('Запрашиваем доступ...');

      return vkBridge.send('VKWebAppGetAuthToken', {{
        app_id: {app_id},
        scope: 'wall,photos,offline'
      }});
    }})
    .then(function (data) {{
      if (!data || !data.access_token) {{
        setStatus('VK не вернул токен.');
        return;
      }}

      setStatus('Токен получен, сохраняем на сервере...');

      return fetch('/vk/save-token', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ access_token: data.access_token }})
      }})
        .then(function (response) {{ return response.json(); }})
        .then(function (result) {{
          if (result && result.ok) {{
            setStatus('Готово! Токен сохранён. Можно закрыть эту страницу.');
          }} else {{
            setStatus('Ошибка сохранения токена на сервере.');
          }}
        }});
    }})
    .catch(function (error) {{
      setStatus('Ошибка: ' + JSON.stringify(error));
    }});
</script>
</body>
</html>"""

    return html


@app.route("/vk/save-token", methods=["POST"])
def vk_save_token():
    global VK_USER_TOKEN

    data = (
        request.get_json(
            force=True,
            silent=True,
        )
        or {}
    )

    access_token = (
        data.get("access_token") or ""
    ).strip()

    if not access_token:
        return (
            json.dumps({"ok": False, "error": "no token"}),
            400,
            {"Content-Type": "application/json"},
        )

    VK_USER_TOKEN = access_token

    print(
        "✅ VK Mini App: пользовательский "
        "access_token получен и сохранён.",
        flush=True,
    )

    save_token_to_supabase(access_token)

    return (
        json.dumps({"ok": True}),
        200,
        {"Content-Type": "application/json"},
    )


@app.route(
    "/vk/callback",
    methods=["POST"]
)
def vk_callback():
    data = (
        request.get_json(
            force=True,
            silent=True,
        )
        or {}
    )

    if data.get("type") == "confirmation":
        return VK_CONFIRMATION_CODE

    if (
        VK_GROUP_SECRET
        and data.get("secret")
        != VK_GROUP_SECRET
    ):
        return "ok"

    event_id = data.get(
        "event_id"
    )

    if event_id:
        with processed_events_lock:
            if event_id in processed_events:
                return "ok"

            processed_events.add(
                event_id
            )

            if len(processed_events) > 5000:
                processed_events.clear()

    event_type = data.get(
        "type"
    )

    obj = data.get(
        "object",
        {},
    )

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


if __name__ == "__main__":
    load_token_from_supabase()

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
