"""
Американистика: посты по лекциям Open Yale Courses (лицензия CC BY-NC-SA).

Единица поста - сюжет: законченный кусок лекции на 10-20 минут (несколько глав подряд;
в расшифровках Йеля лекция разбита на главы с таймкодом). Claude один раз читает лекцию
целиком и делит ее на сюжеты (segment_prompt), разбивка хранится в памяти бота.
Потом по одному сюжету в день: заметки (notes_prompt), по ним пост сплошным текстом
по образцу (post_prompt), большая цитата - дословный отрывок
расшифровки между двумя якорями, переведенный отдельным запросом (translate_prompt).
Сеть, память и отправка - в bot.py (check_lectures).
"""
import re
import json
import random
from html.parser import HTMLParser
import html as htmlmod

from bs4 import BeautifulSoup

import america

COURSES = {
    "hist-119": {
        "url": "https://oyc.yale.edu/history/hist-119",
        "author": "Д. Блайт", "author_full": "Дэвид Блайт", "author_gen": "Дэвида Блайта", "author_gen_short": "Блайта", "tag": "Блайт",
        "about": "историк Гражданской войны и памяти о ней, автор биографии Фредерика Дугласа",
        "title": "Гражданская война и Реконструкция, 1845–1877", "year": 2008,
    },
    "hist-116": {
        "url": "https://oyc.yale.edu/history/hist-116",
        "author": "Дж. Фриман", "author_full": "Джоанн Фриман", "author_gen": "Джоанн Фриман", "author_gen_short": "Фриман", "tag": "Фриман",
        "about": "историк революции и ранней республики, автор книги о политической культуре дуэлей «Affairs of Honor»",
        "title": "Американская революция", "year": 2010,
    },
}

SOURCE_TAGS = [c["tag"] for c in COURSES.values()]


def lecture_numbers(course_html, course_id):
    return sorted({int(n) for n in re.findall(rf"/{course_id}/lecture-(\d+)", course_html)})


def _seconds(ts):
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def parse_lecture(page_html):
    """-> {'title', 'youtube', 'chapters': [{'n', 'title', 'start', 'text'}]}"""
    soup = BeautifulSoup(page_html, "html.parser")
    box = soup.select_one("#inline_content")
    if not box:
        raise RuntimeError("на странице лекции нет расшифровки")
    h2 = box.find("h2")
    lecture_title = h2.get_text(" ", strip=True) if h2 else ""
    lecture_title = re.sub(r"^HIST \d+\s*-\s*Lecture \d+\s*-\s*", "", lecture_title)
    m = re.search(r"youtube\.com/embed/([\w-]{6,})", page_html)
    chapters = []
    for h3 in box.find_all("h3"):
        mm = re.match(r"Chapter (\d+)\.\s*(.*?)\s*\[(\d{1,2}:\d{2}:\d{2})\]", h3.get_text(" ", strip=True))
        if not mm:
            continue
        parts = []
        for sib in h3.find_all_next():
            if sib.name == "h3":
                break
            if sib.name == "p" and not sib.find("h3"):
                t = sib.get_text(" ", strip=True)
                if t and t != "[end of transcript]":
                    parts.append(t)
        chapters.append({"n": int(mm.group(1)), "title": mm.group(2), "start": _seconds(mm.group(3)),
                         "text": "\n\n".join(parts)})
    return {"title": lecture_title, "youtube": m.group(1) if m else None, "chapters": chapters}



def fragment_id(course_id, lecture, segment):
    """Сюжет называется по первой главе: hist-119/5/s2."""
    return f"{course_id}/{lecture}/s{segment['chapters'][0]}"


def pick(course_ids, lectures_by_course, exhausted, last_course=None):
    """Случайная лекция, в которой еще есть свободные сюжеты. Курсы чередуются.

    -> (курс, номер лекции) или None. Сюжет выбирает choose_segment после разбивки.
    """
    order = sorted(course_ids, key=lambda c: c == last_course)  # прошлый курс в конец
    for c in order:
        free = [n for n in lectures_by_course.get(c, []) if f"{c}/{n}" not in exhausted]
        if free:
            return c, random.choice(free)
    return None


def _clock(sec):
    h, rest = divmod(int(sec), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _outline(lecture):
    return "\n".join(f"{c['n']}. [{_clock(c['start'])}] {c['title']}" for c in lecture["chapters"])


# ─── Разбивка на сюжеты ─────────────────────────────────────────────────────

SEGMENT_PROMPT = """Ниже расшифровка лекции {author_gen}, курс «{course}» (Йельский университет, {year}), лекция {lecture_no} «{lecture_title}». Она разбита на главы, у каждой номер и время начала.

Раздели лекцию на сюжеты для постов в телеграм-канале. Сюжет - несколько глав подряд (обычно 2, иногда 1 или 3), которые вместе дают законченную линию рассуждения на 10-20 минут: у нее есть начало, развитие и мысль, к которой лектор приходит. Пост по сюжету должен быть понятен без остальной лекции.
- Главы в сюжете идут подряд, сюжеты не пересекаются. Не обязательно покрывать всю лекцию.
- Организационное (объявления о курсе, экзаменах, списке чтения) и короткие связки в сюжеты не включай.
- topic - одна фраза по-русски: о чем сюжет и к какой мысли он ведет.

Ответ - через инструмент answer:
{{"segments": [{{"chapters": [1, 2], "topic": "..."}}]}}

Главы:
{outline}

Расшифровка:
{text}"""


SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {"segments": {"type": "array", "items": {
        "type": "object",
        "properties": {"chapters": {"type": "array", "items": {"type": "integer"}}, "topic": {"type": "string"}},
        "required": ["chapters", "topic"]}}},
    "required": ["segments"],
}

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean", "description": "true, только если фрагмент организационный"},
        "point": {"type": "string", "description": "мысль, которую лектор доказывает во фрагменте"},
        "why": {"type": "string", "description": "зачем она лектору: к чему ведет в лекции и курсе"},
        "background": {"type": "string", "description": "что читатель должен знать заранее, чтобы понять рассказ"},
        "story": {"type": "array", "items": {"type": "string"},
                  "description": "события и факты фрагмента по порядку рассказа, каждый с тем, как он связан с мыслью"},
        "lecturer_views": {"type": "array", "items": {"type": "string"},
                           "description": "трактовки и замечания самого лектора, которые стоит приписать ему"},
        "quote": {"type": "array", "description": "куски расшифровки для цитаты, по порядку", "items": {
            "type": "object",
            "properties": {"start": {"type": "string", "description": "первые 6-10 слов куска, дословно"},
                           "end": {"type": "string", "description": "последние 6-10 слов куска, дословно"}},
            "required": ["start", "end"]}},
        "images": {"type": "array", "items": {"type": "string"},
                   "description": "2-3 запроса по-английски для поиска иллюстрации на Wikimedia Commons"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


def segment_prompt(course, lecture_no, lecture):
    text = "\n\n".join(f"=== Глава {c['n']}. {c['title']} [{_clock(c['start'])}]\n{c['text']}"
                       for c in lecture["chapters"])
    return SEGMENT_PROMPT.format(
        author_gen=course["author_gen"], course=course["title"], year=course["year"], lecture_no=lecture_no,
        lecture_title=lecture["title"], outline=_outline(lecture), text=text[:90000])


def _json(raw):
    if isinstance(raw, dict):  # ответ через инструмент уже разобран
        return raw
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def parse_segments(raw, lecture):
    """Проверяет разбивку: главы существуют, идут подряд, сюжеты не пересекаются."""
    data = _json(raw)
    if not data or not isinstance(data.get("segments"), list):
        return None
    known = {c["n"] for c in lecture["chapters"]}
    taken, out = set(), []
    for s in data["segments"]:
        try:
            chs = sorted(int(x) for x in s.get("chapters") or [])
        except (TypeError, ValueError):
            continue
        if not chs or len(chs) > 4 or not set(chs) <= known or taken & set(chs):
            continue
        if chs != list(range(chs[0], chs[-1] + 1)):
            continue
        taken |= set(chs)
        out.append({"chapters": chs, "topic": str(s.get("topic") or "").strip()})
    return sorted(out, key=lambda s: s["chapters"][0])


def segment_used(course_id, lecture_no, segment, used):
    """Занят, если по нему уже был пост, или по одной из его глав (старый формат hist-119/5/3)."""
    if fragment_id(course_id, lecture_no, segment) in used:
        return True
    return any(f"{course_id}/{lecture_no}/{n}" in used for n in segment["chapters"])


def choose_segment(course_id, lecture_no, segments, used):
    free = [s for s in segments if not segment_used(course_id, lecture_no, s, used)]
    return random.choice(free) if free else None


def segment_chapters(lecture, segment):
    return [c for c in lecture["chapters"] if c["n"] in segment["chapters"]]


def segment_text(lecture, segment):
    return "\n\n".join(c["text"] for c in segment_chapters(lecture, segment))


def segment_start(lecture, segment):
    return segment_chapters(lecture, segment)[0]["start"]


def segment_minutes(lecture, segment):
    """Длина сюжета: до начала следующей главы (у последней главы - прикидка по тексту)."""
    chs = lecture["chapters"]
    last = segment["chapters"][-1]
    nxt = [c["start"] for c in chs if c["n"] == last + 1]
    if nxt:
        end = nxt[0]
    else:  # ~ 900 знаков расшифровки в минуту
        end = segment_chapters(lecture, segment)[-1]["start"] + len(segment_chapters(lecture, segment)[-1]["text"]) / 15
    return max(1, round((end - segment_start(lecture, segment)) / 60))

# ─── Пост ───────────────────────────────────────────────────────────────────

NOTES_PROMPT = """Ты готовишь заметки для поста в телеграм-канале об истории США по фрагменту лекции. Сам пост будет писаться потом по этим заметкам, поэтому здесь важна не форма, а понимание.

Лекция: {author_full} ({about}), курс «{course}», Йельский университет, {year}. Лекция {lecture_no} из {lectures_total}: «{lecture_title}».
План всей лекции (глава, время, название):
{outline}

Фрагмент - главы {chapters}, около {minutes} мин. О чем он: {topic}

Разберись и запиши:
- point: какую мысль лектор доказывает во фрагменте;
- why: зачем она ему, к чему ведет дальше в лекции и курсе (последняя глава лекции ниже - для этого);
- background: что нужно знать читателю, который лекцию не видел, чтобы рассказ был понятен (обстановка, кто есть кто);
- story: факты и события фрагмента в том порядке, в каком их удобно рассказывать, у каждого - чем он работает на мысль. То, что на мысль не работает, не записывай;
- lecturer_views: что во фрагменте - именно трактовка или замечание лектора, а не общеизвестный факт;
- images: 2-3 поисковых запроса по-английски для Wikimedia Commons, чтобы найти иллюстрацию к посту. Лучше всего то, что интересно разглядывать: фотографии, документы, первые полосы газет, плакаты, карикатуры, гравюры, вещи, старые карты городов и земель. Хуже: живопись на исторические темы и схемы сражений. Запрос - короткий, по конкретному предмету ("Albany Plan of Union 1754", "Join or Die cartoon"), не по абстрактной теме;
- quote: место, где лектор своими словами объясняет и обосновывает мысль (не вступление, не повтор прошлой лекции, не перечисление, не анекдот ради анекдота). 1-4 куска фрагмента по порядку, вместе 200-450 слов. Для каждого куска start и end - первые и последние 6-10 слов, скопированные из расшифровки дословно;
- tags: 2-4 тега из списков, только прямо следующие из фрагмента. Эпохи: {era}. Темы: {theme}.
Пиши по-русски, коротко, как для себя. Только то, что есть во фрагменте и в лекции.
Если фрагмент организационный (о курсе, экзаменах, книгах), ответь skip: true.

Ответ - через инструмент answer.

Последняя глава лекции (для понимания, куда ведет лектор):
{final}

Расшифровка фрагмента:
{text}"""


EXAMPLE_POST = """Олбанский план: союз, который не приняли

К середине XVIII века британские колонии в Северной Америке жили каждая сама по себе. У каждой были своя хартия, своя ассамблея и свои отношения с Лондоном, а общего правительства не было. Договориться о совместных действиях колонии пытались редко, и всякий раз из страха перед внешним врагом. В 1643 году колонии Новой Англии создали конфедерацию, опасаясь голландцев и враждебных индейских племен. В 1754 году по той же причине представители нескольких колоний съехались на конгресс в Олбани.

Даже общая опасность не сделала их единодушнее. Приглашения получили девять колоний, но Нью-Джерси и Виргиния делегатов не прислали: поездку сочли слишком дорогой. Приехавшие привезли от своих ассамблей разные инструкции. Почти всем разрешили говорить только об обороне и делах с индейцами, и лишь делегатам Массачусетса позволили обсуждать союз колоний. Так будет и дальше: на каждое общее собрание колонии будут присылать людей с разными полномочиями, вплоть до Конституционного конвента.

[ЦИТАТА]

И все же в Олбани появился проект союза. В его основу легла схема, которую еще в 1751 году придумал Бенджамин Франклин. Во главе союза стоял президент-генерал: его назначала корона, и он мог наложить вето на любое решение. Рядом с ним работал Большой совет, по два депутата от каждой колонии, их выбирали колониальные ассамблеи. Совет ведал только общими делами: отношениями с индейцами, землями за пределами колоний и обороной. Во всем остальном колонии жили бы, как прежде, по своим хартиям.

Конгресс одобрил план и разослал его по колониям на утверждение. Там он и провалился. Одних колонистов пугало, что союз урежет власть короля, других - что он ущемит права, записанные в хартиях. План задумывали как компромисс между короной и колониями, и поэтому он не устроил ни тех, ни других.

Дело было в том, что в 1754 году колонистам еще не приходилось выбирать. Верность королю и собственные права пока не противоречили друг другу, и общий союз казался лишним. Все изменится через одиннадцать лет, когда парламент примет Гербовый акт и колонисты почувствуют, что под угрозой оказались их права англичан. Тогда они снова соберутся вместе, на Конгресс по Гербовому акту, и объединит их уже не страх перед соседями, а защита своих прав."""


POST_PROMPT = """Ты пишешь пост для телеграм-канала об истории США. Читатель умный, читает книги по истории, но эту лекцию не видел и подробностей не знает. Ты рассказываешь ему, что услышал в лекции, как рассказывают историю: связно, по порядку, своими словами.

Как устроен хороший рассказ:
1. Сначала обстановка, потом событие. Прежде чем сказать, что что-то провалилось или изменилось, объясни, что это было и кто в этом участвовал. Читатель не должен спотыкаться о названия и намеки.
2. Рассказ идет по порядку, а вывод стоит в конце, когда он уже понятен из рассказа. Не начинай с вывода.
3. Каждое предложение отвечает на вопрос, который возникает после предыдущего. Полные предложения: понятно, кто что сделал и о чем речь.
4. Рассказывай голосом лектора: его мысли и трактовки излагай прямо, как свои, без «Фриман замечает», «по мысли Блайта», «лектор подчеркивает». Имя лектора уже стоит в шапке, в тексте его не называй. Если лектор спорит с другим историком или с распространенным мнением, так и скажи («принято считать, что…, но…»). О самой лекции и о том, как лектор ее строит, не пиши.

Вот образец поста по другому фрагменту - так по тону, связности и устройству должен выглядеть и твой:

{example}

Строка [ЦИТАТА] в образце - место, где в пост встанет свернутая цитата лектора. Поставь ее и у себя отдельной строкой, после абзаца, к которому цитата подходит лучше всего. О цитате в тексте не упоминай.

Первая строка ответа - заголовок (3-8 слов, без кавычек и точки). Дальше пост. Длина по материалу: если содержания много, до 2500 знаков; если мысль простая, хватит 800-1200. Без оценок вроде «полезный фрагмент» и без советов посмотреть: ссылка на видео уже стоит в шапке. Букву «ё» не используй. Ответ - только заголовок и текст поста.

Лекция: {author_full} ({about}), курс «{course}», Йель, {year}, лекция {lecture_no} «{lecture_title}».

Заметки к фрагменту:
{notes}

Расшифровка фрагмента (проверяй по ней факты, ничего не добавляй сверх нее):
{text}"""


def _common(course, lecture_no, lecture, segment):
    return dict(author_full=course["author_full"], about=course.get("about", "историк, профессор Йеля"),
                course=course["title"], year=course["year"], lecture_no=lecture_no, lecture_title=lecture["title"],
                text=segment_text(lecture, segment)[:40000])


def notes_prompt(course, lecture_no, lectures_total, lecture, segment):
    last = lecture["chapters"][-1] if lecture["chapters"] else None
    return NOTES_PROMPT.format(
        **_common(course, lecture_no, lecture, segment), lectures_total=lectures_total, outline=_outline(lecture),
        chapters=", ".join(str(n) for n in segment["chapters"]), minutes=segment_minutes(lecture, segment),
        topic=segment.get("topic") or "-",
        final=(last["text"][:6000] if last and last["n"] not in segment["chapters"]
               else "(фрагмент сам содержит конец лекции)"),
        era=", ".join(america.TAGS_ERA), theme=", ".join(america.TAGS_THEME))


def post_prompt(course, lecture_no, lecture, segment, notes):
    shown = {k: v for k, v in notes.items() if k in ("point", "why", "background", "story", "lecturer_views")}
    return POST_PROMPT.format(**_common(course, lecture_no, lecture, segment), example=EXAMPLE_POST,
                              notes=json.dumps(shown, ensure_ascii=False, indent=1))


# ─── Иллюстрация с Wikimedia Commons ────────────────────────────────────────

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_UA = "ShtatnyBot/1.0 (https://github.com/itsyegor/yeletterboxdbot)"  # Wikimedia требует UA


def _plain(html_text, n=300):
    p = _Visible()
    p.feed(str(html_text or ""))
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()[:n]


def commons_params(query, limit=8):
    return {"action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
            "gsrlimit": limit, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1280}


def commons_candidates(data):
    """Ответ API Commons -> подходящие картинки (jpeg/png, не меньше 600 px)."""
    out = []
    for page in ((data or {}).get("query") or {}).get("pages", {}).values():
        ii = (page.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png") or (ii.get("width") or 0) < 600:
            continue
        meta = ii.get("extmetadata") or {}
        val = lambda k: _plain((meta.get(k) or {}).get("value"))
        out.append({"title": page.get("title", "").removeprefix("File:"), "description": val("ImageDescription"),
                    "date": val("DateTimeOriginal")[:60], "artist": val("Artist")[:120],
                    "license": val("LicenseShortName")[:40], "thumb": ii.get("thumburl") or ii.get("url"),
                    "page": ii.get("descriptionurl")})
    return out


PICK_PROMPT = """Ниже пост для телеграм-канала об истории США и найденные на Wikimedia Commons картинки. Выбери одну, которая лучше всего подходит как иллюстрация: прямо связана с тем, о чем пост, или с его эпохой и героями, и при этом ее интересно разглядывать. Картинка, которая к посту не относится, хуже, чем никакой: если подходящей нет, ответь choice: -1.

caption - подпись по-русски, одно-два предложения: что на картинке, когда и кем сделано, и, если нужно, чем она связана с постом. Только то, что следует из описания картинки, ничего не придумывай. Начни с сути, без «На картинке изображено». Букву «ё» не используй.

Ответ - через инструмент answer.

Пост:
{post}

Картинки:
{items}"""

PICK_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "integer", "description": "номер картинки или -1"},
                   "caption": {"type": "string"}},
    "required": ["choice"],
}


def pick_prompt(post_text, candidates):
    items = "\n".join(f"{i}. {c['title']} | {c['description'] or '-'} | дата: {c['date'] or '-'} | автор: "
                      f"{c['artist'] or '-'}" for i, c in enumerate(candidates))
    return PICK_PROMPT.format(post=_plain(post_text, 3000), items=items)


_FREE = ("public domain", "pd", "cc0", "no restrictions")


def image_credit(c):
    """Для свободных от прав картинок достаточно источника; для CC BY нужно указать автора и лицензию."""
    lic = (c.get("license") or "").lower()
    if not lic or any(lic.startswith(f) for f in _FREE):
        return ""
    return ", ".join(x for x in [c.get("artist"), c.get("license")] if x)


QUOTE_MARK = "[ЦИТАТА]"


def parse_post(raw):
    """Текст поста -> {'title', 'before', 'after'} или None. Цитата встает на место строки [ЦИТАТА]."""
    lines = [l.rstrip() for l in str(raw or "").strip().splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if len(lines) < 2:
        return None
    title = lines[0].strip().strip("*#«»\"' ").rstrip(".")
    body = "\n".join(lines[1:]).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    if len(body) < 300:
        return None
    if QUOTE_MARK in body:
        before, after = body.split(QUOTE_MARK, 1)
        after = after.replace(QUOTE_MARK, "")
    else:  # метки нет - цитата после середины текста
        paras = body.split("\n\n")
        k = max(1, len(paras) // 2)
        before, after = "\n\n".join(paras[:k]), "\n\n".join(paras[k:])
    return {"title": title, "before": before.strip(), "after": after.strip()}


def parse_answer(raw):
    data = _json(raw)
    if not data:
        return None
    if data.get("skip"):
        return {"skip": True}
    if not data.get("point") or not data.get("story"):
        return None
    return data


_QUOTES = "\"'“”„«»‘’"


def _keyed(s):
    """Строка для сравнения: только буквы и цифры в нижнем регистре, остальное - один пробел.

    Кавычки, тире, многоточия и апострофы модель копирует неточно, поэтому они не участвуют.
    Возвращает строку и номера исходных символов.
    """
    out, idx, space = [], [], True
    for i, ch in enumerate(s):
        if ch.isalnum():
            space = False
            out.append(ch.lower())
            idx.append(i)
        elif not space:
            space = True
            out.append(" ")
            idx.append(i)
    return "".join(out), idx


def extract_quote(text, start, end, max_len=3000, min_len=400):
    """Дословный отрывок между якорями или None. Слишком длинный обрезается по концу предложения."""
    k, idx = _keyed(text)
    ks, ke = _keyed(start or "")[0].strip(), _keyed(end or "")[0].strip()
    if len(ks) < 15 or len(ke) < 15:
        return None
    a = k.find(ks)
    if a < 0:
        return None
    b = k.find(ke, a + len(ks) // 2)
    if b < 0:
        return None
    passage = text[idx[a]: idx[b + len(ke) - 1] + 1]
    # до конца предложения, если якорь оборвался на середине
    m = re.match(r"[^.!?]{0,120}[.!?]", text[idx[b + len(ke) - 1] + 1:])
    if m and not re.search(r"[.!?]$", passage):
        passage += m.group(0)
    if len(passage) < min_len:
        return None
    if len(passage) > max_len:
        cut = passage[:max_len]
        dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        passage = cut[: dot + 1] if dot > max_len // 2 else cut
    return passage.strip()


def extract_quotes(text, spans, max_len=4500):
    """Склеивает дословные куски по якорям. Куски, которые не нашлись, пропускает."""
    out, total = [], 0
    for sp in spans or []:
        if not isinstance(sp, dict):
            continue
        p = extract_quote(text, sp.get("start"), sp.get("end"), max_len=3000, min_len=80)
        if p and p not in out:
            if total + len(p) > max_len:
                break
            out.append(p)
            total += len(p)
    return out if total >= 400 else []


TRANSLATE_PROMPT = """Ниже несколько кусков устной лекции {author_gen} (Йель), разделенных строкой ---. Сделай из них по-русски одну связную цитату для печати, как в изданной книге лекций.

- Переводи точно по смыслу, ничего не добавляй от себя и не пересказывай.
- Это расшифровка устной речи: убери слова-паразиты, повторы, оговорки, обращения к залу и ремарки вроде [laughter], [смех], обрывы фраз. Короткие пояснения лектора, не несущие мысли («то есть он как бы говорит»), тоже можно убрать.
- Убери отсылки к другим лекциям и к курсу («как я говорила в четверг», «на прошлой неделе я упоминала», «об этом поговорим позже»).
- Если без начала фразы непонятно, о чем речь («две другие», «эта попытка», «он»), назови прямо, о чем или о ком идет речь, по смыслу из самой лекции.
- Куски соедини в связный текст без пометок о пропусках; где переход резкий, начни новый абзац.
- Цитаты из исторических документов - в кавычках-елочках.
- Букву «ё» не используй.

Ответ - только текст цитаты, без вступления.

{text}"""


def translate_prompt(course, passages):
    return TRANSLATE_PROMPT.format(author_gen=course["author_gen"], text="\n\n---\n\n".join(passages))


def post_tags(post, course):
    return ["лекции", course["tag"]] + america.clean_tags(post.get("tags"))


def _esc(s):
    return htmlmod.escape(str(s or ""), quote=False)


def _clean(s):
    return str(s or "").replace("ё", "е").replace("Ё", "Е").strip()


class _Visible(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_len(html_text):
    """Длина текста, как ее считает Telegram (в UTF-16, после разбора разметки)."""
    p = _Visible()
    p.feed(html_text)
    return len("".join(p.parts).encode("utf-16-le")) // 2


def video_url(lecture, segment):
    if not lecture.get("youtube"):
        return None
    return f"https://www.youtube.com/watch?v={lecture['youtube']}&t={segment_start(lecture, segment)}s"


def _trim_quote(q, n):
    if len(q) <= n:
        return q
    cut = q[:n]
    dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind(".\n"))
    return cut[: dot + 1] if dot > n // 3 else cut.rstrip() + "…"


def build_post(post, course, lecture_no, lectures_total, lecture, segment, quote_ru=None, limit=4000, image=None):
    """
    🎓 ИЗ ЛЕКЦИИ
    ▶️ Видео с 12:34 (~15 мин) · 📄 Расшифровка (англ.)

    Заголовок
    Дэвид Блайт · Гражданская война и Реконструкция, 1845–1877 · Йель, 2008 · лекция 5 из 27

    Рассказ... [свернутая цитата лектора] ...рассказ

    #лекции #Блайт #теги
    🖼 подпись к картинке · Wikimedia Commons
    (картинка над постом)
    post - результат parse_post, теги - из заметок (post['tags']).
    """
    links = []
    url = video_url(lecture, segment)
    if url:
        links.append(f'▶️ <a href="{_esc(url)}">Видео с {_clock(segment_start(lecture, segment))}</a> '
                     f'(~{segment_minutes(lecture, segment)} мин)')
    links.append(f'📄 <a href="{course["url"]}/lecture-{lecture_no}">Расшифровка (англ.)</a>')
    head = ["🎓 <b>ИЗ ЛЕКЦИИ</b>", " · ".join(links), "",
            f"<b>{_esc(america._cut(_clean(post['title']), 200))}</b>",
            f"<i>{_esc(course['author_full'])} · {_esc(course['title'])} · Йель, {course['year']} · "
            f"лекция {lecture_no} из {lectures_total}</i>", ""]
    before = _esc(_clean(post.get("before")))
    after = _esc(_clean(post.get("after")))
    tags = " ".join("#" + t for t in post_tags(post, course))

    def assemble(q):
        lines = head + ([before] if before else [])
        if q:
            lines += ["", f"<blockquote expandable>{_esc(q)}</blockquote>"]
        if after:
            lines += ["", after]
        if image and image.get("caption"):
            credit = image_credit(image)
            lines += ["", f"🖼 <i>{_esc(_clean(image['caption']))}</i>"
                          + (f" ({_esc(credit)})" if credit else "")
                          + (f' · <a href="{_esc(image["page"])}">Wikimedia Commons</a>' if image.get("page") else "")]
        lines += ["", tags]
        return "\n".join(lines)

    q = _clean(quote_ru) if quote_ru else ""
    text = assemble(q)
    while q and visible_len(text) > limit:
        over = visible_len(text) - limit
        q = _trim_quote(q, max(0, len(q) - over - 10))
        if len(q) < 400:
            q = ""
        text = assemble(q)
    return text
