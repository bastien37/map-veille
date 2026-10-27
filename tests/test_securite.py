"""Tests de sécurité : SSRF sur les adresses de flux, taille des téléchargements,
assainissement du HTML venu des flux.

Les adresses de flux viennent de l'administration et d'un fichier de configuration : ce
sont des entrées, à traiter comme telles.
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base  # noqa: E402
import collect  # noqa: E402


@pytest.fixture
def sans_reseau_local(monkeypatch):
    """Rétablit la protection, que conftest.py lève pour les autres tests."""
    monkeypatch.setattr(collect, "AUTORISER_RESEAU_LOCAL", False)


# ---------- SSRF ----------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/admin/tableau",   # l'administration du site lui-même
    "http://localhost/secret",
    "http://169.254.169.254/latest/meta-data/",  # métadonnées des hébergeurs
    "http://10.0.0.5/interne",
    "http://192.168.1.1/admin",
    "http://172.16.0.1/",
    "http://[::1]/",
    "http://0.0.0.0/",
])
def test_adresses_internes_refusees(sans_reseau_local, url):
    """Adresses écrites en clair : refusées sans même interroger le DNS."""
    with pytest.raises(collect.AdresseRefusee):
        collect.verifier_forme_url(url)
    with pytest.raises(collect.AdresseRefusee):
        collect.verifier_url(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://exemple.test/flux", "gopher://exemple.test/",
    "javascript:alert(1)", "", "http://", "https:///chemin",
])
def test_schemas_et_adresses_malformees_refuses(sans_reseau_local, url):
    with pytest.raises(collect.AdresseRefusee):
        collect.verifier_forme_url(url)


def test_une_adresse_publique_passe(sans_reseau_local, monkeypatch):
    import ipaddress
    monkeypatch.setattr(collect, "adresses_de", lambda h: {ipaddress.ip_address("93.184.216.34")})
    collect.verifier_url("https://exemple.test/rss")


def test_enregistrer_une_source_n_interroge_pas_le_dns(sans_reseau_local, monkeypatch):
    """Une panne de DNS ne doit pas empêcher d'ouvrir la base ni de sauver une source."""
    def tombe(hote):
        raise AssertionError("le DNS ne doit pas être interrogé à l'enregistrement")
    monkeypatch.setattr(collect, "adresses_de", tombe)
    collect.verifier_forme_url("https://www.lemonde.fr/rss.xml")


def test_nom_introuvable_refuse(sans_reseau_local):
    with pytest.raises(collect.AdresseRefusee, match="introuvable"):
        collect.verifier_url("https://ce-nom-nexiste-vraiment-pas.invalid/rss")


def test_ajouter_une_source_interne_est_refuse(tmp_path, sans_reseau_local):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    with pytest.raises(ValueError, match="réseau interne|refusée"):
        base.ajouter_source(cx, {"nom": "Piège", "type": "rss",
                                 "url": "http://169.254.169.254/latest/meta-data/"})
    cx.close()


class Redirecteur(BaseHTTPRequestHandler):
    """Un site public qui renvoie le collecteur vers le réseau interne."""
    cible = "http://169.254.169.254/latest/meta-data/"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", Redirecteur.cible)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_une_redirection_vers_le_reseau_interne_est_refusee(monkeypatch):
    """La redirection est choisie par le serveur distant : elle doit être vérifiée aussi."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirecteur)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/flux"
    # le réseau local reste permis (comme en développement), mais la CIBLE ne l'est pas
    vrai = collect.verifier_url

    def verifier(u, autoriser_local=None):
        return vrai(u, autoriser_local=not u.startswith("http://169.254."))

    monkeypatch.setattr(collect, "verifier_url", verifier)
    with pytest.raises(collect.AdresseRefusee):
        collect.http(url, pause=0.01)
    srv.shutdown()


# ---------- taille des téléchargements ----------
def test_la_limite_de_taille_est_posee_par_defaut():
    assert collect.TAILLE_MAX <= 16 << 20


# ---------- HTML venu des flux ----------
@pytest.mark.parametrize("brut, attendu", [
    ("<script>alert('xss')</script>Titre", "alert('xss') Titre"),
    ("<b>Gras</b> et <i>italique</i>", "Gras et italique"),
    ("<img src=x onerror=alert(1)>Texte", "Texte"),
    ("Caf&eacute; &amp; th&eacute;", "Café & thé"),
    ("<p>Un\n   paragraphe</p>", "Un paragraphe"),
    ("", ""),
    (None, ""),
])
def test_le_html_des_flux_est_reduit_en_texte(brut, attendu):
    assert collect.nettoyer_html(brut) == attendu


def test_un_lien_non_http_n_entre_pas_en_base(tmp_path):
    """collect.main écarte tout lien qui ne commence pas par http."""
    flux = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            b"<item><title>Pi\xc3\xa8ge</title><link>javascript:alert(1)</link></item>"
            b"<item><title>Bon</title><link>https://exemple.test/1</link></item>"
            b"</channel></rss>")
    fichier = tmp_path / "flux.xml"
    fichier.write_bytes(flux)
    collect.main(["--base", str(tmp_path / "map.sqlite"), "--migrer", str(tmp_path / "rien.json"),
                  "--fichiers", f"Test={fichier}"])
    cx = base.ouvrir(tmp_path / "map.sqlite")
    liens = [r[0] for r in cx.execute("SELECT lien FROM articles")]
    cx.close()
    assert liens == ["https://exemple.test/1"]


def test_le_front_echappe_tout_ce_qui_vient_des_flux():
    page = (RACINE / "site" / "index.html").read_text(encoding="utf-8")
    carte = (RACINE / "site" / "carte.js").read_text(encoding="utf-8")
    for source in (page, carte):
        assert "esc(" in source
    # titres, sources, lieux et liens passent tous par esc()
    for champ in ("esc(i.titre)", "esc(i.source)", "esc(i.lien)"):
        assert champ in page
    for champ in ("esc(s.titre)", "esc(s.source)", "esc(s.lien)", "esc(e.lieu"):
        assert champ in carte
    # les liens externes ne doivent pas donner la main à la page d'origine
    assert 'rel="noopener' in page and 'rel="noopener noreferrer"' in carte
