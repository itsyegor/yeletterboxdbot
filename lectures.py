"""
Американистика: конспекты лекций Open Yale Courses (лицензия CC BY-NC-SA).

Единица поста - глава лекции (в расшифровках Йеля лекция разбита на главы с таймкодом).
Бот берет случайную неиспользованную главу, по очереди из разных курсов, и просит Claude
написать по ней пост. Сеть, память и отправка - в bot.py (check_lectures).
"""
import re
import json
import random
import html as htmlmod

from bs4 import BeautifulSoup

import america

COURSES = {
    "hist-119": {
        "url": "https://oyc.yale.edu/history/hist-119",
        "author": "Д. Блайт", "author_full": "Дэвид Блайт", "author_gen": "Дэвида Блайта", "tag": "Блайт",
        "title": "Гражданская война и Реконструкция, 1845–1877", "year": 2008,
    },
    "hist-116": {
        "url": "https://oyc.yale.edu/history/hist-116",
        "author": "Дж. Фриман", "author_full": "Джоанн Фриман", "author_gen": "Джоанн Фриман", "tag": "Фриман",
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


def fragment_id(course_id, lecture, chapter):
    return f"{course_id}/{lecture}/{chapter}"


def pick(course_ids, lectures_by_course, exhausted, last_course=None):
    """Случайная лекция, в которой еще есть свободные главы. Курсы чередуются.

    -> (курс, номер лекции) или None. Главу выбирает choose_chapter после загрузки лекции.
    """
    order = sorted(course_ids, key=lambda c: c == last_course)  # прошлый курс в конец
    for c in order:
        free = [n for n in lectures_by_course.get(c, []) if f"{c}/{n}" not in exhausted]
        if free:
            return c, random.choice(free)
    return None


def choose_chapter(course_id, lecture_no, lecture, used):
    free = [ch for ch in lecture["chapters"]
            if fragment_id(course_id, lecture_no, ch["n"]) not in used and len(ch["text"]) > 1500]
    return random.choice(free) if free else None


PROMPT = """Ты пишешь пост для телеграм-канала об американистике. Читатель - образованный человек, сам интересуется историей США. Тон академический, как у хорошего университетского конспекта или архивного канала, но живой.

Материал: фрагмент лекции {author_gen}, курс «{course}» (Йельский университет, {year}), лекция {lecture_no} «{lecture_title}», глава «{chapter_title}». Ниже расшифровка этой главы на английском.

Напиши пост на русском:
1. Заголовок: 3-8 слов, по сути фрагмента. Не вопрос и не кликбейт.
2. Текст: 2-4 абзаца, всего 900-1600 знаков. Первая фраза - конкретный факт: дата, имя, событие, цифра, а не общее рассуждение. Пересказывай мысль лектора и его аргументы; где уместно, указывай, что это его трактовка (например, «по словам {author_full}» или «{author_full} обращает внимание»; можно по фамилии), но не начинай каждый абзац с имени лектора. Только то, что есть во фрагменте, ничего не добавляй от себя. Без вводных вроде «В этой лекции», «Интересно, что», без выводов-морали в конце, без оценочных слов, без восклицаний. Букву «ё» не используй.
3. Цитата: выбери из расшифровки один выразительный отрывок, 2-6 предложений, который держит главную мысль фрагмента. Дай его дословно по-английски (quote_en, копируй точно) и в своем переводе на русский (quote_ru).
4. Теги: 2-4 из списка, только прямо следующие из фрагмента.
Эпохи: {era}
Темы: {theme}

Если фрагмент организационный (о курсе, экзаменах, книгах для чтения) и из него не выходит содержательного поста, ответь {{"skip": true}}.

Ответ - только JSON:
{{"title": "...", "body": "абзацы через пустую строку", "quote_en": "...", "quote_ru": "...", "tags": ["..."]}}

Расшифровка:
{text}"""


def build_prompt(course, lecture_no, lecture, chapter):
    return PROMPT.format(
        author_gen=course["author_gen"], author_full=course["author_full"], course=course["title"], year=course["year"],
        lecture_no=lecture_no, lecture_title=lecture["title"], chapter_title=chapter["title"],
        era=", ".join(america.TAGS_ERA), theme=", ".join(america.TAGS_THEME),
        text=chapter["text"][:20000])


def parse_answer(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if data.get("skip"):
        return {"skip": True}
    if not data.get("title") or not data.get("body"):
        return None
    return data


def quote_ok(post, chapter):
    q = post.get("quote_en") or ""
    return bool(q) and bool(post.get("quote_ru")) and america._norm(q) in america._norm(chapter["text"])


def post_tags(post, course):
    return ["конспект", course["tag"]] + america.clean_tags(post.get("tags"))


def _esc(s):
    return htmlmod.escape(str(s or ""), quote=False)


def build_post(post, course, lecture_no, lecture, chapter):
    """
    Заголовок
    Д. Блайт. Гражданская война и Реконструкция, 1845–1877. Йель, 2008. Лекция 5, гл. 3
    Текст
    Свернутая цитата (перевод)
    Смотреть фрагмент · Расшифровка
    #конспект #Блайт #теги
    """
    body = str(post["body"]).replace("ё", "е").replace("Ё", "Е").strip()
    lines = [f"<b>{_esc(america._cut(post['title'], 200))}</b>",
             f"<i>{_esc(course['author'])}. {_esc(course['title'])}. Йель, {course['year']}. "
             f"Лекция {lecture_no}, гл. {chapter['n']}</i>",
             "",
             _esc(america._cut(body, 2600))]
    if quote_ok(post, chapter):
        lines += ["", f"<blockquote expandable>{_esc(america._cut(post['quote_ru'], 900))}</blockquote>"]
    links = []
    if lecture.get("youtube"):
        links.append(f'<a href="https://www.youtube.com/watch?v={lecture["youtube"]}&amp;t={chapter["start"]}s">'
                     f'Смотреть фрагмент</a>')
    links.append(f'<a href="{course["url"]}/lecture-{lecture_no}">Расшифровка</a>')
    lines += ["", " · ".join(links), " ".join("#" + t for t in post_tags(post, course))]
    return "\n".join(lines)
