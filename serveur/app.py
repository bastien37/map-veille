"""
MAP – API et serveur web.

Le front (site/) ne contient aucune donnée : il interroge cette API, qui lit la base SQLite
alimentée par le collecteur. Les filtres et les comptages sont faits côté serveur.

Lancer :
    uv run uvicorn serveur.app:app --reload             # développement : http://localhost:8000
    uv run uvicorn serveur.app:app --host 0.0.0.0       # serveur accessible depuis le réseau

Documentation interactive de l'API : http://localhost:8000/docs
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base # noqa: E402  (schéma et référentiels partagés avec le collecteur)

BASE = Path(os.environ.get("MAP_BASE", base.BASE))
CARTE = RACINE / "donnees" / "carte.json"
SITE = RACINE / "site"

app = FastAPI(
    title="MAP – Monitoring de l'Actualité Planétaire",
    description="API de lecture des actualités collectées et triées par zone et par thème.",
    version="0.3.0",
)


# ---------------------------------------------------------------------------
# Accès à la base (lecture seule)
# ---------------------------------------------------------------------------
def connexion():
    if not BASE.exists():
        raise HTTPException(503, "Base de données absente : lancez d'abord le collecteur "
                                 "(uv run collecteur/collect.py).")
    cx = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    try:
        yield cx
    finally:
        cx.close()


def maintenant() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def verifier(zone: Optional[str], theme: Optional[str]):
    if zone is not None and zone not in base.ZONES:
        raise HTTPException(422, f"Zone inconnue : {zone}. Codes valides : {', '.join(base.ZONES)}")
    if theme is not None and theme not in base.THEMES:
        raise HTTPException(422, f"Thème inconnu : {theme}. Codes valides : {', '.join(base.THEMES)}")


def filtre_sql(zone, theme, jours):
    """Clause WHERE commune : période (D05), zone et thème."""
    depuis = (maintenant() - timedelta(days=jours)).isoformat()
    clauses, params = ["date_pub >= ?"], [depuis]
    if zone:
        clauses.append("zone = ?")
        params.append(zone)
    if theme:
        clauses.append("theme = ?")
        params.append(theme)
    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# Routes de l'API
# ---------------------------------------------------------------------------
@app.get("/api/referentiel", summary="Zones (avec leur tracé sur la carte) et thèmes")
def referentiel(cx: sqlite3.Connection = Depends(connexion)):
    carte = json.loads(CARTE.read_text(encoding="utf-8"))["zones"]
    zones = [{"code": r["code"], "nom": r["nom"], **carte.get(r["code"], {})}
             for r in cx.execute("SELECT code, nom FROM zones")]
    ordre = list(base.ZONES)
    zones.sort(key=lambda z: ordre.index(z["code"]) if z["code"] in ordre else 99)
    themes = [dict(r) for r in cx.execute("SELECT code, nom FROM themes")]
    themes.sort(key=lambda t: list(base.THEMES).index(t["code"]) if t["code"] in base.THEMES else 99)
    return {"zones": zones, "themes": themes,
            "duree_nouveau_heures": int(base.DUREE_NOUVEAU.total_seconds() // 3600)}


@app.get("/api/synthese", summary="Nombre d'articles et de nouveautés par zone (coloration de la carte)")
def synthese(theme: Optional[str] = None, jours: int = Query(30, ge=1, le=365),
             cx: sqlite3.Connection = Depends(connexion)):
    verifier(None, theme)
    where, params = filtre_sql(None, theme, jours)
    seuil_nouveau = (maintenant() - base.DUREE_NOUVEAU).isoformat()
    lignes = cx.execute(
        f"""SELECT zone, COUNT(*) AS total, SUM(ajout >= ?) AS nouveaux
            FROM fil WHERE {where} GROUP BY zone""", [seuil_nouveau, *params]).fetchall()
    par_zone = {code: {"total": 0, "nouveaux": 0} for code in base.ZONES}
    for r in lignes:
        par_zone[r["zone"]] = {"total": r["total"], "nouveaux": r["nouveaux"] or 0}
    derniere = cx.execute("SELECT fin FROM collectes WHERE fin IS NOT NULL AND hors_ligne = 0 "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    sources = [r[0] for r in cx.execute(
        f"SELECT source, COUNT(*) n FROM fil WHERE {where} GROUP BY source ORDER BY n DESC", params)]
    if not derniere:   # pas encore de collecte en ligne : date du dernier ajout en base
        derniere = cx.execute("SELECT MAX(ajout) AS fin FROM articles").fetchone()
    return {"mise_a_jour": derniere["fin"] if derniere else None,
            "zones": par_zone,
            "total": sum(z["total"] for z in par_zone.values()),
            "sources": sources}


@app.get("/api/articles", summary="Fil d'actualité filtré, du plus récent au plus ancien")
def articles(zone: Optional[str] = None, theme: Optional[str] = None,
             jours: int = Query(30, ge=1, le=365),
             limite: int = Query(150, ge=1, le=500), decalage: int = Query(0, ge=0),
             cx: sqlite3.Connection = Depends(connexion)):
    verifier(zone, theme)
    where, params = filtre_sql(zone, theme, jours)
    seuil_nouveau = (maintenant() - base.DUREE_NOUVEAU).isoformat()
    total, nouveaux = cx.execute(f"SELECT COUNT(*), COALESCE(SUM(ajout >= ?), 0) FROM fil WHERE {where}",
                                 [seuil_nouveau, *params]).fetchone()
    lignes = cx.execute(
        f"""SELECT id, titre, source, date_pub AS date, zone, theme, lien, ajout >= ? AS nouveau
            FROM fil WHERE {where} ORDER BY date_pub DESC LIMIT ? OFFSET ?""",
        [seuil_nouveau, *params, limite, decalage]).fetchall()
    return {"total": total, "nouveaux": nouveaux, "decalage": decalage,
            "articles": [dict(r, nouveau=bool(r["nouveau"])) for r in lignes]}


@app.get("/api/collectes", summary="Journal des dernières collectes et état des sources")
def collectes(limite: int = Query(5, ge=1, le=50), cx: sqlite3.Connection = Depends(connexion)):
    sortie = []
    for c in cx.execute("SELECT * FROM collectes ORDER BY id DESC LIMIT ?", (limite,)).fetchall():
        sources = [dict(s) for s in cx.execute(
            "SELECT source, statut, nb, message FROM collectes_sources WHERE collecte_id = ?", (c["id"],))]
        sortie.append({**dict(c), "hors_ligne": bool(c["hors_ligne"]), "sources": sources})
    return sortie


# ---------------------------------------------------------------------------
# Front (fichiers statiques, sans données)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def accueil():
    return FileResponse(SITE / "index.html")


app.mount("/", StaticFiles(directory=SITE), name="site")
