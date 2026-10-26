"""
Лента: бот собирает новое из разных источников и публикует в Telegram-каналы.
Каждый источник привязывается к своему каналу (или нескольким) командой /bind <источник>.

Источники:
  letterboxd  просмотры людей, на которых ты подписан на Letterboxd
  svetov      посты @msvetov в X с «рекомендую» / «не рекомендую»
  kanoner     новости kanoner.com (строительство и город, Петербург)

Переменные окружения (Railway -> Variables):
  TELEGRAM_TOKEN      токен бота от BotFather (обязательно)
  OWNER_ID            твой числовой Telegram id (обязательно)
  TMDB_KEY            ключ TMDB - названия фильмов на двух языках
  ANTHROPIC_API_KEY   ключ Claude API - найти фильм в посте Светова
  X_AUTH_TOKEN, X_CT0 cookie из браузера, где ты залогинен в X
  STATE_DIR           папка для памяти бота (по умолчанию /data - туда подключается volume)
"""
import os
import re
import json
import html
import time
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InputMediaPhoto, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
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
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")

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
        return list(self.data["bindings"].get(source, []))

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

async def tg_call(fn, *args, **kwargs):
    for attempt in range(3):
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
    return await fn(*args, **kwargs)


async def download(client, url):
    r = await http_get(client, url)
    return r.content


async def send_post(bot, client, chat_id, text_builder, images):
    """Пост с одной картинкой или альбомом. text_builder(limit) -> текст."""
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


async def broadcast(bot, client, channels, text_builder, images):
    """Один пост во все каналы источника. Ошибка в одном канале не мешает остальным."""
    sent = 0
    for chat_id in channels:
        try:
            await send_post(bot, client, chat_id, text_builder, images)
            sent += 1
        except Exception:
            log.exception("Пост не ушел в канал %s", chat_id)
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
        out.append({
            "guid": guid,
            "title": html.unescape((item.findtext("title") or "").strip()),
            "link": link,
            "categories": [c.text.strip() for c in item.findall("category") if c.text and c.text.strip()],
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


async def article_image(client, url):
    try:
        r = await http_get(client, url)
        return article_image_from_html(r.text, url)
    except Exception as e:
        log.info("Фото статьи не получено для %s: %s", url, e)
    return None


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


_kanoner_lock = asyncio.Lock()

async def check_kanoner(bot):
    async with _kanoner_lock:
        channels = state.channels("kanoner")
        if not channels:
            return 0
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
                ks["seen"] = [e["guid"] for e in entries][-SEEN_KEEP:]
                ks["initialized"] = True
                state.data["last"]["kanoner"] = time.time()
                state.save()
                log.info("Канонер: первый запуск, отмечено %d новостей", len(entries))
                return 0

            seen = set(ks["seen"])
            for e in [x for x in entries if x["guid"] not in seen]:
                image = e["image"] or await article_image(client, e["link"])
                try:
                    await broadcast(bot, client, channels, lambda lim, e=e: build_kanoner(e, lim), [image])
                    posted += 1
                except Exception:
                    log.exception("Новость Канонера не отправлена")
                    break
                ks["seen"] = (ks["seen"] + [e["guid"]])[-SEEN_KEEP:]
                state.save()
                await asyncio.sleep(3)

        state.data["last"]["kanoner"] = time.time()
        state.save()
        log.info("Канонер: %d постов", posted)
        return posted

# ─── Источники ──────────────────────────────────────────────────────────────
# Новый источник = функция check_<имя>(bot) + строка здесь.

SOURCES = {
    "letterboxd": {"title": "Letterboxd: просмотры твоих подписок", "check": check_letterboxd, "every": LB_INTERVAL},
    "svetov":     {"title": "Светов в X: «рекомендую» / «не рекомендую»", "check": check_x, "every": X_INTERVAL},
    "kanoner":    {"title": "Канонер: стройка и город, Петербург", "check": check_kanoner, "every": KANONER_INTERVAL},
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
            n = await SOURCES[name]["check"](context.bot)
            lines.append(f"{name}: новых постов {n}")
        except Exception as e:
            log.exception("Ручная проверка %s", name)
            lines.append(f"{name}: ошибка ({type(e).__name__})")
    await update.message.reply_text("\n".join(lines))


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
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("x", cmd_x))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_command))
    log.info("Лента запущена, источники: %s", ", ".join(SOURCES))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
