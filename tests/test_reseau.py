"""Tests de la couche réseau du collecteur : parallélisme, en-têtes conditionnels,
limite de taille, réessais et disjoncteur.

Un petit serveur HTTP local joue les flux : aucun accès à Internet n'est nécessaire.
"""
import gzip
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Avant tout appel à urlopen : urllib fige la configuration des proxys au premier usage,
# et un proxy d'entreprise ne doit pas s'intercaler devant le serveur de test.
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base  # noqa: E402
import collect  # noqa: E402

FLUX = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>T</title>'
        b'<item><title>Ukraine : frappes sur Kharkiv</title>'
        b'<link>https://exemple.test/a1</link>'
        b'<pubDate>Sun, 25 Oct 2026 08:00:00 +0000</pubDate></item>'
        b'</channel></rss>')
ETAG = '"v1"'


class Faux(BaseHTTPRequestHandler):
    """Routes d'essai. `appels` compte les requêtes reçues par chemin."""
    protocol_version = "HTTP/1.1"
    appels = None
    verrou = threading.Lock()
    simultanes = {"courant": 0, "maximum": 0}
    echecs_restants = {}

    def log_message(self, *a):
        pass

    def _envoyer(self, code, corps=b"", entetes=()):
        self.send_response(code)
        for k, v in entetes:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        if corps:
            self.wfile.write(corps)

    def do_GET(self):
        chemin = self.path.split("?")[0]
        with Faux.verrou:
            Faux.appels[chemin] = Faux.appels.get(chemin, 0) + 1
            Faux.simultanes["courant"] += 1
            Faux.simultanes["maximum"] = max(Faux.simultanes["maximum"], Faux.simultanes["courant"])
        try:
            if chemin == "/flux":
                if self.headers.get("If-None-Match") == ETAG:
                    return self._envoyer(304, b"", [("ETag", ETAG)])
                return self._envoyer(200, FLUX, [("ETag", ETAG),
                                                 ("Last-Modified", "Sun, 25 Oct 2026 08:00:00 GMT"),
                                                 ("Content-Type", "application/rss+xml")])
            if chemin == "/lent":
                time.sleep(.4)
                return self._envoyer(200, FLUX, [("Content-Type", "application/rss+xml")])
            if chemin == "/gzip":
                return self._envoyer(200, gzip.compress(FLUX), [("Content-Encoding", "gzip")])
            if chemin == "/enorme":
                return self._envoyer(200, b"x" * 300_000)
            if chemin == "/instable":
                with Faux.verrou:
                    reste = Faux.echecs_restants.get(chemin, 0)
                    if reste:
                        Faux.echecs_restants[chemin] = reste - 1
                if reste:
                    return self._envoyer(503)
                return self._envoyer(200, FLUX)
            if chemin == "/mort":
                return self._envoyer(503)
            return self._envoyer(404)
        finally:
            with Faux.verrou:
                Faux.simultanes["courant"] -= 1


@pytest.fixture(autouse=True)
def reessais_rapides(monkeypatch):
    """Les tests ne doivent pas attendre les vraies pauses entre deux essais."""
    monkeypatch.setattr(collect, "PAUSE_REESSAI", 0.01)


@pytest.fixture
def serveur():
    Faux.appels = {}
    Faux.simultanes = {"courant": 0, "maximum": 0}
    Faux.echecs_restants = {}
    # 0.0.0.0 : les tests de parallélisme adressent 127.0.0.1, 127.0.0.2… pour
    # simuler plusieurs domaines (tout 127.0.0.0/8 est du loopback)
    srv = ThreadingHTTPServer(("0.0.0.0", 0), Faux)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ---------- en-têtes conditionnels ----------
def test_etag_memorise_puis_304(serveur):
    entetes = {}
    assert collect.http(f"{serveur}/flux", entetes=entetes) == FLUX
    assert entetes["etag"] == ETAG
    assert entetes["derniere_modif"].startswith("Sun, 25 Oct 2026")
    with pytest.raises(collect.NonModifie):
        collect.http(f"{serveur}/flux", etag=entetes["etag"])
    assert Faux.appels["/flux"] == 2


def test_reponse_gzip_decompressee(serveur):
    assert collect.http(f"{serveur}/gzip") == FLUX


# ---------- garde-fous ----------
def test_reponse_trop_grosse_refusee(serveur):
    with pytest.raises(ValueError, match="Kio"):
        collect.http(f"{serveur}/enorme", taille_max=100_000)
    assert collect.http(f"{serveur}/enorme", taille_max=400_000).startswith(b"xxx")


def test_reessai_sur_503_puis_succes(serveur):
    Faux.echecs_restants["/instable"] = 2
    assert collect.http(f"{serveur}/instable", pause=0.01) == FLUX
    assert Faux.appels["/instable"] == 3


def test_abandon_apres_tous_les_essais(serveur):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError):
        collect.http(f"{serveur}/mort", pause=0.01)
    assert Faux.appels["/mort"] == 3


# ---------- parallélisme ----------
def test_un_seul_telechargement_par_domaine(serveur):
    """Deux requêtes simultanées vers le même domaine doivent s'enchaîner, pas se chevaucher."""
    fils = [threading.Thread(target=collect.http, args=(f"{serveur}/lent",)) for _ in range(4)]
    debut = time.perf_counter()
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert Faux.simultanes["maximum"] == 1
    assert time.perf_counter() - debut >= 4 * 0.4 * 0.9


def test_sources_interrogees_en_parallele(tmp_path, serveur, monkeypatch):
    """Quatre sources sur quatre domaines : la collecte doit durer le temps d'une seule."""
    port = serveur.rsplit(":", 1)[1]
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    cx.execute("DELETE FROM sources")
    for i in range(4):
        base._inserer_source(cx, {"nom": f"M{i}", "type": "rss",
                                  "url": f"http://127.0.0.{i + 1}:{port}/lent", "actif": True})
    cx.commit()
    collecte = base.debut_collecte(cx, collect.MAINTENANT, False)
    debut = time.perf_counter()
    lots = collect.collecter_toutes(cx, collecte, collect.referentiels(), False, parallele=4)
    duree = time.perf_counter() - debut
    cx.close()
    assert len(lots) == 4                       # une entrée par flux
    assert Faux.simultanes["maximum"] == 4      # les 4 domaines ont été interrogés ensemble
    assert duree < 4 * 0.4 * 0.75               # bien moins que la somme des latences


# ---------- disjoncteur ----------
@pytest.mark.parametrize("echecs, attendu_minutes", [(0, 0), (2, 0), (3, 15), (4, 30), (5, 60), (20, 24 * 60)])
def test_duree_de_pause(echecs, attendu_minutes):
    assert base.duree_pause(echecs).total_seconds() == attendu_minutes * 60


def test_source_mise_en_pause_puis_sautee(tmp_path, serveur):
    port = serveur.rsplit(":", 1)[1]
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    cx.execute("DELETE FROM sources")
    base._inserer_source(cx, {"nom": "Morte", "type": "rss",
                              "url": f"http://127.0.0.1:{port}/mort", "actif": True})
    cx.commit()
    ref = collect.referentiels()
    for passage in (1, 2, 3):
        collecte = base.debut_collecte(cx, collect.MAINTENANT, False)
        collect.collecter_toutes(cx, collecte, ref, False, parallele=2)
        s = base.lister_sources(cx)[0]
        assert s["echecs"] == passage
    assert base.source_en_pause(s, collect.MAINTENANT)
    appels_avant = Faux.appels["/mort"]

    # 4e passage : la source est écartée, aucune requête réseau de plus
    collecte = base.debut_collecte(cx, collect.MAINTENANT, False)
    collect.collecter_toutes(cx, collecte, ref, False, parallele=2)
    assert Faux.appels["/mort"] == appels_avant
    journal = cx.execute("SELECT message FROM collectes_sources WHERE collecte_id = ?",
                         (collecte,)).fetchone()
    assert "pause" in journal["message"]
    cx.close()


def test_succes_remet_le_disjoncteur_a_zero(tmp_path, serveur):
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    cx.execute("DELETE FROM sources")
    ident = base._inserer_source(cx, {"nom": "Bancale", "type": "rss",
                                      "url": f"{serveur}/flux", "actif": True})
    cx.commit()
    for _ in range(4):
        base.noter_etat_source(cx, ident, False, collect.MAINTENANT)
    assert base.lire_source(cx, ident)["echecs"] == 4
    assert base.source_en_pause(base.lire_source(cx, ident), collect.MAINTENANT)
    base.noter_etat_source(cx, ident, True, collect.MAINTENANT, etag=ETAG, derniere_modif="hier")
    s = base.lire_source(cx, ident)
    assert s["echecs"] == 0 and s["pause_jusqu_a"] is None
    assert s["etag"] == ETAG
    # un 304 ne renvoie pas d'en-tête : la valeur mémorisée doit être conservée
    base.noter_etat_source(cx, ident, True, collect.MAINTENANT)
    assert base.lire_source(cx, ident)["etag"] == ETAG
    cx.close()


def test_changer_l_adresse_oublie_l_etag(tmp_path, serveur):
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    cx.execute("DELETE FROM sources")
    ident = base._inserer_source(cx, {"nom": "Média", "type": "rss",
                                      "url": f"{serveur}/flux", "actif": True})
    cx.commit()
    base.noter_etat_source(cx, ident, False, collect.MAINTENANT)
    base.noter_etat_source(cx, ident, False, collect.MAINTENANT)
    base.noter_etat_source(cx, ident, False, collect.MAINTENANT)
    base.noter_etat_source(cx, ident, True, collect.MAINTENANT, etag=ETAG)
    cx.execute("UPDATE sources SET etag = ?, echecs = 3 WHERE id = ?", (ETAG, ident))
    cx.commit()
    base.modifier_source(cx, ident, {"url": f"{serveur}/autre"})
    s = base.lire_source(cx, ident)
    assert s["etag"] is None and s["derniere_modif"] is None and s["echecs"] == 0
    cx.close()


def test_flux_inchange_ne_perd_pas_les_articles(tmp_path, serveur):
    """Un 304 ne doit rien supprimer : les articles de la collecte précédente restent."""
    chemin = tmp_path / "map.sqlite"
    cx = base.ouvrir(chemin)
    cx.execute("DELETE FROM sources")
    base._inserer_source(cx, {"nom": "Média", "type": "rss",
                              "url": f"{serveur}/flux", "actif": True})
    cx.commit()
    cx.close()
    collect.main(["--base", str(chemin), "--migrer", str(tmp_path / "rien.json"), "--forcer"])
    cx = base.ouvrir(chemin)
    avant = cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    assert avant == 1 and base.lire_source(cx, 1)["etag"] == ETAG
    cx.close()
    collect.main(["--base", str(chemin), "--migrer", str(tmp_path / "rien.json"), "--forcer"])
    cx = base.ouvrir(chemin)
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == avant
    dernier = cx.execute("SELECT message FROM collectes_sources ORDER BY collecte_id DESC "
                         "LIMIT 1").fetchone()
    assert "inchangé" in dernier["message"]
    cx.close()
