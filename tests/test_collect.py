"""Tests du collecteur MAP. Lancer avec : uv run pytest"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import base  # noqa: E402
import collect  # noqa: E402

TESTS = RACINE / "tests"
# zones : collecteur/regles.json ; thèmes : config/themes.json (chargé par base)
REGLES = {**json.loads((RACINE / "collecteur" / "regles.json").read_text(encoding="utf-8")),
          "themes": base.REGLES_THEMES}
ZONES = collect.compiler(REGLES["zones"])
THEMES = collect.compiler(REGLES["themes"])


# ---------- tri par mots-clés ----------
@pytest.mark.parametrize("titre, zone, theme", [
    ("Ukraine : nouvelles frappes de drones russes sur Kharkiv", "rus", "def"),
    ("Sommet de l'Union européenne à Bruxelles", "eur", "dip"),
    ("Séisme de magnitude 6,8 au Japon", "asi", "cli"),
    ("Brésil : Lula annonce un plan contre la sécheresse", "ame", "cli"),
    ("Sénégal : Dakar renégocie ses contrats pétroliers", "afr", "eco"),
    ("Iran et Arabie saoudite : reprise des pourparlers à Doha", "mena", "dip"),
])
def test_classement(titre, zone, theme):
    assert collect.classer(titre, "", ZONES) == zone
    assert collect.classer(titre, "", THEMES) == theme


def test_actualite_francaise_non_classee():
    assert collect.classer("Réforme des retraites : le débat reprend à l'Assemblée", "", ZONES) is None


def test_mot_court_ne_matche_pas_dans_un_autre_mot():
    # « ue » ne doit pas être reconnu dans « attaque » ou « publique »
    assert collect.classer("Une attaque publique", "", ZONES) is None


# ---------- le tri rapide doit donner exactement le même résultat que la version de référence ----------
def _classer_reference(titre, resume, regles):
    """Implémentation de référence, volontairement naïve : une expression régulière par
    mot-clé, toutes essayées sur chaque article. C'est la version d'origine du tri, gardée
    ici pour verrouiller le comportement du balayage rapide de collect.classer()."""
    import re
    t, r = collect.normaliser(titre), collect.normaliser(resume)
    scores, premiere = {}, {}
    for cat, mots in regles.items():
        s, pos = 0, None
        for mot in mots:
            m = collect.normaliser(mot).strip()
            if not m:
                continue
            fin = r"[a-z0-9]*(?= )" if mot.endswith("*") else r"(?= )"
            rx = re.compile(r"(?<= )" + re.escape(m) + fin)
            hits = [x.start() for x in rx.finditer(t)]
            s += collect.POIDS_TITRE * len(hits) + collect.POIDS_RESUME * len(rx.findall(r))
            if hits:
                pos = min(hits) if pos is None else min(pos, min(hits))
        if s:
            scores[cat] = s
            premiere[cat] = pos if pos is not None else 10**6
    if not scores:
        return None
    return sorted(scores, key=lambda c: (-scores[c], premiere[c]))[0]


CORPUS = [
    ("Ukraine : nouvelles frappes de drones russes sur Kharkiv", "L'armée ukrainienne riposte"),
    ("Union européenne : Bruxelles et le parlement européen", "européennes européen europe"),
    ("Séisme de magnitude 6,8 au Japon, alerte au tsunami levée", ""),
    ("Chine et Taïwan : manœuvres navales en mer de Chine", "Pékin militaire missile"),
    ("L'OPEP relève sa production de pétrole", "baril prix énergie marché"),
    ("", ""),
    ("UE", "ue ue ue"),
    ("Rien de géographique ici", "voiture vélo trottoir"),
    ("ATTAQUE PUBLIQUE à l'assemblée", "une attaque publique"),
    ("Mali Burkina Niger : retrait de la force", "Sahel opération armée française"),
    ("Élection au Nigeria, à Lagos", "croissance économique africaine"),
    ("Washington et Moscou : sanctions et diplomatie", "Kremlin Poutine Maison Blanche"),
    ("Inondations au Pakistan après la mousson", "climat catastrophe naturelle"),
    ("Lancement d'une fusée et mise en orbite d'un satellite", "espace technologie"),
    ("union européenne union européenne", "union européenne"),
]


@pytest.mark.parametrize("titre, resume", CORPUS)
@pytest.mark.parametrize("famille", ["zones", "themes"])
def test_tri_rapide_equivaut_a_la_reference(famille, titre, resume):
    tables = collect.compiler(REGLES[famille])
    assert collect.classer(titre, resume, tables) == _classer_reference(titre, resume, REGLES[famille])


def test_tri_rapide_equivaut_a_la_reference_sur_les_flux_de_test(tmp_path):
    """Même vérification sur tous les articles des flux RSS et Atom enregistrés."""
    articles = []
    for fichier in ("flux_test_rss.xml", "flux_test_atom.xml"):
        articles += collect.lire_rss((TESTS / fichier).read_bytes(), "test")
    assert articles
    for a in articles:
        for famille in ("zones", "themes"):
            tables = collect.compiler(REGLES[famille])
            assert collect.classer(a["titre"], a["resume"], tables) == \
                _classer_reference(a["titre"], a["resume"], REGLES[famille])


# ---------- dates ----------
@pytest.mark.parametrize("texte", [
    "Sun, 25 Oct 2026 08:00:00 +0100",   # RSS
    "2026-10-25T07:00:00Z",              # Atom
    "20261025T070000Z",                  # ISO compact
])
def test_formats_de_date(texte):
    assert collect.lire_date(texte) == datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)


def test_date_illisible():
    assert collect.lire_date("pas une date") is None


# ---------- connecteurs ----------
def test_rss_et_atom():
    rss = collect.lire_rss((TESTS / "flux_test_rss.xml").read_bytes(), "Test")
    atom = collect.lire_rss((TESTS / "flux_test_atom.xml").read_bytes(), "Atom")
    assert len(rss) == 7 and len(atom) == 3
    assert all(a["lien"].startswith("https://") and a["date"] for a in rss + atom)
    assert "<p>" not in rss[0]["resume"]


def test_rss_illisible():
    with pytest.raises(ValueError):
        collect.lire_rss(b"ceci n'est pas un flux", "X")


def test_gdacs_zone_et_nom_francais():
    pays = json.loads((RACINE / "collecteur" / "pays.json").read_text(encoding="utf-8"))
    noms = pays.pop("noms_fr")
    pz = {iso: z for z, codes in pays.items() if not z.startswith("_") for iso in codes}
    lot = collect.source_gdacs({"nom": "GDACS", "fichier_test": "tests/gdacs.json"}, True, pz, noms)
    titres = {a["titre"]: a["zone"] for a in lot}
    assert titres["Séisme – Turquie (alerte orange)"] == "mena"
    assert titres["Inondations – Nigeria (alerte orange)"] == "afr"
    assert all(a["theme"] == "cli" for a in lot)


# ---------- chaîne complète avec la base SQLite ----------
import admin  # noqa: E402
import base  # noqa: E402


def lancer(tmp_path, *options):
    """Lance une collecte sur une base temporaire et renvoie le fil (30 jours) lu dans la base."""
    collect.main(["--base", str(tmp_path / "map.sqlite"), "--migrer", str(tmp_path / "ancien.json"), *options])
    cx = base.ouvrir(tmp_path / "map.sqlite")
    limite = (base.maintenant_utc() - base.DUREE_FIL).isoformat()
    fil = [dict(r) for r in cx.execute(
        "SELECT titre, source, date_pub AS date, zone, theme, lien, ajout FROM fil "
        "WHERE date_pub >= ? ORDER BY date_pub DESC", (limite,))]
    cx.close()
    return fil


def test_chaine_complete_hors_ligne(tmp_path):
    fil = lancer(tmp_path, "--hors-ligne")
    assert len(fil) == 3   # les 3 alertes GDACS des fichiers de test
    ajout = {a["lien"]: a["ajout"] for a in fil}

    fil2 = lancer(tmp_path, "--hors-ligne")   # deuxième passage : pas de doublon, dates d'ajout conservées
    assert {a["lien"]: a["ajout"] for a in fil2} == ajout

    cx = base.ouvrir(tmp_path / "map.sqlite")
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3
    assert cx.execute("SELECT COUNT(*) FROM collectes").fetchone()[0] == 2
    assert cx.execute("SELECT COUNT(*) FROM collectes_sources WHERE statut = 'ok'").fetchone()[0] == 2


def test_aucune_donnee_ecrite_dans_le_front(tmp_path):
    avant = sorted(p.name for p in (RACINE / "site").iterdir())
    lancer(tmp_path, "--hors-ligne")
    assert sorted(p.name for p in (RACINE / "site").iterdir()) == avant
    assert not (RACINE / "site" / "data.json").exists() and not (RACINE / "site" / "data.js").exists()


def test_a_trier_et_article_trop_ancien(tmp_path):
    fil = lancer(tmp_path, "--fichiers", f"Test={TESTS / 'flux_test_rss.xml'}")
    cx = base.ouvrir(tmp_path / "map.sqlite")
    # 6 liens distincts : 1 sans zone (actualité française) gardé à trier, 1 de juin 2026 jamais enregistré
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 5
    assert [r["titre"] for r in cx.execute("SELECT titre FROM a_trier")] == \
        ["Réforme des retraites : le débat reprend à l'Assemblée"]
    assert len(fil) == 4
    assert all("hors fenêtre" not in a["titre"] for a in fil)


def test_collecte_supprime_les_articles_de_plus_de_30_jours(tmp_path):
    from datetime import timedelta
    lancer(tmp_path, "--hors-ligne")
    cx = base.ouvrir(tmp_path / "map.sqlite")
    vieux = (base.maintenant_utc() - timedelta(days=31)).isoformat()
    limite = (base.maintenant_utc() - timedelta(days=29)).isoformat()
    for i, (date, manuel, masque) in enumerate([(vieux, 0, 0), (vieux, 1, 0), (vieux, 0, 1), (limite, 1, 0)]):
        cx.execute("""INSERT INTO articles (lien, titre, titre_norm, source, date_pub, zone, theme, manuel, masque, ajout, maj)
                      VALUES (?, ?, ?, 'Test', ?, 'eur', 'def', ?, ?, ?, ?)""",
                   (f"https://a.test/{i}", f"Article {i}", f" article {i} ", date, manuel, masque, date, date))
    cx.commit()
    cx.close()
    lancer(tmp_path, "--hors-ligne")
    cx = base.ouvrir(tmp_path / "map.sqlite")
    restants = [r[0] for r in cx.execute("SELECT lien FROM articles WHERE source = 'Test'")]
    assert restants == ["https://a.test/3"]   # les 3 vieux (même manuel ou masqué) supprimés, le récent gardé
    assert cx.execute("SELECT nb_supprimes FROM collectes ORDER BY id DESC LIMIT 1").fetchone()[0] == 3


def test_migration_v3_vers_v4(tmp_path):
    import sqlite3
    chemin = tmp_path / "v3.sqlite"
    cx = sqlite3.connect(chemin)
    cx.executescript(base.SCHEMA.replace(
        "    nb_doublons INTEGER NOT NULL DEFAULT 0,\n    nb_supprimes INTEGER NOT NULL DEFAULT 0 -- articles de plus de 30 jours supprimés (D05)\n",
        "    nb_doublons INTEGER NOT NULL DEFAULT 0\n"))
    cx.execute("PRAGMA user_version = 3")
    cx.commit()
    cx.close()
    cx = base.ouvrir(chemin)
    assert "nb_supprimes" in {r["name"] for r in cx.execute("PRAGMA table_info(collectes)")}
    assert base.a_jour(cx)


def test_meme_depeche_reprise_ailleurs_est_regroupee(tmp_path):
    """Changement de comportement : le deuxième article n'est plus jeté.

    Avant, un titre déjà connu renvoyait « doublon » et l'article disparaissait, sa source
    avec lui. Il est désormais conservé et rattaché au même événement : un seul point sur
    la carte, mais les deux médias cités dans le panneau des sources.
    """
    cx = base.ouvrir(tmp_path / "map.sqlite")
    now = base.maintenant_utc()
    a = collect.article("Mali : la junte annonce un nouveau gouvernement", "https://a.test/1", now, "A",
                        zone="afr", theme="dip")
    b = dict(a, lien="https://b.test/2", source="B")
    assert base.enregistrer(cx, a, collect.normaliser(a["titre"]), now, None) == "nouveau"
    assert base.enregistrer(cx, b, collect.normaliser(b["titre"]), now, None) == "regroupe"
    assert cx.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 2
    assert cx.execute("SELECT COUNT(*) FROM evenements").fetchone()[0] == 1
    e = cx.execute("SELECT * FROM evenements").fetchone()
    assert e["nb_sources"] == 2
    sources = [r[0] for r in cx.execute(
        "SELECT source FROM articles WHERE evenement_id = ? ORDER BY source", (e["id"],))]
    assert sources == ["A", "B"]
    cx.close()


def test_correction_manuelle_conservee(tmp_path):
    lancer(tmp_path, "--fichiers", f"Test={TESTS / 'flux_test_rss.xml'}")
    cx = base.ouvrir(tmp_path / "map.sqlite")
    ident = cx.execute("SELECT id FROM a_trier").fetchone()[0]
    cx.close()
    admin.main(["--base", str(tmp_path / "map.sqlite"), "classer", str(ident), "eur", "dip"])
    fil = lancer(tmp_path, "--fichiers", f"Test={TESTS / 'flux_test_rss.xml'}")   # recollecte
    reforme = [a for a in fil if a["titre"].startswith("Réforme")]
    assert len(reforme) == 1 and (reforme[0]["zone"], reforme[0]["theme"]) == ("eur", "dip")


def test_classement_manuel_refuse_un_code_inconnu(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    with pytest.raises(ValueError):
        base.classer_a_la_main(cx, 1, "xyz", "def", base.maintenant_utc())


def test_migration_depuis_ancien_data_json(tmp_path):
    maintenant = base.maintenant_utc().isoformat()
    ancien = {"mise_a_jour": maintenant, "articles": [
        {"titre": "Ukraine : frappes sur Kharkiv", "source": "Le Monde", "date": maintenant,
         "zone": "rus", "theme": "def", "lien": "https://www.lemonde.fr/a", "ajout": "2026-10-26T15:50:11+00:00"}]}
    (tmp_path / "ancien.json").write_text(json.dumps(ancien), encoding="utf-8")
    fil = lancer(tmp_path, "--hors-ligne")
    reprise = [a for a in fil if a["lien"] == "https://www.lemonde.fr/a"]
    assert reprise and reprise[0]["ajout"] == "2026-10-26T15:50:11+00:00"


def test_donnees_exemple_non_migrees(tmp_path):
    exemple = {"exemple": True, "mise_a_jour": "2026-10-26T06:00:00+00:00", "articles": [
        {"titre": "Exemple", "source": "Source 1", "date": base.maintenant_utc().isoformat(),
         "zone": "eur", "theme": "def", "lien": "https://exemple.invalid/0", "ajout": "2026-10-26T06:00:00+00:00"}]}
    (tmp_path / "ancien.json").write_text(json.dumps(exemple), encoding="utf-8")
    fil = lancer(tmp_path, "--hors-ligne")
    assert not any("exemple.invalid" in a["lien"] for a in fil)


# ---------- réponses difficiles des API ----------
def test_gdacs_reponse_vide(monkeypatch):
    monkeypatch.setattr(collect, "http", lambda *a, **k: b"")
    assert collect.source_gdacs({"nom": "GDACS"}, False, {}, {}) == []


def test_reponse_non_json_lisible():
    with pytest.raises(ValueError, match="non JSON"):
        collect.lire_json(b"<html>Erreur</html>")


# ---------- bruit en tête d'un flux ----------
FLUX_PROPRE = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
               b'<title>T</title><item><title>Un titre</title>'
               b'<link>https://exemple.test/a</link></item></channel></rss>')


def test_un_avertissement_php_devant_le_flux_est_retire():
    """Vu en vrai : des sites font précéder leur XML d'un « Warning: » de PHP."""
    sale = b"<br />\n<b>Warning</b>: quelque chose<br />\n" + FLUX_PROPRE
    assert collect.retirer_le_bruit_en_tete(sale) == FLUX_PROPRE
    lot = collect.lire_rss(sale, "essai")
    assert [a["titre"] for a in lot] == ["Un titre"]


def test_un_flux_propre_n_est_pas_touche():
    assert collect.retirer_le_bruit_en_tete(FLUX_PROPRE) == FLUX_PROPRE
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    assert collect.retirer_le_bruit_en_tete(atom) == atom


def test_le_bruit_n_est_cherche_qu_en_tete():
    """Au-delà de quelques kilo-octets, ce n'est plus du bruit : c'est un autre document."""
    loin = b"x" * (collect.BRUIT_MAX + 100) + FLUX_PROPRE
    assert collect.retirer_le_bruit_en_tete(loin) == loin


def test_une_page_sans_le_moindre_xml_reste_une_erreur():
    """East Pendulum ne sert QUE des avertissements : il n'y a rien à sauver."""
    que_du_bruit = b"<br />\n<b>Warning</b>: tout est cass\xc3\xa9<br />\n" * 5
    assert collect.retirer_le_bruit_en_tete(que_du_bruit) == que_du_bruit
    with pytest.raises(ValueError, match="illisible"):
        collect.lire_rss(que_du_bruit, "essai")
