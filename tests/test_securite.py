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


# ---------- sortie par un proxy HTTP ----------
# Une machine qui sort par un proxy ne résout pas les noms elle-même et n'a souvent aucun
# DNS. Le contrôle par adresse IP y est alors impossible. L'interrupteur qui le délègue au
# proxy doit rester fermé par défaut, et ne jamais lever les contrôles de forme.
@pytest.fixture
def derriere_un_proxy(monkeypatch):
    monkeypatch.setattr(collect.urllib.request, "getproxies",
                        lambda: {"http": "http://127.0.0.1:3128",
                                 "https": "http://127.0.0.1:3128"})
    monkeypatch.setattr(collect.urllib.request, "proxy_bypass",
                        lambda hote: hote in ("127.0.0.1", "localhost"))


def test_via_proxy_distingue_ce_qui_passe_par_le_proxy(derriere_un_proxy):
    assert collect.via_proxy("https://exemple.test/rss") is True
    assert collect.via_proxy("http://127.0.0.1:8000/x") is False   # dans no_proxy
    assert collect.via_proxy("ftp://exemple.test/x") is False


def test_sans_proxy_rien_ne_change(sans_reseau_local, monkeypatch):
    monkeypatch.setattr(collect.urllib.request, "getproxies", dict)
    monkeypatch.setattr(collect, "PROXY_RESOUT_LES_NOMS", True)
    with pytest.raises(collect.AdresseRefusee, match="introuvable"):
        collect.verifier_url("https://ce-nom-nexiste-vraiment-pas.invalid/rss")


def test_l_interrupteur_est_ferme_par_defaut(sans_reseau_local, derriere_un_proxy):
    """Sans décision explicite de l'exploitant, le contrôle DNS s'applique — quitte à
    faire échouer visiblement la collecte, ce qui vaut mieux qu'un contrôle retiré en
    silence."""
    assert collect.PROXY_RESOUT_LES_NOMS is False
    with pytest.raises(collect.AdresseRefusee, match="introuvable"):
        collect.verifier_url("https://ce-nom-nexiste-vraiment-pas.invalid/rss")


def test_l_interrupteur_ouvert_delegue_la_resolution(sans_reseau_local, derriere_un_proxy,
                                                     monkeypatch):
    def interdit(hote):
        raise AssertionError("le DNS ne doit plus être interrogé derrière le proxy")
    monkeypatch.setattr(collect, "adresses_de", interdit)
    monkeypatch.setattr(collect, "PROXY_RESOUT_LES_NOMS", True)
    collect.verifier_url("https://ce-nom-nexiste-vraiment-pas.invalid/rss")


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/interne",
    "http://127.0.0.1:8000/api/admin/tableau",
    "http://localhost/secret",
    "file:///etc/passwd",
    "gopher://exemple.test/x",
])
def test_les_controles_de_forme_tiennent_meme_derriere_le_proxy(url, sans_reseau_local,
                                                                derriere_un_proxy,
                                                                monkeypatch):
    """Une adresse interne écrite en clair est refusée avant même d'atteindre le proxy."""
    monkeypatch.setattr(collect, "PROXY_RESOUT_LES_NOMS", True)
    with pytest.raises(collect.AdresseRefusee):
        collect.verifier_url(url)


def test_une_redirection_reste_verifiee_derriere_le_proxy(sans_reseau_local,
                                                          derriere_un_proxy, monkeypatch):
    """Le proxy ne dispense pas de revérifier ce que le serveur distant propose."""
    monkeypatch.setattr(collect, "PROXY_RESOUT_LES_NOMS", True)
    with pytest.raises(collect.AdresseRefusee):
        collect._RedirectionsVerifiees().redirect_request(
            None, None, 302, "Found", {}, "http://169.254.169.254/latest/")
