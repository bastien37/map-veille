"""
MAP – API et serveur web.

Le front (site/) ne contient aucune donnée : il interroge cette API, qui lit la base SQLite
alimentée par le collecteur. Les filtres et les comptages sont faits côté serveur.

Lancer :
    uv run uvicorn serveur.app:app --reload             # développement : http://localhost:8000
    uv run uvicorn serveur.app:app --host 0.0.0.0       # serveur accessible depuis le réseau

Documentation interactive de l'API : http://localhost:8000/docs
"""
import asyncio
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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base # noqa: E402  (schéma et référentiels partagés avec le collecteur)

BASE = Path(os.environ.get("MAP_BASE", base.BASE))
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


def verifier_langue(langue: Optional[str]) -> Optional[str]:
    if langue in (None, "", "toutes"):
        return None
    if langue not in base.LANGUES:
        raise HTTPException(422, f"Langue inconnue : {langue}. "
                                 f"Codes valides : {', '.join(base.LANGUES)}")
    return langue


def themes_demandes(theme: Optional[str], themes: Optional[str]) -> list:
    """Codes de thèmes retenus pour une requête.

    `theme` garde l'ancien usage, un seul thème. `themes` accepte une liste séparée par des
    virgules : c'est ce que le visiteur coche sur le site, et une page de thème n'est qu'un
    cas particulier à un seul code. Rien de demandé = tous les thèmes actifs.
    """
    if themes:
        voulus = [c.strip() for c in themes.split(",") if c.strip()]
        inconnus = [c for c in voulus if c not in base.THEMES_ACTIFS]
        if inconnus:
            raise HTTPException(422, f"Thème inconnu ou désactivé : {', '.join(inconnus)}. "
                                     f"Codes valides : {', '.join(base.THEMES_ACTIFS)}")
        if voulus:
            return list(dict.fromkeys(voulus))
    verifier(None, theme)
    return [theme] if theme else list(base.THEMES_ACTIFS)


def filtre_sql(zone, theme, jours, prefixe: str = "", colonne_date: str = "date_pub",
               themes: Optional[list] = None):
    """Clause WHERE commune : période (D05), zone et thème(s).

    `prefixe` et `colonne_date` permettent d'appliquer le même filtre à la table des
    articles (date_pub) comme à celle des événements (e.date_fin), sans récrire la règle
    deux fois ni bricoler la chaîne SQL après coup.

    La liste de thèmes est injectée telle quelle dans le SQL — ce sont des codes vérifiés
    contre config/themes.json, jamais une saisie brute — pour que le planificateur garde
    son index, ce qu'une sous-requête sur la table themes lui ferait perdre.
    """
    depuis = (maintenant() - timedelta(days=jours)).isoformat()
    clauses, params = [f"{prefixe}{colonne_date} >= ?"], [depuis]
    if zone:
        clauses.append(f"{prefixe}zone = ?")
        params.append(zone)
    codes = themes if themes is not None else ([theme] if theme else list(base.THEMES_ACTIFS))
    inconnus = [c for c in codes if c not in base.THEMES]
    if inconnus:   # garde-fou : rien d'autre que des codes connus n'entre dans la requête
        raise HTTPException(422, f"Thème inconnu : {', '.join(inconnus)}")
    if len(codes) == 1:
        clauses.append(f"{prefixe}theme = ?")
        params.append(codes[0])
    else:
        clauses.append(f"{prefixe}theme IN (%s)" % ", ".join("'%s'" % c for c in codes))
    return " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# Routes de l'API
# ---------------------------------------------------------------------------
# Le référentiel (zones, thèmes, réglages de la carte) ne bouge qu'avec la configuration :
# il est construit, sérialisé et compressé une seule fois.
_referentiel = None      # (corps JSON, corps gzip, ETag)
_carte_datee = None


@app.get("/api/referentiel", summary="Zones (avec leur tracé sur la carte) et thèmes")
def referentiel(requete: Request, cx: sqlite3.Connection = Depends(connexion)):
    global _referentiel, _carte_datee
    datee = base.CARTE_JSON.stat().st_mtime_ns
    if _referentiel is None or datee != _carte_datee:   # fichier modifié : on reconstruit
        _carte_datee = datee
        zones = [{"code": r["code"], "nom": r["nom"]}
                 for r in cx.execute("SELECT code, nom FROM zones")]
        ordre = list(base.ZONES)
        zones.sort(key=lambda z: ordre.index(z["code"]) if z["code"] in ordre else 99)
        # seuls les thèmes actifs de config/themes.json sont proposés au visiteur ;
        # les autres restent en base pour ne pas perdre les articles déjà classés
        themes = [{"code": t["code"], "nom": t["nom"]}
                  for t in base.THEMES_DECLARES if t["actif"]]
        carte_conf = json.loads(base.CARTE_JSON.read_text(encoding="utf-8"))
        corps, etag = serialiser({
            "zones": zones, "themes": themes,
            "langues": [{"code": c, "nom": base.NOMS_LANGUES.get(c, c)} for c in base.LANGUES],
            "duree_nouveau_heures": int(base.DUREE_NOUVEAU.total_seconds() // 3600),
            "carte": {k: v for k, v in carte_conf.items() if not k.startswith("_")}})
        _referentiel = (corps, gzip.compress(corps, 6), etag)
    corps, corps_gzip, etag = _referentiel
    return reponse(corps, etag, CACHE_REFERENTIEL, requete, corps_gzip)


@app.get("/api/synthese", summary="Nombre d'articles et de nouveautés par zone (coloration de la carte)")
def synthese(requete: Request, theme: Optional[str] = None, themes: Optional[str] = None,
             langue: Optional[str] = None, jours: int = Query(30, ge=1, le=365),
             zones: bool = Query(False, description="ajouter les comptages par zone"),
             cx: sqlite3.Connection = Depends(connexion)):
    """Date de la dernière collecte, sources actives, et comptages.

    Les comptages PAR ZONE sont facultatifs (`zones=1`) : plus rien sur le site ne les
    utilise depuis que la carte à points a remplacé celle des six zones, et ils coûtent un
    balayage complet de la fenêtre — 80 ms sur 200 000 articles pour une donnée que
    personne ne lit.
    """
    codes = themes_demandes(theme, themes)
    where, params = filtre_sql(None, None, jours, themes=codes)
    ou_lg, p_lg = base.langue_sql(verifier_langue(langue))
    where, params = where + ou_lg, [*params, *p_lg]
    seuil_nouveau = (maintenant() - base.DUREE_NOUVEAU).isoformat()
    sortie = {}
    if zones:
        lignes = cx.execute(
            f"""SELECT zone, COUNT(*) AS total, SUM(ajout >= ?) AS nouveaux
                FROM fil WHERE {where} GROUP BY zone""", [seuil_nouveau, *params]).fetchall()
        par_zone = {code: {"total": 0, "nouveaux": 0} for code in base.ZONES}
        for r in lignes:
            par_zone[r["zone"]] = {"total": r["total"], "nouveaux": r["nouveaux"] or 0}
        sortie["zones"] = par_zone
    derniere = cx.execute("SELECT fin FROM collectes WHERE fin IS NOT NULL AND hors_ligne = 0 "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    comptes = cx.execute(
        f"SELECT source, COUNT(*) n FROM fil WHERE {where} GROUP BY source ORDER BY n DESC",
        params).fetchall()
    if not derniere:   # pas encore de collecte en ligne : date du dernier ajout en base
        derniere = cx.execute("SELECT MAX(ajout) AS fin FROM articles").fetchone()
    return avec_cache({"mise_a_jour": derniere["fin"] if derniere else None,
                       "total": sum(r["n"] for r in comptes),
                       "sources": [r["source"] for r in comptes], **sortie},
                      CACHE_FIL, requete)


@app.get("/api/articles", summary="Fil d'actualité filtré, du plus récent au plus ancien")
def articles(requete: Request, zone: Optional[str] = None, theme: Optional[str] = None,
             themes: Optional[str] = None, langue: Optional[str] = None,
             jours: int = Query(30, ge=1, le=365),
             limite: int = Query(150, ge=1, le=500), decalage: int = Query(0, ge=0),
             grouper: bool = Query(True, description="une ligne par événement plutôt que par article"),
             cx: sqlite3.Connection = Depends(connexion)):
    """Le fil, du plus récent au plus ancien.

    Par défaut une ligne par ÉVÉNEMENT : la même dépêche reprise par cinq médias n'encombre
    pas la liste cinq fois, et `nb_sources` dit combien de médias la relaient. `grouper=false`
    rend la liste article par article, comme avant le regroupement.
    """
    verifier(zone, None)
    codes = themes_demandes(theme, themes)
    langue = verifier_langue(langue)
    seuil_nouveau = (maintenant() - base.DUREE_NOUVEAU).isoformat()

    if not grouper:
        where, params = filtre_sql(zone, None, jours, themes=codes)
        ou_lg, p_lg = base.langue_sql(langue)
        where, params = where + ou_lg, [*params, *p_lg]
        total, nouveaux = cx.execute(
            f"SELECT COUNT(*), COALESCE(SUM(ajout >= ?), 0) FROM fil WHERE {where}",
            [seuil_nouveau, *params]).fetchone()
        lignes = cx.execute(
            f"""SELECT id, titre, source, date_pub AS date, zone, theme, lien, evenement_id,
                       ajout >= ? AS nouveau
                FROM fil WHERE {where} ORDER BY date_pub DESC LIMIT ? OFFSET ?""",
            [seuil_nouveau, *params, limite, decalage]).fetchall()
        return avec_cache({"total": total, "nouveaux": nouveaux, "decalage": decalage,
                           "groupe": False,
                           "articles": [dict(r, nouveau=bool(r["nouveau"])) for r in lignes]},
                          CACHE_FIL, requete)

    # Vue groupée. Tout se lit dans la table des événements, trois fois plus petite que
    # celle des articles, en TROIS requêtes quel que soit le nombre de lignes affichées :
    # les compteurs, la page d'événements, puis l'article représentatif de chacun.
    #
    # La première version faisait une requête par événement affiché, soit 150 allers-retours
    # par page : 561 ms au 95e centile sur 200 000 articles, plus lent que l'ancien fil non
    # groupé. C'est la forme ci-dessous qui est mesurée dans bancs/bench_api.py.
    ou_evt, p_evt = filtre_sql(zone, None, jours, "e.", "date_fin", themes=codes)
    ou_lg, p_lg = base.langue_sql(langue, "e.")
    ou_evt, p_evt = ou_evt + ou_lg, [*p_evt, *p_lg]
    # nb_sources dans la clause WHERE, et pas un filtre après coup : un événement dont
    # toutes les sources ont été masquées ne doit ni compter dans le total, ni occuper une
    # place dans la page — sinon la pagination renverrait des pages trop courtes.
    ou_evt += " AND e.nb_sources > 0"

    # e.maj est posé à chaque rattachement d'article : « modifié depuis moins de 48 h »
    # revient donc à « a reçu un article récemment », sans sous-requête par événement.
    total_evt, nouveaux_evt, total_articles = cx.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(e.maj >= ?), 0), COALESCE(SUM(e.nb_sources), 0)
            FROM evenements e WHERE {ou_evt}""", [seuil_nouveau, *p_evt]).fetchone()

    evts = cx.execute(
        f"""SELECT e.id, e.lieu, e.lat, e.lon, e.zone, e.theme, e.nb_sources
            FROM evenements e WHERE {ou_evt}
            ORDER BY e.date_fin DESC LIMIT ? OFFSET ?""",
        [*p_evt, limite, decalage]).fetchall()

    representants = {}
    # l'article mis en avant doit être dans la langue demandée, sinon on afficherait
    # un titre anglais sur une page filtrée en français
    ou_art, p_art = base.langue_sql(langue)
    if evts:
        trous = ", ".join("?" * len(evts))
        # la fenêtre glissante ne porte que sur les articles de ces événements-là,
        # quelques centaines de lignes, et non sur toute la table
        for r in cx.execute(
                f"""SELECT evenement_id, id, titre, source, date_pub AS date, lien, nouveau FROM (
                      SELECT evenement_id, id, titre, source, date_pub, lien, ajout >= ? AS nouveau,
                             ROW_NUMBER() OVER (PARTITION BY evenement_id
                                                ORDER BY date_pub DESC) rn
                      FROM fil WHERE evenement_id IN ({trous}){ou_art}) WHERE rn = 1""",
                [seuil_nouveau, *[e["id"] for e in evts], *p_art]):
            representants[r["evenement_id"]] = r

    sortie = []
    for e in evts:
        r = representants.get(e["id"])
        if r is None:
            continue
        sortie.append({"id": r["id"], "titre": r["titre"], "source": r["source"],
                       "date": r["date"], "lien": r["lien"], "nouveau": bool(r["nouveau"]),
                       "evenement_id": e["id"], "nb_sources": e["nb_sources"],
                       "lieu": e["lieu"], "lat": e["lat"], "lon": e["lon"],
                       "zone": e["zone"], "theme": e["theme"]})
    return avec_cache({"total": total_evt, "nouveaux": nouveaux_evt,
                       "total_articles": total_articles,
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
def evenements(requete: Request, theme: Optional[str] = None, themes: Optional[str] = None,
               langue: Optional[str] = None, zone: Optional[str] = None,
               jours: int = Query(30, ge=1, le=365), bbox: Optional[str] = None,
               limite: int = Query(500, ge=1, le=2000),
               cx: sqlite3.Connection = Depends(connexion)):
    """Les événements d'une fenêtre géographique et d'une période.

    Un événement regroupe les articles de plusieurs médias qui parlent de la même chose au
    même endroit : il ne fait qu'un point sur la carte. Les plus suivis (le plus de sources)
    sont renvoyés en premier, pour qu'un plafond de résultats ne coupe pas l'essentiel.
    """
    verifier(zone, None)
    codes = themes_demandes(theme, themes)
    where, params = filtre_sql(zone, None, jours, "e.", "date_fin", themes=codes)
    ou_lg, p_lg = base.langue_sql(verifier_langue(langue), "e.")
    where, params = where + ou_lg, [*params, *p_lg]
    where += " AND e.lat IS NOT NULL"
    sql_bbox, p_bbox = bbox_sql(bbox, "e.")
    where += sql_bbox
    params += p_bbox
    # nb_sources est recalculé depuis la vue fil : un article masqué ou non classé ne doit
    # pas gonfler le compteur affiché au lecteur
    where += " AND e.nb_sources > 0"
    lignes = cx.execute(
        # zone et date_debut ne servent qu'au panneau de détail : inutile de les répéter
        # sur 600 points, le poids de la réponse compte plus que l'exhaustivité ici
        f"""SELECT e.id, e.titre, e.lieu, e.lat, e.lon, e.theme, e.date_fin, e.nb_sources,
                   e.langues
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


# ---------------------------------------------------------------------------
# Mises à jour en quasi temps réel
# ---------------------------------------------------------------------------
INTERVALLE_DIRECT = 10        # secondes entre deux vérifications côté serveur
BATTEMENT_DIRECT = 25         # secondes : un commentaire SSE pour tenir la connexion ouverte


def _empreinte(chemin: Path) -> tuple:
    """Ce qui permet de dire « quelque chose a changé » : la dernière collecte terminée et
    le nombre d'articles. Deux lectures indexées, quelques dizaines de microsecondes."""
    if not chemin.exists():
        return (None, 0)
    cx = sqlite3.connect(Path(chemin).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        fin = cx.execute("SELECT MAX(fin) FROM collectes").fetchone()[0]
        n = cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return (fin, n)
    finally:
        cx.close()


@app.get("/api/direct", summary="Flux d'événements : signale qu'une collecte a eu lieu")
async def direct(requete: Request):
    """Server-Sent Events. Le serveur ne pousse pas les articles, seulement un signal :
    le navigateur recharge alors ce qu'il affiche, avec ses propres filtres. C'est plus
    simple qu'un flux de données, et cela évite d'envoyer à chacun ce qu'il ne regarde pas.

    Repli automatique côté navigateur : si EventSource échoue (proxy qui coupe, réseau
    capricieux), le site repasse à une interrogation légère toutes les deux minutes.
    """
    chemin = Path(BASE)

    async def flux():
        dernier = await run_in_threadpool(_empreinte, chemin)
        yield f"event: bonjour\ndata: {json.dumps({'mise_a_jour': dernier[0]})}\n\n"
        depuis_battement = 0.0
        while not await requete.is_disconnected():
            await asyncio.sleep(INTERVALLE_DIRECT)
            depuis_battement += INTERVALLE_DIRECT
            etat = await run_in_threadpool(_empreinte, chemin)
            if etat != dernier:
                dernier = etat
                yield (f"event: collecte\ndata: "
                       f"{json.dumps({'mise_a_jour': etat[0], 'articles': etat[1]})}\n\n")
                depuis_battement = 0.0
            elif depuis_battement >= BATTEMENT_DIRECT:
                yield ": battement\n\n"   # garde la connexion vivante à travers les proxys
                depuis_battement = 0.0

    return StreamingResponse(flux(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",      # nginx ne doit pas tamponner un flux d'événements
        # le middleware gzip saute les réponses qui déclarent déjà un encodage : sans cela
        # il tamponnerait le flux et les messages n'arriveraient que par paquets
        "Content-Encoding": "identity",
    })


@app.get("/api/recap", summary="Récapitulatif quotidien par thème, écrit par un modèle de langage")
def recap(requete: Request, jour: Optional[str] = None, theme: Optional[str] = None,
          themes: Optional[str] = None, cx: sqlite3.Connection = Depends(connexion)):
    """Les récapitulatifs déjà écrits et rangés en base. Cette route ne calcule rien et
    n'appelle aucun service extérieur : c'est la collecte qui les écrit.

    Sans `jour`, la journée la plus récente qui en a — en début de matinée, c'est la veille.
    """
    codes = themes_demandes(theme, themes)
    if jour is not None:
        if len(jour) != 10 or jour[4] != "-" or jour[7] != "-" or not jour.replace("-", "").isdigit():
            raise HTTPException(422, f"Date attendue au format AAAA-MM-JJ, reçu : {jour}")
    else:
        jour = base.dernier_jour_avec_recap(cx, codes)
    if not jour:
        return avec_cache({"jour": None, "recaps": []}, CACHE_FIL, requete)
    noms = {t["code"]: t["nom"] for t in base.THEMES_DECLARES}
    rang = {t["code"]: i for i, t in enumerate(base.THEMES_DECLARES)}   # ordre d'affichage
    recaps = [{"theme": r["theme"], "nom": noms.get(r["theme"], r["theme"]),
               "texte": r["texte"], "nb_articles": r["nb_articles"],
               "modele": r["modele"], "maj": r["maj"]}
              for r in sorted(base.lister_recaps(cx, jour, codes),
                              key=lambda r: rang.get(r["theme"], 10**6))]
    return avec_cache({"jour": jour, "recaps": recaps}, CACHE_FIL, requete)


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
@app.get("/theme/{code}", include_in_schema=False)
def page_theme(code: str):
    """Page d'un thème. Le serveur rend la même page ; c'est le navigateur qui lit l'adresse
    et pose le filtre. Une adresse par thème se partage et s'ouvre directement."""
    if code not in base.THEMES_ACTIFS:
        raise HTTPException(404, f"Thème inconnu : {code}")
    return FileResponse(SITE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def accueil():
    # revalidation systématique : la page est petite, et une version figée dans le cache du
    # navigateur serait invisible à la mise à jour suivante. Starlette y joint un ETag.
    return FileResponse(SITE / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=SITE), name="site")
