#!/usr/bin/env python3
"""Plans d'execution et effet mesure d'un index composite + d'un compteur approche."""
import gzip
import json
import sqlite3
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

B = Path(__file__).parent
MAINT = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
DEPUIS = (MAINT - timedelta(days=30)).isoformat()
NOUV = (MAINT - timedelta(hours=48)).isoformat()

Q_SYNTHESE = ("SELECT zone, COUNT(*) AS total, SUM(ajout >= ?) AS nouveaux "
              "FROM fil WHERE date_pub >= ? GROUP BY zone")
Q_COUNT = "SELECT COUNT(*), COALESCE(SUM(ajout >= ?),0) FROM fil WHERE date_pub >= ? AND zone = ? AND theme = ?"
Q_PAGE = ("SELECT id, titre, source, date_pub, zone, theme, lien, ajout >= ? FROM fil "
          "WHERE date_pub >= ? AND zone = ? AND theme = ? ORDER BY date_pub DESC LIMIT 150")


def chrono(cx, sql, params, n=20):
    cx.execute(sql, params).fetchall()
    t = time.perf_counter()
    for _ in range(n):
        cx.execute(sql, params).fetchall()
    return (time.perf_counter() - t) / n * 1000


for nom in ("api_5k", "api_50k", "api_200k"):
    cx = sqlite3.connect(B / f"{nom}.sqlite")
    cx.row_factory = sqlite3.Row
    print(f"\n=== {nom} ===")
    for titre, sql, p in (("synthese", Q_SYNTHESE, (NOUV, DEPUIS)),
                          ("count articles", Q_COUNT, (NOUV, DEPUIS, "asi", "def")),
                          ("page articles", Q_PAGE, (NOUV, DEPUIS, "asi", "def"))):
        plan = " | ".join(r[3] for r in cx.execute("EXPLAIN QUERY PLAN " + sql, p))
        print(f"  {titre:<16} {chrono(cx, sql, p):7.2f} ms   {plan[:95]}")
    # effet d'un index composite
    cx.execute("CREATE INDEX IF NOT EXISTS ix ON articles(zone, theme, masque, date_pub DESC)")
    cx.execute("ANALYZE")
    print("  --- apres CREATE INDEX articles(zone, theme, masque, date_pub DESC) ---")
    for titre, sql, p in (("synthese", Q_SYNTHESE, (NOUV, DEPUIS)),
                          ("count articles", Q_COUNT, (NOUV, DEPUIS, "asi", "def")),
                          ("page articles", Q_PAGE, (NOUV, DEPUIS, "asi", "def"))):
        plan = " | ".join(r[3] for r in cx.execute("EXPLAIN QUERY PLAN " + sql, p))
        print(f"  {titre:<16} {chrono(cx, sql, p):7.2f} ms   {plan[:95]}")
    cx.execute("DROP INDEX ix")
    cx.close()

print("\n=== compression des reponses (gzip niveau 6 / brotli non dispo ici) ===")
import urllib.request, os
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for v in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(v, None)
ech = {"referentiel (carte SVG)": json.dumps(json.loads(
          (RACINE / "donnees" / "carte.json").read_text())).encode(),
       "index.html": RACINE / "site" / "index.html".read_bytes()}
for nom, brut in ech.items():
    g = gzip.compress(brut, 6)
    print(f"  {nom:<26} {len(brut)/1024:7.1f} KiB -> gzip {len(g)/1024:6.1f} KiB "
          f"({100-100*len(g)/len(brut):.0f} % de moins)")
