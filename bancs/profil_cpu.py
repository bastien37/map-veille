#!/usr/bin/env python3
"""Profil CPU pur du collecteur (serveur de flux dans un AUTRE processus).
Decoupe aussi le temps par etape : telechargement / parsing / tri / stockage."""
import cProfile
import io
import os
import pstats
import re
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
BENCH = Path(__file__).parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(BENCH))
for v in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(v, None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import base      # noqa: E402
import collect   # noqa: E402
import serveur_flux  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8910
N = 100
CHEMIN = BENCH / "profil.sqlite"

# --- etape 1 : telechargement seul (mesure du cout reseau sequentiel)
urls = [f"http://127.0.0.1:{PORT}/feed/{i}" for i in range(N)
        if i not in serveur_flux.FLUX_LENTS and i not in serveur_flux.FLUX_MORTS]
t = time.perf_counter()
bruts = [collect.http(u) for u in urls]
t_dl = time.perf_counter() - t
octets = sum(len(b) for b in bruts)

# --- etape 2 : parsing feedparser seul
t = time.perf_counter()
lots = [collect.lire_rss(b, f"Media {i}") for i, b in enumerate(bruts)]
t_parse = time.perf_counter() - t
articles = [a for lot in lots for a in lot]

# --- etape 3 : tri par mots-cles seul
ref = collect.referentiels()
t = time.perf_counter()
for a in articles:
    collect.trier(a, ref)
t_tri = time.perf_counter() - t

# --- etape 4 : normalisation + stockage SQLite
if CHEMIN.exists():
    CHEMIN.unlink()
cx = base.ouvrir(CHEMIN)
cid = base.debut_collecte(cx, collect.MAINTENANT, False)
t = time.perf_counter()
for a in articles:
    if a["titre"] and a["lien"].startswith("http"):
        base.enregistrer(cx, a, collect.normaliser(a["titre"]), collect.MAINTENANT, cid)
cx.commit()
t_store = time.perf_counter() - t
cx.close()

tot = t_dl + t_parse + t_tri + t_store
print(f"{len(urls)} flux sains, {octets/1024:.0f} KiB, {len(articles)} articles\n")
print(f"{'etape':<34}{'secondes':>10}{'% du total':>12}{'par article':>14}")
for nom, v in (("1. telechargement (sequentiel)", t_dl), ("2. parsing feedparser", t_parse),
               ("3. tri par mots-cles", t_tri), ("4. normalisation + SQLite", t_store)):
    print(f"{nom:<34}{v:>10.2f}{100*v/tot:>11.1f}%{1000*v/len(articles):>12.2f} ms")
print(f"{'TOTAL':<34}{tot:>10.2f}{100:>11.1f}%{1000*tot/len(articles):>12.2f} ms")

# --- profil detaille du CPU (parsing + tri + stockage, hors reseau)
pr = cProfile.Profile()
pr.enable()
lots2 = [collect.lire_rss(b, f"M{i}") for i, b in enumerate(bruts)]
arts2 = [a for lot in lots2 for a in lot]
for a in arts2:
    collect.trier(a, ref)
    collect.normaliser(a["titre"])
pr.disable()
st = pstats.Stats(pr)
st.sort_stats("tottime")
b = io.StringIO()
st.stream = b
st.print_stats(14)
print("\n--- profil CPU hors reseau (tottime, top 14) ---")
for l in b.getvalue().splitlines():
    if re.match(r"\s+\d", l) or "ncalls" in l or "function calls" in l:
        print(l[:140])
