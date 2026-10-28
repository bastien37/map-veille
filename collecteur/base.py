"""
MAP – Stockage SQLite (étape 4 de la chaîne de l'information).

La base donnees/map.sqlite est la seule source des données : les articles des 30 derniers jours
(D05 : les plus anciens sont supprimés à chaque collecte), les articles à trier (D04),
les corrections manuelles, les sources et le journal des collectes.
Le front ne contient aucune donnée : il les demande à l'API (serveur/app.py), qui lit cette base.

Module standard sqlite3 : aucune bibliothèque à installer.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
BASE = RACINE / "donnees" / "map.sqlite"
ANCIEN_JSON = RACINE / "site" / "data.json"   # ancien format, lu une seule fois pour la migration
CONFIG = RACINE / "config"
THEMES_JSON = CONFIG / "themes.json"
GEOCODAGE_JSON = CONFIG / "geocodage.json"
CARTE_JSON = CONFIG / "carte.json"
FLUX_JSON = CONFIG / "flux.json"
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

LANGUES = ("fr", "en")   # langues gérées pour le tri et l'extraction de lieux
NOMS_LANGUES = {"fr": "Français", "en": "Anglais"}
FREQUENCE_DEFAUT = 60    # minutes entre deux interrogations d'une source


def lire_config(chemin: Path, cle: str) -> list:
    """Lit un fichier de configuration et renvoie sa liste. Une erreur de syntaxe doit être
    lisible : elle bloque toute la veille, autant dire tout de suite quel fichier revoir."""
    try:
        d = json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Fichier de configuration absent : {chemin}") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"{chemin.name} n'est pas un JSON valide : {e}") from None
    if not isinstance(d.get(cle), list):
        raise ValueError(f"{chemin.name} doit contenir une liste « {cle} ».")
    return d[cle]


def charger_themes(chemin: Path = None) -> list:
    """Thèmes déclarés dans config/themes.json, triés par « ordre ».
    Contrôle les codes : ils servent de clé en base et dans l'API."""
    themes, vus = [], set()
    for t in lire_config(chemin or THEMES_JSON, "themes"):
        code = (t.get("code") or "").strip()
        if not re.fullmatch(r"[a-z0-9]{2,8}", code):
            raise ValueError(f"Code de thème invalide : {code!r} (2 à 8 lettres ou chiffres, minuscules).")
        if code in vus:
            raise ValueError(f"Le code de thème « {code} » apparaît deux fois.")
        vus.add(code)
        nom = (t.get("nom") or "").strip()
        if not nom:
            raise ValueError(f"Le thème « {code} » n'a pas de nom.")
        mots = t.get("mots_cles") or []
        if not isinstance(mots, list) or not all(isinstance(m, str) for m in mots):
            raise ValueError(f"Les mots-clés du thème « {code} » doivent être une liste de textes.")
        themes.append({"code": code, "nom": nom, "actif": bool(t.get("actif", True)),
                       "ordre": int(t.get("ordre", 10 * (len(themes) + 1))), "mots_cles": mots})
    if not themes:
        raise ValueError(f"{(chemin or THEMES_JSON).name} ne déclare aucun thème.")
    themes.sort(key=lambda t: (t["ordre"], t["code"]))
    return themes


# Thèmes chargés une fois à l'import. THEMES garde la forme {code: nom} de toujours — tous les
# thèmes déclarés, actifs ou non, pour que les articles déjà classés restent valides. Les
# thèmes désactivés disparaissent seulement de l'affichage (THEMES_ACTIFS).
THEMES_DECLARES = charger_themes()
THEMES = {t["code"]: t["nom"] for t in THEMES_DECLARES}
THEMES_ACTIFS = [t["code"] for t in THEMES_DECLARES if t["actif"]]
REGLES_THEMES = {t["code"]: t["mots_cles"] for t in THEMES_DECLARES}

VERSION_SCHEMA = 15  # v2 : articles.masque ; v3 : table sources ; v4 : collectes.nb_supprimes ;
                    # v5 : index couvrants ; v6 : sources.etag, derniere_modif, echecs,
                    # pause_jusqu_a ; v7 : themes.actif/ordre, sources.langue/frequence/theme/
                    # origine ; v8 : articles.lat/lon/lieu, table lieux_geocodes ;
                    # v9 : table evenements, articles.evenement_id ;
                    # v10 : index couvrant idx_evenements_fil ;
                    # v11 : articles.langue, evenements.langues ;
                    # v12 : idx_evenements_carte porte aussi langues, lieu et titre ;
                    # v13 : articles.theme_ia, table themes_ia ;
                    # v14 : table recaps ;
                    # v15 : articles.theme_force, D04 assoupli (voir PUBLIABLE)

TYPES_SOURCES = {
    "rss": "Flux RSS / Atom",
    "gdacs": "API GDACS (alertes catastrophes)",
    "reliefweb": "API ReliefWeb (rapports humanitaires)",
}
NIVEAUX_GDACS = ("green", "orange", "red")

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    code  TEXT PRIMARY KEY,
    nom   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS themes (
    code  TEXT PRIMARY KEY,
    nom   TEXT NOT NULL,
    actif INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),  -- v7 : thème retiré du site
    ordre INTEGER NOT NULL DEFAULT 0                           -- v7 : ordre d'affichage
);
CREATE TABLE IF NOT EXISTS collectes (
    id          INTEGER PRIMARY KEY,
    debut       TEXT NOT NULL,              -- ISO 8601 UTC
    fin         TEXT,
    hors_ligne  INTEGER NOT NULL DEFAULT 0,
    nb_lus      INTEGER NOT NULL DEFAULT 0, -- éléments reçus des sources
    nb_nouveaux INTEGER NOT NULL DEFAULT 0, -- articles ajoutés à la base
    nb_doublons INTEGER NOT NULL DEFAULT 0, -- articles rattachés à un événement déjà connu
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
    collecte_id INTEGER REFERENCES collectes(id),
    -- v8 : position sur la carte. NULL = article non localisé : il reste dans la liste,
    -- mais n'apparaît pas sur la carte. Mieux vaut pas de point qu'un point faux.
    lat           REAL,
    lon           REAL,
    lieu          TEXT,                         -- nom du lieu retenu, affiché au survol
    lieu_origine  TEXT,                         -- flux | gazetteer | nominatim | photon
    langue        TEXT NOT NULL DEFAULT 'fr',   -- v11 : langue de la source
    -- v9 : événement dont cet article est une des sources
    evenement_id  INTEGER REFERENCES evenements(id) ON DELETE SET NULL
);
-- v9 : un événement regroupe les articles de plusieurs médias qui parlent de la même
-- chose, au même endroit, à peu près au même moment. Un événement = UN point sur la carte,
-- et un panneau qui liste ses sources.
CREATE TABLE IF NOT EXISTS evenements (
    id          INTEGER PRIMARY KEY,
    titre       TEXT NOT NULL,       -- titre de l'article le plus ancien du groupe
    titre_norm  TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    lieu        TEXT,
    zone        TEXT REFERENCES zones(code),
    theme       TEXT REFERENCES themes(code),
    date_debut  TEXT NOT NULL,       -- publication du plus ancien article
    date_fin    TEXT NOT NULL,       -- publication du plus récent
    -- nombre de sources PUBLIABLES (zone et thème connus, non masquées). Tenu à jour à
    -- chaque écriture plutôt que recalculé à la lecture : un EXISTS par événement coûtait
    -- 268 ms sur la vue mondiale (bancs/bench_carte.py).
    nb_sources  INTEGER NOT NULL DEFAULT 0,
    -- v11 : langues de ses sources publiables, entourées de virgules — ",fr," ou ",en,fr,".
    -- Les virgules encadrantes évitent qu'un code soit reconnu dans un autre : instr(…, ",fr,")
    -- ne peut pas accrocher un futur ",fra,".
    langues     TEXT NOT NULL DEFAULT '',
    maj         TEXT NOT NULL
);

-- v8 : cache de géocodage. Les conditions d'usage de Nominatim l'imposent, et de toute
-- façon un lieu ne doit jamais être demandé deux fois. Les échecs sont mémorisés aussi,
-- sans quoi les mêmes introuvables repartiraient à chaque collecte.
-- v14 : récapitulatif du jour écrit par le modèle de langage, un par thème. Stocké et
-- non recalculé à la demande : une page qui s'affiche ne doit jamais déclencher un appel
-- à un service extérieur, encore moins un par visiteur.
CREATE TABLE IF NOT EXISTS recaps (
    jour        TEXT NOT NULL,     -- AAAA-MM-JJ, en UTC comme tout le reste
    theme       TEXT NOT NULL,     -- code de thème
    texte       TEXT NOT NULL,
    modele      TEXT NOT NULL,
    nb_articles INTEGER NOT NULL,  -- articles résumés : sert à savoir s'il faut refaire
    maj         TEXT NOT NULL,
    PRIMARY KEY (jour, theme)
);

-- v13 : cache du classement par modèle de langage. Même règle que pour le géocodage :
-- une question posée une fois ne doit jamais être reposée. La clé contient le modèle et la
-- liste des thèmes, parce qu'en changer change la question.
CREATE TABLE IF NOT EXISTS themes_ia (
    cle     TEXT PRIMARY KEY,   -- empreinte du titre normalisé, du modèle et des thèmes
    titre   TEXT NOT NULL,      -- gardé pour pouvoir relire ce qui a été soumis
    theme   TEXT,               -- NULL = le modèle n'a pas su classer
    modele  TEXT NOT NULL,
    ajout   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lieux_geocodes (
    cle          TEXT PRIMARY KEY,   -- nom normalisé (minuscules, sans accents)
    nom          TEXT NOT NULL,      -- nom renvoyé par le service
    lat          REAL,               -- NULL = cherché et introuvable
    lon          REAL,
    origine      TEXT NOT NULL,
    ajout        TEXT NOT NULL,
    maj          TEXT NOT NULL,
    utilisations INTEGER NOT NULL DEFAULT 0
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
    maj          TEXT NOT NULL,
    -- v7 : décrits dans config/flux.json
    langue            TEXT NOT NULL DEFAULT 'fr',   -- sert au tri et à l'extraction des lieux
    frequence_minutes INTEGER NOT NULL DEFAULT 60,  -- délai minimal entre deux interrogations
    theme_impose      TEXT REFERENCES themes(code), -- NULL = thème décidé par les mots-clés
    origine           TEXT NOT NULL DEFAULT 'config' CHECK (origine IN ('config', 'admin')),
    dernier_succes    TEXT,                         -- dernière collecte réussie (fréquence)
    -- v6 : état de la source d'une collecte à l'autre
    etag           TEXT,                          -- en-tête ETag de la dernière réponse
    derniere_modif TEXT,                          -- en-tête Last-Modified de la dernière réponse
    echecs         INTEGER NOT NULL DEFAULT 0,    -- échecs consécutifs (disjoncteur)
    pause_jusqu_a  TEXT                           -- source écartée des collectes jusqu'à cette date
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
-- v8 : requêtes de la carte (points d'une fenêtre géographique sur une période). Mesuré
-- sur 200 000 articles : un index spatial dédié n'apporte rien à ce volume et dégrade même
-- la vue mondiale (bancs/bench_bbox.py) ; celui-ci suffit.
CREATE INDEX IF NOT EXISTS idx_articles_carte ON articles(masque, date_pub, lat, lon, theme);
-- v9 : regroupement. idx_evenements_fin sert à chercher les candidats dans la fenêtre de
-- temps ; idx_articles_evenement à lister les sources d'un événement.
CREATE INDEX IF NOT EXISTS idx_evenements_fin       ON evenements(date_fin);
-- v12 : index couvrant de la carte. Il porte TOUT ce que /api/evenements lit, langues
-- comprise : la requête ne touche plus la table. Avec un filtre de langue, 111 -> 25 ms sur
-- 66 000 événements ; sans filtre, 34 -> 21 ms. Coût : 1,8 Mio.
-- Le DROP précède le CREATE parce que « CREATE INDEX IF NOT EXISTS » ne refait pas un index
-- existant dont les colonnes ont changé : sans lui, une base déjà migrée garderait l'ancien.
DROP INDEX IF EXISTS idx_evenements_carte;
CREATE INDEX IF NOT EXISTS idx_evenements_carte     ON evenements(theme, date_fin, lat, lon,
                                                     nb_sources, langues, lieu, titre);
-- v10 : index couvrant du fil groupé. Les compteurs de /api/articles (nombre d'événements,
-- de nouveautés, d'articles) balayaient la fenêtre entière en ouvrant la table : 82 ms sur
-- 66 000 événements, contre 11 ms en lisant cet index seul.
CREATE INDEX IF NOT EXISTS idx_evenements_fil       ON evenements(theme, date_fin DESC,
                                                     nb_sources, maj, lieu, lat, lon, zone,
                                                     langues);
CREATE INDEX IF NOT EXISTS idx_articles_evenement   ON articles(evenement_id);
"""

# D04, révisé en v15. La règle d'origine — diffuser seulement ce qui a une zone ET un thème
# — datait de la carte des six zones, où un article sans zone n'avait littéralement nulle
# part où s'afficher. Depuis la carte à points, la zone n'apparaît plus sur le site et la
# carte ne montre que les articles localisés : l'exigence ne protégeait plus rien de visible.
#
# Elle coûtait cher, en revanche : sur 1 372 articles réels du 28/10/2026, 452 (un tiers)
# avaient un thème mais aucune zone et restaient invisibles — surtout les avis du CERT-FR et
# les notes d'instituts, qui ne nomment aucun lieu et n'en nommeront jamais.
#
# Un flux à thème imposé dit lui-même de quoi il parle : son thème ne se devine pas, il est
# déclaré. Exiger en plus une zone revenait à jeter ce qu'on savait déjà. Les flux
# généralistes, eux, gardent la règle : sans zone ni thème trouvés, rien ne garantit qu'un
# de leurs articles ait sa place quelque part.
PUBLIABLE = "theme IS NOT NULL AND (zone IS NOT NULL OR theme_force = 1)"

VUES = f"""
DROP VIEW IF EXISTS fil;
DROP VIEW IF EXISTS a_trier;

-- Articles publiables : un thème, et une zone ou un thème imposé par la source
CREATE VIEW fil AS
    SELECT * FROM articles WHERE {PUBLIABLE} AND masque = 0;

-- Articles que le tri automatique n'a pas su classer (hors articles masqués).
-- Exactement le complément de « fil » : les deux vues ne peuvent pas diverger.
CREATE VIEW a_trier AS
    SELECT * FROM articles WHERE NOT ({PUBLIABLE}) AND masque = 0;
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
            colonnes_sources = {r["name"] for r in cx.execute("PRAGMA table_info(sources)")}
            for nom, definition in (("etag", "TEXT"), ("derniere_modif", "TEXT"),      # v6
                                    ("echecs", "INTEGER NOT NULL DEFAULT 0"),
                                    ("pause_jusqu_a", "TEXT"),
                                    ("langue", "TEXT NOT NULL DEFAULT 'fr'"),          # v7
                                    ("frequence_minutes", f"INTEGER NOT NULL DEFAULT {FREQUENCE_DEFAUT}"),
                                    ("theme_impose", "TEXT REFERENCES themes(code)"),
                                    ("origine", "TEXT NOT NULL DEFAULT 'config'"),
                                    ("dernier_succes", "TEXT")):
                if nom not in colonnes_sources:
                    cx.execute(f"ALTER TABLE sources ADD COLUMN {nom} {definition}")
            for nom, definition in (("lat", "REAL"), ("lon", "REAL"),                  # v8
                                    ("lieu", "TEXT"), ("lieu_origine", "TEXT"),
                                    ("evenement_id", "INTEGER REFERENCES evenements(id)"),  # v9
                                    ("langue", "TEXT NOT NULL DEFAULT 'fr'"),          # v11
                                    ("theme_ia", "INTEGER NOT NULL DEFAULT 0"),      # v13
                                    ("theme_force", "INTEGER NOT NULL DEFAULT 0")):  # v15
                if nom not in colonnes:
                    cx.execute(f"ALTER TABLE articles ADD COLUMN {nom} {definition}")
            if "langues" not in {r["name"] for r in cx.execute("PRAGMA table_info(evenements)")}:
                cx.execute("ALTER TABLE evenements ADD COLUMN langues TEXT NOT NULL DEFAULT ''")
            for nom, definition in (("actif", "INTEGER NOT NULL DEFAULT 1"),           # v7
                                    ("ordre", "INTEGER NOT NULL DEFAULT 0")):
                if nom not in {r["name"] for r in cx.execute("PRAGMA table_info(themes)")}:
                    cx.execute(f"ALTER TABLE themes ADD COLUMN {nom} {definition}")
            for instruction in _instructions(INDEX):   # après les ALTER : les index les utilisent
                cx.execute(instruction)
            for instruction in _instructions(VUES):
                cx.execute(instruction)
            cx.executemany("INSERT OR REPLACE INTO zones VALUES (?, ?)", ZONES.items())
            appliquer_themes(cx)
            appliquer_flux(cx)
            if "theme_force" not in colonnes:
                # v15 : les articles déjà en base ne portent pas le drapeau. On le reconstitue
                # depuis la source, après appliquer_flux() pour que theme_impose soit à jour.
                # Sans cela, un tiers du fonds resterait invisible jusqu'à une recollecte qui
                # n'aura jamais lieu : un article déjà connu n'est pas réécrit.
                cx.execute("UPDATE articles SET theme_force = 1 WHERE theme IS NOT NULL "
                           "AND source IN (SELECT nom FROM sources "
                           "               WHERE theme_impose IS NOT NULL)")
            cx.execute(f"PRAGMA user_version = {VERSION_SCHEMA}")
        cx.commit()
    except Exception:
        cx.rollback()
        cx.close()
        raise
    return cx


def a_jour(cx) -> bool:
    """Vrai si le schéma est à la bonne version et les référentiels identiques à la
    configuration. Modifier config/themes.json suffit donc à déclencher une mise à jour
    de la base à la prochaine ouverture : rien à lancer à la main."""
    if cx.execute("PRAGMA user_version").fetchone()[0] != VERSION_SCHEMA:
        return False
    try:
        if dict(cx.execute("SELECT code, nom FROM zones").fetchall()) != ZONES:
            return False
        en_base = {r["code"]: (r["nom"], bool(r["actif"]), r["ordre"])
                   for r in cx.execute("SELECT code, nom, actif, ordre FROM themes")}
        attendu = {t["code"]: (t["nom"], t["actif"], t["ordre"]) for t in THEMES_DECLARES}
        # un thème retiré de la configuration reste en base (ses articles aussi) : on ne
        # compare donc que les thèmes déclarés
        return all(en_base.get(code) == valeur for code, valeur in attendu.items())
    except sqlite3.OperationalError:
        return False


# ---------------------------------------------------------------------------
# Application de la configuration (config/themes.json et config/flux.json)
# ---------------------------------------------------------------------------
def appliquer_themes(cx) -> dict:
    """Écrit les thèmes déclarés dans la base. Un thème retiré de la configuration est
    désactivé, jamais supprimé : les articles déjà classés dedans resteraient orphelins."""
    avant = {r["code"] for r in cx.execute("SELECT code FROM themes")}
    for t in THEMES_DECLARES:
        cx.execute("INSERT INTO themes (code, nom, actif, ordre) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(code) DO UPDATE SET nom = excluded.nom, actif = excluded.actif, "
                   "ordre = excluded.ordre",
                   (t["code"], t["nom"], int(t["actif"]), t["ordre"]))
    retires = avant - set(THEMES)
    if retires:
        cx.executemany("UPDATE themes SET actif = 0 WHERE code = ?", [(c,) for c in retires])
    return {"declares": len(THEMES_DECLARES), "retires": sorted(retires)}


def appliquer_flux(cx, chemin: Path = None) -> dict:
    """Applique config/flux.json à la table sources.

    Règle, à retenir : **le fichier décrit les flux, l'administration décide lesquels
    tournent.** Les champs du catalogue (type, adresse, thème imposé, langue, fréquence,
    paramètres, notes) sont repris du fichier à chaque fois ; « actif » n'est posé qu'à la
    création, pour que désactiver une source depuis l'administration tienne.

    Une source absente du fichier est désactivée, jamais supprimée : ses articles restent.
    Les sources ajoutées depuis l'administration (origine = 'admin') ne sont pas touchées.
    """
    declares = {}
    for f in lire_config(chemin or FLUX_JSON, "flux"):
        s = valider_source({**f, "theme_impose": f.get("theme")})
        if s["nom"] in declares:
            raise ValueError(f"Le flux « {s['nom']} » est déclaré deux fois dans flux.json.")
        s["fichier_test"] = f.get("fichier_test")
        declares[s["nom"]] = s
    maintenant = maintenant_utc().isoformat()
    connues = {r["nom"]: r for r in cx.execute("SELECT * FROM sources")}
    ajoutes, modifies, a_la_main = 0, 0, []
    for nom, s in declares.items():
        if nom not in connues:
            _inserer_source(cx, {**s, "origine": "config"})
            ajoutes += 1
            continue
        ancienne = connues[nom]
        if ancienne["origine"] == "admin":
            a_la_main.append(nom)   # reprise en main depuis l'administration : on n'écrase pas
            continue
        if ancienne["url"] != s["url"]:   # nouvelle adresse : les en-têtes mémorisés sont caducs
            cx.execute("UPDATE sources SET etag = NULL, derniere_modif = NULL, echecs = 0, "
                       "pause_jusqu_a = NULL WHERE id = ?", (ancienne["id"],))
        cx.execute("""UPDATE sources SET type = ?, url = ?, parametres = ?, notes = ?,
                      fichier_test = ?, langue = ?, frequence_minutes = ?, theme_impose = ?,
                      origine = 'config', maj = ? WHERE id = ?""",
                   (s["type"], s["url"], json.dumps(s["parametres"], ensure_ascii=False),
                    s["notes"], s["fichier_test"], s["langue"], s["frequence_minutes"],
                    s["theme_impose"], maintenant, ancienne["id"]))
        modifies += 1
    retires = [r["nom"] for r in connues.values()
               if r["nom"] not in declares and r["origine"] == "config" and r["actif"]]
    for nom in retires:
        cx.execute("UPDATE sources SET actif = 0, maj = ? WHERE nom = ?", (maintenant, nom))
    return {"ajoutes": ajoutes, "modifies": modifies, "desactives": retires,
            "a_la_main": a_la_main}


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


DUREE_RECAPS = timedelta(days=90)   # les récapitulatifs survivent aux articles qu'ils résument


def purger(cx, maintenant: datetime, collecte_id: int = None) -> int:
    """D05 : supprime définitivement les articles publiés il y a plus de 30 jours
    (y compris ceux classés à la main ou masqués). Renvoie le nombre d'articles supprimés.
    Les événements qui n'ont plus aucune source disparaissent avec eux."""
    limite = (maintenant - DUREE_FIL).isoformat()
    # Un récapitulatif tient dans quelques lignes et coûte un appel au modèle : le garder
    # trois mois, bien après les articles, donne une mémoire du mois écoulé pour rien.
    cx.execute("DELETE FROM recaps WHERE jour < ?",
               ((maintenant - DUREE_RECAPS).strftime("%Y-%m-%d"),))
    n = cx.execute("DELETE FROM articles WHERE date_pub < ?", (limite,)).rowcount
    cx.execute("DELETE FROM evenements WHERE id NOT IN "
               "(SELECT evenement_id FROM articles WHERE evenement_id IS NOT NULL)")
    # depuis « fil » et non « articles » : nb_sources compte les sources PUBLIABLES,
    # celles que le site montre vraiment (zone et thème connus, non masquées)
    cx.execute(f"""UPDATE evenements SET
                     nb_sources = ({SQL_NB_SOURCES.format(id="evenements.id")}),
                     langues    = ({SQL_LANGUES.format(id="evenements.id")})""")
    if collecte_id is not None:
        cx.execute("UPDATE collectes SET nb_supprimes = ? WHERE id = ?", (n, collecte_id))
    cx.commit()
    return n


# ---------------------------------------------------------------------------
# Enregistrement d'un article
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Regroupement des articles en événements
# ---------------------------------------------------------------------------
# Trois conditions, toutes nécessaires, pour que deux articles parlent du même événement.
# Les seuils sont volontairement serrés : fusionner deux faits distincts est pire que de
# les laisser côte à côte, car le lecteur ne voit alors plus qu'un seul point.
RAYON_REGROUPEMENT_KM = 50          # deux lieux plus éloignés sont deux endroits différents
FENETRE_REGROUPEMENT = timedelta(hours=48)   # au-delà, c'est une suite, pas la même dépêche

# Deux seuils de ressemblance des titres, selon ce que le lieu dit déjà. Quand les deux
# articles sont au même endroit, le lieu est un indice fort et le titre peut diverger
# davantage : « Le sommet de l'UE s'ouvre à Bruxelles » et « Ouverture du sommet européen à
# Bruxelles » n'ont que 29 % de mots communs. Sans lieu commun, on exige beaucoup plus,
# sinon « séisme au Japon » et « séisme en Italie » finiraient sur le même point.
# Chiffres mesurés sur tests/regroupement_annote.json.
SEUIL_SIMILARITE = 0.45             # part de mots communs exigée sans lieu concordant
SEUIL_SIMILARITE_LOCALISE = 0.28    # exigée quand les deux articles sont au même endroit

# Mots trop fréquents dans les titres de presse pour dire quoi que ce soit de la ressemblance
MOTS_VIDES = frozenset("""
a au aux avec ce ces dans de des du elle en et eux il ils je la le les leur lui ma mais me
meme mes moi mon ne nos notre nous on ou par pas pour qu que qui sa se ses son sur ta te tes
toi ton tu un une vos votre vous y ont est sont etre avoir plus moins apres avant contre
entre vers chez sans sous tres bien fait faire dit selon the of and in on for to from with
at by as is are was were be been has have had its their this that
""".split())


def jetons_titre(titre_norm: str) -> frozenset:
    """Mots porteurs de sens d'un titre déjà normalisé (voir collect.normaliser)."""
    return frozenset(m for m in titre_norm.split() if len(m) > 2 and m not in MOTS_VIDES)


def similarite(a: frozenset, b: frozenset) -> float:
    """Indice de Jaccard : part de mots communs. 1 = mêmes mots, 0 = rien en commun."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def distance_km(lat1, lon1, lat2, lon2) -> float:
    """Distance à vol d'oiseau (formule de haversine), en kilomètres."""
    from math import asin, cos, radians, sin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(min(1.0, h)))


def _candidats_evenements(cx, a: dict, titre_norm: str, date: datetime):
    """Événements susceptibles de correspondre : même fenêtre de temps, et si l'article est
    localisé, même voisinage. Le filtre géographique est une boîte carrée calculée en SQL ;
    la vraie distance est vérifiée ensuite."""
    debut = (date - FENETRE_REGROUPEMENT).isoformat()
    fin = (date + FENETRE_REGROUPEMENT).isoformat()
    clauses = ["date_fin >= ?", "date_debut <= ?"]
    params = [debut, fin]
    if a.get("lat") is not None:
        marge = RAYON_REGROUPEMENT_KM / 111.0        # 1 degré de latitude ≈ 111 km
        clauses += ["lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
        params += [a["lat"] - marge, a["lat"] + marge,
                   a["lon"] - marge * 3, a["lon"] + marge * 3]   # large en longitude
    else:
        clauses.append("lat IS NULL")
    return cx.execute(f"SELECT * FROM evenements WHERE {' AND '.join(clauses)} "
                      "ORDER BY date_fin DESC LIMIT 200", params).fetchall()


def rattacher(cx, a: dict, titre_norm: str, date: datetime, maintenant: datetime) -> tuple:
    """Rattache l'article à un événement existant, ou en crée un. Renvoie (id, nouveau ?).

    Remplace l'ancien comportement, où un deuxième article de même titre était purement
    et simplement jeté : ses sources étaient perdues. Désormais il rejoint l'événement et
    apparaît dans le panneau des sources.
    """
    mots = jetons_titre(titre_norm)
    meilleur, meilleur_score = None, 0.0
    for e in _candidats_evenements(cx, a, titre_norm, date):
        meme_lieu = False
        if a.get("lat") is not None and e["lat"] is not None:
            if distance_km(a["lat"], a["lon"], e["lat"], e["lon"]) > RAYON_REGROUPEMENT_KM:
                continue
            meme_lieu = True
        seuil = SEUIL_SIMILARITE_LOCALISE if meme_lieu else SEUIL_SIMILARITE
        score = 1.0 if e["titre_norm"] == titre_norm else similarite(mots, jetons_titre(e["titre_norm"]))
        if score >= seuil and score > meilleur_score:
            meilleur, meilleur_score = e, score
    iso = date.isoformat()
    if meilleur is not None:
        cx.execute("""UPDATE evenements
                      SET date_debut = MIN(date_debut, ?), date_fin = MAX(date_fin, ?),
                          lat = COALESCE(lat, ?), lon = COALESCE(lon, ?), lieu = COALESCE(lieu, ?),
                          zone = COALESCE(zone, ?), theme = COALESCE(theme, ?), maj = ?
                      WHERE id = ?""",
                   (iso, iso, a.get("lat"), a.get("lon"), a.get("lieu"), a.get("zone"),
                    a.get("theme"), maintenant.isoformat(), meilleur["id"]))
        return meilleur["id"], False
    cur = cx.execute(
        """INSERT INTO evenements (titre, titre_norm, lat, lon, lieu, zone, theme,
                                   date_debut, date_fin, nb_sources, maj)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (a["titre"], titre_norm, a.get("lat"), a.get("lon"), a.get("lieu"), a.get("zone"),
         a.get("theme"), iso, iso, maintenant.isoformat()))
    return cur.lastrowid, True


# Sous-requêtes partagées entre la mise à jour unitaire et le recalcul complet de purger(),
# pour qu'elles ne puissent pas diverger — c'est déjà arrivé une fois.
# {id} vaut « ? » pour un événement précis, « evenements.id » pour un recalcul complet.
SQL_NB_SOURCES = "SELECT COUNT(*) FROM fil WHERE evenement_id = {id}"
SQL_LANGUES = ("SELECT COALESCE(',' || GROUP_CONCAT(DISTINCT langue) || ',', '') "
               "FROM fil WHERE evenement_id = {id}")


def rafraichir_evenement(cx, evenement_id: int):
    """Recompte les sources publiables d'un événement et relève leurs langues.

    À appeler après toute écriture qui peut les changer : nouvel article, masquage,
    classement manuel. Une écriture SQL directe, hors de ces chemins, laisserait ces
    valeurs fausses — c'est pourquoi purger(), lancée à chaque collecte, les recalcule
    toutes.
    """
    if evenement_id is None:
        return
    cx.execute(f'UPDATE evenements SET nb_sources = ({SQL_NB_SOURCES.format(id="?")}), '
               f'langues = ({SQL_LANGUES.format(id="?")}) WHERE id = ?',
               (evenement_id, evenement_id, evenement_id))


def langue_sql(langue: str, prefixe: str = "") -> tuple:
    """Clause de filtre par langue, côté articles comme côté événements."""
    if not langue:
        return "", []
    if prefixe:   # table evenements : plusieurs langues possibles pour un même point
        return f" AND instr({prefixe}langues, ?) > 0", [f",{langue},"]
    return " AND langue = ?", [langue]


def enregistrer(cx, a: dict, titre_norm: str, maintenant: datetime, collecte_id: int) -> str:
    """
    Enregistre un article trié. Renvoie 'nouveau', 'regroupe', 'maj', 'inchange' ou 'ancien'.
      - publié il y a plus de 30 jours : pas enregistré, il serait supprimé à la collecte
        suivante (D05) ;
      - lien déjà connu : on garde la date d'ajout ; on ne complète le classement
        que s'il manquait, et jamais si l'article a été corrigé à la main ;
      - même dépêche reprise ailleurs (même lieu, titre proche, même fenêtre de temps) :
        l'article est GARDÉ et rattaché au même événement, qui ne fait qu'un point sur la
        carte. C'est le changement par rapport à l'ancien comportement, où ce deuxième
        article était jeté et sa source perdue (D03).
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

    date = a["date"] or maintenant
    evenement_id, premier = rattacher(cx, a, titre_norm, date, maintenant)
    cx.execute(
        """INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme,
                                 ajout, maj, collecte_id, lat, lon, lieu, lieu_origine,
                                 evenement_id, langue, theme_ia, theme_force)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (a["lien"], a["titre"], titre_norm, a["source"], date.isoformat(),
         a["zone"], a["theme"], maintenant.isoformat(), maintenant.isoformat(), collecte_id,
         a.get("lat"), a.get("lon"), a.get("lieu"), a.get("lieu_origine"), evenement_id,
         a.get("langue") or "fr", 1 if a.get("theme_ia") else 0,
         1 if a.get("theme_force") else 0))
    rafraichir_evenement(cx, evenement_id)
    return "nouveau" if premier else "regroupe"


def jour_utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def articles_du_jour(cx, jour: str, theme: str, limite: int = 60) -> list:
    """Articles publiables d'un thème pour une journée, du plus récent au plus ancien.

    Les bornes sont lexicographiques sur la date ISO, ce qui laisse l'index sur date_pub
    travailler : « 2026-10-28T09:00:00+00:00 » est bien entre « 2026-10-28 » et « 2026-10-29 ».
    """
    return cx.execute(
        "SELECT titre, source, lieu, langue FROM fil "
        "WHERE theme = ? AND date_pub >= ? AND date_pub < ? "
        "ORDER BY date_pub DESC LIMIT ?",
        (theme, jour, jour + "z", limite)).fetchall()


def compter_articles_du_jour(cx, jour: str, theme: str) -> int:
    return cx.execute("SELECT COUNT(*) FROM fil WHERE theme = ? AND date_pub >= ? "
                      "AND date_pub < ?", (theme, jour, jour + "z")).fetchone()[0]


def lire_recap(cx, jour: str, theme: str):
    return cx.execute("SELECT * FROM recaps WHERE jour = ? AND theme = ?",
                      (jour, theme)).fetchone()


def lister_recaps(cx, jour: str, themes=None) -> list:
    ou, p = "jour = ?", [jour]
    if themes:
        ou += f" AND theme IN ({','.join('?' * len(themes))})"
        p += list(themes)
    return cx.execute(f"SELECT * FROM recaps WHERE {ou} ORDER BY theme", p).fetchall()


def dernier_jour_avec_recap(cx, themes=None) -> str:
    """Jour le plus récent qui a au moins un récapitulatif — la veille, en début de matinée."""
    ou, p = "1 = 1", []
    if themes:
        ou = f"theme IN ({','.join('?' * len(themes))})"
        p = list(themes)
    ligne = cx.execute(f"SELECT MAX(jour) FROM recaps WHERE {ou}", p).fetchone()
    return ligne[0] if ligne else None


TAILLE_MAX_RECAP = 4000   # un récapitulatif plus long qu'une page n'en est plus un


def ecrire_recap(cx, jour: str, theme: str, texte: str, modele: str, nb: int,
                 maintenant: datetime):
    cx.execute("INSERT OR REPLACE INTO recaps (jour, theme, texte, modele, nb_articles, maj) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (jour, theme, texte[:TAILLE_MAX_RECAP], modele, nb, maintenant.isoformat()))


def classer_a_la_main(cx, article_id: int, zone: str, theme: str, maintenant: datetime) -> bool:
    """Correction manuelle : le collecteur ne modifiera plus cet article."""
    if zone not in ZONES or theme not in THEMES:
        raise ValueError(f"zone ou thème inconnu : {zone} / {theme}")
    cur = cx.execute("UPDATE articles SET zone = ?, theme = ?, manuel = 1, maj = ? WHERE id = ?",
                     (zone, theme, maintenant.isoformat(), article_id))
    _compteur_de_l_article(cx, article_id)   # l'article devient publiable : l'événement le sait
    cx.commit()
    return cur.rowcount == 1


def _compteur_de_l_article(cx, article_id: int):
    ligne = cx.execute("SELECT evenement_id FROM articles WHERE id = ?", (article_id,)).fetchone()
    if ligne:
        rafraichir_evenement(cx, ligne["evenement_id"])


def masquer(cx, article_id: int, masque: bool, maintenant: datetime) -> bool:
    """Retire un article du site (ou l'y remet) sans le supprimer de la base."""
    cur = cx.execute("UPDATE articles SET masque = ?, maj = ? WHERE id = ?",
                     (int(masque), maintenant.isoformat(), article_id))
    _compteur_de_l_article(cx, article_id)
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
        import collect   # importé ici : base.py ne doit pas dépendre du collecteur au chargement
        try:   # contrôle d'écriture seulement : le DNS est vérifié au moment de la collecte
            collect.verifier_forme_url(url)
        except collect.AdresseRefusee as e:
            raise ValueError(str(e)) from None
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
    langue = (d.get("langue") or "fr").strip().lower()
    if langue not in LANGUES:
        raise ValueError(f"Langue inconnue : {langue}. Langues gérées : {', '.join(LANGUES)}.")
    brute = d.get("frequence_minutes")
    try:   # 0 est une erreur de saisie, pas « valeur absente » : seul None prend le défaut
        frequence = FREQUENCE_DEFAUT if brute is None else int(brute)
    except (TypeError, ValueError):
        raise ValueError("La fréquence doit être un nombre de minutes.") from None
    if not 1 <= frequence <= 7 * 24 * 60:
        raise ValueError("La fréquence doit être comprise entre 1 minute et 7 jours.")
    impose = d.get("theme_impose") or None
    if impose is not None and impose not in THEMES:
        raise ValueError(f"Thème imposé inconnu : {impose}. Thèmes déclarés : {', '.join(THEMES)}.")
    return {"nom": nom, "type": type_, "url": url, "actif": bool(d.get("actif", True)),
            "parametres": parametres, "notes": notes, "fichier_test": d.get("fichier_test"),
            "langue": langue, "frequence_minutes": frequence, "theme_impose": impose}


def _inserer_source(cx, s: dict) -> int:
    maintenant = maintenant_utc().isoformat()
    cur = cx.execute(
        """INSERT INTO sources (nom, type, url, actif, parametres, fichier_test, notes,
                                langue, frequence_minutes, theme_impose, origine, ajout, maj)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (s["nom"], s["type"], s.get("url"), int(bool(s.get("actif", True))),
         json.dumps(s.get("parametres") or {}, ensure_ascii=False), s.get("fichier_test"),
         s.get("notes"), s.get("langue", "fr"), s.get("frequence_minutes", FREQUENCE_DEFAUT),
         s.get("theme_impose"), s.get("origine", "admin"), maintenant, maintenant))
    return cur.lastrowid


def _source(ligne) -> dict:
    d = dict(ligne)
    d["actif"] = bool(d["actif"])
    d["parametres"] = json.loads(d["parametres"] or "{}")
    return d


def source_a_rafraichir(s: dict, maintenant: datetime) -> bool:
    """Faux si la source a déjà été interrogée avec succès il y a moins que sa fréquence.
    Permet de lancer la collecte toutes les 10 minutes sans pour autant redemander un
    quotidien toutes les 10 minutes."""
    dernier = s.get("dernier_succes")
    if not dernier:
        return True
    attendu = maintenant - timedelta(minutes=s.get("frequence_minutes") or FREQUENCE_DEFAUT)
    return dernier <= attendu.isoformat()


def lister_sources(cx, actives: bool = False) -> list:
    sql = "SELECT * FROM sources" + (" WHERE actif = 1" if actives else "") + " ORDER BY type, nom COLLATE NOCASE"
    return [_source(r) for r in cx.execute(sql)]


def lire_source(cx, source_id: int):
    r = cx.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _source(r) if r else None


# --- Disjoncteur : une source qui échoue plusieurs fois d'affilée est mise de côté ---------
SEUIL_DISJONCTEUR = 3                      # échecs consécutifs tolérés avant la mise en pause
PAUSE_MINIMALE = timedelta(minutes=15)     # durée de la première mise en pause
PAUSE_MAXIMALE = timedelta(hours=24)       # plafond : une source morte est retentée chaque jour


def duree_pause(echecs: int) -> timedelta:
    """Pause après `echecs` échecs consécutifs : rien, puis 15 min, 30 min, 1 h… plafonné à 24 h."""
    if echecs < SEUIL_DISJONCTEUR:
        return timedelta(0)
    return min(PAUSE_MINIMALE * 2 ** (echecs - SEUIL_DISJONCTEUR), PAUSE_MAXIMALE)


def noter_etat_source(cx, source_id: int, ok: bool, maintenant: datetime,
                      etag: str = None, derniere_modif: str = None):
    """Mémorise les en-têtes conditionnels et tient le disjoncteur.

    Un succès remet le compteur à zéro. Un échec l'incrémente et, au-delà du seuil, écarte la
    source des collectes pendant une durée qui double à chaque fois : une source en panne
    cesse de coûter un aller-retour réseau à chaque passage.
    """
    if source_id is None:
        return
    if ok:
        # un 304 ne renvoie pas d'en-tête : on garde ceux de la dernière réponse complète
        cx.execute("""UPDATE sources SET etag = COALESCE(?, etag),
                                         derniere_modif = COALESCE(?, derniere_modif),
                                         echecs = 0, pause_jusqu_a = NULL, dernier_succes = ?
                       WHERE id = ?""",
                   (etag, derniere_modif, maintenant.isoformat(), source_id))
    else:
        ligne = cx.execute("SELECT echecs FROM sources WHERE id = ?", (source_id,)).fetchone()
        if ligne is None:
            return
        echecs = (ligne["echecs"] or 0) + 1
        pause = duree_pause(echecs)
        cx.execute("UPDATE sources SET echecs = ?, pause_jusqu_a = ? WHERE id = ?",
                   (echecs, (maintenant + pause).isoformat() if pause else None, source_id))
    cx.commit()


def source_en_pause(s: dict, maintenant: datetime) -> bool:
    """Vrai si le disjoncteur de cette source est encore ouvert."""
    jusqu_a = s.get("pause_jusqu_a")
    return bool(jusqu_a) and jusqu_a > maintenant.isoformat()


def config_collecte(s: dict) -> dict:
    """Forme attendue par les connecteurs du collecteur (type, nom, url, fichier_test, paramètres)."""
    conf = {"type": s["type"], "nom": s["nom"], "url": s.get("url"), "fichier_test": s.get("fichier_test"),
            "id": s.get("id"), "etag": s.get("etag"), "derniere_modif": s.get("derniere_modif"),
            "langue": s.get("langue", "fr"), "theme_impose": s.get("theme_impose")}
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


# Champs décrits par config/flux.json. Les modifier depuis l'administration fait passer la
# source « à la main » : le fichier cesse de s'appliquer à elle, et l'administration le dit.
# « actif » n'en fait pas partie : activer ou couper une source reste le rôle normal de
# l'administration, et le fichier ne le repose jamais après la création.
CHAMPS_CATALOGUE = ("nom", "type", "url", "parametres", "notes", "langue",
                    "frequence_minutes", "theme_impose")


def modifier_source(cx, source_id: int, d: dict) -> dict:
    """Met à jour une source. Un changement de nom est reporté sur ses articles et son journal."""
    ancienne = lire_source(cx, source_id)
    if not ancienne:
        raise LookupError(f"Aucune source n° {source_id}.")
    s = valider_source({**ancienne, **d, "parametres": {**ancienne["parametres"], **(d.get("parametres") or {})}})
    catalogue_touche = any(s[c] != ancienne.get(c) for c in CHAMPS_CATALOGUE)
    origine = "admin" if catalogue_touche else ancienne.get("origine", "config")
    try:
        cx.execute(
            """UPDATE sources SET nom = ?, type = ?, url = ?, actif = ?, parametres = ?, notes = ?,
                      langue = ?, frequence_minutes = ?, theme_impose = ?, origine = ?, maj = ?
               WHERE id = ?""",
            (s["nom"], s["type"], s["url"], int(s["actif"]), json.dumps(s["parametres"], ensure_ascii=False),
             s["notes"], s["langue"], s["frequence_minutes"], s["theme_impose"], origine,
             maintenant_utc().isoformat(), source_id))
    except sqlite3.IntegrityError:
        cx.rollback()
        raise ValueError(f"Une source s'appelle déjà « {s['nom']} ».") from None
    if s["nom"] != ancienne["nom"]:
        cx.execute("UPDATE articles SET source = ? WHERE source = ?", (s["nom"], ancienne["nom"]))
        cx.execute("UPDATE collectes_sources SET source = ? WHERE source = ?", (s["nom"], ancienne["nom"]))
    if s["url"] != ancienne.get("url"):
        # nouvelle adresse : les en-têtes conditionnels de l'ancienne ne valent plus rien, et
        # la source mérite d'être retentée tout de suite même si son disjoncteur était ouvert
        cx.execute("UPDATE sources SET etag = NULL, derniere_modif = NULL, echecs = 0, "
                   "pause_jusqu_a = NULL WHERE id = ?", (source_id,))
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
