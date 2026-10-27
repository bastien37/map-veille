#!/usr/bin/env python3
"""Genere une base SQLite realiste pour mesurer l'API (volume parametrable)."""
import random
import sys
import unicodedata
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base  # noqa: E402

MOTS = "ukraine europe japon bresil chine sahel petrole fusee pakistan washington gaza inde russie nigeria berlin cyberattaque sommet marche climat election".split()


def normaliser(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


def gen(chemin: Path, n: int, jours: int = 30, graine: int = 42):
    if chemin.exists():
        chemin.unlink()
    rnd = random.Random(graine)
    cx = base.ouvrir(chemin)
    maintenant = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
    zones, themes = list(base.ZONES), list(base.THEMES)
    sources = [f"Media {i}" for i in range(100)]
    cid = base.debut_collecte(cx, maintenant, False)
    lignes = []
    for i in range(n):
        d = maintenant - timedelta(seconds=rnd.randint(0, jours * 86400))
        titre = " ".join(rnd.sample(MOTS, 6)) + f" {i}"
        # 8 % des articles restent a trier (zone ou theme manquant), 2 % masques
        z = rnd.choice(zones) if rnd.random() > .05 else None
        t = rnd.choice(themes) if rnd.random() > .04 else None
        lignes.append((f"https://exemple.test/{i}", titre, normaliser(titre),
                       rnd.choice(sources), d.isoformat(), z, t,
                       1 if rnd.random() < .02 else 0,
                       (maintenant - timedelta(seconds=rnd.randint(0, 5 * 86400))).isoformat(),
                       maintenant.isoformat(), cid))
    cx.executemany("""INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme,
                      masque, ajout, maj, collecte_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", lignes)
    # Evenements : environ 3 articles par evenement, comme une collecte reelle (sur le banc
    # de flux, 3 920 articles donnent 1 300 evenements environ). Sans eux, le fil groupe
    # renverrait une liste vide et la mesure n'aurait aucun sens.
    if "evenements" in {r[0] for r in cx.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        n_evt = max(1, n // 3)
        cx.executemany(
            "INSERT INTO evenements (id,titre,titre_norm,lat,lon,lieu,zone,theme,date_debut,"
            "date_fin,nb_sources,maj) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(i, f"Evenement {i}", f"evenement {i}",
              rnd.uniform(-60, 70), rnd.uniform(-180, 180), f"Lieu {i}",
              rnd.choice(zones), rnd.choice(themes),
              (maintenant - timedelta(seconds=rnd.randint(0, jours * 86400))).isoformat(),
              (maintenant - timedelta(seconds=rnd.randint(0, jours * 86400))).isoformat(),
              0, maintenant.isoformat()) for i in range(1, n_evt + 1)])
        cx.execute("UPDATE articles SET evenement_id = 1 + (id % ?)", (n_evt,))
        cx.execute("UPDATE evenements SET nb_sources = "
                   "(SELECT COUNT(*) FROM fil WHERE evenement_id = evenements.id)")
    base.fin_collecte(cx, cid, maintenant, n, n, 0)
    cx.commit()
    cx.execute("ANALYZE")
    cx.commit()
    cx.close()
    print(f"{chemin.name} : {n} articles, {chemin.stat().st_size/1024/1024:.1f} Mio")


if __name__ == "__main__":
    b = Path(__file__).parent
    gen(b / "api_5k.sqlite", 5_000)
    gen(b / "api_50k.sqlite", 50_000)
    gen(b / "api_200k.sqlite", 200_000)
