"""Test d'intégration de bout en bout.

Part d'un fichier de configuration et de vrais flux servis en HTTP, lance une collecte
complète, puis interroge l'API et la page comme le ferait un navigateur. C'est le seul test
qui traverse toute la chaîne : configuration → collecte → tri → localisation → regroupement
→ base → API → site.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
sys.path.insert(0, str(RACINE))
import base  # noqa: E402
import collect  # noqa: E402
from serveur import app as serveur  # noqa: E402

AUJOURDHUI = "Mon, 26 Oct 2026 08:00:00 +0000"

# Un même fait couvert par deux médias, plus des cas qui doivent survivre à la collecte.
FLUX = {
    "/presse-a": f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:georss="http://www.georss.org/georss"><channel><title>Presse A</title>
<item><title>Inondations meurtrières à Lagos, des milliers de déplacés</title>
 <link>https://presse-a.test/1</link><pubDate>{AUJOURDHUI}</pubDate>
 <description>Le Nigeria sous les eaux.</description></item>
<item><title>Séisme de magnitude 6,8 au large du Japon</title>
 <link>https://presse-a.test/2</link><pubDate>{AUJOURDHUI}</pubDate>
 <georss:point>38.3 142.4</georss:point></item>
<item><title>Inflation : la hausse des prix ralentit</title>
 <link>https://presse-a.test/3</link><pubDate>{AUJOURDHUI}</pubDate></item>
</channel></rss>""",

    # même événement que presse-a, titre reformulé, autre média
    "/presse-b": f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Presse B</title>
<item><title>Lagos : les inondations font des milliers de déplacés</title>
 <link>https://presse-b.test/1</link><pubDate>{AUJOURDHUI}</pubDate></item>
</channel></rss>""",

    # Flux mal formé mais rattrapable : esperluette nue et balise channel jamais fermée.
    # C'est exactement ce que feedparser sait réparer, et la raison de le garder (voir
    # bancs/DECISION-phase2.md : encoding/xml, en Go, refuserait ce flux).
    "/casse": f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Presse cassée & co</title>
<item><title>Manifestations à Tbilissi & reprise des pourparlers</title>
 <link>https://casse.test/1</link><pubDate>{AUJOURDHUI}</pubDate></item>
</rss>""",

    # Flux irrécupérable : l'encodage déclaré est faux et une balise de structure reste
    # ouverte. Personne ne sait lire ça ; ce qui compte est que la collecte continue.
    "/illisible": """<?xml version="1.0" encoding="UTF-16"?>
<rss version="2.0"><channel><title>Illisible<title>
<item><title>Rien</title><link>https://illisible.test/1</link></item></channel></rss>""",
}


class Serveur(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        chemin = self.path.split("?")[0]
        corps = FLUX.get(chemin)
        if corps is None:
            self.send_error(404)
            return
        etag = '"' + chemin + '"'
        if self.headers.get("If-None-Match") == etag:   # comme un vrai serveur de flux
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        octets = corps.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(octets)))
        self.end_headers()
        self.wfile.write(octets)


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Serveur)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def collecte(site, tmp_path_factory):
    """Écrit une configuration, lance une collecte réelle, renvoie le chemin de la base."""
    dossier = tmp_path_factory.mktemp("bout-en-bout")
    flux = dossier / "flux.json"
    flux.write_text(json.dumps({"flux": [
        {"nom": "Presse A", "type": "rss", "url": f"{site}/presse-a",
         "langue": "fr", "frequence_minutes": 1},
        {"nom": "Presse B", "type": "rss", "url": f"{site}/presse-b",
         "langue": "fr", "frequence_minutes": 1},
        {"nom": "Presse cassée", "type": "rss", "url": f"{site}/casse", "langue": "fr"},
        {"nom": "Presse illisible", "type": "rss", "url": f"{site}/illisible", "langue": "fr"},
        {"nom": "Presse éteinte", "type": "rss", "url": f"{site}/presse-a", "actif": False},
    ]}, ensure_ascii=False), encoding="utf-8")

    chemin = dossier / "map.sqlite"
    ancien = base.FLUX_JSON
    base.FLUX_JSON = flux
    try:
        collect.main(["--base", str(chemin), "--migrer", str(dossier / "rien.json")])
        # --forcer : sans cela la fréquence de rafraîchissement sauterait les sources, et
        # on ne verrait pas si les en-têtes conditionnels font bien leur travail
        collect.main(["--base", str(chemin), "--migrer", str(dossier / "rien.json"), "--forcer"])
    finally:
        base.FLUX_JSON = ancien
    return chemin


@pytest.fixture(scope="module")
def cx(collecte):
    c = base.ouvrir(collecte)
    yield c
    c.close()


@pytest.fixture(scope="module")
def client(collecte):
    serveur.BASE = collecte
    serveur._referentiel = None
    return TestClient(serveur.app)


# ---------- la configuration a bien été appliquée ----------
def test_les_sources_viennent_du_fichier(cx):
    noms = {s["nom"]: s for s in base.lister_sources(cx)}
    assert set(noms) == {"Presse A", "Presse B", "Presse cassée", "Presse illisible",
                         "Presse éteinte"}
    assert noms["Presse éteinte"]["actif"] is False
    assert noms["Presse A"]["frequence_minutes"] == 1
    assert all(s["origine"] == "config" for s in noms.values())


def test_la_source_eteinte_n_a_pas_ete_interrogee(cx):
    interrogees = {r[0] for r in cx.execute("SELECT DISTINCT source FROM collectes_sources")}
    assert "Presse éteinte" not in interrogees


def test_le_second_passage_a_recu_des_304(cx):
    messages = [r[0] for r in cx.execute(
        "SELECT message FROM collectes_sources WHERE collecte_id = "
        "(SELECT MAX(id) FROM collectes)") if r[0]]
    assert any("inchangé" in (m or "") for m in messages)


# ---------- la collecte a bien tout traité ----------
def test_le_flux_mal_forme_a_quand_meme_donne_son_article(cx):
    """Esperluette nue et balise non fermée : feedparser répare, l'article arrive."""
    r = cx.execute("SELECT titre FROM articles WHERE source = 'Presse cassée'").fetchall()
    assert len(r) == 1 and "Tbilissi" in r[0]["titre"]


def test_un_flux_illisible_ne_bloque_pas_les_autres(cx):
    """Celui-là, personne ne sait le lire. L'erreur est notée, la collecte continue."""
    assert cx.execute("SELECT COUNT(*) FROM articles WHERE source = 'Presse illisible'"
                      ).fetchone()[0] == 0
    ligne = cx.execute("SELECT statut, message FROM collectes_sources "
                       "WHERE source = 'Presse illisible' LIMIT 1").fetchone()
    assert ligne["statut"] == "erreur" and "illisible" in ligne["message"]


def test_les_coordonnees_du_flux_sont_reprises(cx):
    r = cx.execute("SELECT lat, lon, lieu_origine FROM articles WHERE lien = ?",
                   ("https://presse-a.test/2",)).fetchone()
    assert (round(r["lat"], 1), round(r["lon"], 1)) == (38.3, 142.4)
    assert r["lieu_origine"] == "flux"


def test_un_lieu_est_reconnu_dans_le_titre(cx):
    r = cx.execute("SELECT lieu, lat, lieu_origine, zone FROM articles WHERE lien = ?",
                   ("https://presse-a.test/1",)).fetchone()
    assert r["lieu"] == "Lagos" and r["lieu_origine"] == "gazetteer"
    assert r["zone"] == "afr"          # déduite du pays du lieu


def test_un_article_sans_lieu_reste_en_base_sans_coordonnees(cx):
    r = cx.execute("SELECT lat, lon FROM articles WHERE lien = ?",
                   ("https://presse-a.test/3",)).fetchone()
    assert r["lat"] is None and r["lon"] is None


def test_les_deux_medias_font_un_seul_evenement(cx):
    e = cx.execute("SELECT * FROM evenements WHERE lieu = 'Lagos'").fetchone()
    assert e is not None and e["nb_sources"] == 2
    sources = {r[0] for r in cx.execute(
        "SELECT source FROM articles WHERE evenement_id = ?", (e["id"],))}
    assert sources == {"Presse A", "Presse B"}


def test_la_seconde_collecte_n_a_rien_dupplique(cx):
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 5
    assert cx.execute("SELECT COUNT(DISTINCT lien) FROM articles").fetchone()[0] == 5


# ---------- l'API rend ce que le site attend ----------
def test_api_referentiel(client):
    r = client.get("/api/referentiel").json()
    assert r["themes"] and r["carte"]["tuiles"]["url"].startswith("http")


def test_api_fil_groupe(client):
    r = client.get("/api/articles").json()
    assert r["groupe"] is True
    lagos = [a for a in r["articles"] if a.get("lieu") == "Lagos"]
    assert len(lagos) == 1 and lagos[0]["nb_sources"] == 2
    assert r["total"] < r["total_articles"]      # moins de lignes que d'articles


def test_api_fil_non_groupe(client):
    r = client.get("/api/articles", params={"grouper": "false"}).json()
    assert r["groupe"] is False
    assert sum(1 for a in r["articles"] if "Lagos" in a["titre"]) == 2


def test_api_carte(client):
    pts = client.get("/api/evenements").json()["evenements"]
    assert pts and all(p["lat"] is not None for p in pts)
    lagos = next(p for p in pts if p["lieu"] == "Lagos")
    detail = client.get(f"/api/evenements/{lagos['id']}").json()
    assert {s["source"] for s in detail["sources"]} == {"Presse A", "Presse B"}
    assert all(s["lien"].startswith("https://") for s in detail["sources"])


def test_api_carte_par_fenetre(client):
    afrique = client.get("/api/evenements", params={"bbox": "-18,-35,52,15"}).json()
    assert [p["lieu"] for p in afrique["evenements"]] == ["Lagos"]
    pacifique = client.get("/api/evenements", params={"bbox": "120,20,160,50"}).json()
    assert all(p["lieu"] != "Lagos" for p in pacifique["evenements"])


def test_api_filtre_par_theme(client):
    tout = client.get("/api/articles").json()["total"]
    climat = client.get("/api/articles", params={"themes": "cli"}).json()["total"]
    assert 0 < climat <= tout


# ---------- le site se charge ----------
def test_la_page_et_ses_ressources(client):
    page = client.get("/")
    assert page.status_code == 200
    for ressource in ("/carte.js", "/vendor/leaflet.js", "/vendor/leaflet.css"):
        assert client.get(ressource).status_code == 200
    assert client.get("/theme/cli").status_code == 200


def test_un_evenement_sans_source_publiable_ne_compte_pas(cx):
    """« Inflation » n'a pas de zone : l'article reste en base (D04) mais son événement
    ne doit compter aucune source publiable, donc ne pas peser dans les totaux."""
    e = cx.execute("SELECT * FROM evenements WHERE titre LIKE 'Inflation%'").fetchone()
    assert e is not None and e["nb_sources"] == 0
    assert cx.execute("SELECT COUNT(*) FROM articles WHERE evenement_id = ?",
                      (e["id"],)).fetchone()[0] == 1
