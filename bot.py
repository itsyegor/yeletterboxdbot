"""
Лента: бот собирает новое из разных источников и публикует в Telegram-каналы.
Каждый источник привязывается к своему каналу (или нескольким) командой /bind <источник>.

Источники:
  letterboxd  просмотры людей, на которых ты подписан на Letterboxd
  svetov      посты @msvetov в X с «рекомендую» / «не рекомендую»
  kanoner     посты Канонера во ВКонтакте (vk.com/kanoner), без ключа ВК - новости kanoner.com
  america     анонсы очных лекций о США и Канаде в Петербурге (см. america.py)
  lectures    посты по лекциям Йеля, раз в день (см. lectures.py)

Переменные окружения (Railway -> Variables):
  TELEGRAM_TOKEN      токен бота от BotFather (обязательно)
  OWNER_ID            твой числовой Telegram id (обязательно)
  TMDB_KEY            ключ TMDB - названия фильмов на двух языках
  ANTHROPIC_API_KEY   ключ Claude API - найти фильм в посте Светова, отобрать лекции
  X_AUTH_TOKEN, X_CT0 cookie из браузера, где ты залогинен в X
  VK_SERVICE_TOKEN    сервисный ключ приложения VK ID - читать стену Канонера в ВК
  TIMEPAD_TOKEN       токен Timepad, если API без него не пускает (необязательно)
  STATE_DIR           папка для памяти бота (по умолчанию /data - туда подключается volume)
"""
import os
import random
import re
import json
import html
import time
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

import america
import lectures
import articles
from telegram import Update, InputMediaPhoto, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lenta")

# ─── Настройки ──────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
TMDB_KEY = os.environ.get("TMDB_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
X_AUTH_TOKEN = os.environ.get("X_AUTH_TOKEN", "").strip()
X_CT0 = os.environ.get("X_CT0", "").strip()
CHANNEL_ID_ENV = os.environ.get("CHANNEL_ID", "").strip()
STATE_DIR = os.environ.get("STATE_DIR", "/data")

LETTERBOXD_USER = os.environ.get("LETTERBOXD_USER", "itsyegor")
X_USER = os.environ.get("X_USER", "msvetov")
X_AUTHOR_NAME = "Михаил Светов"
LB_INTERVAL = float(os.environ.get("LB_INTERVAL_HOURS", "6")) * 3600
X_INTERVAL = float(os.environ.get("X_INTERVAL_HOURS", "24")) * 3600
KANONER_INTERVAL = float(os.environ.get("KANONER_INTERVAL_HOURS", "3")) * 3600
KANONER_FEED = os.environ.get("KANONER_FEED", "https://kanoner.com/feed/")
VK_SERVICE_TOKEN = os.environ.get("VK_SERVICE_TOKEN", "").strip()
KANONER_VK_ID = int(os.environ.get("KANONER_VK_ID", "-26680240"))   # vk.com/kanoner
VK_API_VERSION = "5.199"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
AMERICA_INTERVAL = float(os.environ.get("AMERICA_INTERVAL_HOURS", "12")) * 3600
AMERICA_MODEL = os.environ.get("AMERICA_MODEL", CLAUDE_MODEL)
TIMEPAD_TOKEN = os.environ.get("TIMEPAD_TOKEN", "").strip()
LECTURES_MODEL = os.environ.get("LECTURES_MODEL", "claude-sonnet-5-5")
ARTICLES_HOUR = int(os.environ.get("ARTICLES_HOUR", "18"))            # час публикации поста по статье
LECTURES_HOUR = int(os.environ.get("LECTURES_HOUR", "10"))            # час публикации конспекта
LECTURES_UTC_OFFSET = float(os.environ.get("LECTURES_UTC_OFFSET", "5"))  # Алматы

# На случай, если Letterboxd не отдаст серверу страницу подписок
DEFAULT_USERS = [
    "paulschradernot", "maximershov", "firewalkvvithme", "nezjnee",
    "vlashur", "lilfilm", "danli0n_", "masha_was_here",
]

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
ALBUM_LIMIT = 10
SEEN_KEEP = 300
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Accept-Language": "en-US,en;q=0.9",
}
NS = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# ─── Память бота (файл на volume) ───────────────────────────────────────────

class State:
    def __init__(self, directory):
        self.path = os.path.join(directory, "state.json")
        os.makedirs(directory, exist_ok=True)
        self.data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                log.exception("Не прочитал state.json, начинаю с чистого листа")
        self.data.setdefault("lb", {})
        self.data["lb"].setdefault("users", {})      # ник -> {"seen": [...]}
        self.data["lb"].setdefault("added", [])      # добавлены вручную
        self.data["lb"].setdefault("removed", [])    # убраны вручную
        self.data["lb"].setdefault("following", [])  # последний прочитанный список подписок
        self.data.setdefault("x", {})
        self.data["x"].setdefault("seen", [])
        self.data.setdefault("kanoner", {})
        self.data["kanoner"].setdefault("seen", [])
        self.data["kanoner"].setdefault("vk_seen", [])   # номера постов ВК
        am = self.data.setdefault("america", {})
        am.setdefault("tg", {})            # канал -> номер последнего прочитанного поста
        am.setdefault("feeds_seen", [])    # записи RSS
        am.setdefault("pages", {})         # страница афиши -> отпечаток текста
        am.setdefault("timepad_seen", [])  # события Timepad
        am.setdefault("posted", [])        # опубликованные анонсы: {"date", "title"}
        am.setdefault("fails", {})         # источник -> сколько проверок подряд не читается
        lc = self.data.setdefault("lectures", {})
        lc.setdefault("lists", {})         # курс -> номера лекций
        lc.setdefault("used", [])          # использованные сюжеты: "hist-119/5/s2" (раньше главы: "hist-119/5/3")
        lc.setdefault("segments", {})      # "hist-119/5" -> разбивка лекции на сюжеты
        lc.setdefault("exhausted", [])     # лекции, где свободных сюжетов не осталось
        lc.setdefault("posted", [])        # журнал: глава, заголовок, теги, дата
        ar = self.data.setdefault("articles", {})
        ar.setdefault("pages", {})         # источник -> {"max": число страниц ленты, "checked": время}
        ar.setdefault("used", [])          # ссылки на статьи, по которым уже был пост или которые не подошли
        ar.setdefault("posted", [])        # журнал: ссылка, заголовок, теги, дата
        self.data.setdefault("errors", {})
        self.data.setdefault("last", {})
        self.data.setdefault("bindings", {})  # источник -> [id каналов]
        self.data.setdefault("channel_titles", {})
        self._migrate()

    def _migrate(self):
        """Старая версия: один канал на все. Переносим его на letterboxd и svetov."""
        old = self.data.pop("channel_id", None)
        if CHANNEL_ID_ENV and not old:
            old = int(CHANNEL_ID_ENV) if re.fullmatch(r"-?\d+", CHANNEL_ID_ENV) else CHANNEL_ID_ENV
        if old and not self.data["bindings"]:
            self.data["bindings"] = {"letterboxd": [old], "svetov": [old]}
        last = self.data["last"]
        if "lb" in last:
            last.setdefault("letterboxd", last.pop("lb"))
        if "x" in last:
            last.setdefault("svetov", last.pop("x"))

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def channels(self, source):
        got = list(self.data["bindings"].get(source, []))
        if not got and source == "articles":  # статьи по умолчанию идут туда же, куда лекции
            got = list(self.data["bindings"].get("lectures", []))
        return got

    def bind(self, source, chat_id, title=None):
        lst = self.data["bindings"].setdefault(source, [])
        if chat_id not in lst:
            lst.append(chat_id)
        if title:
            self.data["channel_titles"][str(chat_id)] = title
        self.save()

    def unbind(self, source, chat_id):
        lst = self.data["bindings"].get(source, [])
        if chat_id in lst:
            lst.remove(chat_id)
        self.save()

    def title(self, chat_id):
        return self.data["channel_titles"].get(str(chat_id), str(chat_id))


state: State = None  # создается в main()

# ─── Вспомогательное ────────────────────────────────────────────────────────

def esc(s):
    return html.escape(s or "", quote=False)

def stars(rating):
    if rating is None:
        return ""
    full = int(rating)
    half = rating - full >= 0.5
    return "★" * full + ("½" if half else "")

def has_cyrillic(s):
    return bool(re.search(r"[А-Яа-яЁё]", s or ""))

def cut(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"

def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


async def http_get(client, url, **kw):
    r = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=30, **kw)
    r.raise_for_status()
    return r

# ─── Letterboxd ─────────────────────────────────────────────────────────────

def parse_rss(xml_text):
    """Записи дневника из RSS одного человека (списки пропускаются)."""
    root = ET.fromstring(xml_text)
    entries = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        if not (guid.startswith("letterboxd-watch-") or guid.startswith("letterboxd-review-")):
            continue
        title = item.findtext("letterboxd:filmTitle", namespaces=NS) or ""
        year = item.findtext("letterboxd:filmYear", namespaces=NS) or ""
        rating = item.findtext("letterboxd:memberRating", namespaces=NS)
        link = (item.findtext("link") or "").strip()
        m = re.match(r"https://letterboxd\.com/[^/]+/film/([^/]+)/", link)
        film_url = f"https://letterboxd.com/film/{m.group(1)}/" if m else None

        poster, review, spoiler = None, "", False
        soup = BeautifulSoup(item.findtext("description") or "", "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            poster = img["src"]
        parts = []
        for p in soup.find_all("p"):
            if p.find("img"):
                continue
            for br in p.find_all("br"):
                br.replace_with("\n")
            t = p.get_text().strip()
            if not t:
                continue
            if guid.startswith("letterboxd-watch-") and re.match(r"Watched on\b", t):
                continue
            if "This review may contain spoilers" in t:
                spoiler = True
                continue
            parts.append(t)
        review = "\n\n".join(parts)

        pub = item.findtext("pubDate")
        try:
            pub_ts = parsedate_to_datetime(pub).timestamp()
        except Exception:
            pub_ts = 0

        entries.append({
            "guid": guid,
            "title": title,
            "year": year,
            "rating": float(rating) if rating else None,
            "like": (item.findtext("letterboxd:memberLike", namespaces=NS) == "Yes"),
            "rewatch": (item.findtext("letterboxd:rewatch", namespaces=NS) == "Yes"),
            "tmdb_movie": item.findtext("tmdb:movieId", namespaces=NS),
            "tmdb_tv": item.findtext("tmdb:tvId", namespaces=NS),
            "link": link,
            "film_url": film_url,
            "poster": poster,
            "review": review,
            "spoiler": spoiler,
            "creator": item.findtext("dc:creator", namespaces=NS) or "",
            "pub": pub_ts,
        })
    entries.sort(key=lambda e: e["pub"])
    return entries


def parse_following(page_html):
    """Ники со страницы подписок. Возвращает (ники, есть_следующая_страница)."""
    soup = BeautifulSoup(page_html, "html.parser")
    names = []
    links = soup.select("td.table-person a.name") or soup.select(".person-summary a.name")
    for a in links:
        m = re.match(r"^/([^/]+)/$", a.get("href", ""))
        if m and m.group(1) not in names:
            names.append(m.group(1).lower())
    has_next = bool(soup.select_one("a.next"))
    return names, has_next


async def fetch_following(client):
    names = []
    for page in range(1, 21):
        url = f"https://letterboxd.com/{LETTERBOXD_USER}/following/"
        if page > 1:
            url += f"page/{page}/"
        r = await http_get(client, url)
        found, has_next = parse_following(r.text)
        names += [n for n in found if n not in names]
        if not has_next or not found:
            break
    return names


async def current_users(client):
    lb = state.data["lb"]
    try:
        following = await fetch_following(client)
        if following:
            lb["following"] = following
            lb["following_ok"] = True
        else:
            raise RuntimeError("страница подписок пустая или изменилась верстка")
    except Exception as e:
        log.warning("Подписки не прочитаны (%s), беру сохраненный список", e)
        lb["following_ok"] = False
    base = lb["following"] or DEFAULT_USERS
    users = [u for u in base if u not in lb["removed"]]
    users += [u for u in lb["added"] if u not in users]
    return users


_rating_cache = {}

async def lb_rating(client, film_url):
    """Средний рейтинг фильма на Letterboxd ('4.31') или None."""
    if not film_url:
        return None
    if film_url in _rating_cache:
        return _rating_cache[film_url]
    value = None
    try:
        r = await http_get(client, film_url)
        m = re.search(r'name="twitter:data2" content="([\d.]+) out of 5"', r.text)
        if m:
            value = float(m.group(1))
    except Exception as e:
        log.info("Рейтинг не получен для %s: %s", film_url, e)
    _rating_cache[film_url] = value
    return value

# ─── TMDB ───────────────────────────────────────────────────────────────────

_tmdb_cache = {}

async def tmdb_get(client, path, **params):
    if not TMDB_KEY:
        return None
    headers = {"Accept": "application/json"}
    if len(TMDB_KEY) > 40:  # Read Access Token
        headers["Authorization"] = f"Bearer {TMDB_KEY}"
    else:
        params["api_key"] = TMDB_KEY
    r = await client.get(f"https://api.themoviedb.org/3{path}", params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


async def tmdb_titles(client, movie_id=None, tv_id=None):
    """{'ru': ..., 'en': ..., 'poster': url} или пустой словарь."""
    key = ("movie", movie_id) if movie_id else ("tv", tv_id)
    if not key[1] or not TMDB_KEY:
        return {}
    if key in _tmdb_cache:
        return _tmdb_cache[key]
    out = {}
    try:
        name_field = "title" if key[0] == "movie" else "name"
        ru = await tmdb_get(client, f"/{key[0]}/{key[1]}", language="ru-RU")
        en = await tmdb_get(client, f"/{key[0]}/{key[1]}", language="en-US")
        out["ru"] = (ru or {}).get(name_field)
        out["en"] = (en or {}).get(name_field)
        poster_path = (ru or {}).get("poster_path") or (en or {}).get("poster_path")
        if poster_path:
            out["poster"] = f"https://image.tmdb.org/t/p/w780{poster_path}"
        date = (en or {}).get("release_date") or (en or {}).get("first_air_date") or ""
        out["year"] = date[:4]
    except Exception as e:
        log.info("TMDB не ответил для %s: %s", key, e)
    _tmdb_cache[key] = out
    return out


def second_title(main_title, titles):
    """Название на втором языке, если оно отличается от основного."""
    other = titles.get("en") if has_cyrillic(main_title) else titles.get("ru")
    if other and other.strip().lower() != (main_title or "").strip().lower():
        return other
    return None

# ─── Оформление постов Letterboxd ───────────────────────────────────────────

def film_head(e, titles, avg):
    """<b>Название</b> (год) / второе название / рейтинг."""
    line = f"<b>{esc(e['title'])}</b>"
    if e["year"]:
        line += f" ({esc(e['year'])})"
    lines = [line]
    other = second_title(e["title"], titles)
    if other:
        lines.append(esc(other))
    if avg is not None:
        lines.append(f"Letterboxd: ★ {avg:.2f}".replace(".", ",", 1))
    return lines


def member_line(e, show_name=True):
    bits = []
    if e["rating"] is not None:
        bits.append(stars(e["rating"]))
    if e["like"]:
        bits.append("♥")
    if e["rewatch"]:
        bits.append("пересмотр")
    mark = " · ".join(bits) if bits else "без оценки"
    return mark


def review_block(e, limit):
    if not e["review"]:
        return ""
    text = esc(cut(e["review"], limit))
    if e["spoiler"]:
        text = f"<tg-spoiler>{text}</tg-spoiler>"
    return f"<blockquote expandable>{text}</blockquote>"


def profile_link(user, name):
    return f'<a href="https://letterboxd.com/{esc(user)}/">{esc(name or user)}</a>'


def build_single(user, e, titles, avg, review_limit):
    lines = film_head(e, titles, avg)
    lines.append("")
    lines.append(f"{profile_link(user, e['creator'])} — {member_line(e)}")
    rb = review_block(e, review_limit)
    if rb:
        lines.append(rb)
    links = f'<a href="{esc(e["link"])}">Запись</a>'
    if e["film_url"]:
        links += f' · <a href="{esc(e["film_url"])}">Фильм</a>'
    lines.append(links)
    return "\n".join(lines)


def build_group(user, entries, infos, review_limit):
    name = entries[0]["creator"] or user
    n = len(entries)
    head = f"{profile_link(user, name)} — {n} {plural(n, 'фильм', 'фильма', 'фильмов')}"
    blocks = [head]
    for i, e in enumerate(entries, 1):
        titles, avg = infos[e["guid"]]
        t = f'{i}. <a href="{esc(e["link"])}"><b>{esc(e["title"])}</b></a>'
        if e["year"]:
            t += f" ({esc(e['year'])})"
        rows = [t]
        other = second_title(e["title"], titles)
        sub = []
        if other:
            sub.append(esc(other))
        if avg is not None:
            sub.append(f"LB ★ {avg:.2f}".replace(".", ",", 1))
        if sub:
            rows.append(" · ".join(sub))
        rows.append(member_line(e))
        rb = review_block(e, review_limit)
        if rb:
            rows.append(rb)
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def fit(builder, limit):
    """Подбирает длину рецензий так, чтобы текст влез в лимит."""
    for review_limit in (900, 500, 300, 150, 60, 0):
        text = builder(review_limit)
        if len(text) <= limit:
            return text
    return None

# ─── Отправка в Telegram ────────────────────────────────────────────────────

async def _tg_call(fn, *args, **kwargs):
    for attempt in range(3):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
    return await fn(*args, **kwargs)


async def download(client, url):
    # некоторые сайты (Канонер) отдают картинки только со ссылкой на свою страницу
    p = httpx.URL(url)
    r = await client.get(url, headers={**HEADERS, "Referer": f"{p.scheme}://{p.host}/"},
                         follow_redirects=True, timeout=30)
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/"):
        raise ValueError(f"вместо картинки пришло {r.headers.get('content-type')}")
    return r.content


async def send_post(bot, client, chat_id, text_builder, images, preview_url=None, delivered=None):
    """Пост с одной картинкой или альбомом. text_builder(limit) -> текст.

    preview_url - пост без картинки, но с превью этой ссылки над текстом (видео YouTube).
    delivered - [False]; становится [True], как только хоть одно сообщение поста дошло до канала.
    """
    delivered = delivered if delivered is not None else [False]

    async def tg_call(fn, *args, **kwargs):
        # Telegram часто успевает принять сообщение, но не ответить вовремя (альбом из 10 фото).
        # Повтор в этом случае дает дубль, поэтому таймаут считаем доставкой.
        try:
            result = await _tg_call(fn, *args, **kwargs)
        except TimedOut:
            log.warning("Telegram не ответил вовремя на %s, считаю отправленным", getattr(fn, "__name__", fn))
            delivered[0] = True
            return None
        delivered[0] = True
        return result

    if preview_url and not images:
        msg = await tg_call(bot.send_message, chat_id, text=text_builder(MESSAGE_LIMIT), parse_mode=ParseMode.HTML,
                            link_preview_options=LinkPreviewOptions(url=preview_url, prefer_large_media=True,
                                                                    show_above_text=True))
        log.info("Превью %s: Telegram вернул %s", preview_url, getattr(msg, "link_preview_options", None))
        return msg
    images = [u for u in images if u][:ALBUM_LIMIT]

    if len(images) <= 1:
        image = images[0] if images else None
        caption = fit(text_builder, CAPTION_LIMIT) if image else None
        if image and caption:
            try:
                return await tg_call(bot.send_photo, chat_id, photo=image, caption=caption, parse_mode=ParseMode.HTML)
            except TelegramError as e:
                log.info("Фото по ссылке не ушло (%s), загружаю сам", e)
                try:
                    data = await download(client, image)
                    return await tg_call(bot.send_photo, chat_id, photo=data, caption=caption, parse_mode=ParseMode.HTML)
                except Exception:
                    log.exception("Фото не загрузилось, отправляю с превью")
        text = fit(text_builder, MESSAGE_LIMIT) or cut(text_builder(0), MESSAGE_LIMIT)
        preview = (LinkPreviewOptions(url=image, prefer_large_media=True, show_above_text=True)
                   if image else LinkPreviewOptions(is_disabled=True))
        return await tg_call(bot.send_message, chat_id, text=text, parse_mode=ParseMode.HTML,
                             link_preview_options=preview)

    # Альбом
    caption = fit(text_builder, CAPTION_LIMIT)

    async def send_album(media_list):
        kwargs = {"caption": caption, "parse_mode": ParseMode.HTML} if caption else {}
        return await tg_call(bot.send_media_group, chat_id, media=media_list, **kwargs)

    try:
        await send_album([InputMediaPhoto(u) for u in images])
    except TelegramError as e:
        log.info("Альбом по ссылкам не ушел (%s), загружаю сам", e)
        files = []
        for u in images:
            try:
                files.append(InputMediaPhoto(await download(client, u)))
            except Exception:
                pass
        if len(files) >= 2:
            await send_album(files)
        elif files:
            await tg_call(bot.send_photo, chat_id, photo=files[0].media)
    if not caption:
        text = fit(text_builder, MESSAGE_LIMIT) or cut(text_builder(0), MESSAGE_LIMIT)
        await tg_call(bot.send_message, chat_id, text=text, parse_mode=ParseMode.HTML,
                      link_preview_options=LinkPreviewOptions(is_disabled=True))


async def broadcast(bot, client, channels, text_builder, images, preview_url=None):
    """Один пост во все каналы источника. Ошибка в одном канале не мешает остальным."""
    sent = 0
    for chat_id in channels:
        delivered = [False]
        try:
            await send_post(bot, client, chat_id, text_builder, images, preview_url, delivered)
            sent += 1
        except Exception:
            log.exception("Пост не ушел в канал %s", chat_id)
            if delivered[0]:
                sent += 1  # часть поста уже в канале: повторять целиком нельзя, будет дубль
    if channels and not sent:
        raise RuntimeError("пост не ушел ни в один канал")


async def notify_owner(bot, key, text):
    """Сообщение владельцу об ошибке, не чаще раза в сутки на источник."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.data["errors"].get(key) == today or not OWNER_ID:
        return
    state.data["errors"][key] = today
    state.save()
    try:
        await bot.send_message(OWNER_ID, text)
    except TelegramError:
        log.warning("Не смог написать владельцу (он нажимал /start у бота?)")

# ─── Проверка Letterboxd ────────────────────────────────────────────────────

_lb_lock = asyncio.Lock()

async def check_letterboxd(bot):
    async with _lb_lock:
        channels = state.channels("letterboxd")
        if not channels:
            log.warning("letterboxd: нет канала, напиши /bind letterboxd в канале")
            return 0
        posted = 0
        failed = []
        async with httpx.AsyncClient() as client:
            users = await current_users(client)
            for user in users:
                try:
                    r = await http_get(client, f"https://letterboxd.com/{user}/rss/")
                    entries = parse_rss(r.text)
                except Exception as e:
                    log.warning("RSS %s: %s", user, e)
                    failed.append(user)
                    continue

                ustate = state.data["lb"]["users"].get(user)
                if ustate is None:
                    # Новый человек: все текущее считаем прочитанным, ничего старого не шлем
                    state.data["lb"]["users"][user] = {"seen": [e["guid"] for e in entries][-SEEN_KEEP:]}
                    state.save()
                    log.info("%s: новый, отмечено %d записей", user, len(entries))
                    continue

                seen = set(ustate["seen"])
                new = [e for e in entries if e["guid"] not in seen]
                if not new:
                    continue

                for i in range(0, len(new), ALBUM_LIMIT):
                    chunk = new[i:i + ALBUM_LIMIT]
                    infos = {}
                    for e in chunk:
                        titles = await tmdb_titles(client, e["tmdb_movie"], e["tmdb_tv"])
                        avg = await lb_rating(client, e["film_url"])
                        infos[e["guid"]] = (titles, avg)
                        if not e["poster"] and titles.get("poster"):
                            e["poster"] = titles["poster"]
                    try:
                        if len(chunk) == 1:
                            e = chunk[0]
                            titles, avg = infos[e["guid"]]
                            await broadcast(bot, client, channels,
                                            lambda lim, e=e, t=titles, a=avg: build_single(user, e, t, a, lim),
                                            [e["poster"]])
                        else:
                            await broadcast(bot, client, channels,
                                            lambda lim, c=chunk: build_group(user, c, infos, lim),
                                            [e["poster"] for e in chunk])
                        posted += 1
                    except Exception:
                        log.exception("Пост для %s не отправлен", user)
                        break
                    ustate["seen"] = (ustate["seen"] + [e["guid"] for e in chunk])[-SEEN_KEEP:]
                    state.save()
                    await asyncio.sleep(3)

        if failed and len(failed) == len(users):
            await notify_owner(bot, "letterboxd", "Лента: Letterboxd не отдает ленты. Проверю снова через 6 часов.")
        state.data["last"]["letterboxd"] = time.time()
        state.save()
        log.info("Letterboxd: %d постов", posted)
        return posted

# ─── Светов в X ─────────────────────────────────────────────────────────────

# "Рекомендую" должно стоять в самом конце (после него не больше трех слов)
VERDICT_RE = re.compile(r"(не\s+)?рекомендую(?:\s+\S+){0,3}\W*$", re.IGNORECASE)

def verdict(text):
    """'рекомендую' / 'не рекомендую' в конце поста, иначе None."""
    clean = re.sub(r"https?://\S+", "", text or "").strip()
    m = VERDICT_RE.search(clean)
    if not m:
        return None
    return "не рекомендую" if m.group(1) else "рекомендую"


async def find_film_in_text(text):
    """Claude достает название фильма из поста. {'title','title_ru','year'} или None."""
    if not ANTHROPIC_API_KEY:
        return None
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "Это пост с рецензией на фильм или сериал. Определи, о каком произведении речь.\n"
        "Ответь только JSON без пояснений: "
        '{"title": "оригинальное или английское название", "title_ru": "русское название", '
        '"year": 2024, "type": "movie" или "tv"}. '
        "Если неизвестно поле - null. Если в посте нет конкретного фильма или сериала - ответь null.\n\n"
        f"Пост:\n{text}"
    )
    resp = await client.messages.create(model=CLAUDE_MODEL, max_tokens=200,
                                        messages=[{"role": "user", "content": prompt}])
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if (data.get("title") or data.get("title_ru")) else None


async def film_by_guess(client, guess):
    """По догадке Claude находит фильм в TMDB и на Letterboxd."""
    kind = "tv" if guess.get("type") == "tv" else "movie"
    for query in [guess.get("title"), guess.get("title_ru")]:
        if not query:
            continue
        params = {"query": query, "language": "ru-RU"}
        if guess.get("year"):
            params["year" if kind == "movie" else "first_air_date_year"] = guess["year"]
        try:
            res = await tmdb_get(client, f"/search/{kind}", **params)
        except Exception as e:
            log.info("Поиск TMDB: %s", e)
            continue
        results = (res or {}).get("results") or []
        if not results:
            continue
        tid = results[0]["id"]
        titles = await tmdb_titles(client, movie_id=tid if kind == "movie" else None,
                                   tv_id=tid if kind == "tv" else None)
        film_url, avg = None, None
        try:
            r = await http_get(client, f"https://letterboxd.com/tmdb/{tid}/")
            if "/film/" in str(r.url):
                film_url = str(r.url)
                m = re.search(r'name="twitter:data2" content="([\d.]+) out of 5"', r.text)
                avg = float(m.group(1)) if m else None
        except Exception:
            pass
        return {"titles": titles, "film_url": film_url, "avg": avg}
    return None


def build_x_post(tweet_text, tweet_url, verdict_word, film, guess, limit):
    lines = []
    if film or guess:
        titles = (film or {}).get("titles", {})
        main = titles.get("en") or (guess or {}).get("title") or titles.get("ru") or (guess or {}).get("title_ru")
        year = titles.get("year") or (guess or {}).get("year")
        head = f"<b>{esc(main)}</b>" + (f" ({esc(str(year))})" if year else "")
        lines.append(head)
        ru = titles.get("ru") or (guess or {}).get("title_ru")
        if ru and ru.lower() != (main or "").lower():
            lines.append(esc(ru))
        if film and film.get("avg") is not None:
            lines.append(f"Letterboxd: ★ {film['avg']:.2f}".replace(".", ",", 1))
        lines.append("")
    mark = "👍" if verdict_word == "рекомендую" else "👎"
    lines.append(f"<b>{X_AUTHOR_NAME}</b> — {mark} {verdict_word}")
    lines.append(f"<blockquote expandable>{esc(cut(tweet_text, limit))}</blockquote>")
    links = f'<a href="{esc(tweet_url)}">Пост в X</a>'
    if film and film.get("film_url"):
        links += f' · <a href="{esc(film["film_url"])}">Фильм</a>'
    lines.append(links)
    return "\n".join(lines)


def tweet_text_expanded(tweet):
    text = tweet.full_text or tweet.text or ""
    for u in tweet.urls or []:
        short, full = u.get("url"), u.get("expanded_url")
        if short and full:
            text = text.replace(short, full)
    # ссылка на сами картинки поста не нужна
    text = re.sub(r"\s*https://t\.co/\w+\s*$", "", text)
    return text.strip()


_x_lock = asyncio.Lock()

async def check_x(bot, x_client=None):
    async with _x_lock:
        if not (X_AUTH_TOKEN and X_CT0) and x_client is None:
            return 0
        channels = state.channels("svetov")
        if not channels:
            log.warning("svetov: нет канала, напиши /bind svetov в канале")
            return 0
        xs = state.data["x"]
        posted = 0
        try:
            if x_client is None:
                from twikit import Client
                x_client = Client("en-US")
                x_client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0})
            if not xs.get("user_id"):
                user = await x_client.get_user_by_screen_name(X_USER)
                xs["user_id"] = user.id
            tweets = list(await x_client.get_user_tweets(xs["user_id"], "Tweets", count=40))
        except Exception as e:
            log.exception("X недоступен")
            await notify_owner(bot, "svetov", f"Лента: не получилось прочитать X ({type(e).__name__}). "
                                              "Если повторится, скорее всего истекли cookie - обнови X_AUTH_TOKEN и X_CT0.")
            state.data["last"]["svetov"] = time.time()
            state.save()
            return 0

        first_run = not xs.get("initialized")
        seen = set(xs["seen"])
        new = [t for t in tweets if str(t.id) not in seen]
        new.sort(key=lambda t: int(t.id))

        if first_run:
            xs["seen"] = [str(t.id) for t in tweets][-SEEN_KEEP:]
            xs["initialized"] = True
            state.data["last"]["svetov"] = time.time()
            state.save()
            log.info("X: первый запуск, отмечено %d постов", len(tweets))
            return 0

        async with httpx.AsyncClient() as client:
            for t in new:
                xs["seen"] = (xs["seen"] + [str(t.id)])[-SEEN_KEEP:]
                if getattr(t, "retweeted_tweet", None) is not None:
                    state.save()
                    continue
                text = tweet_text_expanded(t)
                v = verdict(text)
                if not v:
                    state.save()
                    continue
                guess, film = None, None
                try:
                    guess = await find_film_in_text(text)
                    if guess:
                        film = await film_by_guess(client, guess)
                except Exception:
                    log.exception("Не определил фильм в посте")
                url = f"https://x.com/{X_USER}/status/{t.id}"
                images = []
                if film and film["titles"].get("poster"):
                    images.append(film["titles"]["poster"])
                else:
                    for m in t.media or []:
                        if getattr(m, "type", "") == "photo" and getattr(m, "media_url", None):
                            images.append(m.media_url)
                            break
                try:
                    await broadcast(bot, client, channels,
                                    lambda lim, tx=text, u=url, vv=v, f=film, g=guess:
                                        build_x_post(tx, u, vv, f, g, lim),
                                    images[:1])
                    posted += 1
                except Exception:
                    log.exception("Пост Светова не отправлен")
                state.save()
                await asyncio.sleep(3)

        state.data["last"]["svetov"] = time.time()
        state.save()
        log.info("X: %d постов", posted)
        return posted

# ─── Канонер ────────────────────────────────────────────────────────────────

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

def _first_image(html_text):
    if not html_text:
        return None
    soup = BeautifulSoup(html_text, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if not src or "/imgst/" in src or src.endswith(".gif"):
            continue  # служебные значки сайта
        return src
    return None


def _abs(base, url):
    return urljoin(base or "https://kanoner.com/", full_size(url)) if url else None


def _plain(html_text):
    soup = BeautifulSoup(html_text or "", "html.parser")
    text = soup.get_text(" ").strip()
    text = re.sub(r"\s+", " ", text)
    # хвост, который WordPress дописывает в анонс
    text = re.sub(r"\s*(Сообщение|Запись|The post)\s.*?(появились сначала|appeared first).*$", "", text)
    text = re.sub(r"\s*(\[…\]|\[\.\.\.\]|Читать далее.*)$", "", text)
    return text.strip()


def parse_kanoner(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not guid:
            continue
        desc = item.findtext("description") or ""
        content = item.findtext(CONTENT_NS) or ""
        try:
            pub = parsedate_to_datetime(item.findtext("pubDate")).timestamp()
        except Exception:
            pub = 0
        # номер статьи в адресе не меняется, когда сайт правит заголовок или текст
        m = re.search(r"/\d{4}/\d{2}/\d{2}/(\d+)", link) or re.search(r"[?&]p=(\d+)", guid)
        out.append({
            "guid": guid,
            "key": f"id:{m.group(1)}" if m else guid,
            "title": html.unescape((item.findtext("title") or "").strip()),
            "link": link,
            # теги сайта вида «компания_кгиоп» не показываем, только рубрики
            "categories": [c.text.strip() for c in item.findall("category")
                           if c.text and c.text.strip() and "_" not in c.text],
            "summary": _plain(desc) or _plain(content),
            "image": _abs(link, _first_image(content) or _first_image(desc)),
            "pub": pub,
        })
    out.sort(key=lambda e: e["pub"])
    return out


IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp)(\?.*)?$", re.I)

def full_size(url):
    """WordPress-превью photo-400x200.jpg -> оригинал photo.jpg"""
    return re.sub(r"-\d+x\d+(\.(?:jpe?g|png|webp))$", r"\1", url, flags=re.I)


def article_image_from_html(page_html, page_url):
    """Первое фото статьи: обложка страницы, иначе первая картинка из /pics/ в тексте."""
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page_html)
    if m and "/imgst/" not in m.group(1):
        return urljoin(page_url, m.group(1))
    soup = BeautifulSoup(page_html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if "/pics/" not in src:
            continue  # значки сайта, счетчики
        a = img.find_parent("a")
        if a and IMAGE_EXT.search(a.get("href") or ""):
            return urljoin(page_url, a["href"])   # ссылка на полноразмерное фото
        return urljoin(page_url, full_size(src))
    return None


def article_paragraphs(page_html):
    """Абзацы текста статьи: после заголовка h1 и до «Смотрите также» / комментариев."""
    soup = BeautifulSoup(page_html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        return []
    out = []
    for p in h1.find_all_next(["p", "h2", "h3"]):
        t = re.sub(r"\s+", " ", p.get_text(" ")).strip()
        if p.name != "p" or re.match(r"(Смотрите также|Комментари|Изображения, прикрепленные)", t):
            if out:
                break
            continue
        if len(t) < 40 or t.startswith("Фото "):
            continue
        out.append(t)
        if len(out) >= 4:
            break
    return out


async def article_info(client, url):
    """{'image': url или None, 'paragraphs': [...]} со страницы статьи."""
    try:
        r = await http_get(client, url)
        return {"image": article_image_from_html(r.text, url), "paragraphs": article_paragraphs(r.text)}
    except Exception as e:
        log.info("Статья не открылась %s: %s", url, e)
    return {"image": None, "paragraphs": []}


def _stems(text):
    """Значимые слова по первым 5 буквам: «Митрофаньевского» и «Митрофаньевском» совпадут."""
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]+", (text or "").lower()) if len(w) > 3}


def _repeats(sentence, reference, share=0.5):
    ref = _stems(reference)
    return bool(ref) and len(_stems(sentence) & ref) >= max(2, share * len(ref))


def summary_without_title(title, lead, paragraphs, want=200):
    """Лид без предложений, пересказывающих заголовок; если мало - добавить абзац статьи."""
    sentences = re.split(r"(?<=[.!?…])\s+", lead or "")
    kept = [s for s in sentences if s and not _repeats(s, title)]
    text = " ".join(kept).strip()
    for p in paragraphs:
        if len(text) >= want:
            break
        if _repeats(p, title) or (lead and _repeats(p, lead, 0.6)):
            continue  # тот же лид или тот же заголовок
        text = (text + " " + p).strip()
    return text


def build_kanoner(e, limit):
    lines = [f"<b>{esc(e['title'])}</b>"]
    if e["categories"]:
        lines.append(f"<i>{esc(' · '.join(e['categories'][:2]))}</i>")
    if e["summary"] and limit:
        lines.append("")
        lines.append(esc(cut(e["summary"], min(limit, 600))))
    lines.append("")
    lines.append(f'<a href="{esc(e["link"])}">Читать на Канонере</a>')
    return "\n".join(lines)



# ─── Канонер во ВКонтакте ───────────────────────────────────────────────────
# Стена сообщества через API ВК (wall.get) с сервисным ключом приложения VK ID.
# В ВК у Канонера больше заметок, чем на сайте, и у каждой есть фото.

VK_MAX_AGE = 3 * 86400   # старые посты (например, новый закреп) не присылаем

class VKError(Exception):
    pass


async def vk_wall(client, owner_id, count=30):
    # POST: ключ уходит в теле запроса и не попадает в логи вместе с адресом
    r = await client.post("https://api.vk.com/method/wall.get",
                          data={"owner_id": owner_id, "count": count, "extended": 0,
                                "access_token": VK_SERVICE_TOKEN, "v": VK_API_VERSION},
                          headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise VKError(f"{err.get('error_code')}: {err.get('error_msg')}")
    return data["response"]["items"]


def _vk_photo_url(photo):
    """Самый большой размер фото."""
    orig = (photo.get("orig_photo") or {}).get("url")
    if orig:
        return orig
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(sizes, key=lambda z: (z.get("width") or 0) * (z.get("height") or 0))
    return best.get("url")


def _vk_video_preview(video):
    imgs = video.get("image") or []
    if not imgs:
        return None
    return max(imgs, key=lambda z: (z.get("width") or 0) * (z.get("height") or 0)).get("url")


VK_MENTION = re.compile(r"\[((?:club|public|id|event)\d+|[\w.]+)\|([^\]]+)\]")
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[\w.-]+\.(?:com|ru|рф|org|net|me|su)(?:/[^\s<]*)?", re.I)


def vk_text_html(text):
    """Текст поста ВК -> HTML Telegram: упоминания [club1|Имя] и голые ссылки становятся ссылками."""
    out, pos = [], 0
    for m in VK_MENTION.finditer(text):
        out.append(("t", text[pos:m.start()]))
        out.append(("a", m.group(2), f"https://vk.com/{m.group(1)}"))
        pos = m.end()
    out.append(("t", text[pos:]))
    res = []
    for part in out:
        if part[0] == "a":
            res.append(f'<a href="{esc(part[2])}">{esc(part[1])}</a>')
            continue
        chunk, p = part[1], 0
        for m in URL_RE.finditer(chunk):
            url = m.group(0).rstrip(".,;:!?)»\"")
            res.append(esc(chunk[p:m.start()]))
            href = url if url.lower().startswith("http") else "https://" + url
            res.append(f'<a href="{esc(href)}">{esc(url)}</a>')
            p = m.start() + len(url)
        res.append(esc(chunk[p:]))
    return "".join(res)


def parse_vk_post(item):
    """Пост стены -> {'id', 'text', 'images', 'link', 'date'}; None для рекламы."""
    if item.get("marked_as_ads"):
        return None
    src = item
    text = (item.get("text") or "").strip()
    reposted = None
    if item.get("copy_history"):
        src = item["copy_history"][0]
        reposted = src
        inner = (src.get("text") or "").strip()
        text = (text + "\n\n" + inner).strip() if text else inner
    images, extra = [], []
    for a in src.get("attachments") or []:
        t = a.get("type")
        if t == "photo":
            images.append(_vk_photo_url(a["photo"]))
        elif t == "video":
            extra.append(_vk_video_preview(a["video"]))
        elif t == "link":
            ph = (a["link"].get("photo") or {})
            extra.append(_vk_photo_url(ph) if ph else None)
            url = a["link"].get("url") or ""
            if url and url.split("//")[-1].split("?")[0].rstrip("/") not in text:
                text = (text + "\n" + url).strip()
    images = [u for u in images if u] or [u for u in extra if u][:1]
    pid = f"{item['owner_id']}_{item['id']}"
    return {
        "id": pid,
        "text": text,
        "images": images[:ALBUM_LIMIT],
        "link": f"https://vk.com/wall{pid}",
        "date": item.get("date") or 0,
        "pinned": bool(item.get("is_pinned")),
        "repost_of": f"{reposted['owner_id']}_{reposted['id']}" if reposted else None,
    }


def build_vk_post(p, limit):
    body = cut(p["text"], min(limit, 900)) if limit else ""
    lines = [vk_text_html(body)] if body else []
    lines.append(f'<a href="{esc(p["link"])}">Пост Канонера в ВК</a>')
    return "\n\n".join(lines)


async def check_kanoner_vk(bot, channels):
    ks = state.data["kanoner"]
    posted = 0
    async with httpx.AsyncClient() as client:
        try:
            posts = [p for p in (parse_vk_post(i) for i in await vk_wall(client, KANONER_VK_ID)) if p]
        except Exception as e:
            log.exception("ВК Канонера недоступен")
            await notify_owner(bot, "kanoner", f"Лента: не получилось прочитать Канонер в ВК ({type(e).__name__}: {e}).")
            state.data["last"]["kanoner"] = time.time()
            state.save()
            return 0
        posts.sort(key=lambda p: p["date"])

        if not ks.get("vk_initialized"):
            ks["vk_seen"] = [p["id"] for p in posts][-SEEN_KEEP:]
            ks["vk_initialized"] = True
            state.data["last"]["kanoner"] = time.time()
            state.save()
            log.info("Канонер ВК: первый запуск, отмечено %d постов", len(posts))
            return 0

        seen = set(ks["vk_seen"])
        now = time.time()
        for p in posts:
            if p["id"] in seen:
                continue
            if now - p["date"] > VK_MAX_AGE or not (p["text"] or p["images"]):
                ks["vk_seen"] = (ks["vk_seen"] + [p["id"]])[-SEEN_KEEP:]
                continue
            try:
                await broadcast(bot, client, channels, lambda lim, p=p: build_vk_post(p, lim), p["images"])
                posted += 1
            except Exception:
                log.exception("Пост Канонера из ВК не отправлен")
                break
            ks["vk_seen"] = (ks["vk_seen"] + [p["id"]])[-SEEN_KEEP:]
            state.save()
            await asyncio.sleep(3)

    state.data["last"]["kanoner"] = time.time()
    state.save()
    log.info("Канонер ВК: %d постов", posted)
    return posted

_kanoner_lock = asyncio.Lock()

async def check_kanoner(bot):
    async with _kanoner_lock:
        channels = state.channels("kanoner")
        if not channels:
            return 0
        if VK_SERVICE_TOKEN:
            return await check_kanoner_vk(bot, channels)
        ks = state.data["kanoner"]
        posted = 0
        async with httpx.AsyncClient() as client:
            try:
                r = await http_get(client, KANONER_FEED)
                entries = parse_kanoner(r.text)
            except Exception as e:
                log.exception("Канонер недоступен")
                await notify_owner(bot, "kanoner", f"Лента: не получилось прочитать Канонер ({type(e).__name__}).")
                state.data["last"]["kanoner"] = time.time()
                state.save()
                return 0

            if not ks.get("initialized"):
                ks["seen"] = [k for e in entries for k in (e["key"], e["guid"])][-SEEN_KEEP:]
                ks["initialized"] = True
                state.data["last"]["kanoner"] = time.time()
                state.save()
                log.info("Канонер: первый запуск, отмечено %d новостей", len(entries))
                return 0

            # уже отправленные: по номеру статьи; старые записи памяти хранили guid
            seen = set(ks["seen"])
            if not ks.get("keys_migrated"):
                for e in entries:
                    if e["guid"] in seen:
                        seen.add(e["key"])
                        ks["seen"].append(e["key"])
                ks["keys_migrated"] = True
                state.save()
            fresh, keys = [], set()
            for x in entries:
                if x["key"] in seen or x["guid"] in seen or x["key"] in keys:
                    continue
                keys.add(x["key"])
                fresh.append(x)

            for e in fresh:
                info = await article_info(client, e["link"])
                image = e["image"] or info["image"]
                if not image:
                    log.info("Канонер: фото не найдено для %s", e["link"])
                e["summary"] = summary_without_title(e["title"], e["summary"], info["paragraphs"])
                try:
                    await broadcast(bot, client, channels, lambda lim, e=e: build_kanoner(e, lim), [image])
                    posted += 1
                except Exception:
                    log.exception("Новость Канонера не отправлена")
                    break
                ks["seen"] = (ks["seen"] + [e["key"], e["guid"]])[-SEEN_KEEP:]
                state.save()
                await asyncio.sleep(3)

        state.data["last"]["kanoner"] = time.time()
        state.save()
        log.info("Канонер: %d постов", posted)
        return posted

# ─── Американистика: анонсы лекций ──────────────────────────────────────────

async def claude_text(prompt, model, max_tokens=4000):
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    resp = await client.messages.create(model=model, max_tokens=max_tokens,
                                        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


async def claude_json(prompt, model, schema, max_tokens=4000):
    """Ответ по схеме через инструмент answer: кавычки в тексте не ломают JSON.

    Принудительный вызов (tool_choice "tool") новые модели не принимают, поэтому "auto"
    и просьба в промпте; если модель все же ответила текстом - разбираем JSON из текста.
    """
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt + "\n\nОтветь одним вызовом инструмента answer."}],
        tools=[{"name": "answer", "description": "Ответ на задание", "input_schema": schema}],
        tool_choice={"type": "auto"})
    for b in resp.content:
        if getattr(b, "type", "") == "tool_use":
            return b.input
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    log.warning("Claude ответил текстом, а не инструментом (stop_reason=%s): %.500s", resp.stop_reason, text)
    return text


async def america_tg(client, handle, last):
    """Новые посты канала после last (на первой проверке - все, что видно). -> (посты, номер последнего)."""
    r = await http_get(client, f"https://t.me/s/{handle}")
    posts = america.parse_tg(r.text, handle)
    if not posts:
        raise RuntimeError("в веб-версии канала нет постов")
    # с прошлой проверки вышло больше, чем помещается на страницу: читаем еще одну
    if last and posts[0]["id"] > last + 1:
        before = america.tg_before(r.text)
        if before:
            r2 = await http_get(client, f"https://t.me/s/{handle}?before={before}")
            older = america.parse_tg(r2.text, handle)
            posts = sorted({p["id"]: p for p in older + posts}.values(), key=lambda p: p["id"])
    new = [p for p in posts if not last or p["id"] > last]
    return new, posts[-1]["id"]


class TimepadAuth(Exception):
    pass


async def america_timepad(client, seen):
    """Будущие события Timepad в Петербурге по ключевым словам. -> [{'id','text','url'}]."""
    headers = {**HEADERS, "Accept": "application/json"}
    if TIMEPAD_TOKEN:
        headers["Authorization"] = f"Bearer {TIMEPAD_TOKEN}"
    today = datetime.now(america.MSK).strftime("%Y-%m-%d")
    found = {}
    for kw in america.TIMEPAD_KEYWORDS:
        params = {"limit": 100, "cities": america.TIMEPAD_CITY, "keywords": kw,
                  "starts_at_min": today, "fields": "location,description_short", "sort": "+starts_at"}
        r = await client.get("https://api.timepad.ru/v1/events", params=params, headers=headers, timeout=30)
        if r.status_code == 400:  # не понравился формат даты - даты все равно проверит Claude
            params.pop("starts_at_min")
            r = await client.get("https://api.timepad.ru/v1/events", params=params, headers=headers, timeout=30)
        if r.status_code in (401, 403):
            raise TimepadAuth(str(r.status_code))
        r.raise_for_status()
        for c in america.timepad_candidates(r.json()):
            if c["id"] not in seen:
                found[c["id"]] = c
    return list(found.values())


_america_lock = asyncio.Lock()

async def check_america(bot):
    async with _america_lock:
        channels = state.channels("america")
        if not channels:
            return 0
        st = state.data["america"]
        if not ANTHROPIC_API_KEY:
            await notify_owner(bot, "america", "Американистика: без ANTHROPIC_API_KEY не могу отбирать анонсы.")
            state.data["last"]["america"] = time.time()
            state.save()
            return 0

        candidates = []  # {'source', 'url', 'text', 'kind'}
        commits = []     # что запомнить, когда Claude отберет анонсы
        failed, ok = {}, set()

        async with httpx.AsyncClient() as client:
            for handle, venue in america.TG_CHANNELS.items():
                name = f"t.me/{handle}"
                try:
                    new, last_id = await america_tg(client, handle, st["tg"].get(handle))
                    commits.append(("tg", handle, last_id))
                    candidates += [{"source": venue, "url": p["url"], "text": p["text"], "kind": "post"}
                                   for p in new if america.relevant(p["text"])]
                    ok.add(name)
                except Exception as e:
                    failed[name] = e

            for feed in america.FEEDS:
                try:
                    r = await http_get(client, feed["url"])
                    seen = set(st["feeds_seen"])
                    for it in america.parse_feed(r.text):
                        if it["id"] in seen:
                            continue
                        commits.append(("feed", it["id"]))
                        if america.relevant(it["text"]):
                            # в RSS нет даты и места - они на странице события
                            text = it["text"]
                            try:
                                ev_page = await http_get(client, it["url"])
                                text = america.page_text(ev_page.text, str(ev_page.url), limit=6000)
                            except Exception as e:
                                log.info("Страница события не открылась %s: %s", it["url"], e)
                            candidates.append({"source": feed["name"], "url": it["url"], "text": text, "kind": "post"})
                    ok.add(feed["url"])
                except Exception as e:
                    failed[feed["url"]] = e

            for page in america.PAGES:
                try:
                    r = await http_get(client, page["url"])
                    text = america.page_text(r.text, str(r.url))
                    ok.add(page["url"])
                    h = america.digest(text)
                    if h == st["pages"].get(page["url"]):
                        continue
                    commits.append(("page", page["url"], h))
                    if america.relevant(text):
                        candidates.append({"source": page["name"], "url": page["url"], "text": text, "kind": "page"})
                except Exception as e:
                    failed[page["url"]] = e

            if TIMEPAD_TOKEN or not st.get("timepad_needs_token"):
                try:
                    for c in await america_timepad(client, set(st["timepad_seen"])):
                        commits.append(("timepad", c["id"]))
                        candidates.append({"source": "Timepad", "url": c["url"], "text": c["text"], "kind": "post"})
                    st.pop("timepad_needs_token", None)
                    ok.add("Timepad")
                except TimepadAuth as e:
                    if not TIMEPAD_TOKEN:
                        st["timepad_needs_token"] = True
                        if OWNER_ID:
                            try:
                                await bot.send_message(OWNER_ID, f"Американистика: Timepad отказал в доступе (код {e}), "
                                                       "скорее всего API нужен токен. Заведи токен разработчика на "
                                                       "dev.timepad.ru и добавь его в Railway как TIMEPAD_TOKEN. "
                                                       "До тех пор Timepad пропускаю, остальные источники читаю.")
                            except TelegramError:
                                pass
                    else:
                        failed["Timepad"] = "токен не принят"
                except Exception as e:
                    failed["Timepad"] = e

        # Claude отбирает лекции и достает поля анонса
        today = datetime.now(america.MSK).date()
        found = []
        try:
            for batch in america.batches(candidates):
                raw = await claude_text(america.build_prompt(batch, today), AMERICA_MODEL)
                for ev in america.parse_answer(raw):
                    try:
                        src = batch[int(ev.get("src")) - 1]
                    except (TypeError, ValueError, IndexError):
                        continue
                    if not america.valid_event(ev, today):
                        continue
                    ev["source"], ev["source_url"] = src["source"], src["url"]
                    ev["source_text"], ev["source_kind"] = src["text"], src.get("kind")
                    found.append(ev)
        except Exception as e:
            log.exception("Американистика: Claude не ответил")
            await notify_owner(bot, "america", f"Американистика: не получилось отобрать анонсы ({type(e).__name__}). "
                                               "Ничего не потеряно, попробую при следующей проверке.")
            state.data["last"]["america"] = time.time()
            state.save()
            return 0

        for c in commits:
            if c[0] == "tg":
                st["tg"][c[1]] = c[2]
            elif c[0] == "feed":
                st["feeds_seen"].append(c[1])
            elif c[0] == "page":
                st["pages"][c[1]] = c[2]
            elif c[0] == "timepad":
                st["timepad_seen"].append(c[1])
        st["feeds_seen"] = st["feeds_seen"][-500:]
        st["timepad_seen"] = st["timepad_seen"][-1000:]
        state.save()

        posted = 0
        found.sort(key=lambda e: (str(e["date"]), str(e.get("time") or "")))
        async with httpx.AsyncClient() as client:
            for ev in found:
                if any(america.same_event(ev, h) for h in st["posted"]):
                    continue
                try:
                    await broadcast(bot, client, channels, lambda lim, e=ev: america.build_post(e), [])
                except Exception:
                    log.exception("Анонс не отправлен: %s", ev.get("title"))
                    continue
                st["posted"] = (st["posted"] + [{"date": str(ev["date"])[:10], "title": ev["title"],
                                                 "tags": america.event_tags(ev), "source": ev.get("source_url")}])[-500:]
                posted += 1
                state.save()
                await asyncio.sleep(3)

        # источники, которые не читаются три проверки подряд
        fails = st["fails"]
        for name in ok:
            fails.pop(name, None)
        for name in failed:
            fails[name] = fails.get(name, 0) + 1
            log.warning("Американистика: %s не прочитан: %s", name, failed[name])
        broken = [f"• {n} ({failed[n]})" for n in failed if fails[n] >= 3]
        if broken:
            await notify_owner(bot, "america-sources", "Американистика: эти источники не читаются уже несколько проверок "
                                                       "подряд:\n" + "\n".join(broken))

        state.data["last"]["america"] = time.time()
        state.save()
        log.info("Американистика: материалов на проверку %d, анонсов найдено %d, отправлено %d",
                 len(candidates), len(found), posted)
        return posted

# ─── Американистика: конспекты лекций Йеля ──────────────────────────────────

_lectures_lock = asyncio.Lock()

async def lecture_draft(bot, client, st):
    """Готовит пост по следующему свободному сюжету. -> черновик (dict) или None, если все использовано."""
    for cid, course in lectures.COURSES.items():
        if not st["lists"].get(cid):
            r = await http_get(client, course["url"])
            st["lists"][cid] = lectures.lecture_numbers(r.text, cid)
    used = set(st["used"])
    if st.get("draft"):  # просили другой черновик - не повторяем прежний
        used.add(st["draft"]["fragment"])
    for _ in range(6):
        choice = lectures.pick(list(lectures.COURSES), st["lists"], set(st["exhausted"]), st.get("last_course"))
        if not choice:
            return None
        cid, n = choice
        course = lectures.COURSES[cid]
        r = await http_get(client, f"{course['url']}/lecture-{n}")
        lec = lectures.parse_lecture(r.text)
        key = f"{cid}/{n}"
        segs = st["segments"].get(key)
        if segs is None:
            raw = await claude_json(lectures.segment_prompt(course, n, lec), LECTURES_MODEL,
                                    lectures.SEGMENT_SCHEMA, max_tokens=2000)
            segs = lectures.parse_segments(raw, lec)
            if segs is None:
                raise RuntimeError("Claude не разбил лекцию на сюжеты")
            st["segments"][key] = segs
            state.save()
        seg = lectures.choose_segment(cid, n, segs, used)
        if not seg:
            st["exhausted"].append(key)
            continue
        fid = lectures.fragment_id(cid, n, seg)
        total = len(st["lists"][cid])
        notes = lectures.parse_answer(await claude_json(
            lectures.notes_prompt(course, n, total, lec, seg), LECTURES_MODEL, lectures.NOTES_SCHEMA,
            max_tokens=6000))
        if notes is None:
            raise RuntimeError("Claude не подготовил заметки к фрагменту")
        if notes.get("skip"):  # сюжет оказался организационным
            st["used"].append(fid)
            used.add(fid)
            state.save()
            continue
        post = lectures.parse_post(await claude_text(lectures.post_prompt(course, n, lec, seg, notes),
                                                     LECTURES_MODEL, max_tokens=5000))
        if post is None:
            raise RuntimeError("Claude не написал пост по заметкам")
        post["tags"] = notes.get("tags")
        quote_ru = None
        passages = lectures.extract_quotes(lectures.segment_text(lec, seg), notes.get("quote"))
        if passages:
            quote_ru = (await claude_text(lectures.translate_prompt(course, passages), LECTURES_MODEL,
                                          max_tokens=5000)).strip() or None
        else:
            log.info("Конспект %s: куски цитаты не нашлись в расшифровке: %r", fid, notes.get("quote"))
        image = await lecture_image(client, notes.get("images"),
                                    "\n\n".join([post["title"], post["before"], post["after"]]))
        return {"fragment": fid, "course": cid, "title": post["title"], "tags": lectures.post_tags(post, course),
                "text": lectures.build_post(post, course, n, total, lec, seg, quote_ru, image=image),
                "preview": image["thumb"] if image else None}
    return None


async def lecture_image(client, queries, post_text):
    """Иллюстрация к посту: поиск на Wikimedia Commons и в Библиотеке Конгресса, выбор и подпись - Claude.

    Запросы идут от конкретного к общему, так что что-нибудь находится почти всегда.
    -> кандидат с полем caption или None. Ошибка поиска не мешает посту выйти.
    """
    seen, cands = set(), []

    async def search(url, params, parse, headers):
        try:
            r = await client.get(url, params=params, headers=headers, timeout=30, follow_redirects=True)
            r.raise_for_status()
            for c in parse(r.json()):
                if c["thumb"] not in seen:
                    seen.add(c["thumb"])
                    cands.append(c)
        except Exception as e:
            log.info("Картинки: %s %r не удалось (%s)", url, params.get("gsrsearch") or params.get("q"), e)

    for q in (queries or [])[:5]:
        await search(lectures.COMMONS_API, lectures.commons_params(str(q)), lectures.commons_candidates,
                     {"User-Agent": lectures.COMMONS_UA})
        # Библиотека Конгресса ищет хуже с годами в запросе («Dodge City 1870s» - пусто, «Dodge City» - 8)
        await search(lectures.LOC_API, lectures.loc_params(re.sub(r"\b\d{3,4}s?\b", "", str(q)).strip()),
                     lectures.loc_candidates,
                     {"User-Agent": lectures.COMMONS_UA})
    if not cands:
        log.info("Картинки: ничего не нашлось по %r", queries)
        return None
    cands = sorted(cands, key=lambda c: -c.get("usage", 0))[:16]
    ans = await claude_json(lectures.pick_prompt(post_text, cands), LECTURES_MODEL, lectures.PICK_SCHEMA,
                            max_tokens=800)
    try:
        i = int((ans or {}).get("choice", -1))
    except (TypeError, ValueError):
        i = -1
    if not 0 <= i < len(cands) or not (ans or {}).get("caption"):
        log.info("Картинки: ни одна не подошла")
        return None
    return dict(cands[i], caption=ans["caption"])


async def publish_lecture(bot, client, channels, st, draft, today):
    await broadcast(bot, client, channels, lambda lim: draft["text"], [], draft.get("preview"))
    st["used"].append(draft["fragment"])
    st["posted"] = (st["posted"] + [{"fragment": draft["fragment"], "title": draft["title"], "date": today,
                                     "tags": draft["tags"]}])[-1000:]
    st["last_day"], st["last_course"] = today, draft["course"]
    st.pop("draft", None)
    state.save()
    log.info("Конспект %s: %s", draft["fragment"], draft["title"])


async def check_lectures(bot, force=False, preview=False):
    """Раз в день, после LECTURES_HOUR, один конспект сюжета лекции.

    force - не ждать часа и не смотреть, был ли пост сегодня (/check, /publish).
    preview - не публиковать, а прислать владельцу черновик; он уйдет в канал следующим.
    """
    async with _lectures_lock:
        channels = state.channels("lectures")
        if not channels and not preview:
            return 0
        st = state.data["lectures"]
        now = datetime.now(timezone(timedelta(hours=LECTURES_UTC_OFFSET)))
        today = now.date().isoformat()
        if not preview:
            state.data["last"]["lectures"] = time.time()
        if not force and (now.hour < LECTURES_HOUR or st.get("last_day") == today):
            state.save()
            return 0
        if not ANTHROPIC_API_KEY:
            await notify_owner(bot, "lectures", "Конспекты: без ANTHROPIC_API_KEY не могу писать посты.")
            state.save()
            return 0
        try:
            async with httpx.AsyncClient() as client:
                draft = None if preview else st.get("draft")
                if not draft:
                    draft = await lecture_draft(bot, client, st)
                if not draft:
                    await notify_owner(bot, "lectures", "Конспекты: все сюжеты всех курсов уже использованы. "
                                                        "Пора добавить новые курсы.")
                    state.save()
                    return 0
                if preview:
                    st["draft"] = draft
                    state.save()
                    await send_post(bot, client, OWNER_ID, lambda lim: draft["text"], [], draft.get("preview"))
                    await bot.send_message(OWNER_ID, "Это черновик, в канал он пока не ушел. Он выйдет следующим "
                                                     "по расписанию. Опубликовать сейчас: /publish. "
                                                     "Другой черновик: /check lectures.")
                    return 1
                await publish_lecture(bot, client, channels, st, draft, today)
                return 1
        except Exception as e:
            log.exception("Конспекты: ошибка")
            if preview or force:  # ручной запуск: ошибку покажет сама команда
                state.save()
                raise
            await notify_owner(bot, "lectures", f"Конспекты: не получилось подготовить пост ({type(e).__name__}: {e}). "
                                                "Попробую позже.")
        state.save()
        return 0

# ─── Статьи ─────────────────────────────────────────────────────────────────

_articles_lock = asyncio.Lock()


async def feed_pages(client, st, sid):
    """Сколько страниц в архиве ленты (?paged=N). Ищется раз в неделю: удвоение, потом деление пополам."""
    info = st["pages"].get(sid) or {}
    if info.get("max") and time.time() - info.get("checked", 0) < 7 * 86400:
        return info["max"]

    async def exists(p):
        r = await client.get(articles.page_url(sid, p), headers=HEADERS, timeout=30, follow_redirects=True)
        if articles.SOURCES[sid].get("kind") == "listing":
            return r.status_code == 200 and bool(articles.listing_links(sid, r.text))
        return r.status_code == 200 and "<item>" in r.text

    lo, hi = 1, 16
    while await exists(hi) and hi < 4096:
        lo, hi = hi, hi * 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if await exists(mid):
            lo = mid
        else:
            hi = mid
    st["pages"][sid] = {"max": lo, "checked": time.time()}
    state.save()
    return lo


async def article_candidate(client, st, sid, used):
    """Случайная неиспользованная статья источника sid (лента, карта сайта или страница раздела)."""
    src = articles.SOURCES[sid]
    kind = src.get("kind", "feed")
    if kind == "feed":
        pages = await feed_pages(client, st, sid)
        r = await client.get(articles.page_url(sid, random.randint(1, pages)), headers=HEADERS,
                             timeout=30, follow_redirects=True)
        return articles.choose_item(articles.parse_feed(r.text, sid), used)
    if kind == "sitemap":
        r = await client.get(src["sitemap"].format(n=random.randint(1, src["sitemap_pages"])), headers=HEADERS,
                             timeout=60, follow_redirects=True)
        links = articles.sitemap_links(r.text)
    else:
        pages = await feed_pages(client, st, sid)
        r = await client.get(articles.page_url(sid, random.randint(1, pages)), headers=HEADERS,
                             timeout=30, follow_redirects=True)
        links = articles.listing_links(sid, r.text)
    random.shuffle(links)
    for url in [u for u in links if u not in used][:4]:
        p = await client.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        if p.status_code != 200:
            continue
        item = articles.parse_page(p.text, url, sid)
        if len(item["text"]) >= articles.MIN_TEXT and item["title"]:
            return item
        used.add(url)
    return None


async def article_draft(client, st):
    """Пост по случайной статье из архивов. -> черновик или None."""
    used = set(st["used"])
    if st.get("draft"):
        used.add(st["draft"]["fragment"])
    for sid in articles.pick_source(st.get("last_source")) * 2:
        item = await article_candidate(client, st, sid, used)
        if not item:
            continue
        notes = lectures.parse_answer(await claude_json(articles.notes_prompt(item), LECTURES_MODEL,
                                                        lectures.NOTES_SCHEMA, max_tokens=6000))
        if notes is None or notes.get("skip"):  # не об истории США, после 2014 года или пустая
            log.info("Статьи: пропускаю %s", item["url"])
            st["used"].append(item["url"])
            used.add(item["url"])
            state.save()
            continue
        post = lectures.parse_post(await claude_text(articles.post_prompt(item, notes), LECTURES_MODEL,
                                                     max_tokens=5000))
        if post is None:
            raise RuntimeError("Claude не написал пост по статье")
        post["tags"] = notes.get("tags")
        quote_ru = None
        passages = lectures.extract_quotes(item["text"], notes.get("quote"))
        if passages:
            quote_ru = (await claude_text(articles.translate_prompt(passages), LECTURES_MODEL,
                                          max_tokens=5000)).strip() or None
        image = await lecture_image(client, notes.get("images"),
                                    "\n\n".join([post["title"], post["before"], post["after"]]))
        return {"fragment": item["url"], "source": sid, "title": post["title"],
                "tags": articles.post_tags(post, item),
                "text": articles.build_post(post, item, quote_ru, image),
                "preview": image["thumb"] if image else None}
    return None


async def check_articles(bot, force=False, preview=False):
    """Раз в день, после ARTICLES_HOUR, один пост по статье. force и preview - как у check_lectures."""
    async with _articles_lock:
        channels = state.channels("articles")
        if not channels and not preview:
            return 0
        st = state.data["articles"]
        now = datetime.now(timezone(timedelta(hours=LECTURES_UTC_OFFSET)))
        today = now.date().isoformat()
        if not preview:
            state.data["last"]["articles"] = time.time()
        if not force and (now.hour < ARTICLES_HOUR or st.get("last_day") == today):
            state.save()
            return 0
        if not ANTHROPIC_API_KEY:
            state.save()
            return 0
        try:
            async with httpx.AsyncClient() as client:
                draft = None if preview else st.get("draft")
                if not draft:
                    draft = await article_draft(client, st)
                if not draft:
                    log.info("Статьи: подходящей статьи не нашлось, попробую позже")
                    state.save()
                    return 0
                if preview:
                    st["draft"] = draft
                    state.save()
                    await send_post(bot, client, OWNER_ID, lambda lim: draft["text"], [], draft.get("preview"))
                    await bot.send_message(OWNER_ID, "Это черновик поста по статье, в канал он пока не ушел. "
                                                     "Он выйдет следующим по расписанию. Сейчас: /publish articles. "
                                                     "Другой черновик: /check articles.")
                    return 1
                await broadcast(bot, client, channels, lambda lim: draft["text"], [], draft.get("preview"))
                st["used"].append(draft["fragment"])
                st["posted"] = (st["posted"] + [{"fragment": draft["fragment"], "title": draft["title"],
                                                 "date": today, "tags": draft["tags"]}])[-1000:]
                st["last_day"], st["last_source"] = today, draft["source"]
                st.pop("draft", None)
                state.save()
                log.info("Статья %s: %s", draft["fragment"], draft["title"])
                return 1
        except Exception as e:
            log.exception("Статьи: ошибка")
            if preview or force:
                state.save()
                raise
            await notify_owner(bot, "articles", f"Статьи: не получилось подготовить пост ({type(e).__name__}: {e}). "
                                                "Попробую позже.")
        state.save()
        return 0

# ─── Источники ──────────────────────────────────────────────────────────────
# Новый источник = функция check_<имя>(bot) + строка здесь.

SOURCES = {
    "letterboxd": {"title": "Letterboxd: просмотры твоих подписок", "check": check_letterboxd, "every": LB_INTERVAL},
    "svetov":     {"title": "Светов в X: «рекомендую» / «не рекомендую»", "check": check_x, "every": X_INTERVAL},
    "kanoner":    {"title": "Канонер: стройка и город, Петербург (посты из ВК)", "check": check_kanoner, "every": KANONER_INTERVAL},
    "america":    {"title": "Американистика: очные лекции о США и Канаде в Петербурге", "check": check_america,
                   "every": AMERICA_INTERVAL},
    "lectures":   {"title": "Американистика: рассказы по лекциям Йеля, раз в день", "check": check_lectures,
                   "every": 1800},
    "articles":   {"title": "Американистика: рассказы по статьям (Prologue, Библиотека Конгресса, JSTOR Daily, "
                            "American Heritage, Smithsonian), раз в день", "check": check_articles, "every": 1800},
}


def hours(sec):
    h = sec / 3600
    return f"{h:g} ч"

# ─── Расписание ─────────────────────────────────────────────────────────────

async def scheduler(app):
    await asyncio.sleep(15)
    while True:
        for name, src in SOURCES.items():
            if not state.channels(name):
                continue
            if time.time() - state.data["last"].get(name, 0) < src["every"]:
                continue
            try:
                await src["check"](app.bot)
            except Exception:
                log.exception("Ошибка в источнике %s", name)
                state.data["last"][name] = time.time()
                state.save()
        await asyncio.sleep(300)


async def post_init(app):
    # названия каналов, привязанных старой версией бота
    for chat_id in {c for lst in state.data["bindings"].values() for c in lst}:
        if str(chat_id) in state.data["channel_titles"]:
            continue
        try:
            chat = await app.bot.get_chat(chat_id)
            state.data["channel_titles"][str(chat_id)] = chat.title or str(chat_id)
        except TelegramError:
            pass
    state.save()
    app.create_task(scheduler(app))

# ─── Команды ────────────────────────────────────────────────────────────────

def owner_only(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != OWNER_ID:
            return
        return await fn(update, context)
    return wrapper


def sources_overview():
    rows = []
    for name, src in SOURCES.items():
        chans = state.channels(name)
        where = ", ".join(f"«{state.title(c)}»" for c in chans) if chans else "не подключен"
        rows.append(f"• {name} — {src['title']}\n  каналы: {where}; проверка раз в {hours(src['every'])}")
    return "\n".join(rows)


@owner_only
async def cmd_start(update, context):
    await update.message.reply_text(
        "Лента: собираю новое из разных источников и публикую в твои каналы.\n\n"
        "Источники:\n" + sources_overview() + "\n\n"
        "Подключить источник к каналу: добавь меня в канал администратором и напиши в канале "
        "/bind и название источника, например /bind kanoner. "
        "Можно несколько сразу: /bind letterboxd svetov. Отключить: /unbind kanoner.\n\n"
        "Команды здесь:\n"
        "/list - источники, каналы и чьи ленты Letterboxd читаю\n"
        "/check - проверить все источники сейчас (или /check kanoner)\n"
        "/check lectures - черновик поста по лекции мне в личку, /publish - выпустить его в канал\n"
        "/check articles - черновик поста по статье, /publish articles - выпустить его\n"
        "/add ник, /remove ник - люди на Letterboxd"
    )


@owner_only
async def cmd_list(update, context):
    lb = state.data["lb"]
    text = "Источники:\n" + sources_overview()
    if state.channels("letterboxd"):
        async with httpx.AsyncClient() as client:
            users = await current_users(client)
        state.save()
        origin = "из твоих подписок" if lb.get("following_ok") else "из сохраненного списка (страница подписок не открылась)"
        text += f"\n\nLetterboxd, {len(users)} лент {origin}:\n" + "\n".join(users)
        if lb["added"]:
            text += "\nДобавлены вручную: " + ", ".join(lb["added"])
        if lb["removed"]:
            text += "\nУбраны вручную: " + ", ".join(lb["removed"])
    await update.message.reply_text(text)


def clean_nick(arg):
    arg = arg.strip().rstrip("/")
    m = re.search(r"letterboxd\.com/([^/\s]+)", arg)
    return (m.group(1) if m else arg).lstrip("@").lower()


@owner_only
async def cmd_add(update, context):
    if not context.args:
        await update.message.reply_text("Напиши так: /add ник (или ссылку на профиль Letterboxd)")
        return
    nick = clean_nick(context.args[0])
    lb = state.data["lb"]
    if nick in lb["removed"]:
        lb["removed"].remove(nick)
    if nick not in lb["added"]:
        lb["added"].append(nick)
    state.save()
    await update.message.reply_text(f"Добавил {nick} на Letterboxd. Старые записи не пришлю, только новые.")


@owner_only
async def cmd_remove(update, context):
    if not context.args:
        await update.message.reply_text("Напиши так: /remove ник")
        return
    nick = clean_nick(context.args[0])
    lb = state.data["lb"]
    if nick in lb["added"]:
        lb["added"].remove(nick)
    if nick not in lb["removed"]:
        lb["removed"].append(nick)
    state.save()
    await update.message.reply_text(f"Убрал {nick}.")


@owner_only
async def cmd_check(update, context):
    wanted = [a.lower() for a in context.args] or list(SOURCES)
    unknown = [a for a in wanted if a not in SOURCES]
    if unknown:
        await update.message.reply_text("Таких источников нет: " + ", ".join(unknown) +
                                        "\nЕсть: " + ", ".join(SOURCES))
        return
    lines = []
    for name in wanted:
        if not state.channels(name):
            lines.append(f"{name}: не подключен к каналу")
            continue
        await update.message.reply_text(f"Проверяю {name}...")
        try:
            fn = SOURCES[name]["check"]
            if name in ("lectures", "articles"):  # длинные посты сначала владельцу
                n = await fn(context.bot, force=True, preview=True)
                lines.append(f"{name}: черновиков {n}")
                continue
            n = await fn(context.bot)
            lines.append(f"{name}: новых постов {n}")
        except Exception as e:
            log.exception("Ручная проверка %s", name)
            lines.append(f"{name}: ошибка ({type(e).__name__}: {str(e)[:300]})")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_publish(update, context):
    """Черновик поста по лекции (/publish) или по статье (/publish articles) - в канал сейчас."""
    name = (context.args[0].lower() if context.args else "lectures")
    if name not in ("lectures", "articles"):
        await update.message.reply_text("Можно: /publish или /publish articles")
        return
    if not state.channels(name):
        await update.message.reply_text(f"{name} не подключен к каналу")
        return
    try:
        n = await (check_lectures if name == "lectures" else check_articles)(context.bot, force=True)
    except Exception as e:
        await update.message.reply_text(f"Не получилось: {type(e).__name__}: {str(e)[:300]}")
        return
    await update.message.reply_text("Опубликовано." if n else "Публиковать нечего.")


@owner_only
async def cmd_x(update, context):
    context.args = ["svetov"]
    await cmd_check(update, context)


async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post or not post.text:
        return
    parts = post.text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    if cmd not in ("/bind", "/unbind"):
        return
    names = [p.lower() for p in parts[1:]]
    if names == ["all"]:
        names = list(SOURCES)
    try:
        await post.delete()
    except TelegramError:
        pass

    async def tell_owner(text):
        if OWNER_ID:
            try:
                await context.bot.send_message(OWNER_ID, text)
            except TelegramError:
                log.warning("Не смог написать владельцу (он нажимал /start у бота?)")

    title = post.chat.title or str(post.chat.id)
    if not names:
        await tell_owner(f"В канале «{title}» напиши {cmd} и источник: " + ", ".join(SOURCES) +
                         f". Например: {cmd} kanoner")
        return
    unknown = [n for n in names if n not in SOURCES]
    names = [n for n in names if n in SOURCES]
    for n in names:
        if cmd == "/bind":
            state.bind(n, post.chat.id, title)
        else:
            state.unbind(n, post.chat.id)
    msg = []
    if names:
        verb = "подключен к каналу" if cmd == "/bind" else "отключен от канала"
        msg.append(f"{', '.join(names)}: {verb} «{title}».")
        if cmd == "/bind":
            msg.append("Старое не пришлю, только то, что появится с этого момента.")
    if unknown:
        msg.append("Не знаю источники: " + ", ".join(unknown) + ". Есть: " + ", ".join(SOURCES))
    await tell_owner(" ".join(msg))

# ─── Запуск ─────────────────────────────────────────────────────────────────

def main():
    global state
    state = State(STATE_DIR)
    state.save()
    app = (ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init)
           .connect_timeout(20).read_timeout(60).write_timeout(60).media_write_timeout(120)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("x", cmd_x))
    app.add_handler(CommandHandler("publish", cmd_publish))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_command))
    log.info("Лента запущена, источники: %s", ", ".join(SOURCES))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
