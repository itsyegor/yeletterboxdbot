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
        "mixed": True,  # пишет не только об истории США - нерелевантное отсеивают заметки
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
    base = SOURCES[source_id]["feed"]
    return base if page <= 1 else f"{base}?paged={page}"


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

Сначала реши, подходит ли статья. Канал - об истории США (и Северной Америки) любой эпохи, от колоний до 2014 года включительно; события позже 2014 года не берем. Если статья не об истории США, если она о событиях после 2014 года, если это объявление, обзор выставки или новость о самом учреждении без исторического содержания - ответь skip: true.

Если подходит, разберись и запиши:
- point: какую мысль или историю статья рассказывает;
- why: почему это интересно и что это говорит об эпохе;
- background: что нужно знать читателю, чтобы рассказ был понятен (обстановка, кто есть кто);
- story: факты и события в том порядке, в каком их удобно рассказывать, у каждого - чем он работает на мысль. То, что на мысль не работает, не записывай;
- lecturer_views: что в статье - именно трактовка автора, а не общеизвестный факт;
- quote: место, где лучше всего видна суть: живой документ, свидетельство, яркий кусок рассказа или объяснение автора. 1-4 куска статьи по порядку, вместе 200-450 слов. Для каждого куска start и end - первые и последние 6-10 слов, скопированные из текста дословно;
- images: 2-3 поисковых запроса по-английски для Wikimedia Commons. Запрос - по предмету (событие, человек, место, документ, вещь), а не по виду картинки. Короткий и конкретный;
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

Вот образец поста (по лекции, но устройство то же) - так по тону, связности и устройству должен выглядеть и твой:

{example}

Строка [ЦИТАТА] в образце - место, где в пост встанет свернутая цитата из статьи. Поставь ее и у себя отдельной строкой, после абзаца, к которому цитата подходит лучше всего. О цитате в тексте не упоминай.

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


def post_prompt(item, notes):
    shown = {k: v for k, v in notes.items() if k in ("point", "why", "background", "story", "lecturer_views")}
    return POST_PROMPT.format(**_meta(item), example=lectures.EXAMPLE_POST,
                              notes=json.dumps(shown, ensure_ascii=False, indent=1), text=item["text"][:40000])


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
    before, after = esc(clean(post.get("before"))), esc(clean(post.get("after")))
    tags = " ".join("#" + t for t in post_tags(post, item))

    def assemble(q):
        lines = head + ([before] if before else [])
        if q:
            lines += ["", f"<blockquote expandable>{esc(q)}</blockquote>"]
        if after:
            lines += ["", after]
        if image and image.get("caption"):
            credit = lectures.image_credit(image)
            lines += ["", f"🖼 <i>{esc(clean(image['caption']))}</i>"
                          + (f" ({esc(credit)})" if credit else "")
                          + (f' · <a href="{esc(image["page"])}">Wikimedia Commons</a>' if image.get("page") else "")]
        lines += ["", tags]
        return "\n".join(lines)

    q = clean(quote_ru) if quote_ru else ""
    text = assemble(q)
    while q and lectures.visible_len(text) > limit:
        over = lectures.visible_len(text) - limit
        q = lectures._trim_quote(q, max(0, len(q) - over - 10))
        if len(q) < 400:
            q = ""
        text = assemble(q)
    return text
