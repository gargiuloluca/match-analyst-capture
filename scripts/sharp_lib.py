"""sharp_lib.py — funzioni matematiche generiche (nessuna strategia): togliere il margine dalle quote."""

GROUPS = {   # mercati che insieme fanno il 100%: servono per togliere il margine
    "1": ("1", "X", "2"), "X": ("1", "X", "2"), "2": ("1", "X", "2"),
    "OVER_15": ("OVER_15", "UNDER_15"), "UNDER_15": ("OVER_15", "UNDER_15"),
    "OVER_25": ("OVER_25", "UNDER_25"), "UNDER_25": ("OVER_25", "UNDER_25"),
    "OVER_35": ("OVER_35", "UNDER_35"), "UNDER_35": ("OVER_35", "UNDER_35"),
}


def devig_power(prices):
    """Probabilita senza margine col metodo power: p_i = (1/quota_i)^k con somma = 1. Ritorna lista o None."""
    if not prices or any((p or 0) <= 1.0 for p in prices):
        return None
    q = [1.0 / p for p in prices]
    if sum(q) <= 1.0:
        return [x / sum(q) for x in q]
    lo, hi = 1.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if sum(x ** mid for x in q) > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2.0
    p = [x ** k for x in q]
    t = sum(p)
    return [x / t for x in p]
