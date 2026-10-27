# MAP – Monitoring de l'Actualité Planétaire

Prototype de l'outil de veille du projet pluridisciplinaire (EAE). Une carte du monde en 6 zones et un fil d'actualité, alimentés automatiquement par des flux RSS de médias et par des API ouvertes.

## Architecture

```
                 ┌──────────── collecte (1 fois par jour) ────────────┐
Flux RSS + API ──► collecteur/collect.py ──► donnees/map.sqlite
(table sources)    tri zone + thème                 │
                   (regles.json, pays.json)         │ lecture seule
                                                    ▼
                         navigateur ◄── JSON ── serveur/app.py (API FastAPI)
                         site/index.html         /api/referentiel, /api/synthese,
                         (aucune donnée)         /api/articles, /api/collectes
```

Le front ne contient **aucune donnée**, ni actualité, ni liste de zones ou de thèmes, ni tracé de la carte. Il demande tout à l'API, qui lit la base SQLite. Les filtres, les comptages et le repère « Nouveau » sont calculés côté serveur.

1. **Sources** : la table `sources` de la base liste les sources, chacune avec un type et un interrupteur « active ». Elle se gère depuis l'onglet **Sources** de l'administration.
2. **Collecte** : le script interroge chaque source. Une source en panne ne bloque pas les autres, et son erreur est notée dans le journal de la base.
3. **Tri** : chaque article reçoit une zone et un thème.
   - Si l'API donne le pays (GDACS, ReliefWeb), la zone vient de `pays.json` et elle est exacte.
   - Sinon, la zone et le thème viennent des mots-clés de `regles.json`. Un mot dans le titre vaut 3 points, dans le résumé 1 point.
4. **Stockage** : la base `donnees/map.sqlite` ne garde que le titre, la source, la date, la zone, le thème et le lien (C03). Les doublons sont écartés, qu'ils aient le même lien ou le même titre (D03). La base conserve tout l'historique.
5. **Diffusion** : l'API sert le fil des 30 derniers jours (D05). Un article sans zone ou sans thème reste dans la base, mais elle ne le diffuse pas (D04).

## La base de données

`donnees/map.sqlite` est créée automatiquement au premier lancement. Le schéma est décrit dans `collecteur/base.py`.

| Table ou vue | Contenu |
|---|---|
| `articles` | Tous les articles collectés : lien (unique), titre, source, date de publication, zone, thème, date d'ajout, correction manuelle, masquage |
| `zones`, `themes` | Les 6 zones et les 5 thèmes. Les clés étrangères empêchent un code inconnu. |
| `collectes` | Journal des passages du collecteur : date, éléments lus, nouveaux articles, doublons |
| `sources` | Les sources à interroger : nom, type (RSS, GDACS, ReliefWeb), adresse, active ou non, paramètres, notes. Remplie au départ avec les 7 sources d'origine. |
| `collectes_sources` | Pour chaque passage, le résultat de chaque source (ok ou erreur, avec le message) |
| `fil` (vue) | Les articles publiables, c'est-à-dire avec une zone et un thème, et non masqués |
| `a_trier` (vue) | Les articles que le tri n'a pas su classer |

On peut ouvrir la base avec [DB Browser for SQLite](https://sqlitebrowser.org/) ou n'importe quel outil SQL, par exemple pour compter les articles par zone et par semaine.

Outil d'administration en ligne de commande :

```bash
uv run collecteur/admin.py stats                # chiffres par zone et thème, dernières collectes et sources en erreur
uv run collecteur/admin.py a-trier              # articles non classés, avec leur numéro
uv run collecteur/admin.py classer 42 asi def   # classe l'article 42 en Asie / Conflits et défense
uv run collecteur/admin.py chercher "Taïwan"    # recherche dans les titres
```

Un article classé à la main n'est plus jamais modifié par le collecteur, et il apparaît tout de suite sur le site.


## Les sources branchées

| Type | Source | Clé | Ce qu'elle apporte |
|---|---|---|---|
| `rss` | Le Monde, France 24, RFI, franceinfo | aucune | Actualité internationale des grands médias français |
| `gdacs` | [GDACS](https://www.gdacs.org/) (ONU / Commission européenne) | aucune | Alertes orange et rouges : séismes, cyclones, inondations, volcans, sécheresses, feux, tsunamis. Pays fourni par l'API. |
| `reliefweb` | [ReliefWeb](https://apidoc.reliefweb.int/) (OCHA, ONU) | **appname** | Rapports humanitaires en français, pays fourni par l'API. Désactivé tant que l'appname n'est pas obtenu. |

**ReliefWeb** demande un `appname` approuvé pour chaque requête. Pour l'activer :
1. Demander un appname via le formulaire indiqué sur https://apidoc.reliefweb.int/parameters (par exemple `eae-map-veille`, avec une adresse de contact).
2. Définir la variable d'environnement `RELIEFWEB_APPNAME` sur la machine qui lance la collecte : `$env:RELIEFWEB_APPNAME="..."` (PowerShell) ou `export RELIEFWEB_APPNAME=...`.
3. Dans l'administration, onglet **Sources**, activer la source ReliefWeb.

**GDACS** demande de citer la source sous la forme « Global Disaster Alert and Coordination System, GDACS ». C'est fait automatiquement, car c'est le nom de la source affiché sous chaque alerte.

## Interface d'administration

Une console web, réservée à l'équipe, est disponible sur **http://localhost:8000/admin**. Elle a cinq onglets :

- **Tableau de bord** : chiffres clés, articles visibles par zone et par thème, activité de chaque source.
- **À trier** : les articles que le tri automatique n'a pas su classer. On choisit leur zone et leur thème puis on clique sur « Classer » : l'article apparaît aussitôt sur le site. « Masquer » écarte un article hors sujet (actualité française, culture…).
- **Articles** : recherche dans les titres, filtres par statut, zone et thème. On peut reclasser n'importe quel article, le masquer ou le réafficher. Un article masqué reste dans la base mais disparaît du site.
- **Sources** : la liste des sources, avec pour chacune son dernier passage (ok ou erreur) et son nombre d'articles. On peut en ajouter, les modifier, les activer ou les désactiver d'un clic, et les supprimer (leurs articles déjà collectés restent). Le bouton « Tester sans enregistrer » interroge une source et montre un aperçu des articles et de leur classement, sans rien écrire dans la base : pratique pour vérifier l'adresse d'un nouveau flux RSS.
- **Collectes** : bouton « Lancer la collecte » et journal des passages, avec l'état et le message d'erreur de chaque source.

L'accès est protégé par un mot de passe. L'identifiant est `admin`, et le mot de passe est celui de la variable d'environnement `MAP_ADMIN_MOT_DE_PASSE`. Sans cette variable, l'administration est désactivée.

```powershell
# PowerShell, dans le dossier map-veille
$env:MAP_ADMIN_MOT_DE_PASSE = "un-mot-de-passe-long"
uv run uvicorn serveur.app:app --reload
```
```bash
# Linux ou Git Bash
MAP_ADMIN_MOT_DE_PASSE="un-mot-de-passe-long" uv run uvicorn serveur.app:app --reload
```

Le mot de passe n'est jamais écrit dans le code ni dans Git. En production, le site doit être servi en **HTTPS**, sinon le mot de passe circule en clair sur le réseau.

Les mêmes actions existent en ligne de commande avec `collecteur/admin.py` (voir plus haut).

## Installation avec uv

Les bibliothèques du projet sont gérées par [uv](https://docs.astral.sh/uv/). `pyproject.toml` les déclare et `uv.lock` fige leurs versions exactes, pour que tout le monde ait le même environnement.

1. Installer uv (une seule fois par poste), sous Windows (PowerShell) :
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   Sous Linux ou macOS : `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Dans le dossier `map-veille`, installer Python et les bibliothèques :
   ```bash
   uv sync
   ```
   uv télécharge Python 3.12 (version fixée par `.python-version`) s'il n'est pas déjà présent, puis crée l'environnement `.venv/`.

Bibliothèques utilisées :

| Bibliothèque | Rôle |
|---|---|
| `feedparser` | Lecture des flux RSS et Atom, y compris quand ils sont mal formés |
| `fastapi` | API web qui sert les données au front |
| `uvicorn` | Serveur web qui fait tourner l'API |
| `pytest`, `httpx2` (développement) | Tests automatiques du collecteur et de l'API |

Pour ajouter une bibliothèque : `uv add nom` (ou `uv add --dev nom` pour un outil de développement). Il faut ensuite valider `pyproject.toml` et `uv.lock` dans Git.

## Lancer en local

Deux commandes, dans deux terminaux ouverts dans le dossier `map-veille` :

```bash
uv run collecteur/collect.py                     # 1. remplit la base (à relancer pour mettre à jour)
uv run uvicorn serveur.app:app --reload          # 2. démarre le serveur
```

Ouvrir ensuite http://localhost:8000. La documentation interactive de l'API est sur http://localhost:8000/docs : on peut y essayer chaque requête.

La page ne fonctionne plus en double-cliquant sur `site/index.html` : sans serveur, elle n'a aucune donnée à afficher et elle l'indique.

Tests, sans connexion :

```bash
uv run pytest
uv run collecteur/collect.py --hors-ligne --base test.sqlite   # essai sur les réponses enregistrées de tests/
```

### Les routes de l'API

| Route | Paramètres | Réponse |
|---|---|---|
| `GET /api/referentiel` | – | Zones (nom, libellé court, tracé SVG, position de l'étiquette), thèmes, durée du repère « Nouveau » |
| `GET /api/synthese` | `theme`, `jours` (30) | Nombre d'articles et de nouveautés par zone, date de la dernière collecte, liste des sources |
| `GET /api/articles` | `zone`, `theme`, `jours` (30), `limite` (150), `decalage` (0) | Articles du plus récent au plus ancien, avec le total pour la pagination |
| `GET /api/collectes` | `limite` (5) | Journal des collectes et état de chaque source |

Un code de zone ou de thème inconnu renvoie une erreur 422. Si la base n'existe pas encore, l'API répond 503.

## Mise en ligne

Le site a maintenant besoin d'un **serveur qui exécute Python** : GitHub Pages, qui ne sert que des fichiers statiques, ne convient plus. Il faut :

1. **Le serveur web**, lancé en permanence, avec la variable `MAP_ADMIN_MOT_DE_PASSE` définie pour activer l'administration :
   ```bash
   uv run uvicorn serveur.app:app --host 0.0.0.0 --port 8000
   ```
   En production, on le place derrière un serveur HTTPS (nginx, Apache ou IIS) et on le déclare comme service pour qu'il redémarre seul (systemd sous Linux, NSSM sous Windows).
2. **La collecte quotidienne** (D07), sur la même machine, puisque la base est un fichier local :
   - Linux (cron), chaque jour à 6 h 15 :
     ```
     15 6 * * * cd /chemin/map-veille && /chemin/uv run collecteur/collect.py >> collecte.log 2>&1
     ```
   - Windows (tâche planifiée), à lancer une fois dans PowerShell :
     ```powershell
     schtasks /Create /TN "MAP collecte" /SC DAILY /ST 06:15 /TR "cmd /c cd /d C:\chemin\map-veille && uv run collecteur/collect.py >> collecte.log 2>&1"
     ```
   Le serveur lit la base en lecture seule, et SQLite (mode WAL) autorise cette lecture pendant que la collecte écrit. Il n'y a rien à redémarrer après une collecte.
3. **Le lien Moodle** (C02) pointe vers l'adresse du serveur.

Options possibles : un serveur de l'École (le plus adapté au contexte, à voir avec le service informatique), ou un hébergeur Python comme PythonAnywhere, Render ou Alwaysdata. Ce dernier est français et propose une offre gratuite limitée. Dans tous les cas, l'hébergeur doit autoriser une tâche planifiée et l'accès sortant aux sites des sources.

Le workflow GitHub `.github/workflows/tests.yml` ne publie plus le site : il lance seulement les tests à chaque modification du code.

## Ajouter une source

- **Un flux RSS, ou une source GDACS ou ReliefWeb avec d'autres réglages** : dans l'administration, onglet **Sources**, « Ajouter une source ». Testez-la avant de l'enregistrer.
- **Un nouveau type d'API** : il faut du code.
  1. Écrire une fonction `source_xxx(s, hors_ligne, ...)` dans `collect.py`. Elle renvoie une liste construite avec `article(titre, lien, date, source, resume, zone=, theme=)`.
  2. L'appeler dans `collecter_source()`.
  3. Ajouter le type dans `TYPES_SOURCES` et dans la contrainte `CHECK` de la table `sources` (`base.py`).

  Le tri, les doublons et le stockage sont gérés en commun.

## Codes

Codes des zones : `eur` Europe, `rus` Russie et Europe de l'Est, `mena` Moyen-Orient et Afrique du Nord, `afr` Afrique subsaharienne, `asi` Asie et Indo-Pacifique, `ame` Amériques.

Codes des thèmes : `def` Conflits et défense, `dip` Diplomatie et RI, `eco` Économie et énergie, `tec` Sciences, technologies et espace, `cli` Climat et catastrophes naturelles.

## Points à valider (lot LT3)

- Les adresses des flux et les conditions d'utilisation de chaque source (C03, C04).
- Les listes de mots-clés : les enrichir à partir de `admin.py a-trier` après quelques jours de collecte.
- Les cas limites de zones : la Turquie et le Soudan sont classés en Moyen-Orient et Afrique du Nord, le Groenland en Europe.
- Le thème par défaut des rapports ReliefWeb (`theme_defaut`, actuellement Diplomatie et RI).
