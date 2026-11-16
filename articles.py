"""
Американистика: посты по статьям из архивов журналов и блогов об истории США.

Источники - блоги на WordPress, у которых лента листается назад (?paged=N) и отдает полный
текст: так бот берет случайные статьи из многолетних архивов, а не только свежие выпуски.
Пост пишется тем же способом, что посты по лекциям (lectures.py): заметки -> текст по образцу
-> большая цитата (дословные куски оригинала в переводе) -> иллюстрация с Commons.
Сеть, память и отправка - в bot.py (check_articles).
"""

import html as htmlmod
import json
import random
import re
from email.utils import parsedate_to_datetime

import america
import lectures

SOURCES = {
    "prologue": {
        "name": "Prologue, Национальный архив США", "short": "Prologue", "tag": "Prologue",
        "feed": "https://prologue.blogs.archives.gov/feed/",
        "about": "блог журнала Национального архива США: архивисты и историки пишут о документах и событиях",
    },
    "loc": {
        "name": "Headlines & Heroes, Библиотека Конгресса", "short": "Headlines & Heroes", "tag": "БиблиотекаКонгресса",
        "feed": "https://blogs.loc.gov/headlinesandheroes/feed/",
        "about": "блог Библиотеки Конгресса об исторических газетах: истории, найденные в старой прессе",
    },
    "jstor": {
        "name": "JSTOR Daily", "short": "JSTOR Daily", "tag": "JSTORDaily",
        "feed": "https://daily.jstor.org/feed/",
        "about": "научно-популярный журнал JSTOR: статьи историков и журналистов со ссылками на исследования",
        "mixed": True,  # пишет и о других странах - такое отсеивают заметки
    },
    "heritage": {
        "name": "American Heritage", "short": "American Heritage", "tag": "AmericanHeritage",
        "kind": "sitemap", "sitemap": "https://www.americanheritage.com/sitemap.xml?page={n}", "sitemap_pages": 10,
        "about": "журнал об истории США, выходит с 1949 года; пишут историки и журналисты",
    },
    "smithsonian": {
        "name": "Smithsonian Magazine", "short": "Smithsonian", "tag": "Smithsonian",
        "kind": "listing", "listing": "https://www.smithsonianmag.com/history/?page={n}",
        "link_re": r'href="(/history/[a-z0-9-]+-\d{6,}/)"', "base": "https://www.smithsonianmag.com",
        "about": "журнал Смитсоновского института, раздел истории",
        "mixed": True,
    },
}

MIN_TEXT = 2500  # короче - это заметка, а не статья


# ─── Лента ──────────────────────────────────────────────────────────────────

def _tag(item, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", item, re.S)
    if not m:
        return ""
    v = m.group(1).strip()
    v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
    return v


def html_to_text(h):
    """HTML статьи -> текст с абзацами. Подписи, кнопки и скрипты убираются."""
    h = re.sub(r"(?is)<(script|style|figure|figcaption|iframe|noscript)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|h[1-6]|li|blockquote|div)>", "\n\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    h = htmlmod.unescape(h).replace("\xa0", " ")
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in h.split("\n")]
    text = "\n\n".join(p for p in paras if p)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_feed(xml, source_id):
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml or "", re.S):
        link = _tag(item, "link")
        body = _tag(item, "content:encoded")
        if not link or not body:
            continue
        try:
            year = parsedate_to_datetime(_tag(item, "pubDate")).year
        except (TypeError, ValueError):
            year = None
        out.append({
            "source": source_id, "url": link.strip(), "title": htmlmod.unescape(_tag(item, "title")).strip(),
            "author": htmlmod.unescape(_tag(item, "dc:creator")).strip(), "year": year,
            "categories": [htmlmod.unescape(c) for c in re.findall(r"<category>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</category>", item)],
            "text": html_to_text(body),
        })
    return out


def page_url(source_id, page):
    src = SOURCES[source_id]
    if src.get("kind") == "listing":
        return src["listing"].format(n=page)
    base = src["feed"]
    return base if page <= 1 else f"{base}?paged={page}"


def sitemap_links(xml):
    """Ссылки на статьи American Heritage из карты сайта (без разделов, выпусков и служебных страниц)."""
    out = []
    for loc in re.findall(r"<loc>(.*?)</loc>", xml or ""):
        loc = loc.replace("://ftp.", "://www.")  # в карте сайта старый хост с чужим сертификатом
        path = loc.split("://", 1)[-1].split("/", 1)[-1]
        if (path and "/" not in path.strip("/")
                and not re.match(r"(content|taxonomy|node|user|books|search|correspondence|letters|postscripts"
                                 r"|the-editors|readers|in-this-issue)", path)):
            out.append(loc)
    return out


def listing_links(source_id, page_html):
    src = SOURCES[source_id]
    return sorted({src["base"] + p for p in re.findall(src["link_re"], page_html or "")})


def _meta_content(page, *names):
    for n in names:
        m = re.search(rf"""<meta[^>]+(?:name|property)=["']{re.escape(n)}["'][^>]+content=(["'])(.*?)\1""", page)
        if m and m.group(2).strip():
            return htmlmod.unescape(m.group(2)).strip()
    return ""


def parse_page(page, url, source_id):
    """HTML страницы статьи -> item как из ленты. Текст - абзацы основного блока."""
    # основной блок - тот из блоков body, где больше всего текста в абзацах (первый у AH - иконки соцсетей)
    starts = [m.end() for m in re.finditer(r'class="[^"]*(?:field--name-body|articleContent|article-body)[^"]*"[^>]*>', page)]
    chunks = [page[s:s + 300000] for s in starts] or [page]
    chunk = max(chunks, key=lambda c: sum(len(p) for p in re.findall(r"(?s)<p[^>]*>(.*?)</p>", c[:150000])))
    paras = [html_to_text(p) for p in re.findall(r"(?s)<p[^>]*>(.*?)</p>", chunk[:150000])]
    keep = []
    for p in paras:
        if re.search(r"(?i)all rights reserved|privacy statement|cookie policy|terms of use", p):
            break  # дальше подвал сайта
        if len(p) > 60 and not re.match(r"(?i)(sign up|subscribe|get the latest|support (our|independent)|add as preferred source)", p):
            keep.append(p)
    paras = keep
    year = None
    m = (re.search(r'"datePublished"\s*:\s*"(\d{4})', page) or re.search(r"\b[A-Z][a-z]+ (1[89]\d\d|20[012]\d), Volume", page)
         or re.search(r'article:published_time"[^>]+content="(\d{4})', page))
    if m:
        year = int(m.group(1))
    author = _meta_content(page, "author", "article:author", "parsely-author")
    if not author:  # American Heritage: «Authored by» в шапке статьи, первое после заголовка h1
        h1 = page.find("<h1")
        m = re.search(r"Authored by:\s*<a[^>]*>([^<]+)</a>", page[h1 if h1 > 0 else 0:])
        author = htmlmod.unescape(m.group(1)).strip() if m else ""
    if not author:
        m = re.search(r'"author"\s*:\s*(?:\[\s*)?\{[^}]*"name"\s*:\s*"([^"]+)"', page)
        author = m.group(1) if m else ""
    title = _meta_content(page, "og:title")
    if not title:
        m = re.search(r"(?s)<title>(.*?)</title>", page)
        title = htmlmod.unescape(m.group(1)).split(" | ")[0].strip() if m else ""
    if author == "The Editors":
        author = "редакция"
    return {"source": source_id, "url": url, "title": title, "author": author,
            "year": year, "categories": [], "text": "\n\n".join(paras)}


def pick_source(last_source=None):
    ids = list(SOURCES)
    random.shuffle(ids)
    return sorted(ids, key=lambda s: s == last_source)


def choose_item(items, used):
    free = [i for i in items if i["url"] not in used and len(i["text"]) >= MIN_TEXT]
    return random.choice(free) if free else None


# ─── Заметки и пост ─────────────────────────────────────────────────────────

NOTES_PROMPT = """Ты готовишь заметки для поста в телеграм-канале об истории США по статье. Сам пост будет писаться потом по этим заметкам, поэтому здесь важна не форма, а понимание.

Статья: «{title}», автор {author}, {source} ({about}), {year}.

Сначала реши, подходит ли статья. Канал - о прошлом США и Северной Америки, до 2014 года включительно, на любую тему: политика, война, быт, экономика, культура, мода, наука, преступления, курьезы - все годится. Ответь skip: true только в трех случаях: статья о другой стране, а не о США и Северной Америке; статья о событиях после 2014 года; в статье нет рассказа о прошлом вообще (объявление о часах работы, призыв к пожертвованиям и т. п.).

Если подходит, разберись и запиши:
- point: какую мысль или историю статья рассказывает;
- why: почему это интересно и что это говорит об эпохе;
- background: что нужно знать читателю, чтобы рассказ был понятен (обстановка, кто есть кто);
- story: факты и события в том порядке, в каком их удобно рассказывать, у каждого - чем он работает на мысль и все, что нужно, чтобы его понять (что именно произошло, кто, сколько, чем кончилось). То, что на мысль не работает или чего из текста не объяснить, не записывай;
- lecturer_views: что в статье - именно трактовка автора, а не общеизвестный факт;
- quote: прямая речь, которая нужна рассказу и которую лучше услышать дословно: документ, письмо, речь, газетная заметка, свидетельство, которые приводит статья, или формулировка автора, которую стоит дать его словами. Не бери кусок обычного изложения - его пост и так перескажет, и выйдет повтор. 1-4 куска по порядку, вместе 100-450 слов. Если такой прямой речи в статье нет, оставь список пустым. Для каждого куска start и end - первые и последние 6-10 слов, скопированные из текста дословно;
- images: 3-5 поисковых запросов по-английски для поиска иллюстрации (Wikimedia Commons, Библиотека Конгресса), от конкретного к общему: сначала сам предмет (событие, человек, место, документ, вещь), последний - эпоха и место («Philadelphia 1770s», «Kansas cattle town 1870s»), чтобы картинка нашлась всегда. Запрос - по предмету, а не по виду картинки;
- tags: 2-4 тега из списков, только прямо следующие из статьи. Эпохи: {era}. Темы: {theme}.
Пиши по-русски, коротко, как для себя. Только то, что есть в статье.

Ответ - через инструмент answer.

Текст статьи:
{text}"""


POST_PROMPT = """Ты пишешь пост для телеграм-канала об истории США. Читатель умный, читает книги по истории, но эту статью не читал. Ты рассказываешь ему, что из нее узнал, как рассказывают историю: связно, по порядку, своими словами.

Как устроен хороший рассказ:
1. Сначала обстановка, потом событие. Прежде чем сказать, что что-то случилось или изменилось, объясни, что это было и кто в этом участвовал. Читатель не должен спотыкаться о названия и намеки.
2. Рассказ идет по порядку, а вывод стоит в конце, когда он уже понятен из рассказа. Не начинай с вывода.
3. Каждое предложение отвечает на вопрос, который возникает после предыдущего. Полные предложения: понятно, кто что сделал и о чем речь.
4. Рассказывай от себя: мысли и трактовки автора излагай прямо, без «автор пишет», «как отмечает статья». Автор и источник уже стоят в шапке. О самой статье и о том, как она устроена, не пиши.
5. Меньше фактов, но каждый раскрыт до конца. Если упоминаешь событие, решение суда, закон, документ, конфликт - скажи, что именно произошло или что в нем было, кто участвовал, сколько (людей, голосов, лет), чем кончилось и как это связано с рассказом. Если этого не объяснить в одну-две фразы по материалу ниже, выбрось факт целиком: намек хуже, чем ничего. Не перечисляй через запятую книги, имена, события - выбери то, что работает на рассказ.
6. Короткие абзацы, как принято в телеграм-каналах: 2-4 предложения, до 500 знаков. Новый поворот мысли - новый абзац.

Вот образец поста (по лекции, но устройство то же) - так по тону, связности и устройству должен выглядеть и твой:

{example}

{quote_block}

Первая строка ответа - заголовок (3-8 слов, без кавычек и точки). Дальше пост. Длина по материалу: если содержания много, до 2500 знаков; если история простая, хватит 800-1200. Без оценок вроде «интересная статья» и без советов прочитать: ссылка на оригинал уже стоит в шапке. Букву «ё» не используй. Ответ - только заголовок и текст поста.

Статья: «{title}», {author}, {source}, {year}.

Заметки:
{notes}

Текст статьи (проверяй по нему факты, ничего не добавляй сверх него):
{text}"""


TRANSLATE_PROMPT = """Ниже несколько кусков статьи об истории США, разделенных строкой ---. Сделай из них по-русски одну связную цитату.

- Переводи точно и близко к тексту, ничего не добавляй от себя и не пересказывай.
- Если без начала фразы непонятно, о чем речь («он», «эта газета»), назови прямо, о ком или о чем идет речь, по смыслу из статьи.
- Куски соедини в связный текст без пометок о пропусках; где переход резкий, начни новый абзац.
- Цитаты из исторических документов и газет - в кавычках-елочках.
- Букву «ё» не используй.

Ответ - только текст цитаты, без вступления.

{text}"""


def _meta(item):
    src = SOURCES[item["source"]]
    return dict(title=item["title"], author=item["author"] or "автор не указан", source=src["name"],
                about=src["about"], year=item["year"] or "год не указан")


def notes_prompt(item):
    return NOTES_PROMPT.format(**_meta(item), era=", ".join(america.TAGS_ERA), theme=", ".join(america.TAGS_THEME),
                               text=item["text"][:40000])


def post_prompt(item, notes, quote_ru=None):
    shown = {k: v for k, v in notes.items() if k in ("point", "why", "background", "story", "lecturer_views")}
    return POST_PROMPT.format(**_meta(item), example=lectures.EXAMPLE_POST,
                              notes=json.dumps(shown, ensure_ascii=False, indent=1), text=item["text"][:40000],
                              quote_block=lectures.quote_block(quote_ru))


def translate_prompt(passages):
    return TRANSLATE_PROMPT.format(text="\n\n---\n\n".join(passages))


def post_tags(post, item):
    return ["статьи", SOURCES[item["source"]]["tag"]] + america.clean_tags(post.get("tags"))


def build_post(post, item, quote_ru=None, image=None, limit=4000):
    """
    📰 ИЗ СТАТЬИ
    📄 Оригинал (англ.)

    Заголовок
    Автор · «Название» · Prologue, 2019

    Рассказ... [свернутая цитата] ...рассказ
    🖼 подпись к картинке · Wikimedia Commons

    #статьи #Prologue #теги
    """
    esc, clean = lectures._esc, lectures._clean
    src = SOURCES[item["source"]]
    byline = " · ".join(x for x in [item["author"], f"«{item['title']}»", src["short"],
                                    str(item["year"]) if item["year"] else ""] if x)
    head = ["📰 <b>ИЗ СТАТЬИ</b>", f'📄 <a href="{esc(item["url"])}">Оригинал (англ.)</a>', "",
            f"<b>{esc(america._cut(clean(post['title']), 200))}</b>", f"<i>{esc(byline)}</i>", ""]
    tags = " ".join("#" + t for t in post_tags(post, item))

    def assemble(before, q, after):
        lines = head + ([esc(before)] if before else [])
        if q:
            lines += ["", f"<blockquote expandable>{esc(q)}</blockquote>"]
        if after:
            lines += ["", esc(after)]
        if image and image.get("caption"):
            credit = lectures.image_credit(image)
            lines += ["", f"🖼 <i>{esc(clean(image['caption']))}</i>"
                          + (f" ({esc(credit)})" if credit else "")
                          + (f' · <a href="{esc(image["page"])}">{esc(image.get("site", "Wikimedia Commons"))}</a>' if image.get("page") else "")]
        lines += ["", tags]
        return "\n".join(lines)

    return lectures.fit_post(assemble, clean(post.get("before")), clean(quote_ru) if quote_ru else "",
                             clean(post.get("after")), min(limit, 3900))
