"""The same flat seen twice: on two portals, reposted, or matched by two searches.

Matching is by title -- landlords copy the same text everywhere -- but a housing
company reuses one title for many units ("Wohnung in Berlin" x 18), so rooms and
m² must agree too. Identical units in one building (same title, size, rent)
collapse into one card on purpose: to the user they are the same offer.
"""
from __future__ import annotations

import re

AREA_SLACK = 1.5            # m²; portals round differently
# which copy the card shows: exact rents first, then the richer exposé
_RANK = {"inberlinwohnen": 0, "immoscout24": 1, "kleinanzeigen": 2}


def title_key(title: str | None) -> str:
    return re.sub(r"[\W_]+", " ", (title or "").lower()).strip()


def _fits(a: dict, b: dict) -> bool:
    if a["rooms"] is not None and b["rooms"] is not None and a["rooms"] != b["rooms"]:
        return False
    if a["area"] is not None and b["area"] is not None and abs(a["area"] - b["area"]) > AREA_SLACK:
        return False
    return True


def group(rows: list[dict]) -> list[list[dict]]:
    """Clusters in first-seen order of the input; a row with no usable title stays alone."""
    out: list[list[dict]] = []
    by_title: dict[str, list[list[dict]]] = {}
    for r in rows:
        k = title_key(r.get("title"))
        if not k:
            out.append([r])
            continue
        for g in by_title.setdefault(k, []):
            if _fits(g[0], r):
                g.append(r)
                break
        else:
            g = [r]
            by_title[k].append(g)
            out.append(g)
    return out


def pick(g: list[dict], keep=lambda r: True) -> dict:
    """The row the card shows; the other distinct flats ride along as `dupes`."""
    cands = [r for r in g if keep(r)] or g
    best = min(cands, key=lambda r: (_RANK.get(r["source"], 9),
                                     -(r.get("posted_at") or r.get("first_seen_at") or 0)))
    seen = {(best["source"], best["source_id"])}
    dupes = []
    for r in g:
        if (r["source"], r["source_id"]) not in seen:
            seen.add((r["source"], r["source_id"]))
            dupes.append({"source": r["source"], "source_id": r["source_id"], "url": r["url"]})
    return {**best, "dupes": dupes}
