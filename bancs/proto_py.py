#!/usr/bin/env python3
"""Prototype Python optimise : telechargement concurrent + tri en un balayage.
Meme travail que le programme Go de comparaison (memes flux, memes regles)."""
import json
import os
import resource
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
B = Path(__file__).parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(B))
for v in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(v, None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

import collect       # noqa: E402
import proto_tri     # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "8910"
PAR = int(sys.argv[2]) if len(sys.argv) > 2 else 16
LENTS = {7, 23, 61, 13, 47}
urls = [f"http://127.0.0.1:{PORT}/feed/{i}" for i in range(100) if i not in LENTS]

regles = json.loads((RACINE / "collecteur" / "regles.json").read_text(encoding="utf-8"))
auto = {f: proto_tri.compiler_rapide(regles[f]) for f in ("zones", "themes")}

t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=PAR) as ex:
    bruts = list(ex.map(collect.http, urls))
t_dl = time.perf_counter() - t0
octets = sum(len(b) for b in bruts)

t = time.perf_counter()
arts = [a for i, b in enumerate(bruts) for a in collect.lire_rss(b, f"M{i}")]
t_parse = time.perf_counter() - t

t = time.perf_counter()
for a in arts:
    proto_tri.classer_rapide(a["titre"], a["resume"], auto["zones"])
    proto_tri.classer_rapide(a["titre"], a["resume"], auto["themes"])
t_tri = time.perf_counter() - t

rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f"Python optimise : {len(urls)} flux ({PAR} simultanes), {octets//1024} Kio, {len(arts)} articles")
print(f"  telechargement {t_dl:7.2f} s\n  parsing XML    {t_parse:7.2f} s\n"
      f"  tri mots-cles  {t_tri:7.2f} s\n  TOTAL          {t_dl+t_parse+t_tri:7.2f} s")
print(f"  memoire max    {rss:7.1f} Mio")
