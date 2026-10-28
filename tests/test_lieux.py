"""Tests de la localisation des articles (collecteur/lieux.py).

Le fil conducteur : mieux vaut un article sans point qu'un point faux. Les tests vérifient
donc autant ce qui doit être reconnu que ce qui doit être laissé de côté.
"""
import json
import sys
import time
from pathlib import Path

import feedparser
import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base  # noqa: E402
import collect  # noqa: E402
import lieux  # noqa: E402

ANNOTES = json.loads((RACINE / "tests" / "lieux_annotes.json").read_text(encoding="utf-8"))["cas"]


@pytest.fixture(scope="module")
def gaz():
    return lieux.charger_gazetteer()


# ---------- gazetteer ----------
def test_gazetteer_charge(gaz):
    assert len(gaz["index"]) > 10_000
    assert gaz["index"]["france"][0]["t"] == "pays"
    assert gaz["index"]["londres"][0]["n"] == "Londres"
    assert gaz["index"]["kharkiv"][0]["c"] == "UKR"


def test_un_lieu_n_est_jamais_indexe_deux_fois(gaz):
    """« Taiwan » et « Taïwan » donnent la même clé : sans garde-fou le lieu compterait double."""
    for cle, entrees in gaz["index"].items():
        assert len({id(l) for l in entrees}) == len(entrees), cle


def test_noms_trop_courts_et_mots_courants_ecartes(gaz):
    for ignore in ("gao", "ho", "nice", "canton", "tours", "union"):
        assert ignore not in gaz["index"], ignore


def test_la_stopliste_est_respectee(tmp_path):
    stop = tmp_path / "ignores.json"
    stop.write_text(json.dumps({"ignores": ["Londres"]}), encoding="utf-8")
    g = lieux.charger_gazetteer(stopliste=stop)
    assert "londres" not in g["index"] and "london" in g["index"]


# ---------- coordonnées fournies par le flux ----------
def _entree(xml: bytes):
    return feedparser.parse(xml).entries[0]


ENTETE = (b'<?xml version="1.0"?><rss version="2.0" '
          b'xmlns:georss="http://www.georss.org/georss" '
          b'xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#"><channel><title>t</title>')


@pytest.mark.parametrize("balise, attendu", [
    (b"<georss:point>48.85 2.35</georss:point>", (48.85, 2.35)),
    (b"<geo:lat>35.68</geo:lat><geo:long>139.69</geo:long>", (35.68, 139.69)),
    (b'<georss:where><gml:Point xmlns:gml="http://www.opengis.net/gml">'
     b"<gml:pos>-33.86 151.2</gml:pos></gml:Point></georss:where>", (-33.86, 151.2)),
    (b"", None),
    (b"<georss:point>0 0</georss:point>", None),          # « île Null » : pas une position
    (b"<geo:lat>200</geo:lat><geo:long>10</geo:long>", None),
    (b"<geo:lat>abc</geo:lat><geo:long>10</geo:long>", None),
])
def test_coordonnees_du_flux(balise, attendu):
    xml = ENTETE + b"<item><title>A</title><link>http://x/1</link>" + balise + b"</item></channel></rss>"
    assert lieux.coordonnees_du_flux(_entree(xml)) == attendu


def test_lire_rss_remonte_les_coordonnees():
    xml = (ENTETE + b"<item><title>Seisme</title><link>http://x/1</link>"
           b"<georss:point>35.68 139.69</georss:point></item></channel></rss>")
    a = collect.lire_rss(xml, "test")[0]
    assert (a["lat"], a["lon"]) == (35.68, 139.69)


# ---------- extraction par le texte ----------
@pytest.mark.parametrize("cas", ANNOTES, ids=lambda c: c["titre"][:40])
def test_corpus_annote(gaz, cas):
    trouve = lieux.extraire(cas["titre"], cas["resume"], gaz)
    assert (trouve["nom"] if trouve else None) == cas["attendu"]


def test_precision_et_rappel_du_corpus(gaz):
    """Garde-fou global : la precision ne doit jamais retomber, même si un cas est ajouté."""
    vp = fp = fn = 0
    for c in ANNOTES:
        r = lieux.extraire(c["titre"], c["resume"], gaz)
        nom = r["nom"] if r else None
        if nom == c["attendu"]:
            vp += bool(c["attendu"])
        elif nom:
            fp += 1
        else:
            fn += 1
    assert fp == 0, f"{fp} articles mal placés"
    assert vp / max(vp + fn, 1) >= 0.95


def test_un_nom_long_consomme_ses_mots(gaz):
    """« New Delhi » ne doit pas faire gagner « Delhi » au passage."""
    assert lieux.extraire("Inde : New Delhi suffoque", "", gaz)["nom"] == "New Delhi"
    assert lieux.extraire("Attentat à New York", "", gaz)["nom"] == "New York"


def test_minuscule_initiale_ignoree(gaz):
    """Sans la règle de la majuscule, « nice » ou « marche » placeraient des points partout."""
    assert lieux.extraire("réunion à bruxelles", "", gaz) is None
    assert lieux.extraire("Réunion à Bruxelles", "", gaz)["nom"] == "Bruxelles"


def test_homonyme_le_plus_peuple_gagne(gaz):
    """Moscow existe en Russie et dans l'Idaho ; London en Angleterre et en Ontario."""
    assert lieux.extraire("Moscow protests", "", gaz)["pays"] == "RUS"
    assert lieux.extraire("London summit", "", gaz)["pays"] == "GBR"


def test_le_pays_cite_departage_les_homonymes(gaz):
    """Deux villes du même nom : celle du pays mentionné dans le texte l'emporte."""
    trouve = lieux.extraire("Santiago en deuil", "Un séisme a frappé le Chili", gaz)
    assert trouve["pays"] == "CHL"


def test_une_ville_place_mieux_qu_un_pays(gaz):
    trouve = lieux.extraire("Allemagne : manifestation à Hambourg", "", gaz)
    assert trouve["nom"] == "Hambourg" and trouve["type"] == "ville"


def test_le_titre_pese_plus_que_le_resume(gaz):
    trouve = lieux.extraire("Crise à Tokyo", "Des observateurs à Paris, Paris et Paris", gaz)
    assert trouve["nom"] == "Tokyo"


# ---------- géocodeur extérieur ----------
class FauxService:
    """Service de géocodage simulé : compte les appels pour vérifier le cache."""

    def __init__(self, reponse: bytes):
        self.reponse, self.appels = reponse, []

    def __call__(self, url):
        self.appels.append(url)
        return self.reponse


PHOTON = json.dumps({"features": [{"geometry": {"type": "Point", "coordinates": [12.5, 41.9]},
                                   "properties": {"name": "Roma"}}]}).encode()
NOMINATIM = json.dumps([{"lat": "41.9", "lon": "12.5", "display_name": "Roma, Italia"}]).encode()


@pytest.mark.parametrize("service, reponse, attendu", [
    ("photon", PHOTON, ("Roma", 41.9, 12.5)),
    ("nominatim", NOMINATIM, ("Roma, Italia", 41.9, 12.5)),
])
def test_geocodeur_lit_les_deux_formats(service, reponse, attendu):
    faux = FauxService(reponse)
    g = lieux.Geocodeur(service, "http://exemple.test", "MAP-test", 100, ouvrir_url=faux)
    r = g.chercher("Rome")
    assert (r["nom"], r["lat"], r["lon"]) == attendu
    assert "Rome" in faux.appels[0]


def test_service_inconnu_refuse():
    with pytest.raises(ValueError, match="inconnu"):
        lieux.Geocodeur("maps-maison", "http://x", "ua")


def test_limite_de_debit_respectee():
    faux = FauxService(PHOTON)
    g = lieux.Geocodeur("photon", "http://x", "ua", debit_par_seconde=5, ouvrir_url=faux)
    debut = time.perf_counter()
    for _ in range(3):
        g.chercher("Rome")
    assert time.perf_counter() - debut >= 2 / 5 * 0.9   # 3 requêtes = au moins 2 intervalles


def test_cache_un_lieu_n_est_geocode_qu_une_fois(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    faux = FauxService(PHOTON)
    g = lieux.Geocodeur("photon", "http://x", "ua", 100, ouvrir_url=faux)
    for _ in range(4):
        r = lieux.geocoder_avec_cache(cx, "Rome", g)
    assert r["lat"] == 41.9
    assert len(faux.appels) == 1, "le service a été interrogé plusieurs fois pour le même lieu"
    ligne = cx.execute("SELECT * FROM lieux_geocodes").fetchone()
    assert ligne["cle"] == "rome" and ligne["utilisations"] == 4
    cx.close()


def test_cache_memorise_aussi_les_echecs(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    faux = FauxService(b'{"features": []}')
    g = lieux.Geocodeur("photon", "http://x", "ua", 100, ouvrir_url=faux)
    assert lieux.geocoder_avec_cache(cx, "Zzzz inexistant", g) is None
    assert lieux.geocoder_avec_cache(cx, "Zzzz inexistant", g) is None
    assert len(faux.appels) == 1, "un introuvable a été redemandé"
    cx.close()


def test_service_en_panne_ne_casse_pas_la_collecte(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")

    def tombe(url):
        raise OSError("service indisponible")

    g = lieux.Geocodeur("photon", "http://x", "ua", 100, ouvrir_url=tombe)
    assert lieux.geocoder_avec_cache(cx, "Rome", g) is None
    assert cx.execute("SELECT COUNT(*) FROM lieux_geocodes").fetchone()[0] == 0  # rien de figé
    cx.close()


def test_geocodage_desactive_par_defaut():
    assert lieux.charger_geocodeur() is None


def test_user_agent_bidon_refuse(tmp_path):
    f = tmp_path / "geocodage.json"
    f.write_text(json.dumps({"actif": True, "service": "photon", "url": "http://x",
                             "user_agent": "MAP (contact : a-changer@example.org)"}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="user_agent"):
        lieux.charger_geocodeur(f)


# ---------- chaîne complète ----------
def test_ordre_de_priorite(gaz, tmp_path):
    """Les coordonnées du flux l'emportent sur tout le reste."""
    a = collect.article("Séisme à Tokyo", "http://x/1", None, "src", lat=48.85, lon=2.35)
    collect.situer(a, gaz)
    assert (a["lat"], a["lon"]) == (48.85, 2.35) and a["lieu_origine"] == "flux"

    b = collect.article("Séisme à Tokyo", "http://x/2", None, "src")
    collect.situer(b, gaz)
    assert b["lieu"] == "Tokyo" and b["lieu_origine"] == "gazetteer"

    c = collect.article("Rapport humanitaire", "http://x/3", None, "src", lieu="Tchad")
    collect.situer(c, gaz)
    # le nom français est reconnu ET affiché : depuis la v16 les pays portent leur nom
    # français, pris dans collecteur/pays.json
    assert c["lieu"] == "Tchad" and c["lieu_origine"] == "gazetteer"

    d = collect.article("Rien de localisable", "http://x/4", None, "src")
    collect.situer(d, gaz)
    assert d["lat"] is None and d["lon"] is None


def test_lieu_annonce_par_la_source_passe_par_le_geocodeur(gaz, tmp_path):
    """Un nom que le gazetteer ignore, mais qu'une source annonce comme un lieu."""
    cx = base.ouvrir(tmp_path / "map.sqlite")
    faux = FauxService(PHOTON)
    g = lieux.Geocodeur("photon", "http://x", "ua", 100, ouvrir_url=faux)
    a = collect.article("Rapport", "http://x/1", None, "src", lieu="Lieu Totalement Inconnu")
    collect.situer(a, gaz, cx, g)
    assert (a["lat"], a["lon"]) == (41.9, 12.5) and a["lieu_origine"] == "photon"
    assert len(faux.appels) == 1
    cx.close()


def test_coordonnees_enregistrees_en_base(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    a = collect.article("Séisme à Tokyo", "https://x.test/1",
                        base.maintenant_utc(), "src", lat=35.68, lon=139.69)
    a["lieu"], a["lieu_origine"], a["zone"], a["theme"] = "Tokyo", "flux", "asi", "cli"
    assert base.enregistrer(cx, a, "tokyo", base.maintenant_utc(), None) == "nouveau"
    r = cx.execute("SELECT lat, lon, lieu, lieu_origine FROM articles").fetchone()
    assert (r["lat"], r["lon"], r["lieu"], r["lieu_origine"]) == (35.68, 139.69, "Tokyo", "flux")
    cx.close()


# ---------- la zone se déduit du lieu ----------
@pytest.mark.parametrize("titre, zone", [
    ("Inondations à Lagos : des milliers de déplacés", "afr"),
    ("Saint-Pétersbourg : incendie dans un entrepôt", "rus"),
    ("Cracovie accueille le sommet de l'OTAN", "eur"),
    ("La Mecque se prépare au pèlerinage", "mena"),
    ("Nouvelle-Orléans : alerte cyclonique", "ame"),
])
def test_la_zone_se_deduit_du_lieu_trouve(gaz, titre, zone):
    """Le gazetteer connaît plus de lieux que les mots-clés de zone : la zone se déduit du
    pays du lieu, sinon l'article serait placé sur la carte mais pas diffusé (D04)."""
    ref = collect.referentiels()
    a = collect.article(titre, "https://x.test/1", None, "src")
    collect.trier(a, ref)
    assert a["zone"] is None, "ce cas n'est intéressant que si les mots-clés échouent"
    collect.situer(a, gaz, ref=ref)
    assert a["lat"] is not None and a["zone"] == zone


def test_les_mots_cles_restent_prioritaires_sur_la_deduction(gaz):
    ref = collect.referentiels()
    a = collect.article("Ukraine : frappes sur Kharkiv", "https://x.test/1", None, "src")
    collect.trier(a, ref)
    zone_mots_cles = a["zone"]
    collect.situer(a, gaz, ref=ref)
    assert a["zone"] == zone_mots_cles == "rus"


def test_sans_referentiel_aucune_deduction(gaz):
    a = collect.article("Inondations à Lagos", "https://x.test/1", None, "src")
    collect.situer(a, gaz)
    assert a["lat"] is not None and a["zone"] is None


# ---------- noms français ----------
def test_les_pays_portent_leur_nom_francais(gaz):
    """Le gazetteer vient de GeoNames, donc en anglais : « Spain » sous un article du
    Monde détonne. Les 211 noms de collecteur/pays.json font autorité."""
    for cle, attendu in [("spain", "Espagne"), ("south korea", "Corée du Sud"),
                         ("united states", "États-Unis"), ("germany", "Allemagne")]:
        assert gaz["index"][cle][0]["n"] == attendu


def test_les_villes_prennent_leur_exonyme_francais(gaz):
    for cle, attendu in [("beijing", "Pékin"), ("moscow", "Moscou"), ("london", "Londres"),
                         ("cairo", "Le Caire"), ("new york city", "New York")]:
        assert gaz["index"][cle][0]["n"] == attendu


def test_les_formes_datees_ou_discutees_ne_sont_pas_affichees(gaz):
    """La liste d'alias sert à RECONNAÎTRE et accepte « Kharkov », « Madras », « Kiev ».
    La liste d'affichage, elle, est relue à la main et les écarte."""
    for cle, attendu in [("kharkiv", "Kharkiv"), ("kharkov", "Kharkiv"),
                         ("chennai", "Chennai"), ("madras", "Chennai"),
                         ("mumbai", "Mumbai"), ("bombay", "Mumbai"),
                         ("kyiv", "Kyiv"), ("kiev", "Kyiv")]:
        assert gaz["index"][cle][0]["n"] == attendu, cle


def test_les_variantes_d_ecriture_ne_remplacent_pas_le_nom(gaz):
    """« Sao Paulo » est une graphie sans accents, pas un nom français."""
    for cle, attendu in [("sao paulo", "São Paulo"), ("quebec", "Québec")]:
        assert gaz["index"][cle][0]["n"] == attendu


def test_les_faux_lieux_constates_en_production_sont_ignores(gaz):
    """Relevés sur 1 372 dépêches réelles : « Lockheed Martin », « Enterprise AI »,
    « Palo Alto Networks », Marco Rubio, et le mot « cela »."""
    for mot in ("date", "cela", "celle", "martin", "enterprise", "rubio", "palo alto",
                "hitachi", "samba", "carrefour"):
        assert mot not in gaz["index"], f"« {mot} » ne doit plus être reconnu comme un lieu"


def test_les_regions_declarees_restent_en_francais(gaz):
    assert gaz["index"]["crimee"][0]["n"] == "Crimée"
    assert gaz["index"]["gaza"][0]["n"] == "Bande de Gaza"


def test_la_migration_rattrape_les_lieux_deja_enregistres(tmp_path):
    """Un article déjà connu n'est jamais réécrit : sans rattrapage, « Spain » resterait
    affiché à côté d'« Espagne » pendant un mois."""
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    maintenant = base.maintenant_utc().isoformat()
    for i, lieu in enumerate(["Spain", "United States", "Beijing", "Kharkiv", None]):
        cx.execute("INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, "
                   "theme, lieu, ajout, maj) VALUES (?, ?, ?, 'S', ?, 'eur', 'def', ?, ?, ?)",
                   (f"https://x.test/{i}", f"T{i}", f"t{i}", maintenant, lieu,
                    maintenant, maintenant))
    cx.execute("PRAGMA user_version = 15")
    cx.commit()
    cx.close()

    cx = base.ouvrir(chemin)         # déclenche la migration v16
    try:
        lieux_apres = {r[0] for r in cx.execute("SELECT lieu FROM articles")}
        assert lieux_apres == {"Espagne", "États-Unis", "Pékin", "Kharkiv", None}
    finally:
        cx.close()


def test_la_migration_ne_repasse_pas_sur_une_base_a_jour(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    try:
        assert base.a_jour(cx)
        assert base.renommer_les_lieux_en_francais(cx) == 0, "aucune ligne à toucher"
    finally:
        cx.close()


def test_la_migration_depose_les_articles_mal_places(tmp_path):
    """« Lockheed Martin » plaçait des articles en Slovaquie : ils quittent la carte, pas
    le fil. Un article sans point reste lisible ; un point faux raconte une histoire fausse."""
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    maintenant = base.maintenant_utc().isoformat()
    cx.execute("INSERT INTO evenements (titre, titre_norm, lieu, lat, lon, theme, "
               "date_debut, date_fin, maj, nb_sources) "
               "VALUES ('E', 'e', 'Martin', 49.06, 18.92, 'def', ?, ?, ?, 1)",
               (maintenant, maintenant, maintenant))
    evt = cx.execute("SELECT id FROM evenements").fetchone()[0]
    for i, lieu in [(1, "Martin"), (2, "Paris")]:
        cx.execute("INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, "
                   "theme, lieu, lat, lon, lieu_origine, evenement_id, ajout, maj) "
                   "VALUES (?, ?, ?, 'S', ?, 'eur', 'def', ?, 1.0, 2.0, 'gazetteer', ?, ?, ?)",
                   (f"https://x.test/{i}", f"T{i}", f"t{i}", maintenant, lieu, evt,
                    maintenant, maintenant))
    cx.execute("PRAGMA user_version = 16")
    cx.commit()
    cx.close()

    cx = base.ouvrir(chemin)      # déclenche la migration v17
    try:
        mauvais = cx.execute("SELECT * FROM articles WHERE titre = 'T1'").fetchone()
        bon = cx.execute("SELECT * FROM articles WHERE titre = 'T2'").fetchone()
        assert (mauvais["lieu"], mauvais["lat"], mauvais["lieu_origine"]) == (None, None, None)
        assert mauvais["evenement_id"] == evt, "il reste dans le fil, il quitte la carte"
        assert (bon["lieu"], bon["lat"]) == ("Paris", 1.0), "les bons lieux ne bougent pas"
        e = cx.execute("SELECT * FROM evenements").fetchone()
        assert (e["lieu"], e["lat"], e["lon"]) == (None, None, None)
    finally:
        cx.close()
