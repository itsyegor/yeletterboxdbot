"""
Американистика: серия «Федералист» - одна статья Гамильтона, Мэдисона и Джея на пост, по порядку.

Раз в две недели, в субботу, вместо поста по статье (расписание - bot.todays_slots).
Текст - Project Gutenberg (все 85 статей одним файлом, общественное достояние), ссылка на
подлинник - архив Avalon Йельского юридического факультета. Пост пишется тем же способом,
что посты по статьям (articles.py): заметки -> текст по образцу -> цитата -> иллюстрация.
"""

import re

import articles

TEXT_URL = "https://www.gutenberg.org/cache/epub/1404/pg1404.txt"
TOTAL = 85


def avalon_url(n):
    return f"https://avalon.law.yale.edu/18th_century/fed{n:02d}.asp"


def parse_all(text):
    """Файл Gutenberg -> {номер: {title, author, paper, date, year, text}}. Если номер встречается дважды
    (в сборнике есть вариант № 70), берется первый."""
    parts = re.split(r"\n\s*FEDERALIST No\. (\d+)\s*\n", (text or "").replace("\r\n", "\n"))
    out = {}
    for i in range(1, len(parts) - 1, 2):
        n, body = int(parts[i]), parts[i + 1]
        if n in out:
            continue
        end = body.find("*** END OF")
        body = body[:end] if end > 0 else body
        start = body.find("To the People of the State of New York:")
        head = " ".join((body[:start] if start >= 0 else body[:600]).split())
        m = re.search(r"(?:For|From) (?:the )?(.+?)\.\s+([A-Za-z]+, [A-Za-z]+ \d{1,2}, (17\d\d))", head)
        title = head[: m.start()].strip() if m else head
        a = re.search(r"\b(HAMILTON|MADISON|JAY)(?:,? (?:with|and) (HAMILTON|MADISON))?\s*$", head, re.I)
        title = re.sub(r"\s*\b(HAMILTON|MADISON|JAY)\b.*$", "", title).strip()
        prose = body[start + len("To the People of the State of New York:"):] if start >= 0 else body
        paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", prose)]
        out[n] = {
            "title": title, "author": (a.group(0).strip().upper() if a else ""),
            "paper": m.group(1).strip() if m else "", "date": m.group(2) if m else "",
            "year": int(m.group(3)) if m else 1788,
            "text": "\n\n".join(p for p in paras if p and not p.startswith("PUBLIUS")),
        }
    return out


AUTHORS_RU = {"HAMILTON": "Александр Гамильтон", "MADISON": "Джеймс Мэдисон", "JAY": "Джон Джей"}


def author_ru(raw):
    names = [AUTHORS_RU[w] for w in re.findall(r"HAMILTON|MADISON|JAY", raw or "")]
    return " и ".join(names) or "Публий"


def item(n, paper):
    """Статья «Федералиста» в виде материала для конвейера статей (articles.py)."""
    return {"source": "federalist", "url": avalon_url(n), "fragment": f"federalist/{n}",
            "title": f"Federalist No. {n}: {paper['title']}", "author": author_ru(paper["author"]),
            "year": paper["year"], "categories": [], "number": n,
            "text": (f"{paper['paper']}, {paper['date']}.\n\n" if paper["paper"] else "") + paper["text"]}


articles.SOURCES["federalist"] = {
    "name": "«Федералист»", "short": "",  # в шапке и так «Federalist No. N»
    "tag": "Федералист", "rubric": "Федералист",
    "kind": "series", "kind_ru": "статья из «Федералиста»",
    "about": "85 статей Александра Гамильтона, Джеймса Мэдисона и Джона Джея под общим псевдонимом «Публий», "
             "напечатанных в нью-йоркских газетах в 1787-1788 годах в защиту проекта Конституции",
    "scope": "Это первоисточник, его не пропускай. Здесь, в отличие от других материалов, можно опереться на "
             "общеизвестный контекст, которого нет в самом тексте: спор о ратификации Конституции в Нью-Йорке, "
             "с кем полемизирует автор, и, если статья знаменита, как на нее потом ссылались (например, "
             "Верховный суд). Только твердо установленные факты, без догадок.",
}
