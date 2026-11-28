"""
closing_capture.py — Registratore delle QUOTE DI CHIUSURA (Pinnacle senza margine)

Gira ogni 30 minuti (workflow closing-capture.yml). Per ogni campionato con partite che iniziano tra ~5 e ~45 minuti:
  1. scarica SOLO Pinnacle (bookmakers=pinnacle, mercati h2h+totals = 2 crediti per campionato; zero chiamate se non c'e' nulla)
  2. calcola la probabilita vera (metodo power) e la quota di chiusura "giusta" = 1 / probabilita
  3. la scrive in closing_odd di OGNI giocata in attesa di TUTTI i tracker (settle_results.py calcola poi il CLV a partita finita)
  4. archivia la chiusura di ogni partita in data/closing_log.jsonl (serve per provare i modelli sul passato)
Il campionato di una partita si ricava da data/fixtures.json (stesso id evento). Gli eventi "events" dell'API non costano crediti.
"""

import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

from sharp_lib import devig_power, GROUPS
from oddskeys import shuffled_keys, mark_dead

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FIXTURES_FILE = DATA_DIR / "fixtures.json"
LOG_FILE = DATA_DIR / "closing_log.jsonl"
TRACKERS = ["tracker.json", "tracker_xg.json", "tracker_safe.json", "tracker_fund.json",
            "tracker_master.json", "tracker_safe_goals.json", "tracker_sharp.json", "tracker_h.json", "tracker_apex.json"]

ODDSAPI_KEY_RAW = os.environ.get("ODDSAPI_KEY", "").strip()
API_KEYS = [k.strip() for k in ODDSAPI_KEY_RAW.split(",") if k.strip()]
BASE = "https://api.the-odds-api.com/v4/sports"

WINDOW_MIN, WINDOW_MAX = 5, 45     # minuti al calcio d'inizio
LEAGUES = [
    "soccer_italy_serie_a", "soccer_spain_la_liga", "soccer_epl", "soccer_germany_bundesliga", "soccer_france_ligue_one",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league", "soccer_efl_champ", "soccer_italy_serie_b",
    "soccer_spain_segunda_division", "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_belgium_first_div", "soccer_denmark_superliga", "soccer_sweden_allsvenskan", "soccer_norway_eliteserien",
    "soccer_austria_bundesliga", "soccer_england_league1", "soccer_england_league2", "soccer_england_efl_cup",
    "soccer_poland_ekstraklasa", "soccer_argentina_primera_division", "soccer_usa_mls",
]


def parse_dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def api_get(path, params):
    """GET con rotazione delle chiavi. Ritorna (json, header) o (None, None)."""
    for key in shuffled_keys():
        try:
            r = requests.get(f"{BASE}/{path}", params={**params, "apiKey": key}, timeout=20)
            if r.status_code == 200:
                return r.json(), r.headers
            if r.status_code in (401, 429):
                mark_dead(key)
                continue
            print(f"  [{path}] HTTP {r.status_code}")
            return None, None
        except Exception as e:
            print(f"  [{path}] errore connessione: {e}")
    return None, None


def captured_ids():
    ids = set()
    if LOG_FILE.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-3000:]:
            try:
                d = json.loads(line)
                t = parse_dt(d.get("ko"))
                if t and t >= cutoff:
                    ids.add(d["id"])
            except Exception:
                pass
    return ids


def pinnacle_event_probs(ev):
    """{mercato: probabilita vera} dalla quota Pinnacle di un evento dell'API."""
    home = (ev.get("home_team") or "").strip().lower()
    away = (ev.get("away_team") or "").strip().lower()
    prices = {}
    for bm in ev.get("bookmakers", []):
        if (bm.get("key") or "").lower() != "pinnacle" and (bm.get("title") or "").strip().lower() != "pinnacle":
            continue
        for m in bm.get("markets", []):
            if m.get("key") == "h2h":
                for o in m.get("outcomes", []):
                    n = (o.get("name") or "").strip().lower()
                    p = float(o.get("price", 0) or 0)
                    if n == home or n == "home": prices["1"] = p
                    elif n == away or n == "away": prices["2"] = p
                    elif n in ("draw", "pareggio"): prices["X"] = p
            elif m.get("key") == "totals":
                for o in m.get("outcomes", []):
                    pt, side = o.get("point"), (o.get("name") or "").strip().lower()
                    p = float(o.get("price", 0) or 0)
                    if pt in (1.5, 2.5, 3.5) and side in ("over", "under"):
                        prices[("OVER_" if side == "over" else "UNDER_") + str(pt).replace(".", "")] = p
    out = {}
    for grp in {tuple(g) for g in GROUPS.values()}:
        d = devig_power([prices.get(k, 0.0) for k in grp])
        if d:
            out.update(dict(zip(grp, d)))
    return out, prices


def fair_odd_for(key, probs):
    """Quota di chiusura 'giusta' del mercato (anche per le quote derivate 1X, X2, DNB)."""
    key = key.upper()
    if key in probs:
        return 1.0 / probs[key]
    p1, px, p2 = probs.get("1"), probs.get("X"), probs.get("2")
    if not (p1 and px and p2):
        return None
    if key == "1X": return 1.0 / (p1 + px)
    if key == "X2": return 1.0 / (p2 + px)
    if key == "DNB_1": return (p1 + p2) / p1
    if key == "DNB_2": return (p1 + p2) / p2
    return None


def load_trackers():
    out = {}
    for name in TRACKERS:
        f = DATA_DIR / name
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("bets"), list):
                    out[name] = d
            except Exception:
                pass
    return out


def load_log_index():
    """{event_id: ultima chiusura registrata} da closing_log.jsonl (tutta la storia)."""
    idx = {}
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                idx[d["id"]] = d
            except Exception:
                pass
    return idx


def backfill_from_log(trackers, log_idx):
    """Per OGNI giocata (qualsiasi motore, qualsiasi stato) senza quota di chiusura, la ricava dal registro delle chiusure.
    Cosi tutte le partite catturate contano per il CLV, anche se la giocata non era 'in attesa' al momento della cattura."""
    filled = 0
    missing = {"nessuna_chiusura_registrata": 0, "mercato_non_coperto": 0}
    miss_mk = {}
    for t in trackers.values():
        for b in t["bets"]:
            if float(b.get("closing_odd") or 0) > 1.0:
                continue
            ent = log_idx.get(b.get("event_id") or b.get("match_id"))
            if not ent:
                missing["nessuna_chiusura_registrata"] += 1
                continue
            mk = str(b.get("market_key", ""))
            if b.get("price_basis") == "pinnacle_quoted":
                fo = (ent.get("o") or {}).get(mk.upper())
            else:
                fo = fair_odd_for(mk, ent.get("p") or {})
            if fo and fo > 1.0:
                b["closing_odd"] = round(fo, 3)
                b["closing_src"] = "pinnacle_capture_log"
                b["closing_ts"] = ent.get("ts")
                try:
                    b["clv_live_pct"] = round((float(b.get("odd", 0)) / fo - 1.0) * 100, 2)
                except Exception:
                    pass
                filled += 1
            else:
                missing["mercato_non_coperto"] += 1
                miss_mk[mk] = miss_mk.get(mk, 0) + 1
    return filled, missing, miss_mk


def main():
    now = datetime.now(timezone.utc)
    if not API_KEYS:
        print("[Capture] Nessuna chiave ODDSAPI_KEY: esco.")
        return
    if not FIXTURES_FILE.exists():
        print("[Capture] fixtures.json assente: esco.")
        return
    fixtures = json.loads(FIXTURES_FILE.read_text(encoding="utf-8")).get("fixtures", [])
    done = captured_ids()

    # 1. campionati con partite in finestra e non ancora registrate (dai fixtures: nessuna chiamata API)
    todo = {}
    for f in fixtures:
        ko = parse_dt(f.get("commence_time"))
        if not ko or f.get("id") in done:
            continue
        mins = (ko - now).total_seconds() / 60.0
        if WINDOW_MIN <= mins <= WINDOW_MAX:
            todo.setdefault(f.get("sport_key"), []).append(f.get("id"))
    trackers = load_trackers()
    log_idx = load_log_index()
    filled, missing, miss_mk = backfill_from_log(trackers, log_idx)
    print(f"[Capture] Recupero dal registro: {filled} giocate con chiusura aggiunta | senza chiusura: "
          f"{missing['nessuna_chiusura_registrata']} (partita mai catturata) + {missing['mercato_non_coperto']} (mercato non coperto"
          f"{': ' + ', '.join(f'{k}={v}' for k, v in sorted(miss_mk.items(), key=lambda x: -x[1])[:6]) if miss_mk else ''})")

    def save_trackers():
        for name, t in trackers.items():
            (DATA_DIR / name).write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")

    if not todo:
        if filled:
            save_trackers()
        print("[Capture] Nessuna partita tra 5 e 45 minuti da registrare. 0 crediti usati.")
        return

    pend = {}   # event_id -> lista di scommesse in attesa
    for t in trackers.values():
        for b in t["bets"]:
            if b.get("status") in ("PENDING", None):
                pend.setdefault(b.get("event_id") or b.get("match_id"), []).append(b)

    log_lines, updated, credits = [], 0, 0
    for sport, ids in todo.items():
        data, hdr = api_get(f"{sport}/odds", {"bookmakers": "pinnacle", "markets": "h2h,totals", "oddsFormat": "decimal"})
        credits += 2
        if data is None:
            continue
        for ev in data:
            if ev.get("id") not in ids:
                continue
            probs, raw = pinnacle_event_probs(ev)
            if not probs:
                continue
            log_lines.append(json.dumps({
                "id": ev["id"], "sport": sport, "ko": ev.get("commence_time"), "ts": now.isoformat(),
                "home": ev.get("home_team"), "away": ev.get("away_team"),
                "p": {k: round(v, 4) for k, v in probs.items()},
                "o": {k: round(v, 3) for k, v in raw.items()},
            }, ensure_ascii=False))
            for b in pend.get(ev["id"], []):
                mk = str(b.get("market_key", ""))
                if b.get("price_basis") == "pinnacle_quoted":
                    fo = raw.get(mk.upper())   # la tua quota era di Pinnacle: confronto con la chiusura quotata (con margine)
                else:
                    fo = fair_odd_for(mk, probs)
                if fo and fo > 1.0:
                    b["closing_odd"] = round(fo, 3)
                    b["closing_src"] = "pinnacle_capture"
                    b["closing_ts"] = now.isoformat()
                    b["clv_live_pct"] = round((float(b.get("odd", 0)) / fo - 1.0) * 100, 2)
                    updated += 1
        time.sleep(0.3)

    if log_lines:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines) + "\n")
    if updated or filled:
        save_trackers()
    print(f"[Capture] {len(todo)} campionati chiamati (~{credits} crediti) | {len(log_lines)} chiusure registrate | "
          f"{updated} giocate con quota di chiusura aggiornata.")


if __name__ == "__main__":
    main()
