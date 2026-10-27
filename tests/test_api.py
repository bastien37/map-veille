"""Tests de l'API (serveur/app.py). Lancer avec : uv run pytest"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(RACINE))
import collect  # noqa: E402
from serveur import app as serveur  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    base_test = tmp_path / "map.sqlite"
    collect.main(["--base", str(base_test), "--migrer", str(tmp_path / "rien.json"), "--hors-ligne"])
    monkeypatch.setattr(serveur, "BASE", base_test)
    return TestClient(serveur.app)


def test_referentiel(client):
    r = client.get("/api/referentiel").json()
    assert [z["code"] for z in r["zones"]] == ["eur", "rus", "mena", "afr", "asi", "ame"]
    # la zone reste un attribut de classement, mais n'a plus de tracé : le SVG des six
    # zones a été remplacé par la carte à points (87,6 Kio de moins à chaque visite)
    assert all(set(z) == {"code", "nom"} for z in r["zones"])
    assert [t["code"] for t in r["themes"]] == ["def", "dip", "eco", "tec", "cli"]


def test_synthese(client):
    r = client.get("/api/synthese").json()
    assert r["total"] == 3
    assert "zones" not in r          # comptages par zone désormais facultatifs
    assert client.get("/api/synthese", params={"theme": "cli"}).json()["total"] == 3


def test_synthese_avec_les_zones(client):
    r = client.get("/api/synthese", params={"zones": "1"}).json()
    assert r["zones"]["asi"] == {"total": 1, "nouveaux": 1}
    assert sum(z["total"] for z in r["zones"].values()) == r["total"]


def test_articles_filtres_et_pagination(client):
    r = client.get("/api/articles", params={"zone": "asi"}).json()
    assert r["total"] == 1 and {a["zone"] for a in r["articles"]} == {"asi"}
    assert all(a["nouveau"] is True for a in r["articles"])
    dates = [a["date"] for a in client.get("/api/articles").json()["articles"]]
    assert dates == sorted(dates, reverse=True)
    page2 = client.get("/api/articles", params={"limite": 2, "decalage": 2}).json()
    assert len(page2["articles"]) == 1 and page2["total"] == 3


def test_codes_inconnus_refuses(client):
    assert client.get("/api/articles", params={"zone": "xyz"}).status_code == 422
    assert client.get("/api/synthese", params={"theme": "xyz"}).status_code == 422


def test_journal_des_collectes(client):
    j = client.get("/api/collectes").json()
    assert j[0]["hors_ligne"] is True and {s["statut"] for s in j[0]["sources"]} == {"ok"}


def test_base_absente(tmp_path, monkeypatch):
    monkeypatch.setattr(serveur, "BASE", tmp_path / "absente.sqlite")
    assert TestClient(serveur.app).get("/api/articles").status_code == 503


def test_front_sans_donnees(client):
    page = client.get("/")
    assert page.status_code == 200 and 'const API = RACINE + "api"' in page.text
    for interdit in ("MAP_DATA", "data.json", "Conflits et défense", "Europe"):
        assert interdit not in page.text


# ---------- compression et cache HTTP ----------
def test_reponses_compressees(client):
    nu = client.get("/api/referentiel", headers={"Accept-Encoding": "identity"})
    comprime = client.get("/api/referentiel", headers={"Accept-Encoding": "gzip"})
    assert comprime.headers["content-encoding"] == "gzip"
    assert int(comprime.headers["content-length"]) < len(nu.content)
    assert comprime.json() == nu.json()
    assert "content-encoding" not in nu.headers


def test_petites_reponses_non_compressees(client):
    """En dessous de 512 octets, compresser coûte plus que cela ne rapporte."""
    r = client.get("/api/collectes", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers


@pytest.mark.parametrize("route, cache", [
    ("/api/referentiel", "max-age=3600"),
    ("/api/synthese", "max-age=60"),
    ("/api/articles", "max-age=60"),
])
def test_entetes_de_cache(client, route, cache):
    r = client.get(route)
    assert cache in r.headers["cache-control"]
    assert r.headers["etag"]


@pytest.mark.parametrize("route", ["/api/referentiel", "/api/synthese", "/api/articles"])
def test_304_si_le_navigateur_a_deja_la_version(client, route):
    etag = client.get(route).headers["etag"]
    rejoue = client.get(route, headers={"If-None-Match": etag})
    assert rejoue.status_code == 304 and not rejoue.content
    assert client.get(route, headers={"If-None-Match": '"autre"'}).status_code == 200


def test_le_front_ne_demande_pas_deux_fois_les_donnees(client):
    """Le rappel des polices redemandait tout à l'API pour recalculer la largeur des
    étiquettes du SVG. Le SVG est parti, le rappel aussi."""
    page = client.get("/").text
    assert "fonts.ready" not in page
    assert 'cache:"no-store"' not in page


def test_les_polices_ne_bloquent_pas_le_rendu(client):
    page = client.get("/").text
    assert 'media="print" onload="this.media=\'all\'"' in page
    assert '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>' in page


# ---------- carte à points ----------
def test_la_carte_est_la_vue_par_defaut(client):
    page = client.get("/").text
    assert 'data-vue="points" aria-pressed="true"' in page
    assert 'data-vue="liste"' in page


def test_la_carte_des_zones_a_disparu_du_site(client):
    """Décision validée : la carte à points remplace le SVG des six zones. La zone reste
    en base et dans l'administration, mais ne s'affiche plus et ne filtre plus le site."""
    page = client.get("/").text
    for parti in ('data-vue="zones"', "<svg", "zonelist", "placerEtiquettes", "dessinerCarte",
                  "fZone", "heat1"):
        assert parti not in page, parti
    assert not (RACINE / "donnees" / "carte.json").exists()
    # mais la zone est toujours là où elle sert
    assert len(client.get("/api/referentiel").json()["zones"]) == 6
    assert client.get("/api/articles", params={"zone": "eur"}).status_code == 200
    assert client.get("/api/synthese", params={"zones": "1"}).json()["zones"]["eur"] is not None


def test_leaflet_est_servi_par_le_site_pas_par_un_cdn(client):
    """Aucune dépendance extérieure au chargement : les bibliothèques sont dans site/vendor."""
    for fichier, type_attendu in [("/vendor/leaflet.js", "javascript"),
                                  ("/vendor/leaflet.css", "css"),
                                  ("/vendor/leaflet.markercluster.js", "javascript"),
                                  ("/vendor/MarkerCluster.Default.css", "css"),
                                  ("/vendor/images/marker-icon.png", "image"),
                                  ("/carte.js", "javascript")]:
        r = client.get(fichier)
        assert r.status_code == 200, fichier
        assert type_attendu in r.headers["content-type"]
    source = (RACINE / "site" / "carte.js").read_text(encoding="utf-8")
    page = client.get("/").text
    for interdit in ("unpkg.com", "cdnjs", "jsdelivr", "cdn."):
        assert interdit not in source and interdit not in page


def test_les_licences_des_bibliotheques_sont_livrees(client):
    for f in ("/vendor/LICENSE-leaflet.txt", "/vendor/LICENSE-markercluster.txt"):
        assert client.get(f).status_code == 200


def test_la_carte_est_chargee_a_la_demande(client):
    """Ni Leaflet ni carte.js ne doivent être référencés dans le HTML : un visiteur qui
    reste sur la liste ne doit pas les télécharger."""
    page = client.get("/").text
    assert "vendor/leaflet.js" not in page
    assert 'src="carte.js"' not in page and "<script src=" not in page
    assert "await import(`${RACINE}carte.js`)" in page


def test_le_referentiel_porte_la_configuration_de_la_carte(client):
    carte = client.get("/api/referentiel").json()["carte"]
    assert carte["tuiles"]["url"].startswith("http")
    assert "OpenStreetMap" in carte["tuiles"]["attribution"]   # citation obligatoire
    assert carte["vue_initiale"]["zoom"] >= 1 and carte["points_max"] > 0
    assert "_commentaire" not in carte


# ---------- thèmes choisis par le visiteur, pages par thème ----------
def test_plusieurs_themes_a_la_fois(client):
    un = client.get("/api/articles", params={"themes": "cli"}).json()["total"]
    deux = client.get("/api/articles", params={"themes": "cli,def"}).json()["total"]
    tous = client.get("/api/articles").json()["total"]
    assert un <= deux <= tous


def test_themes_equivaut_a_theme_pour_un_seul_code(client):
    a = client.get("/api/articles", params={"theme": "cli"}).json()
    b = client.get("/api/articles", params={"themes": "cli"}).json()
    assert a["total"] == b["total"]


@pytest.mark.parametrize("route", ["/api/articles", "/api/synthese", "/api/evenements"])
def test_theme_inconnu_dans_la_liste_refuse(client, route):
    assert client.get(route, params={"themes": "cli,xyz"}).status_code == 422
    assert client.get(route, params={"themes": "cli"}).status_code == 200
    assert client.get(route, params={"themes": ""}).status_code == 200   # vide = tous


def test_doublons_dans_la_liste_de_themes(client):
    a = client.get("/api/articles", params={"themes": "cli,cli,cli"}).json()["total"]
    assert a == client.get("/api/articles", params={"themes": "cli"}).json()["total"]


def test_page_par_theme(client):
    r = client.get("/theme/cli")
    assert r.status_code == 200 and 'const API = RACINE + "api"' in r.text
    assert client.get("/theme/inconnu").status_code == 404


def test_le_front_sait_ou_il_est_servi(client):
    """La page est rendue telle quelle sur « / » comme sur « /theme/cli » : les chemins
    relatifs doivent être calculés, sinon « api/articles » deviendrait « /theme/api/… »."""
    page = client.get("/").text
    assert "location.pathname.replace(/theme" in page
    assert '"map.themes"' in page          # le choix du visiteur est mémorisé


# ---------- mises à jour en quasi temps réel ----------
def test_flux_direct(client):
    with client.stream("GET", "/api/direct") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        # un flux d'événements ne doit surtout pas être compressé : il serait tamponné
        assert r.headers.get("content-encoding") == "identity"
        premier = ""
        for morceau in r.iter_text():
            premier += morceau
            if "\n\n" in premier:
                break
    assert premier.startswith("event: bonjour")
    assert "mise_a_jour" in premier


def test_le_front_ecoute_le_direct_et_sait_se_replier(client):
    page = client.get("/").text
    assert "new EventSource" in page
    assert "replierSurInterrogation" in page   # si le flux ne passe pas
