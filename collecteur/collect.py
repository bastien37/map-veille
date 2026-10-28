#!/usr/bin/env python3
"""
MAP – Monitoring de l'Actualité Planétaire
Collecteur : sources -> collecte -> tri -> stockage (chaîne de l'information, partie 6).

Types de sources (table « sources » de la base, gérée depuis l'administration) :
    rss        flux RSS 2.0 ou Atom d'un média
    gdacs      API GDACS (alertes catastrophes ONU / Commission européenne, sans clé)
    reliefweb  API ReliefWeb v2 (rapports humanitaires OCHA, appname approuvé requis)

Utilisation (bibliothèques gérées par uv, voir pyproject.toml) :
    uv run collecteur/collect.py                  # interroge les sources en ligne
    uv run collecteur/collect.py --hors-ligne     # utilise les fichiers de test (tests/)
    uv run collecteur/collect.py --fichiers a.xml # test sur des flux RSS enregistrés
    uv run collecteur/collect.py --parallele 32   # plus de sources interrogées en même temps

Les sources sont interrogées en parallèle (une seule à la fois par domaine). Un flux
inchangé répond 304 grâce aux en-têtes ETag / If-Modified-Since et n'est pas retéléchargé ;
une source qui échoue plusieurs fois d'affilée est écartée pendant un moment (disjoncteur).

Les articles sont stockés dans la base SQLite donnees/map.sqlite (voir base.py).
À chaque collecte, les articles publiés il y a plus de 30 jours sont supprimés (D05).
Le site les lit à travers l'API (serveur/app.py) : aucune donnée n'est écrite dans le front.

Seuls le titre, la source, la date et le lien sont conservés (exigence C03).
"""
import argparse
import collections
import gzip
import html
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import feedparser

import base    # stockage SQLite (collecteur/base.py)
import ia      # classement des thèmes par un modèle de langage (collecteur/ia.py)
import lieux   # extraction des lieux et géocodage (collecteur/lieux.py)

RACINE = Path(__file__).resolve().parent.parent
REGLES = RACINE / "collecteur" / "regles.json"
PAYS = RACINE / "collecteur" / "pays.json"
POIDS_TITRE, POIDS_RESUME = 3, 1

PARALLELE = 16            # sources interrogées en même temps (une seule à la fois par domaine)
DELAI_RESEAU = 20         # secondes avant d'abandonner une requête
TAILLE_MAX = 8 << 20      # 8 Mio : au-delà, le flux est refusé sans être chargé en mémoire
ATTENTE_MAX = 30          # plafond du Retry-After respecté
PAUSE_REESSAI = 2         # attente avant le 2e essai ; elle double ensuite
UA = "MAP-EAE-veille/1.1 (projet pluridisciplinaire, Ecole de l'air et de l'espace)"
MAINTENANT = datetime.now(timezone.utc).replace(microsecond=0)


# ======================================================================
# Outils texte et dates
# ======================================================================
def normaliser(texte: str) -> str:
    """minuscules, sans accents, ponctuation -> espaces"""
    texte = unicodedata.normalize("NFD", (texte or "").lower())
    texte = "".join(c for c in texte if unicodedata.category(c) != "Mn")
    texte = texte.replace("’", " ").replace("'", " ")
    return " " + re.sub(r"[^a-z0-9]+", " ", texte).strip() + " "


def nettoyer_html(texte: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", texte or ""))).strip()


def lire_date(txt):
    """RFC 822 (RSS), ISO 8601 (Atom, GDACS, ReliefWeb) ou ISO compact AAAAMMJJTHHMMSSZ."""
    if not txt:
        return None
    txt = str(txt).strip()
    d = None
    m = re.fullmatch(r"(\d{8})T?(\d{6})Z?", txt)
    if m:
        d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    else:
        try:
            d = parsedate_to_datetime(txt)
        except (TypeError, ValueError):
            try:
                d = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            except ValueError:
                return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


# ======================================================================
# Tri par mots-clés
# ======================================================================
JETON = re.compile(r"[a-z0-9]+")


def compiler(regles: dict) -> dict:
    """Prépare le tri. Les mots-clés sont rangés dans trois tables au lieu d'être compilés
    en une expression régulière chacun : le texte est alors balayé une seule fois, quel que
    soit le nombre de mots-clés (487 aujourd'hui, tous essayés sur chaque article avant).

        exact     « bruxelles »        le jeton entier doit être égal au mot-clé
        prefixe   « europeen* »        le jeton doit commencer par la racine
        suite     « union europeenne » n jetons consécutifs

    Les catégories sont gardées en liste, pas en ensemble : un mot-clé écrit deux fois dans
    regles.json compte deux fois, exactement comme le faisaient deux expressions identiques.
    """
    exact, prefixe, suite, nmax = {}, {}, {}, 1
    for cat, mots in regles.items():
        for mot in mots:
            m = normaliser(mot).strip()
            if not m:
                continue
            if " " in m:
                n = m.count(" ") + 1
                nmax = max(nmax, n)
                suite.setdefault((n, m), []).append(cat)
            elif mot.endswith("*"):
                prefixe.setdefault(m, []).append(cat)
            else:
                exact.setdefault(m, []).append(cat)
    return {"exact": exact, "prefixe": prefixe, "suite": suite, "nmax": nmax,
            "lmin": min((len(p) for p in prefixe), default=1),
            "lmax": max((len(p) for p in prefixe), default=0)}


def _balayer(texte: str, tables: dict, poids: int, scores: dict, premiere: dict, noter: bool):
    """Ajoute `poids` point(s) à chaque catégorie dont un mot-clé apparaît dans `texte`.
    `noter` : ne retenir la position du premier mot-clé que pour le titre (départage les égalités)."""
    jetons = [(m.group(), m.start()) for m in JETON.finditer(texte)]
    exact, prefixe, suite = tables["exact"], tables["prefixe"], tables["suite"]
    lmin, lmax, nmax = tables["lmin"], tables["lmax"], tables["nmax"]
    for i, (jeton, pos) in enumerate(jetons):
        trouves = list(exact.get(jeton, ()))
        for k in range(lmin, min(lmax, len(jeton)) + 1):
            trouves += prefixe.get(jeton[:k], ())
        for n in range(2, min(nmax, len(jetons) - i) + 1):
            trouves += suite.get((n, " ".join(j for j, _ in jetons[i:i + n])), ())
        for cat in trouves:
            scores[cat] = scores.get(cat, 0) + poids
            if noter and pos < premiere.get(cat, 10**9):
                premiere[cat] = pos


def classer_scores(titre: str, resume: str, tables: dict) -> tuple:
    """Points par catégorie (titre x3, résumé x1) et position du premier mot-clé de chacune."""
    scores, premiere = {}, {}
    _balayer(normaliser(titre), tables, POIDS_TITRE, scores, premiere, True)
    _balayer(normaliser(resume), tables, POIDS_RESUME, scores, premiere, False)
    return scores, premiere


def meilleur(scores: dict, premiere: dict):
    """Catégorie qui a le plus de points, le mot-clé le plus tôt dans le titre départageant."""
    if not scores:
        return None
    return sorted(scores, key=lambda c: (-scores[c], premiere.get(c, 10**6)))[0]


def classer(titre: str, resume: str, tables: dict):
    """Catégorie qui a le plus de points (titre x3, résumé x1), ou None."""
    return meilleur(*classer_scores(titre, resume, tables))


# Écart en deçà duquel deux thèmes sont considérés à égalité : un seul mot-clé de résumé
# les sépare, ce qui ne suffit pas à trancher. C'est le cas que le modèle de langage sait
# mieux traiter que des mots-clés — « Nouvelle frégate pour la marine indonésienne » touche
# « def » et « eco » presque autant l'un que l'autre.
MARGE_HESITATION = POIDS_RESUME


def theme_hesitant(scores: dict) -> bool:
    """Vrai quand les mots-clés n'ont rien trouvé, ou quand les deux premiers se tiennent."""
    if not scores:
        return True
    points = sorted(scores.values(), reverse=True)
    return len(points) > 1 and points[0] - points[1] <= MARGE_HESITATION


# ======================================================================
# Accès réseau
# ======================================================================
class NonModifie(Exception):
    """Le serveur a répondu 304 : le flux n'a pas changé depuis la dernière collecte."""


class AdresseRefusee(ValueError):
    """L'adresse demandée vise le réseau interne : le collecteur ne doit pas y aller."""


# Les adresses de flux viennent de l'administration et d'un fichier de configuration : ce
# sont des entrées, pas des constantes. Sans contrôle, « http://169.254.169.254/… » ou
# « http://127.0.0.1:8000/api/admin/… » ferait du collecteur un relais vers le réseau
# interne du serveur (SSRF). On vérifie le schéma, puis les adresses IP réellement
# résolues — et de nouveau après chaque redirection, qu'un serveur distant contrôle.
AUTORISER_RESEAU_LOCAL = os.environ.get("MAP_AUTORISER_RESEAU_LOCAL") == "1"

# Cas particulier d'une machine qui sort par un proxy HTTP : elle n'ouvre pas la connexion
# elle-même, elle la demande au proxy, et c'est LE PROXY qui résout le nom. Elle n'a alors
# souvent aucun DNS, et « adresses_de() » échoue pour tous les flux — la collecte s'arrête
# avant d'avoir commencé. L'adresse que nous résoudrions ne serait de toute façon pas celle
# qui est jointe.
#
# Ce réglage reste donc FERMÉ par défaut : on préfère une collecte qui échoue visiblement à
# un contrôle qu'on retire sans le dire. L'ouvrir est une décision d'exploitation, à prendre
# en connaissance de cause : la politique de sortie devient celle du proxy, à qui il revient
# de refuser le réseau interne. Les contrôles de forme, eux, s'appliquent dans tous les cas
# — « http://169.254.169.254/… » et « http://10.0.0.1/… » sont refusés avant le proxy.
PROXY_RESOUT_LES_NOMS = os.environ.get("MAP_PROXY_RESOUT_LES_NOMS") == "1"


def adresses_de(hote: str) -> set:
    infos = socket.getaddrinfo(hote, None, proto=socket.IPPROTO_TCP)
    return {ipaddress.ip_address(i[4][0]) for i in infos}


def verifier_forme_url(url: str, autoriser_local: bool = None):
    """Contrôle d'écriture, SANS résolution DNS : schéma, nom de machine, et adresse IP
    écrite en clair si c'en est une.

    C'est ce contrôle-là qu'on applique en enregistrant une source. Interroger le DNS à ce
    moment serait une mauvaise idée : une panne de résolution empêcherait d'ouvrir la base
    ou de sauvegarder une source parfaitement valable.
    """
    if autoriser_local is None:
        autoriser_local = AUTORISER_RESEAU_LOCAL
    p = urllib.parse.urlparse(url or "")
    if p.scheme not in ("http", "https"):
        raise AdresseRefusee(f"adresse refusée : schéma « {p.scheme or '?'} » "
                             "(seuls http:// et https:// sont acceptés)")
    if not p.hostname:
        raise AdresseRefusee("adresse refusée : nom de machine absent")
    if autoriser_local:
        return
    if p.hostname.lower() in ("localhost", "localhost.localdomain"):
        raise AdresseRefusee("adresse refusée : localhost désigne le serveur lui-même")
    try:
        ip = ipaddress.ip_address(p.hostname.strip("[]"))
    except ValueError:
        return          # c'est un nom : le DNS sera vérifié au moment de la requête
    if not ip.is_global or ip.is_multicast:
        raise AdresseRefusee(f"adresse refusée : {ip} est sur le réseau interne. "
                             "Un flux doit être servi par une machine publique.")


def via_proxy(url: str) -> bool:
    """Vrai si cette requête-ci passera par un proxy HTTP, qui résoudra le nom à sa place.

    Un hôte listé dans no_proxy est joint en direct : le contrôle par adresse IP s'applique
    alors normalement, ce qui est ce qu'on veut pour 127.0.0.1 et le réseau de la machine.
    """
    d = urllib.parse.urlsplit(url or "")
    if d.scheme not in urllib.request.getproxies():
        return False
    try:
        return not urllib.request.proxy_bypass(d.hostname or "")
    except Exception:
        return True


def verifier_url(url: str, autoriser_local: bool = None):
    """Contrôle complet, appliqué juste avant d'ouvrir la connexion : la forme, puis les
    adresses IP réellement résolues. C'est ici que la vérification compte, car un nom peut
    changer de cible entre l'enregistrement de la source et la collecte."""
    if autoriser_local is None:
        autoriser_local = AUTORISER_RESEAU_LOCAL
    verifier_forme_url(url, autoriser_local)
    if autoriser_local:
        return
    if PROXY_RESOUT_LES_NOMS and via_proxy(url):
        return      # contrôle délégué au proxy, sur décision explicite de l'exploitant
    hote = urllib.parse.urlparse(url).hostname
    try:
        adresses = adresses_de(hote)
    except socket.gaierror as e:
        raise AdresseRefusee(f"nom de machine introuvable : {hote} ({e})") from None
    for a in adresses:
        if not a.is_global or a.is_multicast:
            raise AdresseRefusee(
                f"adresse refusée : {hote} pointe vers {a}, qui est sur le réseau "
                "interne. Un flux doit être servi par une machine publique.")


class _RedirectionsVerifiees(urllib.request.HTTPRedirectHandler):
    """Une redirection est choisie par le serveur distant : elle doit être vérifiée aussi,
    sinon un site public pourrait renvoyer le collecteur vers 127.0.0.1."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        verifier_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_ouvreur = urllib.request.build_opener(_RedirectionsVerifiees)


_verrous_domaine = collections.defaultdict(threading.Lock)
_verrou_domaines = threading.Lock()


def verrou_domaine(url: str) -> threading.Lock:
    """Un seul téléchargement à la fois par domaine : les sources sont interrogées en
    parallèle, mais deux flux du même média ne partent jamais ensemble."""
    hote = (urllib.parse.urlparse(url).hostname or "").lower()
    with _verrou_domaines:
        return _verrous_domaine[hote]


def _lire_borne(reponse, taille_max: int) -> bytes:
    """Lit le corps sans dépasser `taille_max` : un flux énorme, ou sans fin, ne doit pas
    pouvoir épuiser la mémoire du collecteur."""
    corps = reponse.read(taille_max + 1)
    if len(corps) > taille_max:
        raise ValueError(f"réponse de plus de {taille_max // 1024} Kio, abandonnée")
    return corps


def http(url: str, donnees: dict = None, essais: int = 3, pause: float = None,
         accept: str = "application/json, application/xml, text/xml, */*",
         etag: str = None, derniere_modif: str = None, entetes: dict = None,
         taille_max: int = TAILLE_MAX, delai: float = DELAI_RESEAU) -> bytes:
    """GET (ou POST JSON si donnees). Réessaie sur 429 / 5xx en respectant l'en-tête Retry-After,
    avec une attente qui double à chaque essai.

    `etag` et `derniere_modif` sont les valeurs reçues à la collecte précédente : le serveur
    peut alors répondre 304 sans renvoyer le corps, ce qui lève NonModifie. Sur 100 flux
    inchangés, cela économise la totalité des 2,6 Mio normalement retéléchargés.

    `entetes` est renseigné en sortie avec l'ETag et le Last-Modified de la réponse, à ranger
    dans la table sources pour la collecte suivante.
    """
    pause = PAUSE_REESSAI if pause is None else pause
    corps = json.dumps(donnees).encode() if donnees is not None else None
    conditionnel = {}
    if etag:
        conditionnel["If-None-Match"] = etag
    if derniere_modif:
        conditionnel["If-Modified-Since"] = derniere_modif
    for i in range(essais):
        req = urllib.request.Request(url, data=corps, headers={
            "User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip",
            **conditionnel,
            **({"Content-Type": "application/json"} if corps else {})})
        try:
            verifier_url(url)
            with verrou_domaine(url):
                with _ouvreur.open(req, timeout=delai) as r:
                    if entetes is not None:
                        entetes["etag"] = r.headers.get("ETag")
                        entetes["derniere_modif"] = r.headers.get("Last-Modified")
                    brut = _lire_borne(r, taille_max)
                    compresse = (r.headers.get("Content-Encoding") or "").lower() == "gzip"
            return gzip.decompress(brut) if compresse else brut
        except urllib.error.HTTPError as e:
            if e.code == 304:
                raise NonModifie() from None
            if e.code in (429, 500, 502, 503, 504) and i < essais - 1:
                attente = pause * (2 ** i)
                try:
                    attente = max(attente, float(e.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                time.sleep(min(attente, ATTENTE_MAX))
                continue
            raise
        except urllib.error.URLError:
            if i < essais - 1:   # coupure réseau, DNS momentanément injoignable
                time.sleep(pause * (2 ** i))
                continue
            raise
    raise RuntimeError("échec après plusieurs essais")


def lire_json(brut: bytes, vide=None):
    """JSON tolérant : une réponse vide donne `vide`, une réponse non JSON une erreur lisible."""
    texte = brut.decode("utf-8-sig", errors="replace").strip()
    if not texte:
        return {} if vide is None else vide
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        raise ValueError(f"réponse non JSON : {texte[:150]!r}") from None


def charger(source: dict, hors_ligne: bool, url: str = None, donnees: dict = None,
            entetes: dict = None, conditionnel: bool = False) -> bytes:
    if hors_ligne:
        return (RACINE / source["fichier_test"]).read_bytes()
    if conditionnel:
        return http(url, donnees, entetes=entetes,
                    etag=source.get("etag"), derniere_modif=source.get("derniere_modif"))
    return http(url, donnees, entetes=entetes)


def article(titre, lien, date, source, resume="", zone=None, theme=None, indice_theme=None,
            langue="fr", lat=None, lon=None, lieu=None):
    return {"titre": nettoyer_html(titre), "lien": (lien or "").split("#")[0].strip(),
            "date": date, "source": source, "resume": nettoyer_html(resume),
            "zone": zone, "theme": theme, "indice_theme": indice_theme, "langue": langue,
            "lat": lat, "lon": lon, "lieu": lieu, "lieu_origine": None}


# ======================================================================
# Connecteurs
# ======================================================================
# Marqueurs du début d'un document de flux. On ne cherche que dans les premiers octets :
# au-delà, ce n'est plus du bruit en tête, c'est un autre document.
DEBUTS_FLUX = (b"<?xml", b"<rss", b"<feed", b"<rdf:RDF")
BRUIT_MAX = 4096


def retirer_le_bruit_en_tete(contenu: bytes) -> bytes:
    """Coupe ce qui précède le début du document XML, quand il y a quelque chose devant.

    Des sites mal configurés font précéder leur flux d'un avertissement PHP ou d'une ligne
    blanche. Le document devient « not well-formed » et feedparser ne rend plus aucune
    entrée, alors que le flux lui-même est parfaitement lisible deux lignes plus bas.
    Couper au premier marqueur ne devine rien : on jette du texte qui n'avait rien à faire
    là. Vu en vrai sur East Pendulum, dont le flux commence par un « Warning: » de PHP.
    """
    trouves = [i for i in (contenu.find(m, 0, BRUIT_MAX) for m in DEBUTS_FLUX) if i >= 0]
    if not trouves or min(trouves) == 0:
        return contenu      # rien devant : le document commence déjà comme il faut
    return contenu[min(trouves):]


def lire_rss(contenu: bytes, nom: str):
    """Flux RSS 0.9x/1.0/2.0 ou Atom, lu par feedparser (tolère les flux mal formés)."""
    flux = feedparser.parse(contenu)
    if flux.bozo and not flux.entries:
        # deuxième chance : le flux est peut-être bon, précédé de bruit
        propre = retirer_le_bruit_en_tete(contenu)
        if propre is not contenu and len(propre) != len(contenu):
            flux = feedparser.parse(propre)
    if flux.bozo and not flux.entries:
        raise ValueError(f"flux illisible ({flux.bozo_exception})")
    sortie = []
    for e in flux.entries:
        cats = " ".join(t.get("term") or "" for t in e.get("tags", []))
        # georss:point, geo:lat/geo:long, georss:where : quand le média donne la position,
        # elle vaut mieux que tout ce qu'on pourrait deviner du titre
        coord = lieux.coordonnees_du_flux(e)
        sortie.append(article(e.get("title", ""), e.get("link", ""),
                              lire_date(e.get("published") or e.get("updated")), nom,
                              (e.get("summary") or "") + " " + cats,
                              lat=coord[0] if coord else None,
                              lon=coord[1] if coord else None))
    return sortie


def source_rss(s, hors_ligne, entetes=None):
    return lire_rss(charger(s, hors_ligne, s["url"], entetes=entetes, conditionnel=True), s["nom"])


TYPES_GDACS = {"EQ": "Séisme", "TC": "Cyclone tropical", "FL": "Inondations", "VO": "Éruption volcanique",
               "DR": "Sécheresse", "WF": "Feux de forêt", "TS": "Tsunami"}
NIVEAUX_GDACS = {"red": "alerte rouge", "orange": "alerte orange", "green": "alerte verte"}


def source_gdacs(s, hors_ligne, pays_zone, noms_fr):
    """GDACS : https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH (GeoJSON)."""
    params = {"eventlist": "EQ;TC;FL;VO;DR;WF;TS", "alertlevel": s.get("niveaux", "orange;red"),
              "fromDate": (MAINTENANT - timedelta(days=s.get("jours", 7))).strftime("%Y-%m-%d"),
              "toDate": MAINTENANT.strftime("%Y-%m-%d"), "pageSize": "100"}
    url = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?" + urllib.parse.urlencode(params)
    # Réponse vide = aucune alerte sur la période (GDACS ne renvoie alors pas de JSON)
    d = lire_json(charger(s, hors_ligne, url) if hors_ligne else http(url, accept="application/json"))
    sortie = []
    for f in d.get("features", []):
        p = f.get("properties", {})
        typ, niv = p.get("eventtype", ""), (p.get("alertlevel") or "").lower()
        codes = [(p.get("iso3") or "").upper()] + \
                [(c.get("iso3") or "").upper() for c in p.get("affectedcountries") or []]
        codes = [c for c in dict.fromkeys(codes) if c]
        zone = next((pays_zone[c] for c in codes if c in pays_zone), None)
        noms = [noms_fr[c] for c in codes if c in noms_fr]
        pays = ", ".join(noms[:3]) if noms else (p.get("country") or "").strip()
        nom_evt = p.get("eventname") or ""
        titre = f"{TYPES_GDACS.get(typ, 'Catastrophe')}{' ' + nom_evt if nom_evt and typ in ('TC', 'VO') else ''}"
        titre += f" – {pays or 'zone non précisée'} ({NIVEAUX_GDACS.get(niv, niv)})"
        sev = (p.get("severitydata") or {}).get("severitytext")
        lien = (p.get("url") or {}).get("report") or \
            f"https://www.gdacs.org/report.aspx?eventtype={typ}&eventid={p.get('eventid')}"
        geo = (f.get("geometry") or {}).get("coordinates") or []
        coord = lieux.valider_coordonnees(geo[1], geo[0]) if len(geo) == 2 else None
        sortie.append(article(titre, lien, lire_date(p.get("fromdate")), s["nom"], sev or "",
                              zone=zone, theme="cli", lieu=pays or None,
                              lat=coord[0] if coord else None,
                              lon=coord[1] if coord else None))
    return sortie


def source_reliefweb(s, hors_ligne, pays_zone):
    """ReliefWeb v2 : https://api.reliefweb.int/v2/reports (POST JSON, appname obligatoire)."""
    appname = os.environ.get("RELIEFWEB_APPNAME", "")
    if not hors_ligne and not appname:
        raise RuntimeError("variable RELIEFWEB_APPNAME absente (appname à demander à ReliefWeb)")
    corps = {"limit": 50, "preset": "latest",
             "filter": {"field": "language.code", "value": "fr"},
             "fields": {"include": ["title", "url_alias", "date.created", "primary_country.iso3",
                                    "source.shortname", "theme.name", "disaster_type.name"]}}
    url = "https://api.reliefweb.int/v2/reports?appname=" + urllib.parse.quote(appname)
    d = json.loads(charger(s, hors_ligne, url, corps))
    sortie = []
    for r in d.get("data", []):
        f = r.get("fields", {})
        iso = ((f.get("primary_country") or {}).get("iso3") or "").upper()
        org = ", ".join(x.get("shortname", "") for x in (f.get("source") or [])[:2])
        catastrophe = bool(f.get("disaster_type"))
        sortie.append(article(f.get("title"), f.get("url_alias") or r.get("href"),
                              lire_date((f.get("date") or {}).get("created")),
                              f"ReliefWeb{' – ' + org if org else ''}",
                              zone=pays_zone.get(iso),
                              lieu=(f.get("primary_country") or {}).get("name"),
                              # thème : par mots-clés ; sinon catastrophe -> cli, sinon theme_defaut
                              indice_theme="cli" if catastrophe else s.get("theme_defaut", "dip")))
    return sortie


# ======================================================================
# Programme principal
# ======================================================================
# ======================================================================
# Trouver l'adresse d'un flux
# ======================================================================
# Une adresse de flux se devine mal : chaque site a sa convention, et un moteur de recherche
# ne les indexe pas. Plutôt que de laisser l'utilisateur essayer à la main, on demande au
# site lui-même. Deux pistes, dans cet ordre :
#   1. l'annonce officielle — <link rel="alternate" type="application/rss+xml"> dans le
#      <head> de la page d'accueil. C'est la méthode prévue par la norme, et elle donne
#      l'adresse exacte quand le site la respecte ;
#   2. les chemins conventionnels, essayés seulement si l'annonce ne donne rien.
CHEMINS_FLUX = ("/feed/", "/rss", "/rss.xml", "/feed.xml", "/index.xml", "/atom.xml",
                "/feeds/all.atom.xml", "/?feed=rss2", "/blog/feed/", "/actualites/feed/",
                "/news/feed/", "/publications/feed/")

TYPES_FLUX = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml",
              "application/json")   # JSON Feed : annoncé, mais feedparser ne le lit pas


class _AnnoncesFlux(HTMLParser):
    """Relève les <link rel="alternate" type="application/rss+xml" href="…"> d'une page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.trouves = []

    def handle_starttag(self, balise, attributs):
        if balise != "link":
            return
        a = {c.lower(): (v or "") for c, v in attributs}
        rel = a.get("rel", "").lower().split()
        if "alternate" in rel and a.get("type", "").lower().split(";")[0].strip() in TYPES_FLUX:
            if a.get("href"):
                self.trouves.append((a["href"], a.get("title", "")))


def flux_annonces(page: bytes, url_page: str) -> list:
    """Adresses de flux annoncées par une page HTML, rendues absolues et dédoublonnées."""
    lecteur = _AnnoncesFlux()
    try:
        lecteur.feed(page.decode("utf-8", "replace"))
    except Exception:
        return []   # une page cassée ne doit pas faire échouer la découverte
    vues, sortie = set(), []
    for href, _titre in lecteur.trouves:
        absolue = urllib.parse.urljoin(url_page, href.strip())
        if absolue.split(":")[0].lower() in ("http", "https") and absolue not in vues:
            vues.add(absolue)
            sortie.append(absolue)
    return sortie


def pages_a_sonder(url: str) -> list:
    """Pages HTML où chercher une annonce : la racine du site, puis le dossier de l'adresse.

    Beaucoup d'adresses fausses sont fausses « de peu » : https://site.org/fr/rss.xml quand
    le flux est sur https://site.org/fr/feed/. Sonder le dossier en plus de la racine
    rattrape les sites multilingues, qui annoncent un flux différent par langue.
    """
    d = urllib.parse.urlsplit(url)
    racine = urllib.parse.urlunsplit((d.scheme, d.netloc, "/", "", ""))
    pages = [racine]
    dossier = d.path.rsplit("/", 1)[0] + "/"
    if dossier not in ("/", ""):
        pages.append(urllib.parse.urlunsplit((d.scheme, d.netloc, dossier, "", "")))
    return pages


def chemins_candidats(url: str) -> list:
    """Adresses conventionnelles à essayer, à la racine du site puis sous son premier dossier."""
    d = urllib.parse.urlsplit(url)
    prefixes = [""]
    premier = d.path.strip("/").split("/")[0] if d.path.strip("/") else ""
    if premier and "." not in premier:      # /fr, /en, /blog… mais pas /rss.xml
        prefixes.append("/" + premier)
    vues, sortie = {url}, []
    for prefixe in prefixes:
        for chemin in CHEMINS_FLUX:
            candidate = urllib.parse.urlunsplit((d.scheme, d.netloc, prefixe + chemin, "", ""))
            if "?" in chemin:               # /?feed=rss2 : la query ne passe pas par le path
                chemin_nu, _, requete = chemin.partition("?")
                candidate = urllib.parse.urlunsplit(
                    (d.scheme, d.netloc, prefixe + chemin_nu, requete, ""))
            if candidate not in vues:
                vues.add(candidate)
                sortie.append(candidate)
    return sortie


def essayer_flux(url: str, lire=None) -> int:
    """Nombre d'articles exploitables à cette adresse, ou -1 si ce n'en est pas un."""
    lire = lire or (lambda u: http(u, accept="application/rss+xml, application/xml, text/xml, */*"))
    try:
        lot = lire_rss(lire(url), "essai")
    except Exception:
        return -1
    return sum(1 for a in lot if a["titre"] and a["lien"].startswith("http"))


def decouvrir_flux(url: str, lire=None, journal=None) -> dict:
    """Cherche l'adresse du flux d'un site à partir d'une adresse qui ne répond pas.

    Renvoie {"url", "articles", "annonce"} pour la première adresse qui rend des articles,
    ou None. `journal` reçoit chaque essai (adresse, résultat) pour l'affichage.
    """
    # un seul Accept pour les deux usages : la page d'accueil et les flux candidats. Le
    # serveur choisit de toute façon d'après l'adresse demandée.
    lire = lire or (lambda u: http(u, accept="text/html, application/xhtml+xml, "
                                             "application/rss+xml, application/atom+xml, "
                                             "application/xml;q=0.9, */*;q=0.8"))
    # Si le nom de machine n'existe pas, aucune des vingt-six adresses candidates n'existera
    # non plus : autant le dire tout de suite plutôt que de les essayer une par une.
    try:
        verifier_url(url)
    except AdresseRefusee as e:
        if journal is not None:
            journal.append((url, str(e)))
        return None
    annoncees = []
    for page in pages_a_sonder(url):
        try:
            annoncees += [u for u in flux_annonces(lire(page), page) if u not in annoncees]
        except Exception as e:
            if journal is not None:
                journal.append((page, f"page illisible ({type(e).__name__})"))
    annoncees = [u for u in annoncees if u != url]
    for candidate in annoncees + [c for c in chemins_candidats(url) if c not in annoncees]:
        n = essayer_flux(candidate, lire)
        if journal is not None:
            journal.append((candidate, f"{n} articles" if n >= 0 else "pas un flux"))
        if n > 0:
            return {"url": candidate, "articles": n, "annonce": candidate in annoncees}
    return None


def referentiels() -> dict:
    """Règles de tri et correspondances pays -> zone, chargées une fois par collecte.
    Les mots-clés des thèmes viennent de config/themes.json ; ceux des zones de regles.json."""
    regles = json.loads(REGLES.read_text(encoding="utf-8"))
    pays = json.loads(PAYS.read_text(encoding="utf-8"))
    noms_fr = pays.pop("noms_fr", {})
    return {"zones": compiler(regles["zones"]), "themes": compiler(base.REGLES_THEMES),
            "noms_fr": noms_fr,
            "pays_zone": {iso: z for z, codes in pays.items() if not z.startswith("_") for iso in codes}}


def collecter_source(s: dict, hors_ligne: bool, ref: dict, entetes: dict = None) -> list:
    """Interroge une source (configuration issue de base.config_collecte) et renvoie ses articles.
    `entetes` reçoit l'ETag et le Last-Modified de la réponse, pour la collecte suivante."""
    t = s["type"]
    if t == "rss":
        return source_rss(s, hors_ligne, entetes)
    if t == "gdacs":
        return source_gdacs(s, hors_ligne, ref["pays_zone"], ref["noms_fr"])
    if t == "reliefweb":
        return source_reliefweb(s, hors_ligne, ref["pays_zone"])
    raise ValueError(f"type inconnu : {t}")


def trier(a: dict, ref: dict) -> dict:
    """Étape 3 : zone et thème (ceux fournis par l'API d'abord, puis les mots-clés)."""
    a["zone"] = a["zone"] or classer(a["titre"], a["resume"], ref["zones"])
    if not a["theme"]:
        scores, premiere = classer_scores(a["titre"], a["resume"], ref["themes"])
        a["theme"] = meilleur(scores, premiere) or a["indice_theme"]
        a["scores_theme"] = scores   # gardé pour savoir si le tri a hésité (voir ia.py)
    return a


def completer_theme(cx, a: dict, classificateur, quand: str, rubriques: dict,
                    maintenant: datetime) -> bool:
    """Demande son thème au modèle de langage quand les mots-clés n'ont pas tranché.

    Renvoie vrai si le modèle a changé quelque chose. Un thème imposé par la source n'est
    jamais remis en cause : le flux sait mieux que quiconque ce qu'il publie.
    """
    if not classificateur or a.get("theme_force"):
        return False
    avant = a.get("theme")
    if quand == "manquant" and avant:
        return False
    if quand == "doute" and avant and not theme_hesitant(a.get("scores_theme")):
        return False
    theme = ia.classer_avec_cache(cx, a["titre"], normaliser(a["titre"]), a["resume"],
                                  rubriques, classificateur, maintenant)
    if not theme or theme == avant:
        return False
    a["theme"], a["theme_ia"] = theme, True
    return True


def zone_du_lieu(a: dict, trouve: dict, ref: dict):
    """Déduit la zone du pays du lieu trouvé, quand les mots-clés n'en ont pas donné.

    Le gazetteer connaît bien plus de lieux que la liste de mots-clés de regles.json :
    « Inondations à Lagos » ou « Saint-Pétersbourg : incendie » sont placés sur la carte
    mais n'ont aucun mot-clé de zone. Comme chaque lieu porte son code pays et que
    pays.json associe déjà pays et zone, la zone se déduit sans rien deviner.
    Sur 51 manchettes localisées, cela fait passer les articles sans zone de 12 à 0.
    """
    if a.get("zone") or not trouve or not ref:
        return
    a["zone"] = ref.get("pays_zone", {}).get(trouve.get("pays"))


def situer(a: dict, gaz: dict, cx=None, geocodeur=None, ref: dict = None) -> dict:
    """Étape 3 bis : où placer l'article sur la carte. Trois chances, dans cet ordre.

    1. Les coordonnées données par le flux (georss, geo:lat/long) ou par l'API de la source.
    2. Le nom de lieu que la source fournit explicitement — le pays d'une alerte GDACS, par
       exemple : cherché d'abord dans le gazetteer local, et seulement s'il y est inconnu,
       auprès du service de géocodage, avec cache. C'est le seul cas où l'on sort : un nom
       qu'une source nous a donné comme étant un lieu, pas un mot deviné dans une phrase.
    3. Un nom de lieu reconnu dans le titre ou le résumé (gazetteer local uniquement).

    Un article qu'on ne sait pas placer garde lat et lon à NULL : il reste dans la liste
    mais n'apparaît pas sur la carte. Mieux vaut pas de point qu'un point faux.
    """
    if a.get("lat") is not None and a.get("lon") is not None:
        a["lieu_origine"] = a.get("lieu_origine") or "flux"
        return a

    annonce = (a.get("lieu") or "").strip()
    if annonce:
        connus = gaz["index"].get(lieux.normaliser(annonce))
        if connus:
            lieu = connus[0]
            a["lat"], a["lon"] = lieu["y"], lieu["x"]
            a["lieu"], a["lieu_origine"] = lieu["n"], "gazetteer"
            zone_du_lieu(a, {"pays": lieu["c"]}, ref)
            return a
        if cx is not None and geocodeur is not None:
            trouve = lieux.geocoder_avec_cache(cx, annonce, geocodeur, MAINTENANT)
            if trouve:
                a["lat"], a["lon"] = trouve["lat"], trouve["lon"]
                a["lieu"], a["lieu_origine"] = trouve["nom"], trouve["origine"]
                return a

    trouve = lieux.extraire(a.get("titre", ""), a.get("resume", ""), gaz)
    if trouve:
        a["lat"], a["lon"] = trouve["lat"], trouve["lon"]
        a["lieu"], a["lieu_origine"] = trouve["nom"], trouve["origine"]
        zone_du_lieu(a, trouve, ref)
    return a


def collecter_toutes(cx, collecte: int, ref: dict, hors_ligne: bool, parallele: int,
                     forcer: bool = False) -> list:
    """Interroge toutes les sources actives en parallèle et renvoie leurs articles.

    Le réseau se fait dans un groupe de fils d'exécution ; les écritures en base restent
    dans le fil principal, au fur et à mesure que les résultats arrivent. Les sources sont
    parcourues dans l'ordre, donc le journal reste lisible malgré la concurrence.

    Quatre protections : une source déjà interrogée depuis moins que sa fréquence est laissée
    tranquille, une source en panne répétée est sautée (disjoncteur), un flux inchangé répond
    304 et n'est pas retéléchargé, et un seul téléchargement part à la fois par domaine.
    `forcer` ignore la fréquence et le disjoncteur : c'est ce que fait le bouton « Lancer la
    collecte » de l'administration, qui doit interroger tout de suite.
    """
    sources, sautees, attendent = [], 0, 0
    for s in base.lister_sources(cx, actives=True):
        if hors_ligne and not s.get("fichier_test"):
            continue   # hors ligne : seules les sources qui ont une réponse enregistrée
        if not hors_ligne and not forcer and not base.source_a_rafraichir(s, MAINTENANT):
            attendent += 1   # interrogée il y a moins que sa fréquence de rafraîchissement
            continue
        if not hors_ligne and not forcer and base.source_en_pause(s, MAINTENANT):
            base.noter_source(cx, collecte, s["nom"], False, 0,
                              f"source en pause jusqu'au {s['pause_jusqu_a'][:16].replace('T', ' ')} UTC "
                              f"après {s['echecs']} échecs")
            print(f"[pause]  {s['nom']} : {s['echecs']} échecs, reprise le "
                  f"{s['pause_jusqu_a'][:16].replace('T', ' ')} UTC")
            sautees += 1
            continue
        sources.append(base.config_collecte(s))
    if not sources:
        return []

    def interroger(s):
        """Exécuté dans un fil d'exécution : réseau et analyse, aucune écriture en base."""
        entetes = {}
        try:
            return s, collecter_source(s, hors_ligne, ref, entetes), entetes, None
        except Exception as e:   # une source en panne ne bloque pas les autres
            return s, None, entetes, e

    bruts, inchangees = [], 0
    with ThreadPoolExecutor(max_workers=max(1, min(parallele, len(sources)))) as pool:
        for s, lot, entetes, erreur in pool.map(interroger, sources):
            if erreur is None:
                if s.get("theme_impose"):        # flux thématique : le thème ne se devine pas
                    for a in lot:
                        a["theme"] = a["theme"] or s["theme_impose"]
                        a["theme_force"] = True
                for a in lot:
                    a["langue"] = s.get("langue", "fr")
                bruts += lot
                base.noter_source(cx, collecte, s["nom"], True, len(lot))
                base.noter_etat_source(cx, s.get("id"), True, MAINTENANT,
                                       entetes.get("etag"), entetes.get("derniere_modif"))
                print(f"[ok]     {s['nom']} : {len(lot)} éléments")
            elif isinstance(erreur, NonModifie):
                inchangees += 1
                base.noter_source(cx, collecte, s["nom"], True, 0, "inchangé depuis la dernière collecte")
                base.noter_etat_source(cx, s.get("id"), True, MAINTENANT)
                print(f"[304]    {s['nom']} : inchangé, rien à retélécharger")
            else:
                base.noter_source(cx, collecte, s["nom"], False, 0, str(erreur))
                base.noter_etat_source(cx, s.get("id"), False, MAINTENANT)
                print(f"[erreur] {s['nom']} : {erreur}")
    if inchangees or sautees or attendent:
        print(f"{inchangees} flux inchangés (304), {sautees} sources en pause (disjoncteur), "
              f"{attendent} pas encore dues (fréquence de rafraîchissement).")
    return bruts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Collecte et tri des actualités pour MAP")
    ap.add_argument("--hors-ligne", action="store_true", help="utilise les fichiers de test au lieu des API")
    ap.add_argument("--fichiers", nargs="*", help="flux RSS enregistrés : nom=chemin ou chemin")
    ap.add_argument("--base", default=str(base.BASE), help="base SQLite (défaut : donnees/map.sqlite)")
    ap.add_argument("--migrer", default=str(base.ANCIEN_JSON),
                    help="ancien data.json repris si la base est vide (migration)")
    ap.add_argument("--forcer", action="store_true",
                    help="interroge toutes les sources actives sans tenir compte de leur "
                         "fréquence de rafraîchissement ni du disjoncteur")
    ap.add_argument("--sans-ia", action="store_true",
                    help="ne pas appeler le modèle de langage, même si config/ia.json est actif")
    ap.add_argument("--parallele", type=int, default=PARALLELE, metavar="N",
                    help=f"sources interrogées en même temps (défaut : {PARALLELE}, "
                         "une seule à la fois par domaine)")
    args = ap.parse_args(argv)
    if args.parallele < 1:
        ap.error("--parallele doit valoir au moins 1")

    ref = referentiels()

    cx = base.ouvrir(args.base)
    if not cx.execute("SELECT 1 FROM articles LIMIT 1").fetchone():
        n = base.importer_json(cx, args.migrer, normaliser)   # première utilisation de la base
        if n:
            print(f"Migration : {n} articles repris de l'ancien {Path(args.migrer).name}.")
    collecte = base.debut_collecte(cx, MAINTENANT, args.hors_ligne)
    supprimes = base.purger(cx, MAINTENANT, collecte)   # D05 : plus de 30 jours -> supprimé
    if supprimes:
        print(f"Suppression : {supprimes} articles publiés il y a plus de 30 jours.")

    # 1-2. Sources et collecte
    bruts = []
    if args.fichiers:
        for f in args.fichiers:
            nom, _, chemin = f.rpartition("=")
            lot = lire_rss(Path(chemin).read_bytes(), nom or Path(chemin).stem)
            base.noter_source(cx, collecte, nom or Path(chemin).stem, True, len(lot))
            bruts += lot
    else:
        bruts += collecter_toutes(cx, collecte, ref, args.hors_ligne, args.parallele,
                                  args.forcer)

    # 3. Tri et localisation, puis 4. stockage dans la base
    compte = {"nouveau": 0, "regroupe": 0, "maj": 0, "inchange": 0, "ancien": 0}
    gaz = lieux.charger_gazetteer()
    geocodeur = lieux.charger_geocodeur()
    classificateur = None if args.sans_ia else ia.charger_classificateur()
    quand = ia.quand_demander() if classificateur else "doute"
    rubriques = ia.rubriques(base.THEMES_DECLARES) if classificateur else {}
    if classificateur:
        print(f"Classement par IA : {classificateur.modele}, au plus "
              f"{classificateur.budget} appels, déclenché « {quand} ».")
    situes = par_ia = 0
    for a in bruts:
        if not a["titre"] or not a["lien"].startswith("http"):
            continue
        trier(a, ref)
        par_ia += completer_theme(cx, a, classificateur, quand, rubriques, MAINTENANT)
        situer(a, gaz, cx, geocodeur, ref)
        situes += a.get("lat") is not None
        compte[base.enregistrer(cx, a, normaliser(a["titre"]), MAINTENANT, collecte)] += 1
    base.fin_collecte(cx, collecte, base.maintenant_utc(), len(bruts), compte["nouveau"],
                      compte["regroupe"])

    # Récapitulatif du jour : un appel par thème, une fois toutes les quelques heures.
    # Il est écrit ici et non à l'affichage : une page qui s'ouvre ne doit jamais déclencher
    # un appel à un service extérieur.
    recap = ia.reglages_recap() if classificateur else {"actif": False}
    if classificateur and recap["actif"]:
        bilans = ia.recaps_du_jour(cx, base.jour_utc(MAINTENANT),
                                   ia.rubriques(base.THEMES_DECLARES), classificateur,
                                   MAINTENANT, recap)
        cx.commit()
        ecrits = [b["theme"] for b in bilans if b["etat"] == "ecrit"]
        if ecrits:
            print(f"Récap : {len(ecrits)} thèmes résumés ({', '.join(ecrits)}).")

    publies = cx.execute("SELECT COUNT(*) FROM fil WHERE date_pub >= ?",
                         ((MAINTENANT - base.DUREE_FIL).isoformat(),)).fetchone()[0]
    a_trier = cx.execute("SELECT COUNT(*) FROM a_trier").fetchone()[0]
    total = cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    evenements = cx.execute("SELECT COUNT(*) FROM evenements").fetchone()[0]
    cx.close()
    print(f"{compte['nouveau']} nouveaux événements, {compte['regroupe']} articles rattachés "
          f"à un événement déjà connu, {compte['ancien']} trop anciens ignorés.")
    print(f"Événements : {evenements} au total "
          f"(un événement = un point sur la carte, avec toutes ses sources).")
    if classificateur and classificateur.appels:
        reste = "budget épuisé" if classificateur.budget <= 0 else f"{classificateur.budget} restants"
        print(f"IA   : {classificateur.appels} appels ({reste}), {par_ia} thèmes corrigés, "
              f"{classificateur.echecs} échecs.")
        if classificateur.derniere_erreur:
            print(f"       dernière erreur : {classificateur.derniere_erreur}")
    elif classificateur:
        print("IA   : aucun appel — les mots-clés ont suffi, ou tout était déjà en cache.")
    if bruts:
        print(f"Carte : {situes} articles sur {len(bruts)} placés "
              f"({100 * situes / len(bruts):.0f} %) ; les autres restent dans la liste.")
    print(f"Base : {total} articles au total, {a_trier} à trier (uv run collecteur/admin.py a-trier).")
    print(f"Fil  : {publies} articles visibles sur le site (30 derniers jours), servis par l'API.")
    if not bruts and not total:
        sys.exit("Aucune source n'a pu être lue.")


if __name__ == "__main__":
    main()
