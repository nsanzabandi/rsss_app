"""
core/village_matcher.py — Match the free-text home address recorded in
eTracker (district code, sector, village) to Rwanda's official villages.

Reference: config/rwanda_villages.csv — 14,815 villages with their official
codes and parent cell / sector / district / province, from the World Bank
"Rwanda Admin Boundaries and Villages" dataset (2012 boundaries, CC BY 4.0,
https://datacatalog.worldbank.org/search/dataset/0041453). Replace the file
with an updated official list (same columns) when RBC/NISR provides one.

eTracker's own text is never changed: results go to the village_match lookup
table (tools/match_villages.py), and reports/downloads join to it.

Matching is deliberately conservative — a wrong village sends a health
worker to the wrong place, which is worse than "not verified":
  verified            exact name (ignoring case, punctuation and the
                      Kinyarwanda leading vowel: Bukorikori = Ubukorikori)
                      in the recorded sector — or, when the sector is
                      missing/unknown, a name that exists only once in the
                      district
  verified_fuzzy      a close spelling (≥ 90% similar) in the recorded sector
  verified_cell_unknown  the name exists in two or more cells of that sector;
                      village spelling is fixed, the cell can't be told
  check               a possible match (80–90% similar), or a name found in
                      several places in the district — listed for review,
                      not applied
  not_found           no plausible match — the recorded text is kept
  missing             no village recorded
Manual decisions in config/village_overrides.csv win over all of the above.
"""
from __future__ import annotations

import csv
import difflib
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

_CONFIG = Path(__file__).resolve().parent.parent / "config"
REF_PATH = _CONFIG / "rwanda_villages.csv"
OVERRIDES_PATH = _CONFIG / "village_overrides.csv"

FUZZY_APPLY = 0.91      # at or above → applied (verified_fuzzy)
FUZZY_SUGGEST = 0.80    # between this and FUZZY_APPLY → suggested for review
SECTOR_FUZZY = 0.85

_PREFIX = re.compile(r"^(umudugudu|mudugudu|umud|vill(age)?|v)\s+(wa\s+)?")


def norm(text) -> str:
    """Lowercase ASCII, punctuation → space, 'umudugudu wa' prefix dropped."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = _PREFIX.sub("", s)
    return re.sub(r"\s+", " ", s)


_ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi", "7": "vii",
          "8": "viii", "9": "ix", "10": "x"}
_DIGIT_SUFFIX = re.compile(r"^(.*[a-z])\s*(\d{1,2})$")                     # Rwimikoni2, Amarembo 2
_WORD_SUFFIX  = re.compile(r"^(.*[a-z])\s+([il]{1,3}|iv|vi{0,3}|ix|x|a|b)$")  # Amarembo II, Kabongoya lI, Nyagatovu A


def _split_number(s: str) -> tuple[str, str]:
    """'amarembo ii' / 'amarembo 2' / 'kabongoya li' → ('amarembo', 'ii').
    Numbered villages (Amarembo I / II) are different places, so the number
    is compared separately and must be equal. Letters and roman numerals
    count only after a space, so 'Gihanga' or 'Musenyi' keep their last letter."""
    m = _DIGIT_SUFFIX.match(s)
    if m and len(m.group(1).replace(" ", "")) >= 3:
        n = str(int(m.group(2)))
        return m.group(1).strip(), _ROMAN.get(n, n)
    m = _WORD_SUFFIX.match(s)
    if m and len(m.group(1).replace(" ", "")) >= 3:
        num = m.group(2)
        if set(num) <= {"i", "l"}:
            num = num.replace("l", "i")
        return m.group(1).strip(), num
    return s, ""


def key(text) -> str:
    """norm() without spaces and without the Kinyarwanda augment (initial
    vowel before a consonant): 'Ubu korikori', 'bukorikori' → 'bukorikori'.
    A trailing village number is kept as '#<roman>': 'Amarembo 2' →
    'marembo#ii'."""
    base, num = _split_number(norm(text))
    s = base.replace(" ", "")
    if len(s) > 3 and s[0] in "aiu" and s[1] not in "aeiou":
        s = s[1:]
    return s + (f"#{num}" if num else "")


_SECTOR_NOISE = re.compile(r"\b(umurenge|sector|secteur|district|akarere|cs|hc|mhc|ps|dh|"
                           r"centre de sante|health cent(er|re)|hospital)\b")


def sector_key(text) -> str:
    """key() for a recorded sector, without the extras people add:
    'Murundi (Kayonza District)', 'Kintobo CS', 'Rutare (gicumbi) MHC' → the sector."""
    s = re.sub(r"\(.*?\)", " ", str(text or ""))
    return key(_SECTOR_NOISE.sub(" ", norm(s)))


class Reference:
    def __init__(self, path: Path = REF_PATH):
        self.villages: dict[str, dict] = {}
        self.sector_by_district: dict[str, dict[str, str]] = defaultdict(dict)   # d → key → sector_id
        self.sector_districts: dict[str, set] = defaultdict(set)                 # key → {d}
        self.in_sector: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.in_district: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.villages[r["village_id"]] = r
                d, sk, vk = r["district_id"], key(r["sector"]), key(r["village"])
                self.sector_by_district[d][sk] = r["sector_id"]
                self.sector_districts[sk].add(d)
                self.in_sector[r["sector_id"]][vk].append(r["village_id"])
                self.in_district[d][vk].append(r["village_id"])


@lru_cache(maxsize=1)
def reference() -> Reference:
    return Reference()


def _close(name: str, candidates) -> tuple[str | None, float]:
    """Closest spelling among candidates with the SAME village number (or
    none) — Amarembo II is never 'close to' Amarembo I."""
    num = name.partition("#")[2]
    pool = [c for c in candidates if c.partition("#")[2] == num]
    best, score = None, 0.0
    for c in difflib.get_close_matches(name, pool, n=1, cutoff=FUZZY_SUGGEST):
        best, score = c, difflib.SequenceMatcher(None, name, c).ratio()
    return best, score


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[tuple, str]:
    """(district_raw, sector_raw, village_raw) → village_id from a reviewed CSV
    (columns: district_raw, sector_raw, village_raw, village_id). A blank
    village_id marks the address as checked and not a real village."""
    out = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["district_raw"].strip(), r["sector_raw"].strip(), r["village_raw"].strip())] = \
                    r.get("village_id", "").strip()
    return out


def match(district_raw, sector_raw, village_raw, overrides: dict | None = None) -> dict:
    """Best official village for one recorded address. Returns
    {status, village_id, candidates, score}; village_id only when applied."""
    ref = reference()
    d_raw, s_raw, v_raw = (str(x or "").strip() for x in (district_raw, sector_raw, village_raw))
    if overrides and (d_raw, s_raw, v_raw) in overrides:
        vid = overrides[(d_raw, s_raw, v_raw)]
        return dict(status="override" if vid in ref.villages else "not_found",
                    village_id=vid if vid in ref.villages else None, candidates=0, score=1.0)
    vk = key(v_raw)
    if not vk or vk.isdigit():
        return dict(status="missing", village_id=None, candidates=0, score=0.0)

    # District: the recorded code, else a sector name that exists in only one district.
    sk = sector_key(s_raw)
    d = d_raw if d_raw in ref.sector_by_district else None
    if d is None and sk and len(ref.sector_districts.get(sk, ())) == 1:
        d = next(iter(ref.sector_districts[sk]))
    if d is None:
        return dict(status="not_found", village_id=None, candidates=0, score=0.0)

    # Sector within the district (exact, else close spelling).
    sid = None
    if sk:
        sid = ref.sector_by_district[d].get(sk)
        if sid is None:
            best, score = _close(sk, ref.sector_by_district[d])
            if best and score >= SECTOR_FUZZY:
                sid = ref.sector_by_district[d][best]

    if sid:
        names = ref.in_sector[sid]
        hit, score = (vk, 1.0) if vk in names else _close(vk, names)
        if hit and score >= FUZZY_APPLY:
            ids = names[hit]
            if len(ids) > 1:
                return dict(status="verified_cell_unknown", village_id=ids[0],
                            candidates=len(ids), score=score)
            return dict(status="verified" if score == 1.0 else "verified_fuzzy",
                        village_id=ids[0], candidates=1, score=score)
        if hit:
            return dict(status="check", village_id=None, candidates=len(names[hit]), score=score)

    # Sector missing/unknown (or no village of that name there): the district.
    names = ref.in_district[d]
    if vk in names:
        ids = names[vk]
        if len(ids) == 1 and not sid:
            return dict(status="verified", village_id=ids[0], candidates=1, score=1.0)
        return dict(status="check", village_id=None, candidates=len(ids), score=1.0)
    best, score = _close(vk, names)
    if best:
        return dict(status="check", village_id=None, candidates=len(names[best]), score=score)
    return dict(status="not_found", village_id=None, candidates=0, score=0.0)


def suggestion(district_raw, sector_raw, village_raw) -> str:
    """Human-readable best guesses for the review list ('Village, Cell, Sector')."""
    ref = reference()
    d = str(district_raw or "").strip()
    vk = key(village_raw)
    pool = ref.in_district.get(d, {})
    out = []
    num = vk.partition("#")[2]
    same_num = [c for c in pool if c.partition("#")[2] == num]
    for name in difflib.get_close_matches(vk, same_num, n=3, cutoff=FUZZY_SUGGEST):
        for vid in pool[name][:3]:
            r = ref.villages[vid]
            out.append(f"{r['village']} ({r['cell']} cell, {r['sector']}) [{vid}]")
    return "; ".join(out[:4])
