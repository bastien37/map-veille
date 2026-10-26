"""
MAP – Stockage SQLite (étape 4 de la chaîne de l'information).

La base donnees/map.sqlite est la seule source des données : elle garde TOUT l'historique
(archivage, D05), les articles à trier (D04), les corrections manuelles et le journal des collectes.
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
DUREE_FIL = timedelta(days=30)   # D05 : le fil affiche 30 jours, la base garde tout
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

VERSION_SCHEMA = 2   # v2 : colonne articles.masque (modération depuis l'administration)
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
    nb_doublons INTEGER NOT NULL DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_articles_date  ON articles(date_pub);
CREATE INDEX IF NOT EXISTS idx_articles_titre ON articles(titre_norm);
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
            for instruction in _instructions(VUES):
                cx.execute(instruction)
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


# ---------------------------------------------------------------------------
# Enregistrement d'un article
# ---------------------------------------------------------------------------
def enregistrer(cx, a: dict, titre_norm: str, maintenant: datetime, collecte_id: int) -> str:
    """
    Enregistre un article trié. Renvoie 'nouveau', 'maj', 'inchange' ou 'doublon'.
      - lien déjà connu : on garde la date d'ajout ; on ne complète le classement
        que s'il manquait, et jamais si l'article a été corrigé à la main ;
      - titre déjà connu sous un autre lien (même dépêche reprise ailleurs) : doublon (D03).
    """
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


def maintenant_utc() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(microsecond=0)
