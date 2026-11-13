"""
Американистика: конспекты лекций Open Yale Courses (лицензия CC BY-NC-SA).

Единица поста - сюжет: законченный кусок лекции на 10-20 минут (несколько глав подряд;
в расшифровках Йеля лекция разбита на главы с таймкодом). Claude один раз читает лекцию
целиком и делит ее на сюжеты (segment_prompt), разбивка хранится в памяти бота.
Потом по одному сюжету в день: пост (write_prompt), большая цитата - дословный отрывок
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
        "author": "Д. Блайт", "author_full": "Дэвид Блайт", "author_gen": "Дэвида Блайта", "tag": "Блайт",
        "about": "историк Гражданской войны и памяти о ней, автор биографии Фредерика Дугласа",
        "title": "Гражданская война и Реконструкция, 1845–1877", "year": 2008,
    },
    "hist-116": {
        "url": "https://oyc.yale.edu/history/hist-116",
        "author": "Дж. Фриман", "author_full": "Джоанн Фриман", "author_gen": "Джоанн Фриман", "tag": "Фриман",
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

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean", "description": "true, только если фрагмент организационный"},
        "title": {"type": "string"}, "intro": {"type": "string"},
        "body": {"type": "string", "description": "абзацы через пустую строку"},
        "verdict": {"type": "string"},
        "quote_start": {"type": "string", "description": "первые 6-10 слов отрывка, дословно"},
        "quote_end": {"type": "string", "description": "последние 6-10 слов отрывка, дословно"},
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

WRITE_PROMPT = """Ты ведешь телеграм-канал об американистике «Штатный». Читатель - образованный человек, сам читает книги по истории США, но не специалист. Ты пишешь конспект фрагмента лекции - как университетский конспект, который делает внимательный и увлеченный слушатель, а не пресс-релиз и не нейросетевой пересказ.

Лекция: {author_full} ({about}), курс «{course}», Йельский университет, {year}. Лекция {lecture_no} из {lectures_total}: «{lecture_title}».
План всей лекции (глава, время, название):
{outline}

Фрагмент для поста - главы {chapters}, около {minutes} мин. О чем он: {topic}

Напиши на русском:

1. title - заголовок, 3-8 слов, по сути фрагмента. Не вопрос, не кликбейт, без двоеточия-подзаголовка.

2. intro - первый абзац, 2-3 предложения, вводит читателя в контекст, как будто он не видел лекцию: о чем лекция в целом и где в ней находится этот фрагмент, какой вопрос лектор здесь разбирает. Имя лектора и название курса уже стоят в шапке поста, не повторяй их в первой фразе.

3. body - 2-4 абзаца, 1000-1700 знаков. Ход рассуждения лектора: от чего он отталкивается, какие доводы и примеры приводит, к чему приходит. Сохраняй его логику и его трактовку, а не просто перечень фактов; можно сослаться на него («Блайт подчеркивает», «по мысли Фриман»), но не в каждом абзаце. Каждого человека, документ или событие, которые появляются впервые, коротко объясни при первом упоминании (кто это и почему важен), а второстепенные имена лучше опустить, чем перечислять. Только то, что есть во фрагменте, ничего не добавляй от себя.

4. verdict - 1-2 предложения от лица автора канала: чем этот фрагмент хорош и кому стоит посмотреть его или всю лекцию целиком. Сдержанно и конкретно (что именно удалось лектору), без «must see», восклицаний и рекламных слов.

Стиль всего текста: ясный академический русский, без канцелярита и без штампов вроде «Интересно, что», «Стоит отметить», «В этой лекции», «погружает в эпоху», без риторических вопросов. Букву «ё» не используй. Проверь грамматику (например: «по прочтении», а не «по прочтению»).

5. quote_start и quote_end - границы большой цитаты. Выбери в расшифровке фрагмента сплошной отрывок примерно 250-400 слов, где лектор сам формулирует главную мысль сюжета (лучше живой, выразительный кусок, чем сухой). quote_start - первые 6-10 слов отрывка, quote_end - последние 6-10 слов, оба скопированы из расшифровки ДОСЛОВНО, символ в символ. quote_end идет после quote_start.

6. tags - 2-4 тега из списков ниже, только прямо следующие из фрагмента.
Эпохи: {era}
Темы: {theme}

Если фрагмент все же организационный и содержательного поста не выходит, ответь {{"skip": true}}.

Ответ - через инструмент answer:
{{"title": "...", "intro": "...", "body": "абзацы через пустую строку", "verdict": "...", "quote_start": "...", "quote_end": "...", "tags": ["..."]}}

Расшифровка фрагмента:
{text}"""


def write_prompt(course, lecture_no, lectures_total, lecture, segment):
    return WRITE_PROMPT.format(
        author_full=course["author_full"], about=course.get("about", "историк, профессор Йеля"),
        course=course["title"], year=course["year"], lecture_no=lecture_no, lectures_total=lectures_total,
        lecture_title=lecture["title"], outline=_outline(lecture),
        chapters=", ".join(str(n) for n in segment["chapters"]), minutes=segment_minutes(lecture, segment),
        topic=segment.get("topic") or "-", era=", ".join(america.TAGS_ERA), theme=", ".join(america.TAGS_THEME),
        text=segment_text(lecture, segment)[:40000])


def parse_answer(raw):
    data = _json(raw)
    if not data:
        return None
    if data.get("skip"):
        return {"skip": True}
    if not data.get("title") or not data.get("body"):
        return None
    return data


_QUOTES = "\"'“”„«»‘’"


def _keyed(s):
    """Строка для сравнения (нижний регистр, без кавычек, один пробел) и номера исходных символов."""
    out, idx, space = [], [], True
    for i, ch in enumerate(s):
        if ch in _QUOTES:
            continue
        if ch.isspace():
            if space:
                continue
            space = True
            out.append(" ")
        else:
            space = False
            out.append(ch.lower())
        idx.append(i)
    return "".join(out), idx


def extract_quote(text, start, end, max_len=3000):
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
    if len(passage) < 400:
        return None
    if len(passage) > max_len:
        cut = passage[:max_len]
        dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        passage = cut[: dot + 1] if dot > max_len // 2 else cut
    return passage.strip()


TRANSLATE_PROMPT = """Переведи на русский отрывок устной лекции {author_gen} (Йель). Переводи полностью и близко к тексту, без пропусков, пересказа и комментариев, но так, чтобы читалось как хорошая русская речь: убери слова-паразиты, повторы и оговорки устной речи (uh, you know, I mean), сохрани интонацию и шутки лектора. Цитаты из исторических документов переводи как цитаты, в кавычках-елочках. Букву «ё» не используй. Сохрани деление на абзацы.

Ответ - только перевод, без вступления.

Отрывок:
{text}"""


def translate_prompt(course, passage):
    return TRANSLATE_PROMPT.format(author_gen=course["author_gen"], text=passage)


def post_tags(post, course):
    return ["конспект", course["tag"]] + america.clean_tags(post.get("tags"))


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
    q = q.removesuffix(" […]")
    if len(q) <= n:
        return q
    cut = q[:n]
    dot = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind(".\n"))
    return (cut[: dot + 1] if dot > n // 3 else cut.rstrip()) + " […]"


def build_post(post, course, lecture_no, lectures_total, lecture, segment, quote_ru=None, limit=4000):
    """
    🎓 КОНСПЕКТ ЛЕКЦИИ
    ▶️ Видео с 12:34 · 📄 Расшифровка (англ.)

    Заголовок
    Дэвид Блайт · Гражданская война и Реконструкция, 1845–1877 · Йель, 2008 · лекция 5 из 27

    Контекст. Ход рассуждения. Голос автора канала.

    Фрагмент лекции в переводе:
    [свернутая большая цитата]

    #конспект #Блайт #теги
    """
    links = []
    url = video_url(lecture, segment)
    if url:
        links.append(f'▶️ <a href="{_esc(url)}">Видео с {_clock(segment_start(lecture, segment))}</a> '
                     f'(~{segment_minutes(lecture, segment)} мин)')
    links.append(f'📄 <a href="{course["url"]}/lecture-{lecture_no}">Расшифровка (англ.)</a>')
    head = ["🎓 <b>КОНСПЕКТ ЛЕКЦИИ</b>", " · ".join(links), "",
            f"<b>{_esc(america._cut(_clean(post['title']), 200))}</b>",
            f"<i>{_esc(course['author_full'])} · {_esc(course['title'])} · Йель, {course['year']} · "
            f"лекция {lecture_no} из {lectures_total}</i>", ""]
    text_parts = [_clean(post.get("intro")), _clean(post["body"]), _clean(post.get("verdict"))]
    body = "\n\n".join(_esc(america._cut(p, 2400)) for p in text_parts if p)
    tags = " ".join("#" + t for t in post_tags(post, course))

    def assemble(q):
        lines = head + [body]
        if q:
            lines += ["", "<i>Фрагмент лекции в переводе:</i>", f"<blockquote expandable>{_esc(q)}</blockquote>"]
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
