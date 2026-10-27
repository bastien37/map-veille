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


def filtre_sql(zone, theme, jours, prefixe: str = "", colonne_date: str = "date_pub"):
    """Clause WHERE commune : période (D05), zone et thème.

    `prefixe` et `colonne_date` permettent d'appliquer le même filtre à la table des
    articles (date_pub) comme à celle des événements (e.date_fin), sans récrire la règle
    deux fois ni bricoler la chaîne SQL après coup.

    Le site ne montre que les thèmes actifs de config/themes.json. La liste est injectée
    telle quelle — ce sont des codes validés à la lecture de la configuration, jamais une
    saisie d'utilisateur — pour que le planificateur garde son index, ce qu'une sous-requête
    sur la table themes lui ferait perdre.
    """
    depuis = (maintenant() - timedelta(days=jours)).isoformat()
    clauses, params = [f"{prefixe}{colonne_date} >= ?"], [depuis]
    if zone:
        clauses.append(f"{prefixe}zone = ?")
        params.append(zone)
    if theme:
        clauses.append(f"{prefixe}theme = ?")
        params.append(theme)
    else:
        clauses.append(f"{prefixe}theme IN (%s)"
                       % ", ".join("'%s'" % c for c in base.THEMES_ACTIFS))
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
             grouper: bool = Query(True, description="une ligne par événement plutôt que par article"),
             cx: sqlite3.Connection = Depends(connexion)):
    """Le fil, du plus récent au plus ancien.

    Par défaut une ligne par ÉVÉNEMENT : la même dépêche reprise par cinq médias n'encombre
    pas la liste cinq fois, et `nb_sources` dit combien de médias la relaient. `grouper=false`
    rend la liste article par article, comme avant le regroupement.
    """
    verifier(zone, theme)
    where, params = filtre_sql(zone, theme, jours)
    seuil_nouveau = (maintenant() - base.DUREE_NOUVEAU).isoformat()
    total, nouveaux = cx.execute(f"SELECT COUNT(*), COALESCE(SUM(ajout >= ?), 0) FROM fil WHERE {where}",
                                 [seuil_nouveau, *params]).fetchone()
    if not grouper:
        lignes = cx.execute(
            f"""SELECT id, titre, source, date_pub AS date, zone, theme, lien, evenement_id,
                       ajout >= ? AS nouveau
                FROM fil WHERE {where} ORDER BY date_pub DESC LIMIT ? OFFSET ?""",
            [seuil_nouveau, *params, limite, decalage]).fetchall()
        return avec_cache({"total": total, "nouveaux": nouveaux, "decalage": decalage,
                           "groupe": False,
                           "articles": [dict(r, nouveau=bool(r["nouveau"])) for r in lignes]},
                          CACHE_FIL, requete)

    # Vue groupée : on part de la table des événements, bien plus petite que celle des
    # articles, puis on va chercher l'article le plus récent de chacun pour le titre, la
    # source et le lien affichés. Une fenêtre glissante sur tous les articles donnerait le
    # même résultat mais devrait tous les parcourir.
    ou_evt, p_evt = filtre_sql(zone, theme, jours, "e.", "date_fin")
    # EXISTS dans la clause WHERE, et pas un filtre après coup : un événement dont toutes
    # les sources ont été masquées ne doit ni être compté dans le total, ni occuper une
    # place dans la page — sinon la pagination renverrait des pages trop courtes.
    ou_evt += " AND e.nb_sources > 0"
    evts = cx.execute(
        f"""SELECT e.id, e.lieu, e.lat, e.lon, e.zone, e.theme,
                   e.nb_sources
            FROM evenements e WHERE {ou_evt}
            ORDER BY e.date_fin DESC LIMIT ? OFFSET ?""",
        [*p_evt, limite, decalage]).fetchall()
    sortie = []
    for e in evts:
        r = cx.execute(
            """SELECT id, titre, source, date_pub AS date, lien, ajout >= ? AS nouveau
               FROM fil WHERE evenement_id = ? ORDER BY date_pub DESC LIMIT 1""",
            (seuil_nouveau, e["id"])).fetchone()
        if r:
            sortie.append({**dict(r), "nouveau": bool(r["nouveau"]), "evenement_id": e["id"],
                           "nb_sources": e["nb_sources"], "lieu": e["lieu"],
                           "lat": e["lat"], "lon": e["lon"],
                           "zone": e["zone"], "theme": e["theme"]})
    total_evt = cx.execute(f"SELECT COUNT(*) FROM evenements e WHERE {ou_evt}", p_evt).fetchone()[0]
    return avec_cache({"total": total_evt, "total_articles": total, "nouveaux": nouveaux,
                       "decalage": decalage, "groupe": True, "articles": sortie},
                      CACHE_FIL, requete)


def bbox_sql(bbox: Optional[str], prefixe: str = ""):
    """Fenêtre de la carte, « ouest,sud,est,nord » en degrés. Une carte n'affiche que ce
    qu'elle montre : charger la planète entière à chaque déplacement serait absurde.
    `prefixe` est l'alias de table à mettre devant lat et lon (« e. » par exemple)."""
    if not bbox:
        return "", []
    try:
        ouest, sud, est, nord = (float(x) for x in bbox.split(","))
    except (ValueError, TypeError):
        raise HTTPException(422, "bbox attendue sous la forme « ouest,sud,est,nord » en degrés.")
    if not (-90 <= sud <= nord <= 90) or not (-180 <= ouest <= 180 and -180 <= est <= 180):
        raise HTTPException(422, "bbox hors des limites : latitudes -90..90, longitudes -180..180.")
    lat, lon = f"{prefixe}lat", f"{prefixe}lon"
    if ouest <= est:
        return f" AND {lat} BETWEEN ? AND ? AND {lon} BETWEEN ? AND ?", [sud, nord, ouest, est]
    # la fenêtre franchit l'antiméridien (Pacifique) : elle se lit en deux morceaux
    return f" AND {lat} BETWEEN ? AND ? AND ({lon} >= ? OR {lon} <= ?)", [sud, nord, ouest, est]


@app.get("/api/evenements", summary="Points de la carte : un événement = un point, plusieurs sources")
def evenements(requete: Request, theme: Optional[str] = None, zone: Optional[str] = None,
               jours: int = Query(30, ge=1, le=365), bbox: Optional[str] = None,
               limite: int = Query(500, ge=1, le=2000),
               cx: sqlite3.Connection = Depends(connexion)):
    """Les événements d'une fenêtre géographique et d'une période.

    Un événement regroupe les articles de plusieurs médias qui parlent de la même chose au
    même endroit : il ne fait qu'un point sur la carte. Les plus suivis (le plus de sources)
    sont renvoyés en premier, pour qu'un plafond de résultats ne coupe pas l'essentiel.
    """
    verifier(zone, theme)
    where, params = filtre_sql(zone, theme, jours, "e.", "date_fin")
    where += " AND e.lat IS NOT NULL"
    sql_bbox, p_bbox = bbox_sql(bbox, "e.")
    where += sql_bbox
    params += p_bbox
    # nb_sources est recalculé depuis la vue fil : un article masqué ou non classé ne doit
    # pas gonfler le compteur affiché au lecteur
    where += " AND e.nb_sources > 0"
    lignes = cx.execute(
        f"""SELECT e.id, e.titre, e.lieu, e.lat, e.lon, e.zone, e.theme,
                   e.date_debut, e.date_fin, e.nb_sources
            FROM evenements e WHERE {where}
            ORDER BY e.nb_sources DESC, e.date_fin DESC LIMIT ?""", [*params, limite]).fetchall()
    points = [dict(r) for r in lignes]
    return avec_cache({"total": len(points), "evenements": points}, CACHE_FIL, requete)


@app.get("/api/evenements/{evenement_id}", summary="Un événement et toutes ses sources")
def evenement(evenement_id: int, requete: Request, cx: sqlite3.Connection = Depends(connexion)):
    e = cx.execute("SELECT * FROM evenements WHERE id = ?", (evenement_id,)).fetchone()
    if not e:
        raise HTTPException(404, f"Aucun événement n° {evenement_id}.")
    seuil_nouveau = (maintenant() - base.DUREE_NOUVEAU).isoformat()
    sources = [dict(r, nouveau=bool(r["nouveau"])) for r in cx.execute(
        """SELECT id, titre, source, date_pub AS date, lien, ajout >= ? AS nouveau
           FROM fil WHERE evenement_id = ? ORDER BY date_pub DESC""",
        (seuil_nouveau, evenement_id))]
    if not sources:
        raise HTTPException(404, f"L'événement n° {evenement_id} n'a aucune source publiable.")
    return avec_cache({**dict(e), "nb_sources": len(sources), "sources": sources},
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
