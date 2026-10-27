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
