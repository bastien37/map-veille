#!/usr/bin/env python3
"""Coût des requêtes de carte et du fil regroupé, au volume cible.

Compare les deux manières d'obtenir « une ligne par événement » :
  - fenêtre glissante sur la table des articles (ROW_NUMBER OVER PARTITION BY) ;
  - lecture de la table des événements, bien plus petite.
"""
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

B = Path(__file__).resolve().parent
sys.path.insert(0, str(B.parent / "collecteur"))
import base  # noqa: E402

CHEMIN = B / "carte.sqlite"
N_ARTICLES, N_EVENEMENTS = 200_000, 60_000
M = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
DEPUIS = (M - timedelta(days=30)).isoformat()
THEMES = ", ".join(f"'{c}'" for c in base.THEMES_ACTIFS)


def construire():
    """Base jetable : 200 000 articles répartis sur 60 000 événements (3,3 sources en moyenne)."""
    if CHEMIN.exists():
        CHEMIN.unlink()
    cx = base.ouvrir(CHEMIN)
    rnd = random.Random(1)
    zones, themes = list(base.ZONES), base.THEMES_ACTIFS
    cx.executemany(
        "INSERT INTO evenements (id,titre,titre_norm,lat,lon,lieu,zone,theme,date_debut,"
        "date_fin,nb_sources,maj) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i, f"Titre {i}", f"titre {i}", rnd.uniform(-60, 70), rnd.uniform(-180, 180),
          f"Lieu {i}", rnd.choice(zones), rnd.choice(themes),
          (M - timedelta(seconds=rnd.randint(0, 30 * 86400))).isoformat(),
          (M - timedelta(seconds=rnd.randint(0, 30 * 86400))).isoformat(), 0, M.isoformat())
         for i in range(1, N_EVENEMENTS + 1)])
    cx.executemany(
        "INSERT INTO articles (id,lien,titre,titre_norm,source,date_pub,zone,theme,ajout,maj,"
        "lat,lon,evenement_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(j, f"https://x.test/{j}", f"Titre {j}", f"titre {j}", f"Media {j % 100}",
          (M - timedelta(seconds=rnd.randint(0, 30 * 86400))).isoformat(),
          rnd.choice(zones), rnd.choice(themes), M.isoformat(), M.isoformat(),
          rnd.uniform(-60, 70), rnd.uniform(-180, 180), 1 + j % N_EVENEMENTS)
         for j in range(1, N_ARTICLES + 1)])
    cx.execute("UPDATE evenements SET nb_sources = "
               "(SELECT COUNT(*) FROM articles WHERE evenement_id = evenements.id)")
    cx.commit()
    cx.execute("ANALYZE")
    cx.commit()
    return cx


def chrono(cx, sql, p, n=5):
    cx.execute(sql, p).fetchall()
    t = time.perf_counter()
    for _ in range(n):
        r = cx.execute(sql, p).fetchall()
    return (time.perf_counter() - t) / n * 1000, len(r)


if __name__ == "__main__":
    cx = construire()
    print(f"{N_ARTICLES} articles, {N_EVENEMENTS} événements, "
          f"base de {CHEMIN.stat().st_size / 1048576:.0f} Mio\n")
    # Les trois premières sont les requêtes réellement servies par serveur/app.py.
    # Les deux dernières sont les formes écartées, gardées pour justifier ce choix sur
    # mesure plutôt que sur opinion.
    mesures = [
        ("fil regroupé (table evenements)",
         f"""SELECT e.id, e.lieu, e.zone, e.theme, e.nb_sources
             FROM evenements e
             WHERE e.date_fin >= ? AND e.theme IN ({THEMES}) AND e.nb_sources > 0
             ORDER BY e.date_fin DESC LIMIT 150""", (DEPUIS,)),
        ("carte, bbox Europe",
         f"""SELECT e.id, e.lat, e.lon, e.theme FROM evenements e
             WHERE e.date_fin >= ? AND e.theme IN ({THEMES}) AND e.lat IS NOT NULL
               AND e.lat BETWEEN ? AND ? AND e.lon BETWEEN ? AND ? AND e.nb_sources > 0
             ORDER BY e.nb_sources DESC LIMIT 500""", (DEPUIS, 41.0, 55.0, -5.0, 20.0)),
        ("carte, bbox monde",
         f"""SELECT e.id, e.lat, e.lon, e.theme FROM evenements e
             WHERE e.date_fin >= ? AND e.theme IN ({THEMES}) AND e.lat IS NOT NULL
               AND e.nb_sources > 0
             ORDER BY e.nb_sources DESC LIMIT 500""", (DEPUIS,)),
        ("sources d'un événement",
         "SELECT id, titre, source, date_pub, lien FROM fil WHERE evenement_id = ? "
         "ORDER BY date_pub DESC", (42,)),
        ("(écartée) carte monde, EXISTS au lieu du compteur",
         f"""SELECT e.id, e.lat, e.lon, e.theme FROM evenements e
             WHERE e.date_fin >= ? AND e.theme IN ({THEMES}) AND e.lat IS NOT NULL
               AND EXISTS (SELECT 1 FROM fil f WHERE f.evenement_id = e.id)
             ORDER BY e.nb_sources DESC LIMIT 500""", (DEPUIS,)),
    ]
    for nom, sql, p in mesures:
        ms, n = chrono(cx, sql, p)
        print(f"  {nom:<36} {ms:8.1f} ms  ({n} lignes)")

    sql_fenetre = f"""SELECT * FROM (
        SELECT a.id, a.titre, a.evenement_id,
               ROW_NUMBER() OVER (PARTITION BY a.evenement_id ORDER BY a.date_pub DESC) rn
        FROM fil a WHERE a.date_pub >= ? AND a.theme IN ({THEMES})) WHERE rn = 1
      ORDER BY id DESC LIMIT 150"""
    ms, n = chrono(cx, sql_fenetre, (DEPUIS,), n=1)
    print(f"  {'(écartée) fenêtre glissante':<36} {ms:8.1f} ms  ({n} lignes)")
    cx.close()
