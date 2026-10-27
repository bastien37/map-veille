"""
MAP – Où placer un article sur la carte.

Trois sources de coordonnées, de la plus sûre à la plus faillible :

  1. les balises géographiques du flux (georss:point, geo:lat/geo:long, georss:where) :
     exactes, fournies par le média, à prendre telles quelles ;
  2. le nom d'un lieu reconnu dans le titre ou le résumé, cherché dans
     donnees/gazetteer.json.gz (12 600 pays et villes, hors ligne, sans limite de débit) ;
  3. un service de géocodage extérieur (Nominatim ou Photon), seulement si la
     configuration en déclare un. Toujours avec cache : un lieu n'est jamais géocodé deux fois.

Principe de bout en bout : **mieux vaut un article sans point qu'un point faux.** Un article
mal placé raconte une histoire fausse au lecteur ; un article absent de la carte reste
lisible dans la liste. Toutes les règles ci-dessous penchent donc du côté du silence.
"""
import gzip
import json
import re
import threading
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
GAZETTEER = RACINE / "donnees" / "gazetteer.json.gz"
STOPLISTE = RACINE / "config" / "lieux_ignores.json"

LONGUEUR_MINIMALE = 4      # « Ho », « Gao »… : trop courts pour être reconnus sans se tromper
POIDS_TITRE, POIDS_RESUME = 3, 1
JETON = re.compile(r"[^\W\d_]+", re.UNICODE)   # suites de lettres, chiffres exclus


def sans_accents(texte: str) -> str:
    t = unicodedata.normalize("NFD", (texte or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, apostrophes et traits d'union en espaces."""
    t = sans_accents(texte).replace("'", " ").replace("’", " ").replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------------------
# Gazetteer
# ---------------------------------------------------------------------------
def charger_gazetteer(chemin: Path = None, stopliste: Path = None) -> dict:
    """Index { nom normalisé -> [lieux] }, prêt pour une recherche en un balayage.

    Les noms trop courts et ceux de la liste d'exclusion sont écartés à la construction :
    « Nice », « Canton » ou « Tours » sont de vraies villes, mais aussi des mots courants,
    et les reconnaître ferait plus de dégâts que de bien.
    """
    chemin = Path(chemin or GAZETTEER)
    donnees = json.loads(gzip.decompress(chemin.read_bytes()).decode("utf-8"))
    ignores = set()
    s = Path(stopliste or STOPLISTE)
    if s.exists():
        ignores = {normaliser(n) for n in json.loads(s.read_text(encoding="utf-8"))["ignores"]}

    index, nmax, pays_par_code = {}, 1, {}
    for lieu in donnees["lieux"]:
        if lieu["t"] == "pays":
            pays_par_code[lieu["c"]] = lieu
        for nom in lieu["noms"]:
            cle = normaliser(nom)
            if len(cle) < LONGUEUR_MINIMALE or cle in ignores:
                continue
            entrees = index.setdefault(cle, [])
            # « Taiwan » et « Taïwan » donnent la même clé : sans ce garde-fou le lieu serait
            # indexé deux fois et compterait double au moment du tri
            if not any(e is lieu for e in entrees):
                entrees.append(lieu)
            nmax = max(nmax, cle.count(" ") + 1)
    for entrees in index.values():   # le plus peuplé d'abord : départage les homonymes
        entrees.sort(key=lambda l: -l["p"])
    return {"index": index, "nmax": nmax, "pays": pays_par_code, "ignores": ignores}


# ---------------------------------------------------------------------------
# 1. Coordonnées fournies par le flux
# ---------------------------------------------------------------------------
def coordonnees_du_flux(entree) -> tuple:
    """(lat, lon) si le flux les donne, sinon None. feedparser comprend georss:point,
    geo:lat / geo:long et georss:where ; il range « where » en (lon, lat), ordre GeoJSON."""
    ou = entree.get("where") if hasattr(entree, "get") else None
    if isinstance(ou, dict) and ou.get("type") == "Point":
        c = ou.get("coordinates")
        if isinstance(c, (list, tuple)) and len(c) == 2:
            return valider_coordonnees(c[1], c[0])
    lat, lon = entree.get("geo_lat"), entree.get("geo_long")
    if lat is not None and lon is not None:
        return valider_coordonnees(lat, lon)
    return None


def valider_coordonnees(lat, lon):
    """Refuse ce qui n'est pas un point terrestre plausible, y compris l'île Null (0, 0)
    que beaucoup de flux renvoient quand ils n'ont en fait aucune position."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if abs(lat) < 0.01 and abs(lon) < 0.01:
        return None
    return round(lat, 5), round(lon, 5)


# ---------------------------------------------------------------------------
# 2. Noms de lieux dans le texte
# ---------------------------------------------------------------------------
def _jetons(texte: str):
    """[(forme normalisée, majuscule initiale ?)] en gardant la casse d'origine."""
    sortie = []
    for m in JETON.finditer(texte or ""):
        mot = m.group()
        sortie.append((sans_accents(mot), mot[0].isupper()))
    return sortie


def _candidats(texte: str, gaz: dict, poids: int, trouves: dict):
    """Repère les noms de lieux de `texte` et leur donne `poids` points.

    Un nom n'est retenu que s'il commence par une majuscule dans le texte d'origine : sans
    cette règle, « marche », « union » ou « nice » feraient apparaître des points partout.
    """
    jetons = _jetons(texte)
    index, nmax = gaz["index"], gaz["nmax"]
    i = 0
    while i < len(jetons):
        if not jetons[i][1]:        # pas de majuscule : ce n'est pas un nom propre
            i += 1
            continue
        for n in range(min(nmax, len(jetons) - i), 0, -1):
            cle = " ".join(j for j, _ in jetons[i:i + n])
            entrees = index.get(cle)
            if not entrees:
                continue
            for lieu in entrees:
                vu = trouves.setdefault(id(lieu), {"lieu": lieu, "score": 0, "nom": cle})
                vu["score"] += poids
            # le nom le plus long gagne ET consomme ses mots : « New Delhi » ne doit pas
            # compter aussi « Delhi », ni « New York » compter « York »
            i += n
            break
        else:
            i += 1


def extraire(titre: str, resume: str, gaz: dict) -> dict:
    """Meilleur lieu pour cet article, ou None.

    Départage, dans l'ordre : le score (titre x3, résumé x1), puis la précision (une ville
    vaut mieux qu'un pays : « Kharkiv » place mieux qu'« Ukraine »), puis la population.
    Si un pays est cité dans le même texte, les villes de ce pays passent devant : c'est ce
    qui distingue le Moscou de Russie du Moscow de l'Idaho.
    """
    trouves = {}
    _candidats(titre, gaz, POIDS_TITRE, trouves)
    _candidats(resume, gaz, POIDS_RESUME, trouves)
    if not trouves:
        return None
    pays_cites = {v["lieu"]["c"] for v in trouves.values() if v["lieu"]["t"] == "pays"}
    meilleur = max(trouves.values(), key=lambda v: (
        v["score"],
        v["lieu"]["t"] == "ville" and v["lieu"]["c"] in pays_cites,   # ville confirmée par son pays
        v["lieu"]["t"] != "pays",                                     # une ville place mieux
        v["lieu"]["p"]))
    lieu = meilleur["lieu"]
    return {"nom": lieu["n"], "lat": lieu["y"], "lon": lieu["x"], "pays": lieu["c"],
            "type": lieu["t"], "origine": "gazetteer", "score": meilleur["score"]}


# ---------------------------------------------------------------------------
# 3. Géocodeur extérieur, avec cache obligatoire
# ---------------------------------------------------------------------------
class Geocodeur:
    """Client Nominatim ou Photon, à débit limité et toujours derrière un cache.

    Les conditions d'usage de Nominatim imposent un user-agent identifiant, au plus une
    requête par seconde, et de ne pas redemander ce qu'on a déjà. Les trois sont appliquées
    ici : le cache est en base (table lieux_geocodes), jamais vidé automatiquement.
    """

    def __init__(self, service: str, url: str, user_agent: str, debit_par_seconde: float = 1.0,
                 ouvrir_url=None):
        if service not in ("nominatim", "photon"):
            raise ValueError(f"Service de géocodage inconnu : {service}.")
        self.service, self.url, self.user_agent = service, url.rstrip("/"), user_agent
        self.intervalle = 1.0 / max(debit_par_seconde, 0.01)
        self._dernier = 0.0
        self._verrou = threading.Lock()
        self._ouvrir = ouvrir_url or self._ouvrir_defaut

    def _ouvrir_defaut(self, url: str) -> bytes:
        import urllib.request
        requete = urllib.request.Request(url, headers={"User-Agent": self.user_agent,
                                                       "Accept": "application/json"})
        with urllib.request.urlopen(requete, timeout=20) as r:
            return r.read(2 << 20)

    def _attendre(self):
        """Limite de débit : au plus une requête tous les `intervalle` secondes."""
        with self._verrou:
            attente = self._dernier + self.intervalle - time.monotonic()
            if attente > 0:
                time.sleep(attente)
            self._dernier = time.monotonic()

    def chercher(self, nom: str) -> dict:
        if self.service == "nominatim":
            url = f"{self.url}/search?" + urllib.parse.urlencode(
                {"q": nom, "format": "jsonv2", "limit": 1})
        else:
            url = f"{self.url}/api?" + urllib.parse.urlencode({"q": nom, "limit": 1})
        self._attendre()
        d = json.loads(self._ouvrir(url).decode("utf-8", "replace") or "[]")
        if self.service == "nominatim":
            if not d:
                return None
            c = valider_coordonnees(d[0].get("lat"), d[0].get("lon"))
            return {"nom": d[0].get("display_name", nom), "lat": c[0], "lon": c[1],
                    "origine": "nominatim"} if c else None
        entites = (d or {}).get("features") or []
        if not entites:
            return None
        coord = (entites[0].get("geometry") or {}).get("coordinates") or []
        c = valider_coordonnees(coord[1], coord[0]) if len(coord) == 2 else None
        nom_trouve = (entites[0].get("properties") or {}).get("name") or nom
        return {"nom": nom_trouve, "lat": c[0], "lon": c[1], "origine": "photon"} if c else None


def charger_geocodeur(chemin: Path = None) -> "Geocodeur":
    """Lit config/geocodage.json. Renvoie None si aucun service n'est déclaré actif —
    c'est le cas par défaut : la base locale suffit et ne demande rien à personne."""
    chemin = Path(chemin or RACINE / "config" / "geocodage.json")
    if not chemin.exists():
        return None
    d = json.loads(chemin.read_text(encoding="utf-8"))
    if not d.get("actif"):
        return None
    agent = (d.get("user_agent") or "").strip()
    if not agent or "a-changer@example.org" in agent:
        raise ValueError(
            "config/geocodage.json : renseignez un user_agent avec une vraie adresse de "
            "contact avant d'activer le géocodage. Nominatim et Photon l'exigent, et une "
            "adresse bidon fait bloquer le projet entier.")
    return Geocodeur(d.get("service", "photon"), d.get("url", ""), agent,
                     float(d.get("debit_par_seconde", 1)))


DUREE_CACHE_ECHEC = timedelta(days=30)   # un lieu introuvable n'est pas redemandé avant un mois


def geocoder_avec_cache(cx, nom: str, geocodeur: Geocodeur, maintenant: datetime = None) -> dict:
    """Cherche `nom`, en passant d'abord par le cache en base. Les échecs sont mémorisés
    eux aussi : sans cela, les mêmes lieux introuvables seraient redemandés à chaque collecte."""
    maintenant = maintenant or datetime.now(timezone.utc).replace(microsecond=0)
    cle = normaliser(nom)
    if not cle:
        return None
    ligne = cx.execute("SELECT * FROM lieux_geocodes WHERE cle = ?", (cle,)).fetchone()
    if ligne:
        if ligne["lat"] is not None:
            cx.execute("UPDATE lieux_geocodes SET utilisations = utilisations + 1 WHERE cle = ?",
                       (cle,))
            return {"nom": ligne["nom"], "lat": ligne["lat"], "lon": ligne["lon"],
                    "origine": ligne["origine"], "pays": None, "type": "geocode"}
        if ligne["maj"] > (maintenant - DUREE_CACHE_ECHEC).isoformat():
            return None   # échec récent : inutile de redemander
    if geocodeur is None:
        return None
    try:
        trouve = geocodeur.chercher(nom)
    except Exception:
        return None   # service indisponible : on réessaiera à la prochaine collecte
    cx.execute("""INSERT INTO lieux_geocodes (cle, nom, lat, lon, origine, ajout, maj, utilisations)
                  VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                  ON CONFLICT(cle) DO UPDATE SET nom = excluded.nom, lat = excluded.lat,
                      lon = excluded.lon, origine = excluded.origine, maj = excluded.maj,
                      utilisations = lieux_geocodes.utilisations + 1""",
               (cle, (trouve or {}).get("nom", nom), (trouve or {}).get("lat"),
                (trouve or {}).get("lon"), (trouve or {}).get("origine", geocodeur.service),
                maintenant.isoformat(), maintenant.isoformat()))
    if not trouve:
        return None
    return {**trouve, "pays": None, "type": "geocode"}


# ---------------------------------------------------------------------------
# Chaîne complète
# ---------------------------------------------------------------------------
def situer(article: dict, gaz: dict, cx=None, geocodeur: Geocodeur = None) -> dict:
    """Place un article : balises du flux, puis gazetteer, puis géocodeur. None si rien."""
    if article.get("lat") is not None and article.get("lon") is not None:
        c = valider_coordonnees(article["lat"], article["lon"])
        if c:
            return {"nom": article.get("lieu") or "", "lat": c[0], "lon": c[1],
                    "pays": None, "type": "flux", "origine": "flux", "score": 99}
    trouve = extraire(article.get("titre", ""), article.get("resume", ""), gaz)
    if trouve or cx is None or geocodeur is None:
        return trouve
    return None
