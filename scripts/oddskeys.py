"""
oddskeys.py — Uso a rotazione casuale delle chiavi The Odds API (secret ODDSAPI_KEY = chiave1,chiave2,...)

- shuffled_keys(): ordine casuale a ogni chiamata, cosi' l'uso si distribuisce su TUTTE le chiavi invece di svuotare la prima.
  Le chiavi esaurite (401/429) vengono escluse fino a fine esecuzione (mark_dead).
- main(): controllo giornaliero, una chiamata a /v4/sports per ogni chiave (non consuma crediti): stampa quale chiave risponde
  e, se l'API li restituisce, i crediti residui. Serve a vedere a colpo d'occhio se una chiave e' esaurita o non valida.
"""

import os
import random
import requests

API_KEYS = [k.strip() for k in os.environ.get("ODDSAPI_KEY", "").split(",") if k.strip()]
_dead = set()


def shuffled_keys():
    keys = [k for k in API_KEYS if k not in _dead]
    random.shuffle(keys)
    return keys


def mark_dead(key):
    _dead.add(key)


def main():
    if not API_KEYS:
        print("[Chiavi] Nessuna chiave in ODDSAPI_KEY.")
        return
    print(f"[Chiavi] {len(API_KEYS)} chiavi configurate.")
    for i, key in enumerate(API_KEYS, 1):
        tag = f"chiave {i} ({key[:4]}...)"
        try:
            r = requests.get("https://api.the-odds-api.com/v4/sports", params={"apiKey": key}, timeout=15)
            rem = r.headers.get("x-requests-remaining", "?")
            used = r.headers.get("x-requests-used", "?")
            if r.status_code == 200:
                print(f"[Chiavi] {tag}: OK | crediti residui {rem} | usati {used}")
            else:
                print(f"[Chiavi] {tag}: HTTP {r.status_code} (esaurita o non valida)")
        except Exception as e:
            print(f"[Chiavi] {tag}: errore di connessione ({e})")


if __name__ == "__main__":
    main()
