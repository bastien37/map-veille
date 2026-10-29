"""Tests de la découverte d'adresse de flux.

Une adresse de flux se devine mal, et c'est exactement le problème posé par les seize
sources du thème « geo » : écrites d'après les conventions, jamais testées. La découverte
demande au site lui-même où est son flux, puis essaie les chemins habituels.

Un serveur HTTP local joue les sites : aucun accès à Internet n'est nécessaire.
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import collect  # noqa: E402

FLUX = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>T</title>'
        b'<item><title>Autonomie strategique europeenne</title>'
        b'<link>https://exemple.test/a1</link>'
        b'<pubDate>Sun, 25 Oct 2026 08:00:00 +0000</pubDate></item>'
        b'<item><title>Rivalite systemique</title><link>https://exemple.test/a2</link>'
        b'<pubDate>Sun, 25 Oct 2026 09:00:00 +0000</pubDate></item>'
        b'</channel></rss>')

PAGE_QUI_ANNONCE = (
    b'<!doctype html><html><head><title>Institut</title>'
    b'<link rel="stylesheet" href="/style.css">'
    b'<link rel="alternate" type="application/rss+xml" title="Actualites" '
    b'href="/publications/flux.xml">'
    b'</head><body>Rien d\'autre</body></html>')

PAGE_MUETTE = b'<!doctype html><html><head><title>Revue</title></head><body>x</body></html>'


class Site(BaseHTTPRequestHandler):
    """Deux sites en un : « /annonce/ » annonce son flux, la racine ne l'annonce pas."""
    protocol_version = "HTTP/1.1"
    appels = None

    def log_message(self, *a):
        pass

    def _envoyer(self, code, corps=b"", type_mime="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        if Site.appels is not None:
            Site.appels.append(self.path)
        xml = "application/rss+xml"
        routes = {
            # site muet : rien d'annoncé, mais /feed/ répond — le chemin conventionnel
            "/": (200, PAGE_MUETTE, "text/html"),
            "/feed/": (200, FLUX, xml),
            # site poli : sa page annonce l'adresse exacte de son flux
            "/annonce/": (200, PAGE_QUI_ANNONCE, "text/html"),
            "/publications/flux.xml": (200, FLUX, xml),
            # une page qui renvoie du HTML là où on attend un flux
            "/pas-un-flux.xml": (200, PAGE_MUETTE, "text/html"),
        }
        if self.path in routes:
            code, corps, mime = routes[self.path]
            return self._envoyer(code, corps, mime)
        self._envoyer(404, b"introuvable")


@pytest.fixture(scope="module")
def site():
    serveur = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{serveur.server_address[1]}"
    serveur.shutdown()


# ---------- analyse d'une page, sans réseau ----------
def test_annonce_lue_et_rendue_absolue():
    trouves = collect.flux_annonces(PAGE_QUI_ANNONCE, "https://site.test/fr/page.html")
    assert trouves == ["https://site.test/publications/flux.xml"]


def test_feuille_de_style_et_autres_liens_ignores():
    page = (b'<link rel="alternate" type="text/html" href="/version-texte">'
            b'<link rel="icon" href="/favicon.ico">'
            b'<link rel="alternate" type="application/atom+xml" href="/atom">')
    assert collect.flux_annonces(page, "https://s.test/") == ["https://s.test/atom"]


def test_page_illisible_ne_fait_pas_echouer():
    assert collect.flux_annonces(b"\xff\xfe pas du HTML <link rel=", "https://s.test/") == []


def test_adresse_javascript_refusee():
    """Le href vient d'une page distante : seul http(s) est accepté."""
    page = b'<link rel="alternate" type="application/rss+xml" href="javascript:alert(1)">'
    assert collect.flux_annonces(page, "https://s.test/") == []


def test_chemins_candidats():
    c = collect.chemins_candidats("https://site.test/fr/rss.xml")
    assert "https://site.test/feed/" in c
    assert "https://site.test/fr/feed/" in c       # préfixe de langue repris
    assert "https://site.test/fr/rss.xml" not in c  # l'adresse qui a échoué n'est pas réessayée
    assert len(c) == len(set(c))


def test_chemins_candidats_avec_requete():
    assert "https://s.test/?feed=rss2" in collect.chemins_candidats("https://s.test/rss")


def test_pages_a_sonder():
    assert collect.pages_a_sonder("https://s.test/fr/rss.xml") == \
        ["https://s.test/", "https://s.test/fr/"]
    assert collect.pages_a_sonder("https://s.test/rss") == ["https://s.test/"]


# ---------- découverte complète, avec un vrai serveur ----------
def test_flux_trouve_par_annonce(site, monkeypatch):
    monkeypatch.setattr(collect, "AUTORISER_RESEAU_LOCAL", True)
    journal = []
    trouve = collect.decouvrir_flux(f"{site}/annonce/mauvaise-adresse.xml", journal=journal)
    assert trouve["url"] == f"{site}/publications/flux.xml"
    assert trouve["articles"] == 2 and trouve["annonce"] is True
    assert len(journal) == 1, "l'annonce doit suffire, sans essayer les chemins conventionnels"


def test_flux_trouve_par_chemin_conventionnel(site, monkeypatch):
    monkeypatch.setattr(collect, "AUTORISER_RESEAU_LOCAL", True)
    journal = []
    trouve = collect.decouvrir_flux(f"{site}/muet/rss.xml", journal=journal)
    assert trouve["url"] == f"{site}/feed/"
    assert trouve["annonce"] is False
    assert len(journal) > 1


def test_une_page_html_n_est_pas_un_flux(site, monkeypatch):
    monkeypatch.setattr(collect, "AUTORISER_RESEAU_LOCAL", True)
    assert collect.essayer_flux(f"{site}/pas-un-flux.xml") == -1
    assert collect.essayer_flux(f"{site}/publications/flux.xml") == 2


def test_rien_trouve_renvoie_none(monkeypatch):
    """Un site injoignable ne doit pas lever : la commande continue avec les autres."""
    def refuse(url):
        raise OSError("injoignable")
    assert collect.decouvrir_flux("https://nexistepas.test/rss", lire=refuse) is None


def test_la_decouverte_respecte_la_protection_ssrf(monkeypatch):
    """Les adresses essayées passent par http(), donc par le contrôle anti-SSRF."""
    monkeypatch.setattr(collect, "AUTORISER_RESEAU_LOCAL", False)
    assert collect.essayer_flux("http://127.0.0.1:1/feed/") == -1
    with pytest.raises(collect.AdresseRefusee):
        collect.http("http://169.254.169.254/latest/meta-data/")


# ---------- report dans config/flux.json ----------
sys.path.insert(0, str(RACINE))
import admin  # noqa: E402
import base  # noqa: E402


def test_la_note_perd_son_avertissement_une_fois_verifiee():
    ancienne = ("Institut français des relations internationales. Adresse NON VÉRIFIÉE : "
                "elle n'a pas pu être testée depuis l'environnement de développement "
                "(réseau sortant bloqué). Lancer « verifier-flux » avant la première collecte.")
    neuve = admin._note_verifiee(ancienne, 12, "28/10/2026")
    assert neuve.startswith("Institut français des relations internationales")
    assert "NON VÉRIFIÉE" not in neuve and "verifier-flux" not in neuve
    assert "Adresse vérifiée le 28/10/2026 : 12 articles lus." in neuve


def test_la_note_vide_ne_pose_pas_de_probleme():
    assert admin._note_verifiee("", 3, "28/10/2026") == "Adresse vérifiée le 28/10/2026 : 3 articles lus."


def test_ecrire_corrige_l_adresse_et_garde_le_reste(tmp_path, monkeypatch):
    import json
    fichier = tmp_path / "flux.json"
    fichier.write_text(json.dumps({"_commentaire": "à garder", "flux": [
        {"nom": "Institut", "type": "rss", "url": "https://i.test/faux.xml",
         "langue": "fr", "notes": "Un institut. Adresse NON VÉRIFIÉE : à tester."},
        {"nom": "Autre", "type": "rss", "url": "https://a.test/rss", "langue": "fr"},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(base, "FLUX_JSON", fichier)

    n = admin._ecrire_flux({"Institut": {"url": "https://i.test/feed/", "articles": 7}},
                           "28/10/2026")
    ecrit = json.loads(fichier.read_text(encoding="utf-8"))
    assert n == 1
    assert ecrit["_commentaire"] == "à garder"
    assert ecrit["flux"][0]["url"] == "https://i.test/feed/"
    assert "7 articles lus" in ecrit["flux"][0]["notes"]
    assert ecrit["flux"][1] == {"nom": "Autre", "type": "rss", "url": "https://a.test/rss",
                                "langue": "fr"}, "une source non vérifiée ne doit pas bouger"


def test_un_hote_inexistant_arrete_tout_de_suite(monkeypatch):
    """Inutile d'essayer vingt-six adresses sur un nom de machine qui n'existe pas."""
    monkeypatch.setattr(collect, "AUTORISER_RESEAU_LOCAL", False)   # le contrôle DNS s'applique
    journal = []
    essais = []
    assert collect.decouvrir_flux("https://nexistepas.invalid/rss",
                                  lire=lambda u: essais.append(u) or b"",
                                  journal=journal) is None
    assert essais == [], "aucune requête ne doit partir"
    assert len(journal) == 1 and "nexistepas.invalid" in journal[0][1]
