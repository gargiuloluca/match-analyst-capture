#!/usr/bin/env python3
"""
state_sync.py — Archivio dati dei motori su Supabase (tabella engine_files).

Comandi:
  pull     scarica da Supabase tutti i file in data/ (da lanciare a inizio workflow).
           Si ferma con errore se la tabella e vuota o Supabase non risponde.
  push     rimanda su Supabase SOLO i file cambiati dal pull in poi (fine workflow).
           Protezioni: JSON valido, niente file che si riduce a meno della meta,
           niente sovrascrittura se qualcun altro (es. pannello admin) lo ha cambiato nel frattempo.
  init     prima migrazione: carica data/ della repo su Supabase. Rifiuta se la tabella non e vuota.
  verify   controlla che Supabase contenga gli stessi file di data/ (usato dopo init).
  public   riscrive data/tracker.json e data/tracker_safe.json senza le giocate PENDING
           (sono le uniche copie che restano pubbliche sul sito).

Variabili: SUPABASE_URL, SUPABASE_SERVICE_KEY. Solo libreria standard.
"""
import base64
import gzip
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MANIFEST = ROOT / ".sync_manifest.json"
TABLE = "engine_files"
EXCLUDE = {"backup_giornaliero.json"}          # derivato, finisce gia nel backup privato
GZ_THRESHOLD = 300_000                          # sopra questa soglia: gzip + base64
PUBLIC_FILES = ["tracker.json", "tracker_safe.json"]
SHRINK_MIN_BYTES = 5_000
SHRINK_RATIO = 0.5
# file che per natura si svuotano nei giorni senza partite: possono rimpicciolirsi senza che sia un errore
# engine_eval.json e h_map.json si ricalcolano a ogni analisi (dipendono dalle partite del giorno): non sono storico
VOLATILE = {"fixtures.json", "h_upcoming.json", "h_injuries.json", "engine_eval.json", "h_map.json"}


def die(msg):
    print(f"ERRORE: {msg}", file=sys.stderr)
    sys.exit(1)


def cfg():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        die("mancano i secret SUPABASE_URL e/o SUPABASE_SERVICE_KEY")
    return url, key


def req(method, path, body=None, extra=None):
    url, key = cfg()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra:
        headers.update(extra)
    r = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        die(f"Supabase ha risposto {e.code} su {path.split('?')[0]}: {detail}")
    except Exception as e:  # rete, timeout
        die(f"Supabase non raggiungibile ({type(e).__name__}: {e})")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode(text):
    raw = text.encode("utf-8")
    if len(raw) > GZ_THRESHOLD:
        return base64.b64encode(gzip.compress(raw, 9)).decode("ascii"), "gz64"
    return text, "plain"


def decode(body, enc):
    if enc == "gz64":
        return gzip.decompress(base64.b64decode(body)).decode("utf-8")
    return body


def local_files():
    if not DATA.exists():
        return []
    out = []
    for p in sorted(DATA.iterdir()):
        if p.is_file() and p.suffix in (".json", ".jsonl") and p.name not in EXCLUDE and not p.name.startswith("."):
            out.append(p)
    return out


def valid_content(name, text):
    try:
        if name.endswith(".jsonl"):
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        else:
            json.loads(text)
        return True
    except Exception:
        return False


def remote_index():
    rows = req("GET", f"{TABLE}?select=name,sha,size", extra={"Range-Unit": "items", "Range": "0-999"}) or []
    return {r["name"]: r for r in rows}


def upsert(name, text):
    body, enc = encode(text)
    row = {
        "name": name,
        "body": body,
        "enc": enc,
        "sha": sha(text),
        "size": len(text.encode("utf-8")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    req("POST", f"{TABLE}?on_conflict=name", [row],
        extra={"Prefer": "resolution=merge-duplicates,return=minimal"})


def load_manifest():
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- comandi
def cmd_pull():
    rows = req("GET", f"{TABLE}?select=name,body,enc,sha", extra={"Range-Unit": "items", "Range": "0-999"}) or []
    if not rows:
        die("la tabella engine_files e vuota: esegui prima il workflow 'Migrazione dati su Supabase'")
    DATA.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for r in rows:
        name = r["name"]
        if "/" in name or name.startswith("."):
            continue
        try:
            text = decode(r["body"], r.get("enc") or "plain")
        except Exception as e:
            die(f"file {name} illeggibile su Supabase ({e})")
        (DATA / name).write_bytes(text.encode("utf-8"))
        manifest[name] = {"remote": r.get("sha"), "local": sha(text)}
    MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    print(f"[sync] pull: {len(manifest)} file scaricati da Supabase")


def cmd_push():
    manifest = load_manifest()
    if not manifest:
        die("manifest assente: il pull non e stato eseguito, push annullato")
    remote = remote_index()
    uploaded, skipped, errors = [], [], 0
    for p in local_files():
        name = p.name
        text = p.read_text(encoding="utf-8")
        h = sha(text)
        m = manifest.get(name)
        if m and m["local"] == h:
            continue                                   # invariato
        if not valid_content(name, text):
            print(f"::error::{name}: contenuto non valido, NON caricato")
            errors += 1
            continue
        rem = remote.get(name)
        if rem:
            if not m or rem.get("sha") != m.get("remote"):
                print(f"::warning::{name}: modificato su Supabase durante l'esecuzione (pannello admin?), salto per non sovrascrivere")
                skipped.append(name)
                continue
            old = rem.get("size") or 0
            new = len(text.encode("utf-8"))
            if old > SHRINK_MIN_BYTES and new < old * SHRINK_RATIO and name not in VOLATILE and os.environ.get("ALLOW_SHRINK") != "1":
                print(f"::error::{name}: passerebbe da {old} a {new} byte, NON caricato (protezione)")
                errors += 1
                continue
        upsert(name, text)
        uploaded.append(name)
        manifest[name] = {"remote": sha(text), "local": h}
    MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    print(f"[sync] push: {len(uploaded)} caricati {uploaded} | saltati {skipped} | errori {errors}")
    if errors:
        sys.exit(1)


def cmd_init():
    remote = remote_index()
    if remote and os.environ.get("FORCE_INIT") != "1":
        die(f"la tabella contiene gia {len(remote)} file: init annullato per non sovrascrivere dati piu recenti")
    n = 0
    for p in local_files():
        text = p.read_text(encoding="utf-8")
        if not valid_content(p.name, text):
            die(f"{p.name}: non e un JSON valido, migrazione annullata")
        upsert(p.name, text)
        n += 1
    print(f"[sync] init: {n} file caricati su Supabase")


def cmd_verify():
    rows = req("GET", f"{TABLE}?select=name,body,enc", extra={"Range-Unit": "items", "Range": "0-999"}) or []
    remote = {r["name"]: decode(r["body"], r.get("enc") or "plain") for r in rows}
    bad = []
    for p in local_files():
        text = p.read_text(encoding="utf-8")
        if remote.get(p.name) != text:
            bad.append(p.name)
    if bad:
        die(f"verifica fallita, file diversi o mancanti su Supabase: {bad}")
    print(f"[sync] verify OK: {len(local_files())} file identici su Supabase")


def cmd_public():
    for name in PUBLIC_FILES:
        p = DATA / name
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        bets = d.get("bets", [])
        d["bets"] = [b for b in bets if b.get("status") != "PENDING"]
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[sync] public: {name} senza giocate in attesa ({len(bets) - len(d['bets'])} nascoste)")


COMMANDS = {"pull": cmd_pull, "push": cmd_push, "init": cmd_init, "verify": cmd_verify, "public": cmd_public}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        die("uso: state_sync.py [pull|push|init|verify|public]")
    COMMANDS[sys.argv[1]]()
