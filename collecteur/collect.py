#!/usr/bin/env python3
"""
MAP – Monitoring de l'Actualité Planétaire
Collecteur : sources -> collecte -> tri -> stockage (chaîne de l'information, partie 6).

Types de sources (collecteur/sources.json) :
    rss        flux RSS 2.0 ou Atom d'un média
    gdacs      API GDACS (alertes catastrophes ONU / Commission européenne, sans clé)
    reliefweb  API ReliefWeb v2 (rapports humanitaires OCHA, appname approuvé requis)

Utilisation (bibliothèques gérées par uv, voir pyproject.toml) :
    uv run collecteur/collect.py                  # interroge les sources en ligne
    uv run collecteur/collect.py --hors-ligne     # utilise les fichiers de test (tests/)
    uv run collecteur/collect.py --fichiers a.xml # test sur des flux RSS enregistrés

Les articles sont stockés dans la base SQLite donnees/map.sqlite (voir base.py).
Le site les lit à travers l'API (serveur/app.py) : aucune donnée n'est écrite dans le front.

Seuls le titre, la source, la date et le lien sont conservés (exigence C03).
"""
import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser

import base   # stockage SQLite (collecteur/base.py)

RACINE = Path(__file__).resolve().parent.parent
SOURCES = RACINE / "collecteur" / "sources.json"
REGLES = RACINE / "collecteur" / "regles.json"
PAYS = RACINE / "collecteur" / "pays.json"
POIDS_TITRE, POIDS_RESUME = 3, 1
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
def compiler(regles: dict) -> dict:
    sortie = {}
    for cat, mots in regles.items():
        motifs = []
        for mot in mots:
            m = normaliser(mot).strip()
            fin = r"[a-z0-9]*(?= )" if mot.endswith("*") else r"(?= )"
            motifs.append(re.compile(r"(?<= )" + re.escape(m) + fin))
        sortie[cat] = motifs
    return sortie


def classer(titre: str, resume: str, motifs: dict):
    """Catégorie qui a le plus de points (titre x3, résumé x1), ou None."""
    t, r = normaliser(titre), normaliser(resume)
    scores, premiere = {}, {}
    for cat, regexes in motifs.items():
        s, pos = 0, None
        for rx in regexes:
            hits = [m.start() for m in rx.finditer(t)]
            s += POIDS_TITRE * len(hits) + POIDS_RESUME * len(rx.findall(r))
            if hits:
                pos = min(hits) if pos is None else min(pos, min(hits))
        if s:
            scores[cat] = s
            premiere[cat] = pos if pos is not None else 10**6
    if not scores:
        return None
    return sorted(scores, key=lambda c: (-scores[c], premiere[c]))[0]


# ======================================================================
# Accès réseau
# ======================================================================
def http(url: str, donnees: dict = None, essais: int = 3, pause: float = 6,
         accept: str = "application/json, application/xml, text/xml, */*") -> bytes:
    """GET (ou POST JSON si donnees). Réessaie sur 429 / 5xx, en respectant l'en-tête Retry-After."""
    corps = json.dumps(donnees).encode() if donnees is not None else None
    for i in range(essais):
        req = urllib.request.Request(url, data=corps, headers={
            "User-Agent": UA, "Accept": accept,
            **({"Content-Type": "application/json"} if corps else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < essais - 1:
                attente = pause * (i + 1)
                try:
                    attente = max(attente, float(e.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                time.sleep(min(attente, 120))
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


def charger(source: dict, hors_ligne: bool, url: str = None, donnees: dict = None) -> bytes:
    if hors_ligne:
        return (RACINE / source["fichier_test"]).read_bytes()
    return http(url, donnees)


def article(titre, lien, date, source, resume="", zone=None, theme=None, indice_theme=None):
    return {"titre": nettoyer_html(titre), "lien": (lien or "").split("#")[0].strip(),
            "date": date, "source": source, "resume": nettoyer_html(resume),
            "zone": zone, "theme": theme, "indice_theme": indice_theme}


# ======================================================================
# Connecteurs
# ======================================================================
def lire_rss(contenu: bytes, nom: str):
    """Flux RSS 0.9x/1.0/2.0 ou Atom, lu par feedparser (tolère les flux mal formés)."""
    flux = feedparser.parse(contenu)
    if flux.bozo and not flux.entries:
        raise ValueError(f"flux illisible ({flux.bozo_exception})")
    sortie = []
    for e in flux.entries:
        cats = " ".join(t.get("term") or "" for t in e.get("tags", []))
        sortie.append(article(e.get("title", ""), e.get("link", ""),
                              lire_date(e.get("published") or e.get("updated")), nom,
                              (e.get("summary") or "") + " " + cats))
    return sortie


def source_rss(s, hors_ligne):
    return lire_rss(charger(s, hors_ligne, s["url"]), s["nom"])


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
        sortie.append(article(titre, lien, lire_date(p.get("fromdate")), s["nom"], sev or "",
                              zone=zone, theme="cli"))
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
                              # thème : par mots-clés ; sinon catastrophe -> cli, sinon theme_defaut
                              indice_theme="cli" if catastrophe else s.get("theme_defaut", "dip")))
    return sortie


# ======================================================================
# Programme principal
# ======================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Collecte et tri des actualités pour MAP")
    ap.add_argument("--hors-ligne", action="store_true", help="utilise les fichiers de test au lieu des API")
    ap.add_argument("--fichiers", nargs="*", help="flux RSS enregistrés : nom=chemin ou chemin")
    ap.add_argument("--base", default=str(base.BASE), help="base SQLite (défaut : donnees/map.sqlite)")
    ap.add_argument("--migrer", default=str(base.ANCIEN_JSON),
                    help="ancien data.json repris si la base est vide (migration)")
    args = ap.parse_args(argv)

    regles = json.loads(REGLES.read_text(encoding="utf-8"))
    zones, themes = compiler(regles["zones"]), compiler(regles["themes"])
    pays = json.loads(PAYS.read_text(encoding="utf-8"))
    noms_fr = pays.pop("noms_fr", {})
    pays_zone = {iso: z for z, codes in pays.items() if not z.startswith("_") for iso in codes}
    conf = json.loads(SOURCES.read_text(encoding="utf-8"))

    cx = base.ouvrir(args.base)
    if not cx.execute("SELECT 1 FROM articles LIMIT 1").fetchone():
        n = base.importer_json(cx, args.migrer, normaliser)   # première utilisation de la base
        if n:
            print(f"Migration : {n} articles repris de l'ancien {Path(args.migrer).name}.")
    collecte = base.debut_collecte(cx, MAINTENANT, args.hors_ligne)

    # 1-2. Sources et collecte
    bruts = []
    if args.fichiers:
        for f in args.fichiers:
            nom, _, chemin = f.rpartition("=")
            lot = lire_rss(Path(chemin).read_bytes(), nom or Path(chemin).stem)
            base.noter_source(cx, collecte, nom or Path(chemin).stem, True, len(lot))
            bruts += lot
    else:
        for s in conf["sources"]:
            if not s.get("actif") or (args.hors_ligne and s["type"] == "rss"):
                continue
            try:
                t = s["type"]
                if t == "rss":
                    lot = source_rss(s, args.hors_ligne)
                elif t == "gdacs":
                    lot = source_gdacs(s, args.hors_ligne, pays_zone, noms_fr)
                elif t == "reliefweb":
                    lot = source_reliefweb(s, args.hors_ligne, pays_zone)
                else:
                    raise ValueError(f"type inconnu : {t}")
                bruts += lot
                base.noter_source(cx, collecte, s["nom"], True, len(lot))
                print(f"[ok]     {s['nom']} : {len(lot)} éléments")
            except Exception as e:   # une source en panne ne bloque pas les autres
                base.noter_source(cx, collecte, s["nom"], False, 0, str(e))
                print(f"[erreur] {s['nom']} : {e}")

    # 3. Tri, puis 4. stockage dans la base
    compte = {"nouveau": 0, "maj": 0, "inchange": 0, "doublon": 0}
    for a in bruts:
        if not a["titre"] or not a["lien"].startswith("http"):
            continue
        a["zone"] = a["zone"] or classer(a["titre"], a["resume"], zones)
        a["theme"] = a["theme"] or classer(a["titre"], a["resume"], themes) or a["indice_theme"]
        compte[base.enregistrer(cx, a, normaliser(a["titre"]), MAINTENANT, collecte)] += 1
    base.fin_collecte(cx, collecte, base.maintenant_utc(), len(bruts), compte["nouveau"], compte["doublon"])

    publies = cx.execute("SELECT COUNT(*) FROM fil WHERE date_pub >= ?",
                         ((MAINTENANT - base.DUREE_FIL).isoformat(),)).fetchone()[0]
    a_trier = cx.execute("SELECT COUNT(*) FROM a_trier").fetchone()[0]
    total = cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    cx.close()
    print(f"{compte['nouveau']} nouveaux articles, {compte['doublon']} doublons écartés.")
    print(f"Base : {total} articles au total, {a_trier} à trier (uv run collecteur/admin.py a-trier).")
    print(f"Fil  : {publies} articles visibles sur le site (30 derniers jours), servis par l'API.")
    if not bruts and not total:
        sys.exit("Aucune source n'a pu être lue.")


if __name__ == "__main__":
    main()
