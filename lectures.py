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

Сначала выбери ОДНУ линию: одну историю, эпизод или мысль, самую цельную и интересную, которую можно рассказать от начала до конца. Весь пост будет только о ней. Остальные темы текста выбрось, даже интересные: лучше одна история целиком, чем пять по абзацу. В point запиши эту линию, в story - только то, что к ней относится, в quote - только куски о ней же.

Разберись и запиши:
- point: какую мысль лектор доказывает во фрагменте;
- why: зачем она ему, к чему ведет дальше в лекции и курсе (последняя глава лекции ниже - для этого);
- background: что нужно знать читателю, который лекцию не видел, чтобы рассказ был понятен (обстановка, кто есть кто);
- story: факты и события фрагмента в том порядке, в каком их удобно рассказывать, у каждого - чем он работает на мысль и все, что нужно, чтобы его понять (что именно произошло, кто, сколько, чем кончилось). То, что на мысль не работает или чего из текста не объяснить, не записывай;
- lecturer_views: что во фрагменте - именно трактовка или замечание лектора, а не общеизвестный факт;
- images: 3-5 поисковых запросов по-английски для поиска иллюстрации (Wikimedia Commons, Библиотека Конгресса), от конкретного к общему, последний - эпоха и место («Philadelphia 1770s»), чтобы картинка нашлась всегда. Запрос - по предмету поста (событие, человек, место, документ, вещь), а не по виду картинки: что найдется, фото или плакат, решит выдача. Запрос - короткий и конкретный ("Albany Plan of Union 1754", "Join or Die cartoon"), не по абстрактной теме;
- quote: прямая речь, которая нужна рассказу и которую лучше услышать дословно: документ, письмо, речь, газета, свидетельство, которые лектор зачитывает, или его собственная яркая формулировка главной мысли. Не бери кусок обычного изложения - его пост и так перескажет своими словами, и выйдет повтор. Не вступление, не перечисление. 1-4 куска фрагмента по порядку, вместе 100-450 слов. Если такой прямой речи во фрагменте нет, оставь список пустым. Для каждого куска start и end - первые и последние 6-10 слов, скопированные из расшифровки дословно;
- tags: 2-3 тега из списков, только прямо следующие из фрагмента, самый точный первым. Эпохи: {era}. Темы: {theme}.
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
5. Меньше фактов, но каждый раскрыт до конца. Если упоминаешь событие, решение суда, закон, документ, конфликт - скажи, что именно произошло или что в нем было, кто участвовал, сколько (людей, голосов, лет), чем кончилось и как это связано с рассказом. Если этого не объяснить в одну-две фразы по материалу ниже, выбрось факт целиком: намек хуже, чем ничего. Не перечисляй через запятую книги, имена, события - выбери то, что работает на рассказ.
6. Короткие абзацы, как принято в телеграм-каналах: 2-4 предложения, до 500 знаков. Новый поворот мысли - новый абзац.
7. Никаких штампов нейросетевого текста: определений через отрицание и противопоставлений для эффекта («это был не бред, а образец», «не просто X, а Y», «дело не в X, а в Y», «это не случайность»), а также «стоит отметить», «важно понимать», «не что иное, как», «в этом и заключается», «по сути». Говори прямо, что есть, а не чем это не является.
8. Одна линия: весь пост - об одной истории или мысли из заметок. Не перескакивай на другие темы, даже если они есть в тексте.
9. Ничего непонятного: сокращения и аббревиатуры расшифровывай при первом упоминании («Организация освобождения Палестины», а не «ООП»); о каждом человеке - кто он и почему здесь важен («Теодор Рузвельт», а не «Рузвельт», если из текста неясно, какой). Не пиши «Вашингтон», «Париж» в значении правительства страны - пиши «американское правительство». В каждом предложении ясно, кто действует и над кем. Каждое следующее предложение логически вытекает из предыдущего; если связь надо додумывать, допиши ее или убери предложение.

Вот образец поста по другому фрагменту - так по тону, связности и устройству должен выглядеть и твой:

{example}

{quote_block}

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


def prev_lines_text(lines):
    """Строка для заметок: по каким линиям этого текста уже были посты."""
    if not lines:
        return ""
    return ("\n\nПо этому тексту уже были посты, вот их линии:\n" + "\n".join(f"- {l}" for l in lines) +
            "\nВыбери другую линию, не повторяя эти ни целиком, ни частично. Если другой цельной и интересной "
            "линии в тексте нет, ответь skip: true.")


def notes_prompt(course, lecture_no, lectures_total, lecture, segment, prev_lines=None):
    last = lecture["chapters"][-1] if lecture["chapters"] else None
    return _notes_prompt_text(course, lecture_no, lectures_total, lecture, segment, last) + prev_lines_text(prev_lines)


def _notes_prompt_text(course, lecture_no, lectures_total, lecture, segment, last):
    return NOTES_PROMPT.format(
        **_common(course, lecture_no, lecture, segment), lectures_total=lectures_total, outline=_outline(lecture),
        chapters=", ".join(str(n) for n in segment["chapters"]), minutes=segment_minutes(lecture, segment),
        topic=segment.get("topic") or "-",
        final=(last["text"][:6000] if last and last["n"] not in segment["chapters"]
               else "(фрагмент сам содержит конец лекции)"),
        era=", ".join(america.TAGS_ERA), theme=", ".join(america.TAGS_THEME))


QUOTE_WITH = """Строка [ЦИТАТА] в образце - место, где в пост встанет свернутая цитата. В твоем посте будет вот эта цитата (уже переведена):

<<<
{quote}
>>>

Поставь строку [ЦИТАТА] отдельно, там, где эта прямая речь нужна рассказу. Текст поста не должен пересказывать то, что сказано в цитате: подведи к ней (кто говорит, когда и по какому поводу) и иди дальше, а не повторяй ее своими словами. О цитате как о цитате в тексте не пиши («как видно из цитаты» и т. п.)."""

QUOTE_WITHOUT = """Строка [ЦИТАТА] в образце - место для свернутой цитаты. В этом посте цитаты не будет, строку [ЦИТАТА] не ставь."""


def quote_block(quote_ru):
    return QUOTE_WITH.format(quote=quote_ru) if quote_ru else QUOTE_WITHOUT


def post_prompt(course, lecture_no, lecture, segment, notes, quote_ru=None):
    shown = {k: v for k, v in notes.items() if k in ("point", "why", "background", "story", "lecturer_views")}
    return POST_PROMPT.format(**_common(course, lecture_no, lecture, segment), example=EXAMPLE_POST,
                              notes=json.dumps(shown, ensure_ascii=False, indent=1), quote_block=quote_block(quote_ru))


# ─── Иллюстрация: Wikimedia Commons и Библиотека Конгресса ──────────────────

LOC_API = "https://www.loc.gov/photos/"


def loc_params(query, limit=8):
    return {"q": query, "fo": "json", "c": limit}


def loc_candidates(data):
    """Ответ поиска Библиотеки Конгресса -> кандидаты в том же виде, что с Commons."""
    out = []
    for x in (data or {}).get("results") or []:
        urls = [u for u in x.get("image_url") or [] if re.search(r"\.jpg", u)]
        best, width = None, 0
        for u in urls:
            m = re.search(r"#h=\d+&w=(\d+)", u)
            w = int(m.group(1)) if m else 0
            if w > width:
                best, width = u.split("#")[0], w
        if not best or width < 500:
            continue
        desc = x.get("description") or []
        out.append({"title": str(x.get("title") or "")[:200], "description": _plain(" ".join(desc) if isinstance(desc, list) else desc),
                    "date": str(x.get("date") or "")[:20], "artist": ", ".join(x.get("contributor") or [])[:120],
                    "license": "", "thumb": best, "page": x.get("url"), "site": "Библиотека Конгресса",
                    "usage": 0, "articles": []})
    return out

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_UA = "ShtatnyBot/1.0 (https://github.com/itsyegor/yeletterboxdbot)"  # Wikimedia требует UA


def _plain(html_text, n=300):
    p = _Visible()
    p.feed(str(html_text or ""))
    return re.sub(r"\s+", " ", "".join(p.parts)).strip()[:n]


def commons_params(query, limit=8):
    return {"action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrnamespace": 6,
            "gsrlimit": limit, "prop": "imageinfo|globalusage", "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": 1280, "gulimit": 500, "gunamespace": 0}


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
                    "page": ii.get("descriptionurl"),
                    # в скольких статьях Википедии (на всех языках) стоит картинка: признанное изображение темы
                    "usage": len(page.get("globalusage") or []),
                    "articles": [f"{u.get('wiki', '')}: {u.get('title', '')}" for u in (page.get("globalusage") or [])
                                 if str(u.get("wiki", "")).startswith(("en.", "ru."))][:5]})
    return sorted(out, key=lambda c: -c["usage"])


PICK_PROMPT = """Ниже пост для телеграм-канала об истории США и найденные картинки (Wikimedia Commons и Библиотека Конгресса). Выбери одну в иллюстрацию к посту.

Как выбирать:
1. Картинка относится к предмету поста: прямо или через его героев, место и время. Картинка нужна каждому посту: если прямо связанной нет, возьми ближайшую по эпохе, месту или людям и в подписи скажи, как она связана. choice: -1 - только если ни одна картинка не связана с постом даже так.
2. Ее интересно разглядывать, она сама что-то рассказывает. Вид картинки не важен: фотография, плакат, документ, газетная полоса, карикатура, гравюра, карта, вещь - все на равных. Ниже остальных только живопись на исторические сюжеты и технические схемы сражений: их бери, только если ничего другого подходящего нет.
3. При прочих равных выбирай ту, что чаще используется в статьях Википедии (число «статей»): это признанное изображение темы, а не случайный скан.

caption - подпись по-русски, коротко, до 200 знаков: что на картинке (когда и кем сделано - если это важно) и чем она связана с постом. Связь с постом обязательна, остальное - по необходимости. Только то, что следует из описания картинки и названий статей, ничего не придумывай. Начни с сути, без «На картинке изображено». Букву «ё» не используй.

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
                      f"{c['artist'] or '-'} | статей: {c.get('usage', 0)}"
                      + (f" ({'; '.join(c['articles'])})" if c.get("articles") else "")
                      for i, c in enumerate(candidates))
    return PICK_PROMPT.format(post=_plain(post_text, 3000), items=items)


_FREE = ("public domain", "pd", "cc0", "no restrictions")


def image_credit(c):
    """Для свободных от прав картинок достаточно источника; для CC BY нужно указать автора и лицензию."""
    lic = (c.get("license") or "").lower()
    if not lic or any(lic.startswith(f) for f in _FREE):
        return ""
    return ", ".join(x for x in [c.get("artist"), c.get("license")] if x)


EDIT_PROMPT = """Ты редактор телеграм-канала об истории США. Ниже черновик поста и исходный текст, по которому он написан. Читатель умный, но исходник не читал. Отредактируй черновик.

Проверь каждый абзац:
1. Каждое событие, решение, закон, документ, имя и термин понятны без исходника: что именно произошло или что в нем было, кто участвовал, сколько, чем кончилось. Если в черновике только намек («день позора», «было два правительства»), раскрой его по исходнику в одну-две фразы. Если по исходнику не раскрыть или факт не связан с рассказом, удали его.
2. Каждый факт связан с тем, о чем рассказ; читатель понимает, зачем он здесь. Перечни книг, имен и событий сократи до того, что работает на рассказ.
3. Абзацы короткие: 2-4 предложения, до 500 знаков. Длинные раздели по смыслу. Весь текст поста - не больше 2500 знаков: пост в телеграме ограничен, и туда еще встанут цитата и подпись к картинке.
4. Переходы между абзацами естественные, текст читается как один рассказ.
4а. Никаких штампов нейросетевого текста: определений через отрицание и противопоставлений для эффекта («это был не бред, а образец», «не просто X, а Y», «дело не в X, а в Y», «это не случайность»), а также «стоит отметить», «важно понимать», «не что иное, как», «в этом и заключается», «по сути». Говори прямо, что есть, а не чем это не является. Найденные такие места перепиши прямым утверждением.
4б. Пост - об одной линии (первые абзацы задают ее). Абзацы о других темах удали целиком, даже если они интересны.
4в. Аббревиатуры расшифрованы при первом упоминании; о каждом человеке ясно, кто он; «Вашингтон», «Москва» в значении правительства заменены на «американское правительство» и т. п.; в каждом предложении ясно, кто действует. Проверь каждое предложение: следует ли оно из предыдущего? Если переход надо додумывать - допиши связь или убери предложение.
5. Если ниже дана цитата, текст поста не повторяет ее содержание: то, что пересказано в тексте и сказано в цитате, убери из текста, оставив короткую подводку к цитате.

Ничего не добавляй сверх исходника. Сохрани тон, голос и порядок рассказа, заголовок (первая строка) и строку [ЦИТАТА] на своем месте. Длина может уменьшиться - это нормально. Букву «ё» не используй.

Ответ - только отредактированный пост в том же виде: первая строка - заголовок, дальше текст.

Черновик:
{draft}

Цитата, которая встанет в пост на место строки [ЦИТАТА]:
{quote}

Исходный текст:
{source}"""


def edit_prompt(raw_draft, source_text, quote_ru=None):
    return EDIT_PROMPT.format(draft=str(raw_draft or "").strip(), source=str(source_text or "")[:40000],
                              quote=quote_ru or "(цитаты нет; если в черновике есть строка [ЦИТАТА], убери ее)")


def split_long_paragraphs(text, limit=650):
    """Страховка к правилу о коротких абзацах: слишком длинный абзац делится по концу предложения."""
    out = []
    for p in str(text or "").split("\n\n"):
        while len(p) > limit:
            mid = len(p) // 2
            cuts = [m.end() for m in re.finditer(r"[.!?…»] ", p)]
            if not cuts:
                break
            cut = min(cuts, key=lambda c: abs(c - mid))
            if cut < 150 or len(p) - cut < 150:
                break
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        out.append(p)
    return "\n\n".join(out)


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
    return {"title": title, "before": split_long_paragraphs(before.strip()),
            "after": split_long_paragraphs(after.strip())}


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


def extract_quotes(text, spans, max_len=4500, max_gap=4000):
    """Склеивает дословные куски по якорям. Куски, которые не нашлись, пропускает.
    Берутся только куски рядом с первым (не дальше max_gap знаков): иначе в цитату попадают
    несвязанные места длинного интервью."""
    out, total, first_pos = [], 0, None
    for sp in spans or []:
        if not isinstance(sp, dict):
            continue
        p = extract_quote(text, sp.get("start"), sp.get("end"), max_len=3000, min_len=80)
        if p:
            pos = text.find(p[:60])
            if first_pos is None:
                first_pos = pos
            elif pos >= 0 and abs(pos - first_pos) > max_gap + total:
                continue
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
    """Ровно три тега: рубрика и две самые точные темы (лектор и так виден в шапке)."""
    return ["лекции"] + america.clean_tags(post.get("tags"))[:2]


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


def fit_post(assemble, before, quote, after, limit=3900):
    """Укладывает пост в лимит Telegram (4096 знаков, считаем с запасом).

    assemble(before, quote, after) -> HTML. Порядок: цитата укорачивается до ~800 знаков, потом выпадают
    абзацы из середины рассказа (первый абзац и вывод остаются), и только потом цитата сокращается
    дальше или убирается совсем - текст подводит к ней, поэтому она ценнее среднего абзаца.
    """
    q = quote or ""
    b = [p for p in before.split("\n\n") if p.strip()]
    a = [p for p in after.split("\n\n") if p.strip()]
    text = lambda: assemble("\n\n".join(b), q, "\n\n".join(a))
    over = lambda: visible_len(text()) - limit

    def shrink_quote(floor):
        nonlocal q
        while q and over() > 0 and len(q) > floor:
            q = _trim_quote(q, max(floor, len(q) - over() - 10))
            if len(q) <= floor and over() > 0:
                break

    shrink_quote(800)
    while over() > 0 and len(b) + len(a) > 2:
        if len(b) > 1:
            b.pop()          # конец первой части - середина рассказа
        else:
            a.pop(0)         # начало второй части; вывод в конце остается
    shrink_quote(400)
    if over() > 0:
        q = ""
    while over() > 0:        # крайний случай: укорачиваем последний абзац
        part = a if a else b
        if not part or len(part[-1]) < 200:
            break
        part[-1] = part[-1][: int(len(part[-1]) * 0.9)].rsplit(" ", 1)[0] + "…"
    return text()


def build_post(post, course, lecture_no, lectures_total, lecture, segment, quote_ru=None, limit=4000, image=None):
    """
    Заголовок
    English Lecture Title (ссылка на расшифровку) · Дэвид Блайт · Йель, 2008 · ▶️ видео с 12:34, 15 мин

    Рассказ... [свернутая цитата лектора] ...рассказ

    #лекции #Блайт #теги
    🖼 подпись к картинке · Wikimedia Commons
    (картинка над постом)
    post - результат parse_post, теги - из заметок (post['tags']).
    """
    # шапка: русский заголовок, под ним английское название лекции (ссылка на расшифровку), лектор и видео
    url = video_url(lecture, segment)
    video = (f' · <a href="{_esc(url)}">▶️ видео с {_clock(segment_start(lecture, segment))}, '
             f'{segment_minutes(lecture, segment)} мин</a>' if url else "")
    head = [f"<b>{_esc(america._cut(_clean(post['title']), 200))}</b>",
            f'<i><a href="{course["url"]}/lecture-{lecture_no}">{_esc(lecture.get("title") or course["title"])}</a>'
            f" · {_esc(course['author_full'])} · Йель, {course['year']}{video}</i>", ""]
    tags = " ".join("#" + t for t in post_tags(post, course))

    def assemble(before, q, after):
        lines = head + ([_esc(before)] if before else [])
        if q:
            lines += ["", f"<blockquote expandable>{_esc(q)}</blockquote>"]
        if after:
            lines += ["", _esc(after)]
        if image and image.get("caption"):
            credit = image_credit(image)
            lines += ["", f"🖼 <i>{_esc(_clean(image['caption']))}</i>"
                          + (f" ({_esc(credit)})" if credit else "")
                          + (f' · <a href="{_esc(image["page"])}">{_esc(image.get("site", "Wikimedia Commons"))}</a>' if image.get("page") else "")]
        lines += ["", tags]
        return "\n".join(lines)

    return fit_post(assemble, _clean(post.get("before")), _clean(quote_ru) if quote_ru else "",
                    _clean(post.get("after")), min(limit, 3900))
