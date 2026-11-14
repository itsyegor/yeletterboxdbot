"""
Американистика: рубрика «картинка» - короткий пост по одной картинке.

По будням это второй, легкий пост дня рядом с одним длинным (лекция или статья).
1. Тема: случайная эпоха и случайная тема из словаря тегов (america.TAGS_ERA / TAGS_THEME),
   не повторяя последние; Claude предлагает конкретные предметы для поиска.
2. Поиск на Wikimedia Commons; выше те, что стоят во многих статьях Википедии.
3. Claude выбирает самую интересную для отдельного поста и пишет текст по описанию картинки
   и по статье английской Википедии, где она используется.
Сеть, память и отправка - в bot.py (check_pictures).
"""

import random
import re

import america
import lectures

WIKI_API = "https://en.wikipedia.org/w/api.php"


def wiki_params(title):
    return {"action": "query", "format": "json", "prop": "extracts", "explaintext": 1, "redirects": 1,
            "titles": title.replace("_", " ")}


def wiki_text(data, limit=8000):
    for page in ((data or {}).get("query") or {}).get("pages", {}).values():
        if page.get("extract"):
            return page["extract"][:limit]
    return ""


def en_titles(cand):
    return [a.split(": ", 1)[1] for a in cand.get("articles") or [] if a.startswith("en.") and ": " in a]


def pick_theme(recent):
    """Эпоха и тема, которых не было в последних постах рубрики."""
    recent = set(recent or [])
    eras = [e for e in america.TAGS_ERA if e not in recent] or list(america.TAGS_ERA)
    themes = [t for t in america.TAGS_THEME if t not in recent] or list(america.TAGS_THEME)
    return random.choice(eras), random.choice(themes)


IDEAS_PROMPT = """Ты подбираешь картинку для короткого поста в телеграм-канале об истории США. Эпоха: {era}. Тема: {theme} (тему можно понимать широко; если она не вяжется с эпохой, возьми любую близкую).

Предложи 5 конкретных предметов, у которых, скорее всего, есть известное изображение на Wikimedia Commons и которые интересно разглядывать: фотография, плакат, газетная полоса, документ, карикатура, карта, вещь, реклама, вывеска - что угодно, кроме живописи на исторические сюжеты и схем сражений. Лучше неожиданное и конкретное, чем хрестоматийное и общее. Только до 2014 года.

Не повторяй уже бывшее в канале: {recent}

queries - 5 поисковых запросов по-английски для Commons, коротких и конкретных («Woolworth lunch counter sit-in 1960», «Sears catalog 1897», «Route 66 sign»).

Ответ - через инструмент answer."""

IDEAS_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


def ideas_prompt(era, theme, recent_titles):
    return IDEAS_PROMPT.format(era=era, theme=theme, recent="; ".join(recent_titles[-30:]) or "пока ничего")


CHOOSE_PROMPT = """Ниже картинки с Wikimedia Commons. Выбери одну для отдельного короткого поста в канале об истории США: ее должно быть интересно разглядывать саму по себе, и о ней должно быть что рассказать. Вид картинки не важен; ниже остальных только живопись на исторические сюжеты и технические схемы сражений. При прочих равных бери ту, что стоит в большем числе статей Википедии. Ничего позже 2014 года. Если ни одна не годится, ответь choice: -1.

tags - 1-3 тега к выбранной картинке, только прямо к ней относящиеся. Эпохи: {era}. Темы: {theme}.

Ответ - через инструмент answer.

{items}"""

CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["choice"],
}


def choose_prompt(cands):
    items = "\n".join(f"{i}. {c['title']} | {c['description'] or '-'} | дата: {c['date'] or '-'} | статей: "
                      f"{c.get('usage', 0)} ({'; '.join(c.get('articles') or []) or '-'})"
                      for i, c in enumerate(cands))
    return CHOOSE_PROMPT.format(items=items, era=", ".join(america.TAGS_ERA), theme=", ".join(america.TAGS_THEME))


WRITE_PROMPT = """Напиши короткий пост к картинке для телеграм-канала об истории США. Читатель умный, но об этом предмете ничего не знает. Пост читают с телефона в перерыве, поэтому коротко: 400-900 знаков.

Что в нем должно быть: что это за картинка (что изображено, когда, кем и зачем сделано), в какой момент истории она появилась и что на ней стоит заметить - деталь, которую без подсказки не увидишь, или история, которая за ней стоит. Одна-две такие вещи, а не пересказ всей статьи. Рассказывай связно, своими словами, полными предложениями, без оценок вроде «удивительно» и без риторических вопросов. Букву «ё» не используй.

Только то, что есть в описании картинки и в статье ниже, ничего не добавляй от себя.

Первая строка ответа - заголовок (3-7 слов, без кавычек и точки), дальше текст поста. Ничего больше.

Картинка: {title}
Описание с Commons: {description}
Дата: {date}. Автор: {artist}.

Статья Википедии «{article}», где стоит эта картинка:
{text}"""


def write_prompt(cand, article, text):
    return WRITE_PROMPT.format(title=cand["title"], description=cand["description"] or "-", date=cand["date"] or "-",
                               artist=cand["artist"] or "-", article=article or "-", text=text or "(статьи нет)")


def parse_post(raw):
    lines = [l.strip() for l in str(raw or "").strip().splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if len(lines) < 2:
        return None
    title = lines[0].strip("*#«»\"' ").rstrip(".")
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines[1:]).strip())
    return {"title": title, "body": body} if len(body) >= 200 else None


def build_post(post, cand, tags):
    esc, clean = lectures._esc, lectures._clean
    credit = lectures.image_credit(cand)
    tags = " ".join("#" + t for t in ["картинки"] + america.clean_tags(tags))
    lines = ["🖼 <b>КАРТИНКА</b>", "", f"<b>{esc(clean(post['title']))}</b>", "", esc(clean(post["body"])), "",
             (f"<i>{esc(credit)}</i> · " if credit else "")
             + (f'<a href="{esc(cand["page"])}">Wikimedia Commons</a>' if cand.get("page") else ""),
             "", tags]
    return "\n".join(lines)
