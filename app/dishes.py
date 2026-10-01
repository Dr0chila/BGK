"""Шпаргалка по блюдам: данные из webapp/dishes.json (собирается tools/build_dishes.py).

Поиск такой же, как в мини-аппе: каждое слово запроса должно совпасть с началом
слова в названии или синонимах. Окончания срезаем грубо («аджарский» ~ «по-аджарски»).
"""
import difflib
import json
import re
from html import escape
from pathlib import Path

DATA = json.loads((Path(__file__).resolve().parent.parent / "webapp" / "dishes.json").read_text("utf-8"))


def norm(s: str) -> list[str]:
    return re.sub(r"[^0-9a-zа-я]+", " ", s.lower().replace("ё", "е")).split()


def stem(t: str) -> str:
    return t[: max(3, len(t) - 2)] if len(t) > 4 else t


_HAY = [(d, norm(" ".join([d["name"], *d.get("alias", [])])), norm(d["name"])) for d in DATA]


def search(q: str, limit: int = 8) -> list[dict]:
    qt = norm(q)
    if not qt:
        return []
    found = []
    for d, hay, name in _HAY:
        if all(any(h.startswith(stem(t)) for h in hay) for t in qt):
            # точное совпадение > название начинается с запроса > больше слов совпало в названии
            score = 100 * (name == qt) + 30 * name[0].startswith(stem(qt[0])) + 10 * sum(any(h.startswith(stem(t)) for h in name) for t in qt) - len(name)
            found.append((score, d))
    found.sort(key=lambda x: -x[0])
    return [d for _, d in found[:limit]]


def similar(q: str, limit: int = 5) -> list[str]:
    names = {d["name"].lower().replace("ё", "е"): d["name"] for d in DATA}
    hits = difflib.get_close_matches(q.lower().replace("ё", "е"), list(names), n=limit, cutoff=0.45)
    return [names[h] for h in hits]


def card(d: dict) -> str:
    lines = [f"<b>{escape(d['name'])}</b> · {escape(d['w'])} · {escape(d['price'])}",
             f"<i>{escape(d['sec'])}</i>", "", escape(d["what"])]
    if d.get("ing") and d["ing"] != d["what"]:
        lines.append(f"\n<b>Состав:</b> {escape(d['ing'])}")
    if d.get("serve"):
        lines.append(f"<b>Подача:</b> {escape(d['serve'])}")
    if d.get("tags"):
        lines.append("\n🏷 " + escape(" · ".join(d["tags"])))
    if d.get("note"):
        lines.append(f"\n⚠️ {escape(d['note'])}")
    return "\n".join(lines)
