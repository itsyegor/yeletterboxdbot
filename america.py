"""
Американистика: анонсы очных лекций о США и Канаде в Петербурге.

Здесь только разбор источников, отбор и оформление. Сеть, память и отправка - в bot.py
(функция check_america).

Как устроен отбор:
  1. Источники: публичные Telegram-каналы площадок (веб-версия t.me/s/), RSS афиши
     Naked Science, страницы афиш на сайтах, Timepad.
  2. Дешевый фильтр по словам (США, америк*, Нью-Йорк, Линкольн...) отсекает почти все.
  3. Оставшееся читает Claude: лекция ли это, очно ли в Петербурге, про Северную Америку ли,
     не прошла ли дата. Он же достает поля анонса.
  4. Одна лекция может прийти из нескольких мест - склеиваем по дате и словам названия.
"""
import re
import json
import hashlib
import html as htmlmod
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

MSK = timezone(timedelta(hours=3))

# ─── Источники ──────────────────────────────────────────────────────────────

# Telegram-каналы площадок: ник -> название площадки
TG_CHANNELS = {
    "newhollandsp": "Новая Голландия",
    "doctrinaetnobiles": "Doctrina et Nobiles",
    "hermitageyouth": "Молодежный центр Эрмитажа",
    "euspb": "Европейский университет",
    "sevcableport": "Севкабель Порт",
    "mayakovka": "Библиотека Маяковского",
    "izdaniya": "Подписные издания",
    "dklurie": "ДК Льва Лурье",
    "domradio_online": "Дом Радио",
}

# RSS
FEEDS = [
    {"url": "https://naked-science.ru/event/feed/", "name": "Naked Science, афиша"},
]

# Страницы афиш (читаются целиком, Claude находит анонсы в тексте)
PAGES = [
    {"url": "https://eusp.org/events", "name": "Европейский университет"},
    {"url": "https://spb.hse.ru/announcements/", "name": "ВШЭ Петербург"},
    {"url": "https://spbmuseum.ru/events/", "name": "Музей истории Петербурга"},
    {"url": "https://kuda-spb.ru/event/learn/", "name": "Куда-СПБ"},
]

# Поиск на Timepad
TIMEPAD_KEYWORDS = ["США", "Америка", "американский", "американская", "Останбекова"]
TIMEPAD_CITY = "Санкт-Петербург"

# Слова-признаки темы. Широкий сетью: лишнее отсеет Claude.
KEYWORDS = re.compile(
    r"\bсша\b|соединенн\w*\s+штат|америк|нью-йорк|вашингтон|линкольн|джефферсон|гамильтон"
    r"|голливуд|бродве|манхэттен|уолл-стрит|лос-анджелес|сан-франциско|чикаго|бостон"
    r"|филадельфи|новый орлеан|миссисипи|калифорни|техас|аляск|канад|квебек|торонто"
    r"|вестерн|ковбо|индейц|дикий запад|джаз|блюз|фолкнер|хемингуэ|фицджеральд|марк твен"
    r"|сэлинджер|керуак|уитмен|стейнбек|набоков в америке|рузвельт|кеннеди|никсон|рейган"
    r"|токвил|курилл|останбеков|\busa\b|\bamerica",
    re.IGNORECASE,
)


def relevant(text):
    return bool(KEYWORDS.search((text or "").replace("ё", "е").replace("Ё", "Е")))

# ─── Текст из HTML ──────────────────────────────────────────────────────────

def text_with_links(el, base=None):
    """Текст элемента; ссылки остаются в виде «текст (адрес)», чтобы Claude мог их вернуть."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for a in el.find_all("a"):
        href = (a.get("href") or "").strip()
        label = a.get_text(" ", strip=True)
        if href.startswith(("http", "/")):
            href = urljoin(base, href) if base else href
            a.replace_with(f"{label} ({href})" if label and label != href else href)
        else:
            a.replace_with(label)
    for block in el.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
        block.insert_after("\n")
    text = el.get_text("")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_tg(page_html, handle):
    """Посты из веб-версии канала t.me/s/<ник>: [{'id', 'text', 'date', 'url'}], по возрастанию id."""
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for m in soup.select("div.tgme_widget_message[data-post]"):
        post = m.get("data-post") or ""
        mm = re.match(r"[^/]+/(\d+)$", post)
        if not mm:
            continue
        body = m.select_one(".tgme_widget_message_text")
        text = text_with_links(body) if body else ""
        t = m.select_one("time[datetime]")
        out.append({
            "id": int(mm.group(1)),
            "text": text,
            "date": t.get("datetime") if t else None,
            "url": f"https://t.me/{handle}/{mm.group(1)}",
        })
    out.sort(key=lambda p: p["id"])
    return out


def tg_before(page_html):
    """Номер для ссылки на предыдущую страницу канала (?before=N) или None."""
    m = re.search(r'\?before=(\d+)', page_html)
    return int(m.group(1)) if m else None


CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"

def parse_feed(xml_text):
    """Записи RSS: [{'id', 'title', 'text', 'url'}]."""
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        title = htmlmod.unescape((item.findtext("title") or "").strip())
        body = item.findtext(CONTENT_NS) or item.findtext("description") or ""
        text = text_with_links(BeautifulSoup(body, "html.parser"), link)
        out.append({"id": guid, "title": title, "text": f"{title}\n\n{text}".strip(), "url": link})
    return out


def page_text(page_html, base_url, limit=40000):
    """Текст страницы афиши без меню и скриптов."""
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form", "iframe"]):
        t.decompose()
    main = soup.find("main") or soup.body or soup
    text = text_with_links(main, base_url)
    return text[:limit]


def digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]

# ─── Timepad ────────────────────────────────────────────────────────────────

def timepad_candidates(data):
    """Ответ api.timepad.ru/v1/events -> [{'id', 'text', 'url'}]."""
    out = []
    for e in (data or {}).get("values") or []:
        loc = e.get("location") or {}
        org = e.get("organization") or {}
        parts = [
            e.get("name") or "",
            f"Начало: {e.get('starts_at') or ''}",
            f"Организатор: {org.get('name') or ''}",
            f"Место: {loc.get('city') or ''}, {loc.get('address') or ''}",
            BeautifulSoup(e.get("description_short") or e.get("description_html") or "", "html.parser").get_text(" "),
            f"Ссылка: {e.get('url') or ''}",
        ]
        out.append({"id": str(e.get("id")), "text": "\n".join(p for p in parts if p.strip()),
                    "url": e.get("url") or ""})
    return out

# ─── Claude: отбор и поля анонса ────────────────────────────────────────────

PROMPT = """Ты отбираешь анонсы для канала об американистике. Сегодня {today}.

Ниже пронумерованные материалы: посты площадок, записи афиш, страницы афиш. На странице афиши бывает много событий сразу.

Найди анонсы событий, которые подходят по ВСЕМ условиям:
1. Формат: лекция, беседа, дискуссия, семинар, круглый стол, презентация книги, открытое занятие, кинопоказ с лекцией или обсуждением. Концерты, спектакли, выставки, экскурсии, прогулки, мастер-классы, ярмарки и кинопоказы без лекции не подходят.
2. Тема существенно посвящена Северной Америке: США или Канаде, включая колониальный период и коренные народы. Подходят история, политика, право, общество, экономика, религия, культура, литература, кино, музыка, искусство. Мимолетное упоминание не считается: лекция о европейской живописи, где назван американский коллекционер, не подходит. Латинская Америка не подходит.
3. Проходит очно в Санкт-Петербурге. Только онлайн, а также другие города - не подходят.
4. Дата сегодня или позже. Если дат несколько - бери ближайшую будущую. Если год не указан, считай ближайшую будущую такую дату.

Ничего не выдумывай: каждое поле бери из текста материала, если поля нет - null.

Ответь только JSON-массивом без пояснений. Для каждого подходящего события объект:
{{"src": номер материала, "title": "название как в анонсе", "lecturer": "лектор и кратко кто он, или null", "date": "ГГГГ-ММ-ДД", "time": "ЧЧ:ММ или null", "venue": "площадка", "address": "адрес или null", "price": "как в анонсе, например «800 ₽» или «бесплатно, по регистрации», или null", "link": "ссылка на регистрацию, билеты или страницу события из текста, или null", "summary": "одно-два предложения о содержании в академическом тоне: о чем лекция, какие вопросы рассматриваются; без оценок, восклицаний и рекламных слов"}}

Если подходящих нет - ответь [].

{items}"""


def build_prompt(candidates, today):
    blocks = []
    for i, c in enumerate(candidates, 1):
        head = f"[{i}] Источник: {c['source']}"
        if c.get("url"):
            head += f" ({c['url']})"
        blocks.append(f"{head}\n{c['text']}")
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    return PROMPT.format(today=f"{today.isoformat()}, {days[today.weekday()]}",
                         items="\n\n---\n\n".join(blocks))


def batches(candidates, budget=24000, item_limit=4000):
    """Группы материалов, чтобы один запрос не был слишком длинным. Страницы идут поодиночке."""
    batch, size = [], 0
    for c in candidates:
        c = dict(c)
        if c.get("kind") != "page":
            c["text"] = c["text"][:item_limit]
        n = len(c["text"])
        if batch and (size + n > budget or c.get("kind") == "page"):
            yield batch
            batch, size = [], 0
        batch.append(c)
        size += n
        if c.get("kind") == "page":
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def parse_answer(raw):
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)]


def valid_event(ev, today):
    """Отбрасывает ответы без даты и прошедшие события."""
    try:
        d = date.fromisoformat(str(ev.get("date"))[:10])
    except (TypeError, ValueError):
        return False
    return d >= today and bool(ev.get("title"))

# ─── Повторы ────────────────────────────────────────────────────────────────

def stems(text):
    return {w[:5] for w in re.findall(r"[а-яa-z0-9]+", (text or "").lower().replace("ё", "е")) if len(w) > 3}


def same_event(a, b):
    """Одна и та же лекция: та же дата и названия совпадают хотя бы наполовину."""
    if str(a.get("date"))[:10] != str(b.get("date"))[:10]:
        return False
    sa, sb = stems(a.get("title")), stems(b.get("title"))
    if not sa or not sb:
        return False
    common = len(sa & sb)
    return common >= max(1, 0.5 * min(len(sa), len(sb)))

# ─── Оформление ─────────────────────────────────────────────────────────────

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def human_date(iso, t=None):
    d = date.fromisoformat(str(iso)[:10])
    s = f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"
    if t and re.fullmatch(r"\d{1,2}:\d{2}", str(t)):
        s += f", {t}"
    return s


def _esc(s):
    return htmlmod.escape(str(s or ""), quote=False)


def _href(url):
    return htmlmod.escape(str(url), quote=True)


def _cut(s, n):
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def build_post(ev):
    """Текст анонса (HTML для Telegram). Поля обрезаются заранее, чтобы не резать разметку."""
    lines = [f"<b>{_esc(_cut(ev['title'], 300))}</b>"]
    if ev.get("lecturer"):
        lines.append(_esc(_cut(ev["lecturer"], 300)))
    lines.append("")
    lines.append(human_date(ev["date"], ev.get("time")))
    place = ", ".join(str(x) for x in [ev.get("venue"), ev.get("address")] if x)
    if place:
        lines.append(_esc(_cut(place, 300)))
    if ev.get("price"):
        lines.append(_esc(_cut(ev["price"], 200)))
    if ev.get("summary"):
        lines.append("")
        lines.append(_esc(_cut(ev["summary"], 900)))
    links = []
    if ev.get("link") and str(ev["link"]).startswith("http"):
        links.append(f'<a href="{_href(ev["link"])}">Регистрация и подробности</a>')
    if ev.get("source_url") and ev.get("source_url") != ev.get("link"):
        links.append(f'<a href="{_href(ev["source_url"])}">Источник: {_esc(ev.get("source"))}</a>')
    elif ev.get("source"):
        links.append(f"Источник: {_esc(ev['source'])}")
    if links:
        lines.append("")
        lines.append(" · ".join(links))
    return "\n".join(lines)
