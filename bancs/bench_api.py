#!/usr/bin/env python3
"""Baseline API : latences (p50/p95/p99) et taille des reponses, par volume de base."""
import os
import signal
import statistics as st
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(__file__).parent
RACINE = Path(__file__).resolve().parent.parent
for v in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(v, None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
import httpx2 as httpx  # noqa: E402

ROUTES = [
    ("referentiel", "/api/referentiel"),
    ("synthese", "/api/synthese"),
    ("synthese?theme", "/api/synthese?theme=def"),
    ("articles (defaut 150)", "/api/articles?limite=150"),
    ("articles zone+theme", "/api/articles?zone=asi&theme=def&limite=150"),
    ("articles page 10", "/api/articles?limite=150&decalage=1500"),
    ("articles langue=fr", "/api/articles?limite=150&langue=fr"),
    ("evenements monde", "/api/evenements?limite=500"),
    ("evenements langue=fr", "/api/evenements?limite=500&langue=fr"),
    ("collectes", "/api/collectes"),
    ("page / (HTML)", "/"),
]
N = 60


def mesurer(port, base_sqlite, titre):
    env = {**os.environ, "MAP_BASE": str(base_sqlite), "PYTHONPATH": str(RACINE)}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "serveur.app:app",
                          "--port", str(port), "--log-level", "error"],
                         cwd=RACINE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base_url, timeout=30) as c:
            for _ in range(80):
                try:
                    if c.get("/api/referentiel").status_code < 500:
                        break
                except Exception:
                    pass
                time.sleep(.25)
            print(f"\n### {titre}")
            print(f"{'route':<24}{'p50':>8}{'p95':>8}{'p99':>8}{'taille':>10}"
                  f"{'gzip?':>7}{'cache?':>18}")
            total_poids = 0
            for nom, url in ROUTES:
                c.get(url)  # warmup
                lats = []
                for _ in range(N):
                    t = time.perf_counter()
                    r = c.get(url, headers={"Accept-Encoding": "gzip, br"})
                    lats.append((time.perf_counter() - t) * 1000)
                lats.sort()
                taille = len(r.content)
                enc = r.headers.get("content-encoding", "non")
                cache = r.headers.get("cache-control") or ("etag" if r.headers.get("etag") else "aucun")
                if nom.startswith(("referentiel", "synthese", "articles (defaut", "page /")):
                    total_poids += taille
                print(f"{nom:<24}{lats[len(lats)//2]:>7.1f}{st.quantiles(lats, n=20)[18]:>8.1f}"
                      f"{lats[-1]:>8.1f}{taille/1024:>9.1f}k{enc:>7}{str(cache)[:17]:>18}")
            print(f"  -> charge initiale de la page (HTML + 3 appels API) : {total_poids/1024:.1f} KiB")
    finally:
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)


if __name__ == "__main__":
    mesurer(8950, BENCH / "api_5k.sqlite", "Base 5 000 articles (echelle actuelle : 5 sources)")
    mesurer(8951, BENCH / "api_50k.sqlite", "Base 50 000 articles (~40 flux x 30 j)")
    mesurer(8952, BENCH / "api_200k.sqlite", "Base 200 000 articles (100 flux x 30 j, cible)")
