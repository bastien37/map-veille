"""
MAP – Interface d'administration (page /admin et routes /api/admin/*).

Accès protégé par un mot de passe (authentification HTTP Basic, utilisateur « admin »),
défini par la variable d'environnement MAP_ADMIN_MOT_DE_PASSE. Sans cette variable,
l'administration est désactivée. En production, servir le site en HTTPS : en HTTP simple,
le mot de passe circule en clair.
"""
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import base

RACINE = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "admin.html"
UTILISATEUR = "admin"

securite = HTTPBasic(realm="Administration MAP")
routeur = APIRouter()


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------
def administrateur(identifiants: HTTPBasicCredentials = Depends(securite)) -> str:
    attendu = os.environ.get("MAP_ADMIN_MOT_DE_PASSE", "")
    if not attendu:
        raise HTTPException(503, "Administration désactivée : définissez la variable "
                                 "d'environnement MAP_ADMIN_MOT_DE_PASSE puis redémarrez le serveur.")
    ok_nom = secrets.compare_digest(identifiants.username.encode(), UTILISATEUR.encode())
    ok_mdp = secrets.compare_digest(identifiants.password.encode(), attendu.encode())
    if not (ok_nom and ok_mdp):
        raise HTTPException(401, "Identifiant ou mot de passe incorrect.",
                            headers={"WWW-Authenticate": 'Basic realm="Administration MAP"'})
    return identifiants.username


def chemin_base() -> Path:
    from serveur import app   # lu à chaque appel : les tests peuvent changer la base
    return Path(app.BASE)


def connexion_ecriture():
    # multi_thread : FastAPI ouvre cette dépendance et exécute la route dans deux threads différents
    cx = base.ouvrir(chemin_base(), multi_thread=True)   # crée ou migre la base si besoin
    try:
        yield cx
    finally:
        cx.close()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@routeur.get("/admin", include_in_schema=False)
def page_admin(_: str = Depends(administrateur)):
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Tableau de bord
# ---------------------------------------------------------------------------
@routeur.get("/api/admin/tableau", tags=["administration"], summary="Chiffres clés de la base")
def tableau(_: str = Depends(administrateur), cx=Depends(connexion_ecriture)):
    limite = (base.maintenant_utc() - base.DUREE_FIL).isoformat()
    un = lambda sql, *p: cx.execute(sql, p).fetchone()[0]   # noqa: E731
    croise = {z: {t: 0 for t in base.THEMES} for z in base.ZONES}
    for r in cx.execute("SELECT zone, theme, COUNT(*) n FROM fil WHERE date_pub >= ? GROUP BY zone, theme",
                        (limite,)):
        croise[r["zone"]][r["theme"]] = r["n"]
    sources = [dict(r) for r in cx.execute(
        """SELECT source, COUNT(*) AS total, SUM(date_pub >= ?) AS recents, MAX(date_pub) AS dernier
           FROM articles WHERE masque = 0 GROUP BY source ORDER BY recents DESC, total DESC LIMIT 25""",
        (limite,))]
    return {
        "compteurs": {
            "total": un("SELECT COUNT(*) FROM articles"),
            "fil": un("SELECT COUNT(*) FROM fil WHERE date_pub >= ?", limite),
            "a_trier": un("SELECT COUNT(*) FROM a_trier"),
            "manuels": un("SELECT COUNT(*) FROM articles WHERE manuel = 1"),
            "masques": un("SELECT COUNT(*) FROM articles WHERE masque = 1"),
        },
        "zones": base.ZONES, "themes": base.THEMES,
        "croise": croise,
        "sources": sources,
    }


# ---------------------------------------------------------------------------
# Articles : recherche, classement manuel, masquage
# ---------------------------------------------------------------------------
@routeur.get("/api/admin/articles", tags=["administration"], summary="Liste des articles avec filtres")
def articles(statut: str = Query("tous", pattern="^(tous|a_trier|publies|masques|manuels)$"),
             q: Optional[str] = None, zone: Optional[str] = None, theme: Optional[str] = None,
             limite: int = Query(50, ge=1, le=200), decalage: int = Query(0, ge=0),
             _: str = Depends(administrateur), cx=Depends(connexion_ecriture)):
    clauses, params = [], []
    clauses.append({"tous": "1", "a_trier": "(zone IS NULL OR theme IS NULL) AND masque = 0",
                    "publies": "zone IS NOT NULL AND theme IS NOT NULL AND masque = 0",
                    "masques": "masque = 1", "manuels": "manuel = 1"}[statut])
    if q:
        from collect import normaliser
        clauses.append("titre_norm LIKE ?")
        params.append(f"%{normaliser(q).strip()}%")
    for champ, valeur, ref in (("zone", zone, base.ZONES), ("theme", theme, base.THEMES)):
        if valeur == "aucun":
            clauses.append(f"{champ} IS NULL")
        elif valeur:
            if valeur not in ref:
                raise HTTPException(422, f"Code inconnu : {valeur}")
            clauses.append(f"{champ} = ?")
            params.append(valeur)
    where = " AND ".join(clauses)
    total = cx.execute(f"SELECT COUNT(*) FROM articles WHERE {where}", params).fetchone()[0]
    lignes = cx.execute(
        f"""SELECT id, titre, source, date_pub AS date, zone, theme, lien, manuel, masque, ajout
            FROM articles WHERE {where} ORDER BY date_pub DESC LIMIT ? OFFSET ?""",
        [*params, limite, decalage]).fetchall()
    return {"total": total, "articles": [dict(r, manuel=bool(r["manuel"]), masque=bool(r["masque"]))
                                         for r in lignes]}


class Modification(BaseModel):
    zone: Optional[str] = None
    theme: Optional[str] = None
    masque: Optional[bool] = None


@routeur.patch("/api/admin/articles/{article_id}", tags=["administration"],
               summary="Classer un article à la main, le masquer ou le réafficher")
def modifier(article_id: int, m: Modification, _: str = Depends(administrateur),
             cx=Depends(connexion_ecriture)):
    ligne = cx.execute("SELECT zone, theme FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not ligne:
        raise HTTPException(404, f"Aucun article n° {article_id}.")
    maintenant = base.maintenant_utc()
    if m.zone is not None or m.theme is not None:
        zone, theme = m.zone or ligne["zone"], m.theme or ligne["theme"]
        try:
            base.classer_a_la_main(cx, article_id, zone, theme, maintenant)
        except ValueError as e:
            raise HTTPException(422, str(e))
    if m.masque is not None:
        base.masquer(cx, article_id, m.masque, maintenant)
    r = cx.execute("SELECT id, titre, source, date_pub AS date, zone, theme, lien, manuel, masque, ajout "
                   "FROM articles WHERE id = ?", (article_id,)).fetchone()
    return dict(r, manuel=bool(r["manuel"]), masque=bool(r["masque"]))


# ---------------------------------------------------------------------------
# Sources : liste, ajout, modification, suppression, test
# ---------------------------------------------------------------------------
class SourceSaisie(BaseModel):
    nom: Optional[str] = None
    type: Optional[str] = None
    url: Optional[str] = None
    actif: Optional[bool] = None
    parametres: Optional[dict] = None
    notes: Optional[str] = None


def _avec_statistiques(cx, sources: list) -> list:
    """Ajoute à chaque source son dernier passage de collecte et son nombre d'articles."""
    limite = (base.maintenant_utc() - base.DUREE_FIL).isoformat()
    for s in sources:
        dernier = cx.execute(
            """SELECT cs.statut, cs.nb, cs.message, c.debut AS date
               FROM collectes_sources cs JOIN collectes c ON c.id = cs.collecte_id
               WHERE cs.source = ? AND c.hors_ligne = 0 ORDER BY c.id DESC LIMIT 1""", (s["nom"],)).fetchone()
        s["dernier_passage"] = dict(dernier) if dernier else None
        s["articles"], s["articles_30j"] = cx.execute(
            "SELECT COUNT(*), COALESCE(SUM(date_pub >= ?), 0) FROM articles WHERE source = ?",
            (limite, s["nom"])).fetchone()
        s.pop("fichier_test", None)
    return sources


@routeur.get("/api/admin/sources", tags=["administration"], summary="Sources et leur état")
def sources(_: str = Depends(administrateur), cx=Depends(connexion_ecriture)):
    return {"types": base.TYPES_SOURCES, "themes": base.THEMES, "niveaux_gdacs": base.NIVEAUX_GDACS,
            "sources": _avec_statistiques(cx, base.lister_sources(cx))}


@routeur.post("/api/admin/sources", tags=["administration"], status_code=201, summary="Ajouter une source")
def ajouter_source(saisie: SourceSaisie, _: str = Depends(administrateur), cx=Depends(connexion_ecriture)):
    try:
        s = base.ajouter_source(cx, saisie.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _avec_statistiques(cx, [s])[0]


@routeur.patch("/api/admin/sources/{source_id}", tags=["administration"],
               summary="Modifier une source (nom, adresse, paramètres, activation)")
def modifier_source(source_id: int, saisie: SourceSaisie, _: str = Depends(administrateur),
                    cx=Depends(connexion_ecriture)):
    try:
        s = base.modifier_source(cx, source_id, saisie.model_dump(exclude_none=True))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _avec_statistiques(cx, [s])[0]


@routeur.delete("/api/admin/sources/{source_id}", tags=["administration"], status_code=204,
                summary="Supprimer une source (ses articles déjà collectés sont conservés)")
def supprimer_source(source_id: int, _: str = Depends(administrateur), cx=Depends(connexion_ecriture)):
    if not base.supprimer_source(cx, source_id):
        raise HTTPException(404, f"Aucune source n° {source_id}.")


@routeur.post("/api/admin/sources/tester", tags=["administration"],
              summary="Interroger une source sans rien enregistrer (aperçu et classement)")
def tester_source(saisie: SourceSaisie, _: str = Depends(administrateur)):
    import collect
    try:
        s = base.valider_source(saisie.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ref = collect.referentiels()
    try:
        lot = collect.collecter_source(base.config_collecte(s), False, ref)
    except Exception as e:   # adresse fausse, site injoignable, flux illisible…
        return {"ok": False, "message": str(e), "total": 0, "a_trier": 0, "apercu": []}
    articles = [collect.trier(a, ref) for a in lot if a["titre"] and a["lien"].startswith("http")]
    a_trier = sum(1 for a in articles if not a["zone"] or not a["theme"])
    apercu = [{"titre": a["titre"], "lien": a["lien"], "date": a["date"].isoformat() if a["date"] else None,
               "zone": a["zone"], "theme": a["theme"]} for a in articles[:10]]
    return {"ok": True, "total": len(articles), "a_trier": a_trier, "apercu": apercu,
            "message": f"{len(articles)} articles lus, dont {a_trier} que le tri ne sait pas classer."}


# ---------------------------------------------------------------------------
# Collectes : journal et lancement à la demande
# ---------------------------------------------------------------------------
_collecte = {"en_cours": False, "debut": None, "fin": None, "code": None, "sortie": ""}
_verrou = threading.Lock()


def _executer_collecte(hors_ligne: bool):
    commande = [sys.executable, str(RACINE / "collecteur" / "collect.py"), "--base", str(chemin_base())]
    if hors_ligne:
        commande.append("--hors-ligne")
    try:
        p = subprocess.run(commande, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900, cwd=RACINE)
        _collecte.update(code=p.returncode, sortie=(p.stdout + p.stderr).strip())
    except subprocess.TimeoutExpired:
        _collecte.update(code=-1, sortie="Collecte interrompue : plus de 15 minutes.")
    finally:
        _collecte.update(en_cours=False, fin=base.maintenant_utc().isoformat())
        _verrou.release()


@routeur.post("/api/admin/collecte", tags=["administration"], status_code=202,
              summary="Lancer une collecte maintenant (en arrière-plan)")
def lancer_collecte(hors_ligne: bool = False, _: str = Depends(administrateur)):
    if not _verrou.acquire(blocking=False):
        raise HTTPException(409, "Une collecte est déjà en cours.")
    _collecte.update(en_cours=True, debut=base.maintenant_utc().isoformat(), fin=None, code=None, sortie="")
    threading.Thread(target=_executer_collecte, args=(hors_ligne,), daemon=True).start()
    return _collecte


@routeur.get("/api/admin/collecte", tags=["administration"], summary="État de la collecte lancée")
def etat_collecte(_: str = Depends(administrateur)):
    return _collecte


@routeur.get("/api/admin/collectes", tags=["administration"], summary="Journal détaillé des collectes")
def journal(limite: int = Query(15, ge=1, le=100), _: str = Depends(administrateur),
            cx=Depends(connexion_ecriture)):
    sortie = []
    for c in cx.execute("SELECT * FROM collectes ORDER BY id DESC LIMIT ?", (limite,)).fetchall():
        sources = [dict(s) for s in cx.execute(
            "SELECT source, statut, nb, message FROM collectes_sources WHERE collecte_id = ? ORDER BY source",
            (c["id"],))]
        sortie.append({**dict(c), "hors_ligne": bool(c["hors_ligne"]), "sources": sources})
    return sortie
