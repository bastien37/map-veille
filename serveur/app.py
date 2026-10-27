"""
MAP – API et serveur web.

Le front (site/) ne contient aucune donnée : il interroge cette API, qui lit la base SQLite
alimentée par le collecteur. Les filtres et les comptages sont faits côté serveur.

Lancer :
    uv run uvicorn serveur.app:app --reload             # développement : http://localhost:8000
    uv run uvicorn serveur.app:app --host 0.0.0.0       # serveur accessible depuis le réseau

Documentation interactive de l'API : http://localhost:8000/docs
"""
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base # noqa: E402  (schéma et référentiels partagés avec le collecteur)

BASE = Path(os.environ.get("MAP_BASE", base.BASE))
CARTE = RACINE / "donnees" / "carte.json"
SITE = RACINE / "site"

# Durées de cache côté navigateur. Le référentiel (zones, thèmes, tracé de la carte) ne change
# qu'avec le code : il est mis en cache longtemps et revalidé par ETag. Le fil change à chaque
# collecte : une minute suffit à absorber les rechargements de page sans servir de vieilles
# données. must-revalidate évite qu'un navigateur serve du périmé hors ligne.
CACHE_REFERENTIEL = "public, max-age=3600, must-revalidate"
CACHE_FIL = "public, max-age=60, must-revalidate"

app = FastAPI(
    title="MAP – Monitoring de l'Actualité Planétaire",
    description="API de lecture des actualités collectées et triées par zone et par thème.",
    version="0.4.0",
)

# Compression des réponses. 87,6 Kio de référentiel tombent à 32,7 Kio, la page à 5,4 Kio.
# En dessous de 512 octets, compresser coûte plus que cela ne rapporte.
# Brotli ferait mieux encore (~20 %) mais demande une dépendance supplémentaire : sur un
# serveur avec nginx ou Caddy devant, l'activer là est plus simple et plus efficace.
app.add_middleware(GZipMiddleware, minimum_size=512, compresslevel=6)


def serialiser(contenu) -> tuple:
    """Corps JSON compact et son ETag."""
    corps = json.dumps(contenu, ensure_ascii=False, separators=(",", ":")).encode()
    return corps, '"' + hashlib.blake2b(corps, digest_size=16).hexdigest() + '"'


def reponse(corps: bytes, etag: str, cache: str, requete: Request,
            corps_gzip: bytes = None) -> Response:
    """Réponse JSON avec Cache-Control et ETag. Si le navigateur a déjà cette version
    (If-None-Match), renvoie 304 sans le corps.

    `corps_gzip` : version déjà compressée, pour un contenu figé qu'il serait absurde de
    recompresser à chaque appel. Content-Encoding étant alors posé, le middleware gzip
    laisse la réponse telle quelle.
    """
    entetes = {"Cache-Control": cache, "ETag": etag}
    if corps_gzip is not None:
        # deux corps possibles pour la même adresse : les caches intermédiaires doivent
        # les distinguer (le middleware gzip pose ce Vary lui-même dans l'autre cas)
        entetes["Vary"] = "Accept-Encoding"
    if requete.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=entetes)
    if corps_gzip is not None and "gzip" in requete.headers.get("accept-encoding", ""):
        return Response(corps_gzip, media_type="application/json",
                        headers={**entetes, "Content-Encoding": "gzip"})
    return Response(corps, media_type="application/json", headers=entetes)


def avec_cache(contenu, cache: str, requete: Request) -> Response:
    corps, etag = serialiser(contenu)
    return reponse(corps, etag, cache, requete)


# ---------------------------------------------------------------------------
# Accès à la base (lecture seule)
# ---------------------------------------------------------------------------
def connexion():
    if not BASE.exists():
        raise HTTPException(503, "Base de données absente : lancez d'abord le collecteur "
                                 "(uv run collecteur/collect.py).")
    # as_uri() donne file:///C:/... : la seule forme d'URI correcte sous Windows (pas de « \\ »)
    cx = sqlite3.connect(Path(BASE).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
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
    if theme is not None and theme not in base.THEMES_ACTIFS:
        raise HTTPException(422, f"Thème inconnu ou désactivé : {theme}. "
                                 f"Codes valides : {', '.join(base.THEMES_ACTIFS)}")


def filtre_sql(zone, theme, jours):
    """Clause WHERE commune : période (D05), zone et thème.

    Le site ne montre que les thèmes actifs de config/themes.json. La liste est injectée
    telle quelle (ce sont des codes validés à la lecture de la configuration, pas une saisie
    d'utilisateur) : le planificateur garde ainsi son index, ce qu'une sous-requête
    sur la table themes lui ferait perdre.
    """
    depuis = (maintenant() - timedelta(days=jours)).isoformat()
    clauses, params = ["date_pub >= ?"], [depuis]
    if zone:
        clauses.append("zone = ?")
        params.append(zone)
    if theme:
        clauses.append("theme = ?")
        params.append(theme)
    else:
        clauses.append("theme IN (%s)" % ", ".join("'%s'" % c for c in base.THEMES_ACTIFS))
    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# Routes de l'API
# ---------------------------------------------------------------------------
# Le référentiel (zones, thèmes, tracé SVG) ne bouge qu'avec le code : il est construit,
# sérialisé ET compressé une seule fois. Le relire et le recompresser à chaque appel coûtait
# 7 ms par requête pour un contenu rigoureusement identique.
_referentiel = None      # (corps JSON, corps gzip, ETag)
_carte_datee = None


@app.get("/api/referentiel", summary="Zones (avec leur tracé sur la carte) et thèmes")
def referentiel(requete: Request, cx: sqlite3.Connection = Depends(connexion)):
    global _referentiel, _carte_datee
    datee = CARTE.stat().st_mtime_ns
    if _referentiel is None or datee != _carte_datee:   # carte.json modifiée : on reconstruit
        _carte_datee = datee
        carte = json.loads(CARTE.read_text(encoding="utf-8"))["zones"]
        zones = [{"code": r["code"], "nom": r["nom"], **carte.get(r["code"], {})}
                 for r in cx.execute("SELECT code, nom FROM zones")]
        ordre = list(base.ZONES)
        zones.sort(key=lambda z: ordre.index(z["code"]) if z["code"] in ordre else 99)
        # seuls les thèmes actifs de config/themes.json sont proposés au visiteur ;
        # les autres restent en base pour ne pas perdre les articles déjà classés
        themes = [{"code": t["code"], "nom": t["nom"]}
                  for t in base.THEMES_DECLARES if t["actif"]]
        corps, etag = serialiser({"zones": zones, "themes": themes,
                                  "duree_nouveau_heures": int(base.DUREE_NOUVEAU.total_seconds() // 3600)})
        _referentiel = (corps, gzip.compress(corps, 6), etag)
    corps, corps_gzip, etag = _referentiel
    return reponse(corps, etag, CACHE_REFERENTIEL, requete, corps_gzip)


@app.get("/api/synthese", summary="Nombre d'articles et de nouveautés par zone (coloration de la carte)")
def synthese(requete: Request, theme: Optional[str] = None, jours: int = Query(30, ge=1, le=365),
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
    return avec_cache({"mise_a_jour": derniere["fin"] if derniere else None,
                       "zones": par_zone,
                       "total": sum(z["total"] for z in par_zone.values()),
                       "sources": sources}, CACHE_FIL, requete)


@app.get("/api/articles", summary="Fil d'actualité filtré, du plus récent au plus ancien")
def articles(requete: Request, zone: Optional[str] = None, theme: Optional[str] = None,
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
    return avec_cache({"total": total, "nouveaux": nouveaux, "decalage": decalage,
                       "articles": [dict(r, nouveau=bool(r["nouveau"])) for r in lignes]},
                      CACHE_FIL, requete)


@app.get("/api/collectes", summary="Journal des dernières collectes et état des sources")
def collectes(limite: int = Query(5, ge=1, le=50), cx: sqlite3.Connection = Depends(connexion)):
    sortie = []
    for c in cx.execute("SELECT * FROM collectes ORDER BY id DESC LIMIT ?", (limite,)).fetchall():
        sources = [dict(s) for s in cx.execute(
            "SELECT source, statut, nb, message FROM collectes_sources WHERE collecte_id = ?", (c["id"],))]
        sortie.append({**dict(c), "hors_ligne": bool(c["hors_ligne"]), "sources": sources})
    return sortie


# ---------------------------------------------------------------------------
# Administration (page /admin et routes /api/admin/*, protégées par mot de passe)
# ---------------------------------------------------------------------------
from serveur.admin import routeur as routeur_admin  # noqa: E402

app.include_router(routeur_admin)


# ---------------------------------------------------------------------------
# Front (fichiers statiques, sans données)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def accueil():
    # revalidation systématique : la page est petite, et une version figée dans le cache du
    # navigateur serait invisible à la mise à jour suivante. Starlette y joint un ETag.
    return FileResponse(SITE / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=SITE), name="site")
