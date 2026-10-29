#!/usr/bin/env python3
"""Stockage de la carte a points : SQLite suffit-il pour les requetes par bbox ?

Compare, a volume cible (100 000 articles / 35 000 points sur 30 jours) :
  a) aucun index spatial (filtre en balayage)
  b) index B-tree (theme, date, lat, lon)
  c) table virtuelle R*Tree (equivalent SQLite de l'index spatial)
"""
import random
import sqlite3
import time
from pathlib import Path

B = Path(__file__).resolve().parent
CHEMIN = B / "bbox.sqlite"
N_POINTS, N_ART, JOURS = 35_000, 100_000, 30
THEMES = [f"t{i}" for i in range(9)]

# bbox d'essai : Europe de l'Ouest (~2 % du monde), Afrique (~8 %), monde entier
BBOX = {"Europe de l'Ouest": (41.0, 55.0, -5.0, 20.0),
        "Afrique": (-35.0, 15.0, -18.0, 52.0),
        "monde": (-90.0, 90.0, -180.0, 180.0)}


def construire():
    if CHEMIN.exists():
        CHEMIN.unlink()
    cx = sqlite3.connect(CHEMIN)
    rnd = random.Random(7)
    cx.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE points(id INTEGER PRIMARY KEY, lat REAL, lon REAL, nom TEXT);
      CREATE TABLE articles(id INTEGER PRIMARY KEY, point_id INT, theme TEXT, date_pub TEXT,
                            titre TEXT, source TEXT);
      -- index que toute implementation naive aurait de toute facon (cle etrangere) :
      -- sans lui la jointure degenere et la comparaison des variantes n'a plus de sens
      CREATE INDEX ix_fk ON articles(point_id);
    """)
    # les points suivent la densite de population : 70 % dans les zones habitees
    pts = []
    for i in range(N_POINTS):
        if rnd.random() < .7:
            lat = rnd.gauss(rnd.choice([48, 40, 25, 35, -10]), 8)
            lon = rnd.gauss(rnd.choice([5, -95, 80, 115, 20]), 15)
        else:
            lat, lon = rnd.uniform(-60, 75), rnd.uniform(-180, 180)
        pts.append((i + 1, max(-85, min(85, lat)), ((lon + 180) % 360) - 180, f"Lieu {i}"))
    cx.executemany("INSERT INTO points VALUES (?,?,?,?)", pts)
    arts = [(j + 1, rnd.randint(1, N_POINTS), rnd.choice(THEMES),
             f"2026-{10 if rnd.random() < .5 else 9:02d}-{rnd.randint(1,28):02d}T12:00:00+00:00",
             f"Titre {j}", f"Media {j % 100}") for j in range(N_ART)]
    cx.executemany("INSERT INTO articles VALUES (?,?,?,?,?,?)", arts)
    cx.commit()
    cx.execute("ANALYZE")
    cx.commit()
    return cx


def chrono(cx, sql, p, n=15):
    r = cx.execute(sql, p).fetchall()
    t = time.perf_counter()
    for _ in range(n):
        cx.execute(sql, p).fetchall()
    return (time.perf_counter() - t) / n * 1000, len(r)


DEPUIS = "2026-09-27T00:00:00+00:00"
Q_SIMPLE = """SELECT p.id, p.lat, p.lon, COUNT(*) n FROM points p JOIN articles a ON a.point_id = p.id
  WHERE p.lat BETWEEN ? AND ? AND p.lon BETWEEN ? AND ? AND a.date_pub >= ? AND a.theme = ?
  GROUP BY p.id LIMIT 500"""
Q_RTREE = """SELECT p.id, p.lat, p.lon, COUNT(*) n FROM grille g JOIN points p ON p.id = g.id
  JOIN articles a ON a.point_id = p.id
  WHERE g.minlat <= ? AND g.maxlat >= ? AND g.minlon <= ? AND g.maxlon >= ?
    AND a.date_pub >= ? AND a.theme = ? GROUP BY p.id LIMIT 500"""

cx = construire()
taille = CHEMIN.stat().st_size / 1024 / 1024
print(f"{N_POINTS} points, {N_ART} articles, base {taille:.1f} Mio\n")
print(f"{'variante':<34}" + "".join(f"{n:>22}" for n in BBOX))

resultats = {}
for etape in ("a) aucun index spatial", "b) index B-tree (lat, lon)", "c) table R*Tree"):
    if etape.startswith("b"):
        cx.execute("CREATE INDEX ix_pts ON points(lat, lon)")
        cx.execute("CREATE INDEX ix_art ON articles(theme, date_pub, point_id)")
        cx.execute("ANALYZE")
    if etape.startswith("c"):
        cx.executescript("""
          CREATE VIRTUAL TABLE grille USING rtree(id, minlat, maxlat, minlon, maxlon);
          INSERT INTO grille SELECT id, lat, lat, lon, lon FROM points;""")
        cx.execute("ANALYZE")
    ligne = f"{etape:<34}"
    for nom, (la1, la2, lo1, lo2) in BBOX.items():
        if etape.startswith("c"):
            ms, n = chrono(cx, Q_RTREE, (la2, la1, lo2, lo1, DEPUIS, "t3"))
        else:
            ms, n = chrono(cx, Q_SIMPLE, (la1, la2, lo1, lo2, DEPUIS, "t3"))
        ligne += f"{ms:>14.2f} ms ({n:>3})"
        resultats[(etape, nom)] = ms
    print(ligne)

print(f"\ntaille finale avec index + R*Tree : {CHEMIN.stat().st_size/1024/1024:.1f} Mio")
cx.close()
