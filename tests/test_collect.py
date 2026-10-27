"""Tests du collecteur MAP. Lancer avec : uv run pytest"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "collecteur"))
import collect  # noqa: E402

TESTS = RACINE / "tests"
REGLES = json.loads((RACINE / "collecteur" / "regles.json").read_text(encoding="utf-8"))
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


def test_doublon_par_titre(tmp_path):
    cx = base.ouvrir(tmp_path / "map.sqlite")
    now = base.maintenant_utc()
    a = collect.article("Mali : la junte annonce un nouveau gouvernement", "https://a.test/1", now, "A",
                        zone="afr", theme="dip")
    b = dict(a, lien="https://b.test/2", source="B")
    assert base.enregistrer(cx, a, collect.normaliser(a["titre"]), now, None) == "nouveau"
    assert base.enregistrer(cx, b, collect.normaliser(b["titre"]), now, None) == "doublon"


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
