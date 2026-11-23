"""
h_lineups.py — Registra le formazioni ufficiali (API-Football) poco prima del calcio d'inizio

Gira nel workflow closing-capture (ogni 30 minuti). Per le partite che iniziano tra 5 e 60 minuti e che hanno gia' un
collegamento in data/h_map.json chiede /fixtures/lineups e salva in data/lineups_log.jsonl: formazione, undici titolari
e quali top marcatori della squadra NON partono (e non erano gia' indicati come infortunati).
Per ora e' solo raccolta: servira a stimare quanto le formazioni muovono le quote.
Senza APIFOOTBALL_KEY o senza collegamenti esce senza errori.
"""

import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

import apifootball as AF

DATA_DIR = AF.DATA_DIR
FIXTURES_FILE = DATA_DIR / "fixtures.json"
MAP_FILE = DATA_DIR / "h_map.json"
PLAYERS = DATA_DIR / "h_players.json"
INJURIES = DATA_DIR / "h_injuries.json"
LOG = DATA_DIR / "lineups_log.jsonl"
WINDOW = (5, 60)
MAX_CALLS = 40
TOP_N = 3   # quanti marcatori principali per squadra controllare


def read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    if not AF.KEY:
        print("[Lineups] APIFOOTBALL_KEY assente: esco.")
        return
    mapping = read(MAP_FILE, {})
    if not mapping or not FIXTURES_FILE.exists():
        print("[Lineups] Nessun collegamento partite: esco.")
        return
    now = datetime.now(timezone.utc)
    fixtures = read(FIXTURES_FILE, {}).get("fixtures", [])
    players = read(PLAYERS, {}).get("teams", {})
    injuries = read(INJURIES, {}).get("fixtures", {})
    done = set()
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines()[-3000:]:
            try:
                done.add(json.loads(line)["af_id"])
            except Exception:
                pass
    lines, calls = [], 0
    for f in fixtures:
        m = mapping.get(f.get("id"))
        if not m or m["af_id"] in done:
            continue
        try:
            ko = datetime.fromisoformat(str(f["commence_time"]).replace("Z", "+00:00"))
        except Exception:
            continue
        mins = (ko - now).total_seconds() / 60.0
        if not (WINDOW[0] <= mins <= WINDOW[1]) or calls >= MAX_CALLS:
            continue
        resp = AF.get("fixtures/lineups", {"fixture": m["af_id"]})
        calls += 1
        if not resp:
            continue
        rec = {"af_id": m["af_id"], "event_id": f["id"], "ts": now.isoformat(), "ko": f["commence_time"], "teams": []}
        injured = {(i["team_id"], i["player_id"]) for i in injuries.get(str(m["af_id"]), [])}
        for blk in resp:
            tid = (blk.get("team") or {}).get("id")
            xi = [((p.get("player") or {}).get("id")) for p in blk.get("startXI") or []]
            top = sorted((players.get(str(tid)) or {}).items(), key=lambda kv: -float(kv[1].get("goals", 0)))[:TOP_N]
            benched = [{"player": v.get("name"), "goals": v.get("goals")} for pid, v in top
                       if int(pid) not in xi and (tid, int(pid)) not in injured]
            rec["teams"].append({"team_id": tid, "formation": blk.get("formation"), "startXI": xi, "top_scorers_not_starting": benched})
        lines.append(json.dumps(rec, ensure_ascii=False))
    if lines:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"[Lineups] {len(lines)} formazioni registrate ({calls} chiamate). {AF.status_line()}")


if __name__ == "__main__":
    main()
