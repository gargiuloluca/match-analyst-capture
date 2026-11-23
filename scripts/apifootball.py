"""
apifootball.py — Client API-Football (v3, abbonamento diretto) + utilita' condivise dal motore H

- Chiave dal secret APIFOOTBALL_KEY (header x-apisports-key). Senza chiave tutti gli script H escono senza errori.
- get(): ritorna la lista 'response' oppure None; legge la quota residua dagli header e segnala gli errori del piano.
- LEAGUES: campionati di The Odds API (sport_key) -> id campionato di API-Football. Coppe/Champions escluse dal modello
  (le squadre arrivano da campionati diversi e i rating non sono confrontabili).
- match_name(): collega i nomi delle squadre di The Odds API a quelli di API-Football (normalizzazione + alias + somiglianza).
"""

import os
import re
import time
import unicodedata
import requests
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://v3.football.api-sports.io"
KEY = os.environ.get("APIFOOTBALL_KEY", "").strip()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# sport_key (The Odds API) -> (id API-Football, campionato a anno solare?)
LEAGUES = {
    "soccer_epl": (39, False), "soccer_efl_champ": (40, False),
    "soccer_england_league1": (41, False), "soccer_england_league2": (42, False),
    "soccer_france_ligue_one": (61, False), "soccer_germany_bundesliga": (78, False),
    "soccer_italy_serie_a": (135, False), "soccer_italy_serie_b": (136, False),
    "soccer_spain_la_liga": (140, False), "soccer_spain_segunda_division": (141, False),
    "soccer_netherlands_eredivisie": (88, False), "soccer_portugal_primeira_liga": (94, False),
    "soccer_belgium_first_div": (144, False), "soccer_denmark_superliga": (119, False),
    "soccer_austria_bundesliga": (218, False), "soccer_poland_ekstraklasa": (106, False),
    "soccer_sweden_allsvenskan": (113, True), "soccer_norway_eliteserien": (103, True),
    "soccer_argentina_primera_division": (128, True), "soccer_usa_mls": (253, True),
}
ID_TO_SPORT = {v[0]: k for k, v in LEAGUES.items()}

_state = {"remaining": None, "calls": 0}


def season_for(sport_key: str, when: datetime) -> int:
    cal = LEAGUES[sport_key][1]
    return when.year if (cal or when.month >= 7) else when.year - 1


def get(path: str, params: dict, retries: int = 2):
    """GET su API-Football. Ritorna la lista 'response' o None."""
    if not KEY:
        return None
    for attempt in range(retries + 1):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, headers={"x-apisports-key": KEY}, timeout=25)
            _state["calls"] += 1
            rem = r.headers.get("x-ratelimit-requests-remaining")
            if rem is not None:
                _state["remaining"] = rem
            if r.status_code == 429:
                time.sleep(3 + 3 * attempt)
                continue
            if r.status_code != 200:
                print(f"  [AF {path}] HTTP {r.status_code}")
                return None
            body = r.json()
            errs = body.get("errors")
            if errs:   # puo' essere lista o dizionario; vuoto = nessun errore
                print(f"  [AF {path}] errori: {errs}")
                return None
            time.sleep(0.2)
            return body.get("response", [])
        except Exception as e:
            print(f"  [AF {path}] connessione: {e}")
            time.sleep(1.5)
    return None


def status_line() -> str:
    return f"{_state['calls']} chiamate, quota residua oggi: {_state['remaining']}"


# ---------- nomi squadre ----------
_DROP = {"fc", "cf", "ac", "as", "sc", "afc", "ssc", "ud", "cd", "rcd", "sv", "fk", "bk", "if", "sk", "calcio", "club", "the"}
ALIASES = {
    "inter": "internazionale", "inter milan": "internazionale", "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers", "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "man united": "manchester united", "man city": "manchester city", "newcastle": "newcastle united",
    "brighton and hove albion": "brighton", "west ham": "west ham united", "leeds": "leeds united",
    "nottm forest": "nottingham forest", "athletic bilbao": "athletic club", "real betis": "betis",
    "borussia monchengladbach": "monchengladbach", "bayer leverkusen": "leverkusen",
    "paris saint germain": "paris saint germain", "psg": "paris saint germain",
    "rb leipzig": "rb leipzig", "leipzig": "rb leipzig", "atletico madrid": "atletico madrid",
    "club brugge": "club brugge", "sporting lisbon": "sporting cp", "sporting": "sporting cp",
    "fc porto": "porto", "sl benfica": "benfica", "psv eindhoven": "psv", "ajax amsterdam": "ajax",
}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if t not in _DROP]
    s = " ".join(toks)
    return ALIASES.get(s, s)


def name_score(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    sc = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        sc = max(sc, len(ta & tb) / max(len(ta), len(tb)))
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        sc = max(sc, 0.9)
    return sc


def match_fixture(odds_fix: dict, af_fixtures: list, max_hours: float = 30.0):
    """
    Collega una partita di The Odds API alla stessa di API-Football (stesso campionato, orario vicino, nomi simili).
    Ritorna (fixture_af, punteggio) oppure (None, punteggio_migliore).
    """
    try:
        ko = datetime.fromisoformat(str(odds_fix.get("commence_time")).replace("Z", "+00:00"))
    except Exception:
        return None, 0.0
    lg = LEAGUES.get(odds_fix.get("sport_key"), (None,))[0]
    best, best_sc = None, 0.0
    for f in af_fixtures:
        if lg is not None and f.get("league_id") != lg:
            continue
        try:
            fko = datetime.fromtimestamp(int(f["ts"]), tz=timezone.utc)
        except Exception:
            continue
        if abs((fko - ko).total_seconds()) > max_hours * 3600:
            continue
        sh = name_score(odds_fix.get("home_team", ""), f.get("home", ""))
        sa = name_score(odds_fix.get("away_team", ""), f.get("away", ""))
        sc = min(sh, sa) * 0.5 + (sh + sa) / 2 * 0.5
        if sc > best_sc:
            best, best_sc = f, sc
    if best and best_sc >= 0.75:
        return best, best_sc
    return None, best_sc
