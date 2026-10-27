"""
MAP – Stockage SQLite (étape 4 de la chaîne de l'information).

La base donnees/map.sqlite est la seule source des données : les articles des 30 derniers jours
(D05 : les plus anciens sont supprimés à chaque collecte), les articles à trier (D04),
les corrections manuelles, les sources et le journal des collectes.
Le front ne contient aucune donnée : il les demande à l'API (serveur/app.py), qui lit cette base.

Module standard sqlite3 : aucune bibliothèque à installer.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
BASE = RACINE / "donnees" / "map.sqlite"
ANCIEN_JSON = RACINE / "site" / "data.json"   # ancien format, lu une seule fois pour la migration
DUREE_FIL = timedelta(days=30)   # D05 : durée de conservation ; au-delà, l'article est supprimé à la collecte
DUREE_NOUVEAU = timedelta(hours=48)   # F06 : repère « Nouveau »

ZONES = {
    "eur": "Europe",
    "rus": "Russie et Europe de l'Est",
    "mena": "Moyen-Orient et Afrique du Nord",
    "afr": "Afrique subsaharienne",
    "asi": "Asie et Indo-Pacifique",
    "ame": "Amériques",
}
THEMES = {
    "def": "Conflits et défense",
    "dip": "Diplomatie et relations internationales",
    "eco": "Économie et énergie",
    "tec": "Sciences, technologies et espace",
    "cli": "Climat et catastrophes naturelles",
}

VERSION_SCHEMA = 5   # v2 : articles.masque ; v3 : table sources ; v4 : collectes.nb_supprimes ;
                    # v5 : index couvrants idx_articles_zone et idx_articles_source

TYPES_SOURCES = {
    "rss": "Flux RSS / Atom",
    "gdacs": "API GDACS (alertes catastrophes)",
    "reliefweb": "API ReliefWeb (rapports humanitaires)",
}
NIVEAUX_GDACS = ("green", "orange", "red")

# Sources installées à la création de la base (ou à la migration vers v3).
# Ensuite, la liste se gère uniquement depuis l'administration (onglet Sources).
SOURCES_INITIALES = [
    {"nom": "Le Monde", "type": "rss", "url": "https://www.lemonde.fr/international/rss_full.xml", "actif": True},
    {"nom": "France 24", "type": "rss", "url": "https://www.france24.com/fr/rss", "actif": True},
    {"nom": "RFI", "type": "rss", "url": "https://www.rfi.fr/fr/rss", "actif": True},
    {"nom": "franceinfo", "type": "rss", "url": "https://www.francetvinfo.fr/monde.rss", "actif": True},
    {"nom": "Courrier international", "type": "rss",
     "url": "https://www.courrierinternational.com/feed/all/rss.xml", "actif": False},
    {"nom": "GDACS (ONU / Commission européenne)", "type": "gdacs", "actif": True,
     "parametres": {"niveaux": ["orange", "red"], "jours": 7}, "fichier_test": "tests/gdacs.json",
     "notes": "Alertes de catastrophes naturelles. Citer « Global Disaster Alert and Coordination System, GDACS »."},
    {"nom": "ReliefWeb (OCHA)", "type": "reliefweb", "actif": False,
     "parametres": {"theme_defaut": "dip"}, "fichier_test": "tests/reliefweb.json",
     "notes": "Nécessite un appname approuvé par ReliefWeb, dans la variable d'environnement RELIEFWEB_APPNAME."},
]
SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    code  TEXT PRIMARY KEY,
    nom   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS themes (
    code  TEXT PRIMARY KEY,
    nom   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collectes (
    id          INTEGER PRIMARY KEY,
    debut       TEXT NOT NULL,              -- ISO 8601 UTC
    fin         TEXT,
    hors_ligne  INTEGER NOT NULL DEFAULT 0,
    nb_lus      INTEGER NOT NULL DEFAULT 0, -- éléments reçus des sources
    nb_nouveaux INTEGER NOT NULL DEFAULT 0, -- articles ajoutés à la base
    nb_doublons INTEGER NOT NULL DEFAULT 0,
    nb_supprimes INTEGER NOT NULL DEFAULT 0 -- articles de plus de 30 jours supprimés (D05)
);
CREATE TABLE IF NOT EXISTS collectes_sources (
    collecte_id INTEGER NOT NULL REFERENCES collectes(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    statut      TEXT NOT NULL CHECK (statut IN ('ok', 'erreur')),
    nb          INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    PRIMARY KEY (collecte_id, source)
);
CREATE TABLE IF NOT EXISTS articles (
    id          INTEGER PRIMARY KEY,
    lien        TEXT NOT NULL UNIQUE,           -- D03 : un lien = un article
    titre       TEXT NOT NULL,
    titre_norm  TEXT NOT NULL,                  -- titre normalisé, pour repérer les doublons
    source      TEXT NOT NULL,
    date_pub    TEXT NOT NULL,                  -- ISO 8601 UTC
    zone        TEXT REFERENCES zones(code),    -- NULL = à trier (D04)
    theme       TEXT REFERENCES themes(code),   -- NULL = à trier (D04)
    manuel      INTEGER NOT NULL DEFAULT 0 CHECK (manuel IN (0, 1)),
    masque      INTEGER NOT NULL DEFAULT 0 CHECK (masque IN (0, 1)),  -- retiré du site par un administrateur
    ajout       TEXT NOT NULL,                  -- première collecte (repère « Nouveau »)
    maj         TEXT NOT NULL,                  -- dernière modification
    collecte_id INTEGER REFERENCES collectes(id)
);
CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY,
    nom          TEXT NOT NULL UNIQUE,           -- nom affiché sous chaque article
    type         TEXT NOT NULL CHECK (type IN ('rss', 'gdacs', 'reliefweb')),
    url          TEXT,                           -- obligatoire pour un flux RSS
    actif        INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),
    parametres   TEXT NOT NULL DEFAULT '{}',     -- JSON propre au type (niveaux GDACS, thème par défaut…)
    fichier_test TEXT,                           -- réponse enregistrée pour le mode --hors-ligne
    notes        TEXT,                           -- conditions d'utilisation, validation LT3…
    ajout        TEXT NOT NULL,
    maj          TEXT NOT NULL
);
"""

# Index créés APRÈS les migrations de colonnes : idx_articles_zone s'appuie sur « masque »,
# qu'une base v1 n'a pas encore quand le schéma est rejoué.
INDEX = """
CREATE INDEX IF NOT EXISTS idx_articles_date  ON articles(date_pub);
CREATE INDEX IF NOT EXISTS idx_articles_titre ON articles(titre_norm);
-- v5 : deux index « couvrants ». Ils portent toutes les colonnes lues par les comptages de
-- l'API, qui n'a donc plus à ouvrir la table : à 200 000 articles, la synthèse par zone passe
-- de 386 à 30 ms et par source de 371 à 22 ms (mesures : bancs/RAPPORT-phase1.md).
-- Ne pas ajouter d'index (zone, theme, …) en plus : le planificateur le préfère à tort et
-- le comptage filtré retombe de 2,9 à 8,5 ms.
CREATE INDEX IF NOT EXISTS idx_articles_zone   ON articles(zone, masque, date_pub, theme, ajout);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source, masque, date_pub, zone, theme);
"""

VUES = """
DROP VIEW IF EXISTS fil;
DROP VIEW IF EXISTS a_trier;

-- Articles publiables : zone ET thème connus, non masqués
CREATE VIEW fil AS
    SELECT * FROM articles WHERE zone IS NOT NULL AND theme IS NOT NULL AND masque = 0;

-- Articles que le tri automatique n'a pas su classer (hors articles masqués)
CREATE VIEW a_trier AS
    SELECT * FROM articles WHERE (zone IS NULL OR theme IS NULL) AND masque = 0;
"""


ATTENTE_VERROU = 30   # secondes d'attente si un autre programme écrit dans la base


def ouvrir(chemin=BASE, multi_thread: bool = False) -> sqlite3.Connection:
    """Ouvre la base. La crée ou la met à jour (migration) seulement si c'est nécessaire :
    une base déjà à jour est ouverte sans aucune écriture, ce qui évite les conflits de verrou
    entre le serveur, l'administration et le collecteur.

    multi_thread=True autorise l'usage de la connexion depuis un autre thread que celui qui l'a
    ouverte : nécessaire dans FastAPI, qui ouvre la dépendance et exécute la route dans deux
    threads différents. La connexion reste propre à une seule requête."""
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(chemin, timeout=ATTENTE_VERROU, check_same_thread=not multi_thread)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    if a_jour(cx):
        return cx
    cx.execute("PRAGMA journal_mode = WAL")   # l'API peut lire pendant que le collecteur écrit
    cx.execute("BEGIN IMMEDIATE")             # prend le verrou d'écriture d'un coup, puis attend s'il est pris
    try:
        if not a_jour(cx):                    # un autre programme a pu migrer pendant l'attente
            for instruction in _instructions(SCHEMA):
                cx.execute(instruction)
            colonnes = {r["name"] for r in cx.execute("PRAGMA table_info(articles)")}
            if "masque" not in colonnes:      # migration d'une base v1
                cx.execute("ALTER TABLE articles ADD COLUMN masque INTEGER NOT NULL DEFAULT 0 "
                           "CHECK (masque IN (0, 1))")
            if "nb_supprimes" not in {r["name"] for r in cx.execute("PRAGMA table_info(collectes)")}:
                cx.execute("ALTER TABLE collectes ADD COLUMN nb_supprimes INTEGER NOT NULL DEFAULT 0")   # v4
            for instruction in _instructions(INDEX):   # après les ALTER : les index les utilisent
                cx.execute(instruction)
            for instruction in _instructions(VUES):
                cx.execute(instruction)
            if not cx.execute("SELECT 1 FROM sources LIMIT 1").fetchone():   # migration vers v3
                for s in SOURCES_INITIALES:
                    _inserer_source(cx, s)
            cx.executemany("INSERT OR REPLACE INTO zones VALUES (?, ?)", ZONES.items())
            cx.executemany("INSERT OR REPLACE INTO themes VALUES (?, ?)", THEMES.items())
            cx.execute(f"PRAGMA user_version = {VERSION_SCHEMA}")
        cx.commit()
    except Exception:
        cx.rollback()
        cx.close()
        raise
    return cx


def a_jour(cx) -> bool:
    """Vrai si le schéma est à la bonne version et les référentiels identiques au code."""
    if cx.execute("PRAGMA user_version").fetchone()[0] != VERSION_SCHEMA:
        return False
    try:
        return (dict(cx.execute("SELECT code, nom FROM zones").fetchall()) == ZONES
                and dict(cx.execute("SELECT code, nom FROM themes").fetchall()) == THEMES)
    except sqlite3.OperationalError:
        return False


def _instructions(script: str):
    """Découpe un script SQL en instructions (sans commentaires), pour les exécuter
    dans une seule transaction (executescript ferait un COMMIT intermédiaire)."""
    sans_commentaires = "\n".join(l.split("--")[0] for l in script.splitlines())
    return [i.strip() for i in sans_commentaires.split(";") if i.strip()]


# ---------------------------------------------------------------------------
# Journal des collectes
# ---------------------------------------------------------------------------
def debut_collecte(cx, maintenant: datetime, hors_ligne: bool) -> int:
    cur = cx.execute("INSERT INTO collectes (debut, hors_ligne) VALUES (?, ?)",
                     (maintenant.isoformat(), int(hors_ligne)))
    cx.commit()   # ne pas garder le verrou d'écriture pendant que les sources sont interrogées
    return cur.lastrowid


def noter_source(cx, collecte_id: int, source: str, ok: bool, nb: int = 0, message: str = None):
    cx.execute("INSERT OR REPLACE INTO collectes_sources VALUES (?, ?, ?, ?, ?)",
               (collecte_id, source, "ok" if ok else "erreur", nb, message))
    cx.commit()


def fin_collecte(cx, collecte_id: int, fin: datetime, nb_lus: int, nb_nouveaux: int, nb_doublons: int):
    cx.execute("UPDATE collectes SET fin = ?, nb_lus = ?, nb_nouveaux = ?, nb_doublons = ? WHERE id = ?",
               (fin.isoformat(), nb_lus, nb_nouveaux, nb_doublons, collecte_id))
    cx.commit()


def purger(cx, maintenant: datetime, collecte_id: int = None) -> int:
    """D05 : supprime définitivement les articles publiés il y a plus de 30 jours
    (y compris ceux classés à la main ou masqués). Renvoie le nombre d'articles supprimés."""
    limite = (maintenant - DUREE_FIL).isoformat()
    n = cx.execute("DELETE FROM articles WHERE date_pub < ?", (limite,)).rowcount
    if collecte_id is not None:
        cx.execute("UPDATE collectes SET nb_supprimes = ? WHERE id = ?", (n, collecte_id))
    cx.commit()
    return n


# ---------------------------------------------------------------------------
# Enregistrement d'un article
# ---------------------------------------------------------------------------
def enregistrer(cx, a: dict, titre_norm: str, maintenant: datetime, collecte_id: int) -> str:
    """
    Enregistre un article trié. Renvoie 'nouveau', 'maj', 'inchange', 'doublon' ou 'ancien'.
      - publié il y a plus de 30 jours : pas enregistré, il serait supprimé à la collecte suivante (D05) ;
      - lien déjà connu : on garde la date d'ajout ; on ne complète le classement
        que s'il manquait, et jamais si l'article a été corrigé à la main ;
      - titre déjà connu sous un autre lien (même dépêche reprise ailleurs) : doublon (D03).
    """
    if a["date"] and a["date"] < maintenant - DUREE_FIL:
        return "ancien"
    ligne = cx.execute("SELECT id, zone, theme, manuel FROM articles WHERE lien = ?",
                       (a["lien"],)).fetchone()
    if ligne:
        if ligne["manuel"] or (ligne["zone"] and ligne["theme"]):
            return "inchange"
        zone, theme = ligne["zone"] or a["zone"], ligne["theme"] or a["theme"]
        if (zone, theme) == (ligne["zone"], ligne["theme"]):
            return "inchange"
        cx.execute("UPDATE articles SET zone = ?, theme = ?, maj = ? WHERE id = ?",
                   (zone, theme, maintenant.isoformat(), ligne["id"]))
        return "maj"

    if cx.execute("SELECT 1 FROM articles WHERE titre_norm = ? LIMIT 1", (titre_norm,)).fetchone():
        return "doublon"

    cx.execute(
        """INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme,
                                 ajout, maj, collecte_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (a["lien"], a["titre"], titre_norm, a["source"], (a["date"] or maintenant).isoformat(),
         a["zone"], a["theme"], maintenant.isoformat(), maintenant.isoformat(), collecte_id))
    return "nouveau"


def classer_a_la_main(cx, article_id: int, zone: str, theme: str, maintenant: datetime) -> bool:
    """Correction manuelle : le collecteur ne modifiera plus cet article."""
    if zone not in ZONES or theme not in THEMES:
        raise ValueError(f"zone ou thème inconnu : {zone} / {theme}")
    cur = cx.execute("UPDATE articles SET zone = ?, theme = ?, manuel = 1, maj = ? WHERE id = ?",
                     (zone, theme, maintenant.isoformat(), article_id))
    cx.commit()
    return cur.rowcount == 1


def masquer(cx, article_id: int, masque: bool, maintenant: datetime) -> bool:
    """Retire un article du site (ou l'y remet) sans le supprimer de la base."""
    cur = cx.execute("UPDATE articles SET masque = ?, maj = ? WHERE id = ?",
                     (int(masque), maintenant.isoformat(), article_id))
    cx.commit()
    return cur.rowcount == 1


def importer_json(cx, chemin, normaliser) -> int:
    """Migration : reprend les articles d'un ancien site/data.json (avant SQLite).
    Ignore les données d'exemple. Renvoie le nombre d'articles importés."""
    chemin = Path(chemin)
    if not chemin.exists():
        return 0
    try:
        d = json.loads(chemin.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if d.get("exemple"):
        return 0
    n = 0
    for a in d.get("articles", []):
        if a.get("zone") not in ZONES or a.get("theme") not in THEMES:
            continue
        cur = cx.execute(
            """INSERT OR IGNORE INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme,
                                               manuel, ajout, maj)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (a["lien"], a["titre"], normaliser(a["titre"]), a["source"], a["date"], a["zone"],
             a["theme"], int(bool(a.get("manuel"))), a.get("ajout", a["date"]), a.get("ajout", a["date"])))
        n += cur.rowcount
    cx.commit()
    return n


# ---------------------------------------------------------------------------
# Sources (gérées depuis l'administration)
# ---------------------------------------------------------------------------
def valider_source(d: dict) -> dict:
    """Contrôle et normalise une source saisie. Lève ValueError avec un message lisible."""
    nom = (d.get("nom") or "").strip()
    if not 1 <= len(nom) <= 80:
        raise ValueError("Le nom est obligatoire (80 caractères au plus).")
    type_ = d.get("type")
    if type_ not in TYPES_SOURCES:
        raise ValueError(f"Type inconnu : {type_}. Types possibles : {', '.join(TYPES_SOURCES)}.")
    url = (d.get("url") or "").strip() or None
    p = d.get("parametres") or {}
    parametres = {}
    if type_ == "rss":
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError("Un flux RSS a besoin d'une adresse commençant par http:// ou https://.")
    else:
        url = None   # l'adresse des API est fixée dans le collecteur
    if type_ == "gdacs":
        demandes = p["niveaux"] if "niveaux" in p else ["orange", "red"]   # liste vide = erreur, pas défaut
        niveaux = [n for n in NIVEAUX_GDACS if n in (demandes or [])]
        if not niveaux:
            raise ValueError("Choisissez au moins un niveau d'alerte GDACS.")
        try:
            jours = int(p.get("jours", 7))
        except (TypeError, ValueError):
            raise ValueError("La période GDACS doit être un nombre de jours.") from None
        if not 1 <= jours <= 30:
            raise ValueError("La période GDACS doit être comprise entre 1 et 30 jours.")
        parametres = {"niveaux": niveaux, "jours": jours}
    if type_ == "reliefweb":
        theme = p.get("theme_defaut", "dip")
        if theme not in THEMES:
            raise ValueError(f"Thème par défaut inconnu : {theme}.")
        parametres = {"theme_defaut": theme}
    notes = (d.get("notes") or "").strip()[:1000] or None
    return {"nom": nom, "type": type_, "url": url, "actif": bool(d.get("actif", True)),
            "parametres": parametres, "notes": notes, "fichier_test": d.get("fichier_test")}


def _inserer_source(cx, s: dict) -> int:
    maintenant = maintenant_utc().isoformat()
    cur = cx.execute(
        """INSERT INTO sources (nom, type, url, actif, parametres, fichier_test, notes, ajout, maj)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (s["nom"], s["type"], s.get("url"), int(bool(s.get("actif", True))),
         json.dumps(s.get("parametres") or {}, ensure_ascii=False), s.get("fichier_test"),
         s.get("notes"), maintenant, maintenant))
    return cur.lastrowid


def _source(ligne) -> dict:
    d = dict(ligne)
    d["actif"] = bool(d["actif"])
    d["parametres"] = json.loads(d["parametres"] or "{}")
    return d


def lister_sources(cx, actives: bool = False) -> list:
    sql = "SELECT * FROM sources" + (" WHERE actif = 1" if actives else "") + " ORDER BY type, nom COLLATE NOCASE"
    return [_source(r) for r in cx.execute(sql)]


def lire_source(cx, source_id: int):
    r = cx.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _source(r) if r else None


def config_collecte(s: dict) -> dict:
    """Forme attendue par les connecteurs du collecteur (type, nom, url, fichier_test, paramètres)."""
    conf = {"type": s["type"], "nom": s["nom"], "url": s.get("url"), "fichier_test": s.get("fichier_test")}
    p = s.get("parametres") or {}
    if s["type"] == "gdacs":
        conf["niveaux"] = ";".join(p.get("niveaux") or ["orange", "red"])
        conf["jours"] = p.get("jours", 7)
    if s["type"] == "reliefweb":
        conf["theme_defaut"] = p.get("theme_defaut", "dip")
    return conf


def ajouter_source(cx, d: dict) -> dict:
    s = valider_source(d)
    try:
        ident = _inserer_source(cx, s)
    except sqlite3.IntegrityError:
        raise ValueError(f"Une source s'appelle déjà « {s['nom']} ».") from None
    cx.commit()
    return lire_source(cx, ident)


def modifier_source(cx, source_id: int, d: dict) -> dict:
    """Met à jour une source. Un changement de nom est reporté sur ses articles et son journal."""
    ancienne = lire_source(cx, source_id)
    if not ancienne:
        raise LookupError(f"Aucune source n° {source_id}.")
    s = valider_source({**ancienne, **d, "parametres": {**ancienne["parametres"], **(d.get("parametres") or {})}})
    try:
        cx.execute(
            """UPDATE sources SET nom = ?, type = ?, url = ?, actif = ?, parametres = ?, notes = ?, maj = ?
               WHERE id = ?""",
            (s["nom"], s["type"], s["url"], int(s["actif"]), json.dumps(s["parametres"], ensure_ascii=False),
             s["notes"], maintenant_utc().isoformat(), source_id))
    except sqlite3.IntegrityError:
        cx.rollback()
        raise ValueError(f"Une source s'appelle déjà « {s['nom']} ».") from None
    if s["nom"] != ancienne["nom"]:
        cx.execute("UPDATE articles SET source = ? WHERE source = ?", (s["nom"], ancienne["nom"]))
        cx.execute("UPDATE collectes_sources SET source = ? WHERE source = ?", (s["nom"], ancienne["nom"]))
    cx.commit()
    return lire_source(cx, source_id)


def supprimer_source(cx, source_id: int) -> bool:
    """Supprime la source de la liste. Ses articles déjà collectés restent dans la base."""
    cur = cx.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    cx.commit()
    return cur.rowcount == 1


def maintenant_utc() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(microsecond=0)
