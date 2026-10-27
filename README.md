# MAP – Monitoring de l'Actualité Planétaire

Outil de veille du projet pluridisciplinaire (EAE). Des flux RSS et des API ouvertes sont
collectés, triés par thème, **placés sur une carte** et **regroupés par événement** : quand
cinq médias parlent du même fait, la carte n'affiche qu'un point, et un panneau liste les
cinq sources.

## Architecture

```
config/themes.json ─┐
config/flux.json ───┤
                    ▼
Flux RSS + API ──► collecteur/collect.py ──────────► donnees/map.sqlite
(table sources)    · téléchargement parallèle        articles, événements,
                     (ETag, disjoncteur)             sources, collectes,
                   · tri par mots-clés               cache de géocodage
                   · localisation (collecteur/lieux.py)      │
                   · regroupement en événements               │ lecture seule
                                                              ▼
                              navigateur ◄── JSON ── serveur/app.py (API FastAPI)
                              site/index.html         /api/referentiel, /api/synthese,
                              site/carte.js           /api/articles, /api/evenements,
                              site/vendor/ (Leaflet)  /api/direct (temps réel)
```

Le front ne contient **aucune donnée** : ni actualité, ni liste de thèmes, ni réglage de
carte. Il demande tout à l'API, qui lit la base SQLite. Les filtres, les comptages et le
repère « Nouveau » sont calculés côté serveur.

### La chaîne, étape par étape

1. **Sources** — décrites dans `config/flux.json`, appliquées à la table `sources`.
2. **Collecte** — les sources sont interrogées **en parallèle**, une seule à la fois par
   domaine. Un flux inchangé répond `304` et n'est pas retéléchargé ; une source en panne
   répétée est mise de côté quelque temps (disjoncteur) ; une source interrogée il y a moins
   que sa fréquence est laissée tranquille.
3. **Tri** — zone et thème par mots-clés, en un seul balayage du texte.
4. **Localisation** — balises géographiques du flux, sinon un nom de lieu reconnu dans le
   titre, sinon rien (voir « Comment un article arrive sur la carte »).
5. **Regroupement** — même lieu, même fenêtre de temps, titres proches : un seul événement.
6. **Stockage** — `donnees/map.sqlite`. Seuls le titre, la source, la date, le lien et la
   position sont conservés (C03). **Les articles de plus de 30 jours sont supprimés à chaque
   collecte (D05)**, et les événements qui n'ont plus de source avec eux.
7. **Diffusion** — l'API sert le fil et les points de la carte. Un article sans zone ou sans
   thème reste en base mais n'est pas diffusé (D04).

## Démarrer

```bash
uv sync                                   # installe Python 3.12 et les bibliothèques
uv run collecteur/collect.py              # 1. remplit la base
uv run uvicorn serveur.app:app --reload   # 2. démarre le serveur
```

Puis http://localhost:8000. Documentation interactive de l'API : http://localhost:8000/docs.

La page ne fonctionne pas en double-cliquant sur `site/index.html` : sans serveur, elle n'a
aucune donnée et le dit.

Essais sans connexion :

```bash
uv run pytest                                                  # 317 tests, ~17 s
uv run collecteur/collect.py --hors-ligne --base test.sqlite   # réponses enregistrées de tests/
```

Pour développer avec un serveur de flux local, lever la protection SSRF qui refuse les
adresses internes :

```bash
MAP_AUTORISER_RESEAU_LOCAL=1 uv run collecteur/collect.py
```

## Configurer

Tout ce qui se règle sans toucher au code est dans `config/`. Chaque fichier porte en tête
un commentaire qui explique ses champs.

| Fichier | Ce qu'il décrit |
|---|---|
| `config/themes.json` | Les thèmes : code, nom, ordre d'affichage, interrupteur, mots-clés de tri |
| `config/flux.json` | Les sources : nom, type, adresse, thème imposé, langue, fréquence de rafraîchissement |
| `config/carte.json` | Le fond de carte : serveur de tuiles, attribution, vue initiale, nombre de points |
| `config/geocodage.json` | Le service de géocodage extérieur — **désactivé par défaut** |
| `config/exonymes.json` | Les noms français des lieux (Londres, Pékin, Le Caire…) |
| `config/lieux_ignores.json` | Les noms de lieux à ne pas reconnaître, parce qu'ils sont aussi des mots courants |

### Ajouter ou retirer un thème

Ajouter une entrée dans `config/themes.json` suffit : la base se met à jour à la prochaine
ouverture, l'API et le site suivent, sans toucher au code.

```json
{ "code": "spo", "nom": "Sport", "actif": true, "ordre": 60,
  "mots_cles": ["jeux olympiques", "coupe du monde", "football", "championnat*"] }
```

Les mots-clés s'écrivent **en minuscules, sans accents**. Une étoile finale accepte n'importe
quelle suite de lettres : `armee*` reconnaît *armées* et *armement*.

Mettre `"actif": false` retire le thème du site **sans rien supprimer** : les articles déjà
classés dedans restent en base, et réactiver le thème les fait réapparaître. Ne jamais
réutiliser un code pour autre chose.

### Ajouter une source

Deux chemins, qui ne se marchent pas dessus :

- **`config/flux.json`** est le catalogue : c'est là qu'on décrit les flux.
- **L'administration** décide lesquels tournent.

Concrètement : les champs du catalogue (type, adresse, thème imposé, langue, fréquence) sont
repris du fichier à chaque collecte ; l'interrupteur « actif » n'est posé qu'à la création,
donc couper une source depuis l'administration tient. Si on modifie l'adresse, la langue ou
la fréquence **depuis l'administration**, la source passe « à la main » : le fichier cesse de
s'y appliquer, et l'administration l'indique. Une source retirée du fichier est désactivée,
jamais supprimée ; ses articles restent.

```json
{ "nom": "Le Monde", "type": "rss",
  "url": "https://www.lemonde.fr/international/rss_full.xml",
  "theme": null, "langue": "fr", "frequence_minutes": 60, "actif": true }
```

`theme` impose un thème à tous les articles de la source (`null` = tri par mots-clés).
`frequence_minutes` est le délai minimal entre deux interrogations : on peut ainsi lancer la
collecte toutes les dix minutes sans redemander un quotidien à chaque fois.

Vérifier ce que la configuration a produit :

```bash
uv run collecteur/admin.py config
```

### Vérifier que les flux répondent

Une adresse de flux se devine mal, et une source muette ne se remarque qu'au bout de
quelques collectes. Après chaque ajout dans `config/flux.json` :

```bash
uv run collecteur/admin.py verifier-flux            # les sources actives
uv run collecteur/admin.py verifier-flux --toutes   # y compris celles qui sont coupées
uv run collecteur/admin.py verifier-flux --nom ifri
```

La commande interroge chaque source en parallèle **sans rien enregistrer** et affiche, pour
chacune, `ok` avec le nombre d'articles et la date du plus récent, `vide` si elle répond sans
rien d'exploitable, ou `NON` avec l'erreur. Les sources à revoir sont rappelées à la fin avec
leur adresse.

### Le thème « Géopolitique et analyses stratégiques »

`geo` rassemble 16 flux d'instituts et de revues — IFRI, IRSEM, Le Grand Continent, Revue
Défense Nationale, Le Rubicon, Revue Conflits, Diploweb, Institut Montaigne, Fondation Robert
Schuman, Areion24.news, The National Interest, War on the Rocks, ECFR, Carnegie Endowment,
Atlantic Council, Lowy Institute. Onze en français, cinq en anglais, interrogés toutes les
six heures : ces sources publient quelques fois par semaine, pas toutes les heures.

Ses mots-clés visent le **registre d'analyse** (« rivalité systémique », « multipolaire »,
« autonomie stratégique », « puissance »), pas les faits : une frappe de missile reste dans
`def`, une visite d'ambassadeur dans `dip`. Un test le vérifie
(`tests/test_config.py::test_les_mots_cles_geo_ne_devorent_pas_les_autres_themes`).

> ⚠️ **Aucune de ces 16 adresses n'a pu être testée** depuis l'environnement où elles ont été
> ajoutées : le réseau extérieur n'y est pas joignable. Elles ont été écrites d'après les
> conventions habituelles (`/feed/` pour les sites WordPress), et chacune porte une note le
> disant dans `config/flux.json`. **Lancer `verifier-flux` avant la mise en production** et
> corriger ou retirer ce qui ne répond pas.

**Un nouveau type d'API** demande du code : écrire `source_xxx(s, hors_ligne, …)` dans
`collect.py`, l'appeler depuis `collecter_source()`, et ajouter le type dans `TYPES_SOURCES`
et dans la contrainte `CHECK` de la table `sources` (`base.py`). Le tri, la localisation, le
regroupement et le stockage sont communs.

## Comment un article arrive sur la carte

Trois chances, de la plus sûre à la plus faillible. La règle est la même partout :
**mieux vaut un article sans point qu'un point faux.** Un article mal placé raconte une
histoire fausse ; un article absent de la carte reste lisible dans la liste.

1. **Les balises du flux** — `georss:point`, `geo:lat` / `geo:long`, `georss:where`, et la
   géométrie des alertes GDACS. Exactes, fournies par le média.
2. **Un nom de lieu reconnu dans le titre ou le résumé**, cherché dans
   `donnees/gazetteer.json.gz` : 12 629 pays et villes, 322 Kio, hors ligne, sans limite de
   débit. Un nom n'est retenu que s'il porte une majuscule dans le texte d'origine, fait au
   moins quatre lettres et n'est pas dans `config/lieux_ignores.json`. Le nom le plus long
   consomme ses mots (« New Delhi » ne fait pas gagner « Delhi ») et les homonymes se
   départagent par la population, puis par le pays cité dans le même texte.
3. **Un service de géocodage extérieur**, désactivé par défaut, et seulement pour un nom
   qu'une *source* annonce comme étant un lieu — jamais pour deviner un lieu dans une phrase.

Mesuré sur `tests/lieux_annotes.json` (52 cas annotés à la main, dont 14 qui ne doivent
**pas** être placés) : 100 % de precision, 100 % de rappel. Sur 4 000 articles du banc de
flux, 81 % sont placés.

La **zone** se déduit du pays du lieu quand les mots-clés n'en donnent pas : sans cela, un
quart des points seraient perdus faute de zone (D04).

### Refaire la base de lieux

`donnees/gazetteer.json.gz` est versionné : l'installation et la collecte n'ont besoin ni de
réseau, ni de bibliothèque supplémentaire. Pour le reconstruire après avoir enrichi
`config/exonymes.json` :

```bash
uv run --with geonamescache outils/construire_gazetteer.py
```

Les « alternatenames » de GeoNames ne sont **pas** reprises : toutes langues mélangées et sans
étiquette, elles donnent *Landan* ou *Ljondan* pour London. Les noms français sont relus à la
main dans `config/exonymes.json`.

### Activer un géocodeur extérieur

Dans `config/geocodage.json`, mettre `"actif": true` et renseigner un `user_agent` avec une
**vraie adresse de contact** — Nominatim et Photon l'exigent, et une adresse bidon fait
bloquer le projet entier. Le programme refuse de démarrer avec l'adresse d'exemple.

Tout nom cherché est mémorisé dans la table `lieux_geocodes`, succès **comme échec** : un même
nom n'est jamais demandé deux fois. Sur un serveur dédié, auto-héberger Photon lève la limite
de débit.

## Comment deux articles n'en font qu'un

Trois conditions, **toutes nécessaires** : moins de 50 km, moins de 48 h d'écart, et des
titres qui se ressemblent assez. Le seuil de ressemblance dépend de ce que le lieu dit déjà :
28 % de mots communs si les deux articles sont au même endroit, 45 % sinon.

Les seuils sont réglés et surveillés sur `tests/regroupement_annote.json`, des paires de
manchettes annotées à la main. Ils penchent volontairement du côté de la séparation :
réunir deux faits distincts est pire que les laisser côte à côte, car le lecteur ne voit
alors plus qu'un point là où il se passe deux choses.

**Deux langues ne se rejoignent que par les mots qu'elles partagent.** « Inondations
meurtrières à Lagos » et « Deadly floods in Lagos » restent deux points au même endroit : la
ressemblance se mesure sur des mots, et la traduction n'en laisse aucun. Quand les titres
partagent un nom propre ou une date — « Sommet d'Oslo 2026 » et « Oslo 2026 summit » — le
regroupement a lieu, et l'événement porte alors ses deux langues. Les réunir dans tous les
cas demanderait de la traduction ou des plongements multilingues, hors du périmètre de ce
projet ; le filtre par langue est la façon de n'en voir qu'un.

> **Changement par rapport à la version précédente.** Un deuxième article portant le même
> titre était auparavant *jeté* — sa source était perdue. Il est désormais conservé et
> rattaché au même événement. Le fil rend une ligne par événement, avec `nb_sources` ;
> `grouper=false` retrouve la liste article par article.

## La base de données

`donnees/map.sqlite` est créée au premier lancement et migrée automatiquement. Le schéma est
dans `collecteur/base.py` (version 12).

| Table ou vue | Contenu |
|---|---|
| `articles` | Titre, source, date, lien, zone, thème, **langue**, **position**, événement, correction manuelle, masquage |
| `evenements` | Un fait = une ligne : titre, lieu, position, période, nombre de sources publiables, langues présentes |
| `themes`, `zones` | Référentiels. `themes` vient de `config/themes.json` |
| `sources` | Les sources : état du cache conditionnel, disjoncteur, langue, fréquence, origine |
| `lieux_geocodes` | Cache de géocodage, succès et échecs |
| `collectes`, `collectes_sources` | Journal de chaque passage, source par source |
| `fil` (vue) | Les articles publiables : zone **et** thème connus, non masqués |
| `a_trier` (vue) | Les articles que le tri n'a pas su classer |

Outil en ligne de commande :

```bash
uv run collecteur/admin.py stats      # chiffres par zone et thème, dernières collectes
uv run collecteur/admin.py config     # thèmes et sources tels que la configuration les a posés
uv run collecteur/admin.py verifier-flux   # quelles sources répondent vraiment
uv run collecteur/admin.py a-trier    # articles non classés, avec leur numéro
uv run collecteur/admin.py classer 42 asi def
uv run collecteur/admin.py chercher "Taïwan"
```

Un article classé à la main n'est plus jamais modifié par le collecteur.

## Les routes de l'API

| Route | Paramètres | Réponse |
|---|---|---|
| `GET /api/referentiel` | – | Zones, thèmes actifs, réglages de la carte, durée du repère « Nouveau » |
| `GET /api/synthese` | `theme`, `themes`, `langue`, `jours`, `zones` | Total, date de la dernière collecte, sources ; comptages par zone si `zones=1` |
| `GET /api/articles` | `zone`, `theme`, `themes`, `langue`, `jours`, `limite`, `decalage`, `grouper` | Le fil, une ligne par événement par défaut |
| `GET /api/evenements` | `theme`, `themes`, `zone`, `langue`, `jours`, `bbox`, `limite` | Les points de la carte, les plus suivis d'abord |
| `GET /api/evenements/{id}` | – | Un événement et **toutes ses sources** |
| `GET /api/direct` | – | Flux Server-Sent Events : signale qu'une collecte a eu lieu |
| `GET /api/collectes` | `limite` | Journal des collectes et état de chaque source |

`themes` accepte une liste séparée par des virgules. `bbox` s'écrit `ouest,sud,est,nord` en
degrés et accepte le passage de l'antiméridien. `langue` vaut `fr`, `en`, ou rien (ou
`toutes`) pour ne pas filtrer. Un code inconnu renvoie 422 ; si la base n'existe pas
encore, 503.

Sur la carte, `langue` garde les événements dont **au moins une** source est dans cette
langue ; sur le fil, l'article mis en avant est alors choisi dans cette langue, pour ne pas
afficher un titre anglais à qui a demandé du français.

Les réponses sont compressées (gzip) et portent `Cache-Control` et `ETag` : le référentiel est
gardé une heure, le fil une minute, et un navigateur à jour reçoit `304`.

## Le site

Deux vues — **Carte** et **Liste seule** — un filtre de période, un filtre de langue
(Toutes / Français / Anglais) et des thèmes que le visiteur coche à sa guise (ses choix sont
retenus localement). Chaque thème a sa page : `/theme/eco`,
adresse partageable, sans rechargement.

Leaflet et Leaflet.markercluster sont **servis par le site** (`site/vendor/`, licences
comprises) : aucune requête vers un CDN. Le module de carte et ces bibliothèques ne sont
téléchargés qu'à la première ouverture de la carte — un visiteur qui reste sur la liste ne
paie pas ces 60 Kio. Les points ne sont demandés que pour la fenêtre affichée.

Le fond de carte vient par défaut d'OpenStreetMap, dont la politique d'usage demande de citer
la source — c'est fait en permanence sur la carte — et de rester sur un trafic modeste. Pour
un site qui marche, installer son propre serveur de tuiles et changer l'adresse dans
`config/carte.json`.

**Accessibilité** : contrastes conformes WCAG AA en thème clair comme sombre (le plus faible
mesuré est 4,69 : 1), navigation complète au clavier avec focus visible, zones `aria-live`
pour l'état de la carte, cibles tactiles d'au moins 24 px, et la vue « Liste seule » donne
exactement les mêmes données sans carte. Vérifié par `bancs/bench_accessibilite.mjs`.

**Mises à jour sans recharger** : le serveur signale la fin d'une collecte, le navigateur
redemande ce qu'il affiche avec ses propres filtres. Si le flux d'événements ne passe pas
(proxy qui coupe), le site se replie tout seul sur une interrogation toutes les deux minutes,
suspendue quand l'onglet est caché.

## Administration

Console web réservée à l'équipe sur **/admin**, protégée par mot de passe.

```bash
MAP_ADMIN_MOT_DE_PASSE="un-mot-de-passe-long" uv run uvicorn serveur.app:app --reload
```

```powershell
$env:MAP_ADMIN_MOT_DE_PASSE = "un-mot-de-passe-long"   # PowerShell
uv run uvicorn serveur.app:app --reload
```

Le mot de passe n'est jamais écrit dans le code ni dans Git. **En production, servir le site
en HTTPS** : en HTTP simple, le mot de passe circule en clair.

Cinq onglets : tableau de bord, articles à trier, recherche et reclassement, sources (avec
leur dernier passage, leur disjoncteur et un bouton « Tester sans enregistrer »), et journal
des collectes avec un bouton « Lancer la collecte » — qui force le passage sans attendre la
fréquence ni la fin d'une mise en pause.

## Sécurité

| Risque | Ce qui est fait |
|---|---|
| **SSRF** — une adresse de flux qui vise le réseau interne | Schéma et adresse IP écrite en clair vérifiés à l'enregistrement ; adresses réellement résolues vérifiées **juste avant chaque requête**, et de nouveau après chaque redirection. `MAP_AUTORISER_RESEAU_LOCAL=1` lève la protection pour le développement. |
| **Mémoire** — un flux énorme ou sans fin | Lecture bornée à 8 Mio, au-delà la source est abandonnée |
| **XSS** — du HTML dans un flux | Les balises sont retirées à la collecte, et tout ce qui vient d'un flux repasse par un échappement à l'affichage |
| **Liens piégés** | Seuls les liens `http(s)` entrent en base ; les liens externes portent `rel="noopener noreferrer"` |
| **Mot de passe d'administration** | Variable d'environnement, comparaison à temps constant, HTTPS requis en production |

Le contrôle DNS est fait au moment de la requête et non à l'enregistrement : une panne de
résolution ne doit pas empêcher d'ouvrir la base ni de sauvegarder une source valable.

## Mise en ligne

Il faut un serveur qui exécute Python (GitHub Pages ne convient pas) :

1. **Le serveur web**, en permanence, avec `MAP_ADMIN_MOT_DE_PASSE` :
   ```bash
   uv run uvicorn serveur.app:app --host 0.0.0.0 --port 8000
   ```
   Derrière nginx, Apache ou Caddy en HTTPS, déclaré comme service (systemd) pour qu'il
   redémarre seul. Avec nginx, laisser passer `/api/direct` sans tampon
   (`proxy_buffering off;`) — l'en-tête `X-Accel-Buffering: no` s'en charge déjà.
   Activer brotli au niveau du serveur frontal si l'on veut aller plus loin que gzip.
2. **La collecte planifiée** (D07), sur la même machine puisque la base est un fichier local.
   Avec les fréquences par source, une collecte toutes les dix minutes est raisonnable :
   ```
   */10 * * * * cd /chemin/map-veille && /chemin/uv run collecteur/collect.py >> collecte.log 2>&1
   ```
   Le serveur lit la base en lecture seule et SQLite (mode WAL) autorise cette lecture pendant
   l'écriture : rien à redémarrer après une collecte.
3. **Le lien Moodle** (C02) pointe vers l'adresse du serveur.

### Exemple : Alwaysdata

Sur Alwaysdata, on n'installe pas uv (66 Mo, trop pour l'offre gratuite) : on utilise `pip`
avec `requirements.txt`, généré depuis `uv.lock`. Remplacer `NOM` par le nom du compte.

1. **Environnement > Python** : version 3.12.
2. **En SSH** (`ssh NOM@ssh-NOM.alwaysdata.net`) :
   ```bash
   git clone git@github.com:wakadu13/map-veille.git ~/map-veille
   cd ~/map-veille
   python -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python collecteur/collect.py          # première collecte : crée la base
   ```
3. **Web > Sites > Ajouter un site**, type **Programme utilisateur** :
   - commande : `/home/NOM/map-veille/.venv/bin/uvicorn serveur.app:app --host $IP --port $PORT`
   - répertoire de travail : `/home/NOM/map-veille`
   - variable d'environnement : `MAP_ADMIN_MOT_DE_PASSE=...`
4. **Avancé > Tâches planifiées** : `cd /home/NOM/map-veille && .venv/bin/python collecteur/collect.py`
5. **Mise à jour** : `git pull && .venv/bin/python -m pip install -r requirements.txt`, puis
   redémarrer le site.

Pour un dépôt privé, donner au serveur une **clé de déploiement** en lecture seule
(`ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""`, puis coller la clé publique dans GitHub,
*Settings > Deploy keys*).

Après chaque `uv add`, régénérer `requirements.txt` :
`uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt`,
puis remettre les lignes de commentaire en tête.

## Mesures

Le dossier `bancs/` contient des bancs de mesure reproductibles, et les rapports :

- `bancs/RAPPORT-phase1.md` — audit de départ, goulots trouvés par profilage ;
- `bancs/DECISION-phase2.md` — comparaison Python / Go / Rust et choix du stockage ;
- `bancs/RESULTATS-phase4.md` — tableau avant / après.

```bash
uv run bancs/serveur_flux.py --port 8910 --flux 100   # serveur de flux, dans un terminal à part
uv run bancs/bench_ingestion.py --flux 100 --port 8911
uv run bancs/gen_base.py && uv run bancs/bench_api.py
uv run bancs/bench_lieux.py                           # precision de la localisation
node bancs/bench_accessibilite.mjs http://127.0.0.1:8000/
```

### Pourquoi Python, et quand rouvrir la question

Le choix de rester en Python plutôt que de passer à Go repose sur des mesures, pas sur une
préférence : à l'échelle visée l'écart est de 3 s par collecte, et `encoding/xml` rejette les
flux mal formés que `feedparser` répare. Le dossier complet est dans
`bancs/DECISION-phase2.md`.

**Seuils à partir desquels re-mesurer** avec `bancs/` :

| Signal | Seuil indicatif |
|---|---|
| Nombre de flux | au-delà de ~500 |
| Fréquence de collecte | toutes les 5 minutes ou moins |
| Durée d'une collecte | plus de 30 s de façon répétée |
| Articles en base | au-delà de 500 000 (re-mesurer aussi SQLite contre PostgreSQL/PostGIS) |
| p95 de l'API | plus de 100 ms |

## Codes

Zones : `eur` Europe, `rus` Russie et Europe de l'Est, `mena` Moyen-Orient et Afrique du Nord,
`afr` Afrique subsaharienne, `asi` Asie et Indo-Pacifique, `ame` Amériques.

Les thèmes ne sont plus codés en dur : voir `config/themes.json`.

## Points à valider (lot LT3)

- **Les 16 adresses du thème `geo`, jamais testées** : lancer
  `uv run collecteur/admin.py verifier-flux` et corriger ce qui ne répond pas.
- Les adresses des autres flux et les conditions d'utilisation de chaque source (C03, C04).
- Les mots-clés : les enrichir à partir de `admin.py a-trier` après quelques jours.
- Les cas limites de zones : la Turquie et le Soudan sont en Moyen-Orient et Afrique du Nord,
  le Groenland en Europe.
- Les exonymes français (`config/exonymes.json`) et les noms à ignorer
  (`config/lieux_ignores.json`), à enrichir dès qu'un cas réel est mal traité.
- Le fond de carte : rester sur les tuiles OpenStreetMap ou installer son propre serveur.
- D04 : faut-il encore exiger une zone **et** un thème pour diffuser, maintenant que la zone
  n'apparaît plus sur le site ? Elle est aujourd'hui déduite du lieu, donc la question ne
  coûte plus de points sur la carte, mais elle reste ouverte.
