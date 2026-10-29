#!/usr/bin/env python3
"""Construit donnees/gazetteer.json.gz, la base de lieux utilisée pour placer les articles.

À lancer UNE FOIS par le mainteneur, pas à chaque collecte. Le fichier produit est versionné :
l'installation et la collecte n'ont donc besoin ni de réseau, ni de geonamescache (182 Mio),
ni d'un service de géocodage.

    uv run --with geonamescache outils/construire_gazetteer.py

Contenu :
  - les pays, avec leur nom français (collecteur/pays.json, déjà relu par l'équipe),
    leur nom anglais et les coordonnées de leur capitale ;
  - les villes de plus de 50 000 habitants, plus toutes les capitales ;
  - les exonymes français de config/exonymes.json (Londres, Pékin, Le Caire…).

Choix volontaire : on ne reprend PAS les « alternatenames » de GeoNames. Elles mélangent
toutes les langues sans étiquette (London y donne Landan, Londe, Ljondan…) et feraient
placer des articles n'importe où. Mieux vaut un lieu manquant qu'un point faux.
"""
import gzip
import json
import sys
import unicodedata
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "donnees" / "gazetteer.json.gz"
POPULATION_MINIMALE = 50_000


def sans_accents(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def main():
    try:
        import geonamescache
    except ImportError:
        sys.exit("geonamescache manquant. Lancer :\n"
                 "  uv run --with geonamescache outils/construire_gazetteer.py")

    gc = geonamescache.GeonamesCache()
    pays_json = json.loads((RACINE / "collecteur" / "pays.json").read_text(encoding="utf-8"))
    noms_fr = pays_json.get("noms_fr", {})
    exonymes = json.loads((RACINE / "config" / "exonymes.json").read_text(encoding="utf-8"))

    villes_gn = gc.get_cities()
    pays_gn = gc.get_countries()
    iso2_vers_iso3 = {c["iso"]: c["iso3"] for c in pays_gn.values()}
    capitales = {(c["capital"], c["iso"]) for c in pays_gn.values() if c.get("capital")}

    lieux = []

    # --- pays : le point retenu est leur capitale, convention habituelle des cartes de presse
    par_pays = {}
    for v in villes_gn.values():
        par_pays.setdefault(v["countrycode"], []).append(v)
    for liste in par_pays.values():
        liste.sort(key=lambda v: -v["population"])
    for c in pays_gn.values():
        villes_du_pays = par_pays.get(c["iso"], [])
        cap = next((v for v in villes_du_pays if v["name"] == c.get("capital")), None)
        if not cap and c.get("capital"):   # capitale absente ou orthographiée autrement
            cible = sans_accents(c["capital"])
            cap = next((v for v in villes_du_pays if sans_accents(v["name"]) == cible), None)
        if not cap:   # faute de capitale, la plus grande ville sert d'ancrage
            cap = villes_du_pays[0] if villes_du_pays else None
        if not cap:
            continue
        noms = [c["name"]]
        fr = noms_fr.get(c["iso3"])
        if fr and fr != c["name"]:
            noms.append(fr)
        lieux.append({"t": "pays", "n": noms[0], "noms": noms, "y": round(cap["latitude"], 4),
                      "x": round(cap["longitude"], 4), "c": c["iso3"], "p": c["population"]})

    # --- villes
    for v in villes_gn.values():
        est_capitale = (v["name"], v["countrycode"]) in capitales
        if v["population"] < POPULATION_MINIMALE and not est_capitale:
            continue
        noms = [v["name"]]
        for alias in exonymes.get("villes", {}).get(v["name"], []):
            if alias not in noms:
                noms.append(alias)
        lieux.append({"t": "ville", "n": v["name"], "noms": noms, "y": round(v["latitude"], 4),
                      "x": round(v["longitude"], 4),
                      "c": iso2_vers_iso3.get(v["countrycode"], v["countrycode"]),
                      "p": v["population"]})

    # exonymes déclarés pour un lieu absent de GeoNames (région, territoire…)
    for nom, d in exonymes.get("lieux", {}).items():
        lieux.append({"t": d.get("type", "region"), "n": nom, "noms": [nom] + d.get("noms", []),
                      "y": d["lat"], "x": d["lon"], "c": d.get("pays"), "p": d.get("population", 0)})

    lieux.sort(key=lambda l: -l["p"])
    corps = json.dumps({"_genere_par": "outils/construire_gazetteer.py",
                        "population_minimale": POPULATION_MINIMALE,
                        "lieux": lieux}, ensure_ascii=False, separators=(",", ":"))
    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    SORTIE.write_bytes(gzip.compress(corps.encode("utf-8"), 9))
    noms_total = sum(len(l["noms"]) for l in lieux)
    print(f"{len(lieux)} lieux ({sum(1 for l in lieux if l['t'] == 'pays')} pays, "
          f"{sum(1 for l in lieux if l['t'] == 'ville')} villes), {noms_total} noms")
    print(f"{SORTIE.relative_to(RACINE)} : {SORTIE.stat().st_size / 1024:.0f} Kio compressés, "
          f"{len(corps) / 1024:.0f} Kio bruts")


if __name__ == "__main__":
    main()
