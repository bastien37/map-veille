# MAP – Monitoring de l'Actualité Planétaire

Outil de veille du projet pluridisciplinaire (EAE). Des flux RSS et des API ouvertes sont
collectés, triés par thème, **placés sur une carte** et **regroupés par événement** : quand
cinq médias parlent du même fait, la carte n'affiche qu'un point, et un panneau liste les
cinq sources.

## Architecture

```
config/themes.json ─┐
config/flux.json ───┤
config/ia.json ─────┤
                    ▼
Flux RSS + API ──► collecteur/collect.py ──────────► donnees/map.sqlite
(table sources)    · téléchargement parallèle        articles, événements,
                     (ETag, disjoncteur)             sources, collectes,
                   · tri par mots-clés, puis         caches de géocodage
                     modèle de langage si doute      et de classement
                     (collecteur/ia.py)                      │
                   · localisation (collecteur/lieux.py)      │
                   · regroupement en événements               │
                   · récapitulatif du jour par thème          │ lecture seule
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
3. **Tri** — zone et thème par mots-clés, en un seul balayage du texte. Quand les
   mots-clés hésitent, un modèle de langage peut trancher — désactivé par défaut (voir
   « Comment un article reçoit son thème »).
4. **Localisation** — balises géographiques du flux, sinon un nom de lieu reconnu dans le
   titre, sinon rien (voir « Comment un article arrive sur la carte »).
5. **Regroupement** — même lieu, même fenêtre de temps, titres proches : un seul événement.
6. **Stockage** — `donnees/map.sqlite`. Seuls le titre, la source, la date, le lien et la
   position sont conservés (C03). **Les articles de plus de 30 jours sont supprimés à chaque
   collecte (D05)**, et les événements qui n'ont plus de source avec eux.
7. **Récapitulatif** — si le modèle est actif, un paragraphe par thème pour la journée.
8. **Diffusion** — l'API sert le fil et les points de la carte. Un article sans zone ou sans
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
| `config/ia.json` | Le modèle de langage qui classe les thèmes douteux — **désactivé par défaut** |
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

### Vérifier que les flux répondent — et trouver leur adresse

Une adresse de flux se devine mal, et une source muette ne se remarque qu'au bout de
quelques collectes. Après chaque ajout dans `config/flux.json` :

```bash
uv run collecteur/admin.py verifier-flux                       # les sources actives
uv run collecteur/admin.py verifier-flux --decouvrir           # et cherche ce qui échoue
uv run collecteur/admin.py verifier-flux --decouvrir --ecrire  # et corrige le fichier
uv run collecteur/admin.py verifier-flux --toutes --nom ifri   # une seule, même désactivée
```

La commande interroge chaque source en parallèle **sans rien enregistrer** et affiche, pour
chacune, `ok` avec le nombre d'articles et la date du plus récent, `vide` si elle répond sans
rien d'exploitable, ou `NON` avec l'erreur.

`--decouvrir` ne se contente pas de constater l'échec : il demande au site **où est son
flux**. D'abord l'annonce officielle — la balise `<link rel="alternate"
type="application/rss+xml">` que tout site bien élevé met dans son `<head>` —, sur la page
d'accueil puis sur le dossier de l'adresse donnée, ce qui rattrape les sites multilingues.
Si rien n'est annoncé, les chemins conventionnels sont essayés : `/feed/`, `/rss.xml`,
`/index.xml`, `/atom.xml`, `?feed=rss2`… à la racine puis sous le préfixe de langue. La
première adresse qui rend de vrais articles gagne.

`--ecrire` reporte dans `config/flux.json` les adresses trouvées, et remplace
l'avertissement « adresse non vérifiée » par la date de vérification et le nombre d'articles
lus. Les adresses essayées passent par le même contrôle anti-SSRF que la collecte.

### Les sources livrées

**62 sources déclarées, 46 actives.** Toutes les adresses ont été interrogées pour
de bon le 28 octobre 2026 : 44 répondaient, dont 8 à une autre adresse que celle écrite au
départ, et 14 ne répondent pas du tout — celles-là sont coupées, avec le motif constaté
écrit dans leur note. Chaque entrée de `config/flux.json` porte désormais soit
« Adresse vérifiée le … », soit « NE RÉPOND PAS — … », et trois tests l'exigent.

| Thème | Sources actives |
|---|---|
| *(aucun — tri par mots-clés)* | Le Monde, France 24, RFI, franceinfo, The Conversation France, Ars Technica |
| `def` Conflits et défense | Opex360 (Zone militaire), Forces Operations Blog, Defense News, Breaking Defense, Defense One, Naval News, The War Zone |
| `geo` Géopolitique | Le Grand Continent, The National Interest, Le Rubicon, Revue Conflits, Diploweb, Institut Montaigne, Areion24.news, War on the Rocks, Atlantic Council, Lowy Institute – The Interpreter, Le Monde diplomatique, Orient XXI, Politique étrangère (Ifri), Foreign Affairs, Foreign Policy, The Diplomat, RUSI, CSIS, SWP Berlin, Responsible Statecraft |
| `cyb` Cybersécurité et informatique | CERT-FR (ANSSI), LeMagIT, Le Monde Informatique, Next, ZATAZ, Krebs on Security, BleepingComputer, The Record, The Hacker News, Dark Reading, CISA – avis de sécurité, Schneier on Security |
| `cli` Climat et catastrophes | GDACS (ONU / Commission européenne) |

**Les quatorze sources coupées, et pourquoi** (à réactiver depuis l'administration si elles
reviennent) :

| Motif | Sources |
|---|---|
| Filtrage anti-robot : 403 ou connexion coupée sur tout le site | IFRI, Chatham House, ECFR, Géoconfluences, ANSSI (`cyber.gouv.fr`) |
| Plus aucun flux : 404 partout, rien d'annoncé par le site | IRSEM, Revue Défense Nationale, Ministère des Armées, OTAN |
| Répond 200 mais sert du HTML | Carnegie Endowment, Fondation Robert Schuman, Mer et Marine |
| Flux retiré volontairement (410 Gone) | Meta-Defense |
| Flux cassé côté site : que des avertissements PHP, aucun XML | East Pendulum |

Le CERT-FR, lui, répond très bien (40 avis) : c'est le portail `cyber.gouv.fr` de l'ANSSI
qui est derrière une protection anti-robot, pas le CERT.

Les fréquences suivent le rythme réel de publication : 60 min pour un quotidien, 120 à 180
pour la presse spécialisée, 360 pour les instituts, 1 440 pour une revue universitaire.
Interroger un trimestriel toutes les heures n'a aucun sens et use le crédit de bonne volonté
du site.

**Le thème `geo`** vise le *registre d'analyse* (« rivalité systémique », « multipolaire »,
« autonomie stratégique », « puissance »), pas les faits : une frappe de missile reste dans
`def`, une visite d'ambassadeur dans `dip`. Un test le vérifie
(`tests/test_config.py::test_les_mots_cles_geo_ne_devorent_pas_les_autres_themes`).

**Le thème `cyb`** couvre la cybersécurité *et* l'informatique au sens large. En le créant,
`cyber*`, `internet`, `numerique`, `puce*` et `semi conducteur*` ont **quitté `tec`** : les
laisser aux deux endroits aurait donné un point à chacun sur tout article de cybersécurité,
donc une égalité permanente. `tec` garde l'espace, les sciences et l'intelligence
artificielle, et a reçu en échange `recherche`, `laboratoire`, `biotech*`, `telescope`,
`orbite`, `astronom*`, `physique`, `genome`, `vaccin*`. Un test interdit désormais qu'un
mot-clé appartienne à deux thèmes, ou qu'une racine en couvre un autre du même thème
(`cyber*` rendait `cyberattaque*` inutile, et doublait son score).

À refaire de temps en temps, un flux ne prévenant pas quand il déménage :

```bash
uv run collecteur/admin.py verifier-flux --decouvrir --ecrire
```

Les huit adresses corrigées l'ont été par cette commande ou par le même procédé à la main :
`nationalinterest.org/feed/`, `navalnews.com/feed/?feed=rss2`,
`rusi.org/rss/latest-commentary.xml` (annoncée par le site), `csis.org/rss.xml`,
`swp-berlin.org/rss.xml`, `feeds.arstechnica.com/arstechnica/index.xml`,
`lemagit.fr/rss/ContentSyndication.xml` et `orientxxi.info/spip.php?page=backend&lang=fr`.

Ars Technica n'a pas de flux pour sa seule rubrique sécurité : c'est le flux général du
site qui est repris, et son thème est donc laissé au tri par mots-clés plutôt qu'imposé
à `cyb` — sinon l'astronomie finirait en cybersécurité.

**Un nouveau type d'API** demande du code : écrire `source_xxx(s, hors_ligne, …)` dans
`collect.py`, l'appeler depuis `collecter_source()`, et ajouter le type dans `TYPES_SOURCES`
et dans la contrainte `CHECK` de la table `sources` (`base.py`). Le tri, la localisation, le
regroupement et le stockage sont communs.

## Comment un article reçoit son thème

**Les mots-clés d'abord, toujours.** Un thème imposé par la source l'emporte sur tout ; sinon
les mots-clés de `config/themes.json` sont comptés (titre ×3, résumé ×1) et le thème qui a le
plus de points gagne. C'est gratuit, instantané, et chaque décision s'explique en montrant le
mot qui l'a provoquée.

Ils échouent sur deux familles de titres :

```
« Pékin et Washington reprennent langue »            aucun mot-clé ne tombe
« Nouvelle frégate pour la marine indonésienne »     « def » et « eco » à égalité
```

**Un modèle de langage peut prendre le relais, là et seulement là.** Il est **désactivé par
défaut** : sans `config/ia.json` actif, rien ne part sur le réseau et le collecteur se
comporte exactement comme avant.

```bash
export MAP_IA_CLE="..."              # jamais dans le fichier : il est suivi par Git
# puis « actif: true » dans config/ia.json
uv run collecteur/collect.py
uv run collecteur/admin.py ia        # ce que le modèle a classé, et son état
uv run collecteur/collect.py --sans-ia   # une collecte sans l'appeler
```

Cinq services sont préréglés dans le fichier, tous en dialecte « chat completions »
d'OpenAI : **Groq**, **Mistral**, **OpenRouter**, **Google Gemini**, et **Ollama** en local.
Ollama est le seul vraiment gratuit et le seul où rien ne sort du serveur : c'est le choix à
préférer sur un VPS qui a la mémoire pour. Les quotas et les noms de modèles des autres
changent souvent — les vérifier chez le fournisseur avant de s'y fier.

`quand` règle le déclenchement : `doute` (défaut : aucun thème trouvé **ou** deux thèmes à
égalité), `manquant` (seulement quand rien n'a été trouvé), `tout` (chaque article, coûteux).

Cinq garde-fous, tous nécessaires :

| | |
|---|---|
| **Cache obligatoire** | Un titre soumis une fois ne l'est jamais deux, même après redémarrage (table `themes_ia`). Changer de modèle ou de liste de thèmes invalide les entrées : la question n'est plus la même. Les réponses négatives sont mémorisées aussi. |
| **Budget par collecte** | `max_par_collecte` appels au plus, et une limite de débit. Une offre gratuite se consomme vite. |
| **Jamais bloquant** | Panne, délai dépassé, quota épuisé, réponse incompréhensible : on garde ce que les mots-clés ont trouvé. Après trois échecs d'affilée le modèle n'est plus appelé de la collecte, comme le disjoncteur des sources. Une panne n'est **pas** mise en cache. |
| **Ce qui part** | Le titre et les 400 premiers caractères du résumé. Jamais l'article entier (C03). |
| **Ce qui revient** | Un code de thème de la liste active, ou rien. Toute autre réponse est jetée — y compris une réponse approchante : « defense » n'est pas `def`. |

Le dernier point est une mesure de sécurité, pas une coquetterie : le titre soumis vient d'un
flux, donc d'un tiers, et peut contenir une phrase qui ressemble à une consigne. Comme la
seule sortie acceptée est un code de la liste, le pire qu'un flux hostile puisse obtenir est
un mauvais classement d'un de ses propres articles — ce qu'il pouvait déjà faire en
choisissant ses mots.

Un article classé par le modèle est marqué (`articles.theme_ia`) : `admin.py ia` les liste, et
`admin.py classer` corrige à la main ce qui est faux. **Un article classé à la main n'est plus
jamais modifié**, ni par les mots-clés ni par le modèle.

## Le récapitulatif du jour

Un paragraphe par thème, écrit par le modèle de langage à partir des **seuls titres parus
ce jour-là**, affiché en haut de la page dans un bloc pliable.

```bash
uv run collecteur/admin.py recap                      # le jour même
uv run collecteur/admin.py recap --jour 2026-10-27
uv run collecteur/admin.py recap --theme cyb --refaire
```

Il est écrit **à la collecte** et rangé en base (`"recap": {"actif": true}` dans
`config/ia.json`). Afficher la page ne déclenche jamais d'appel à un service extérieur : ce
serait un appel par visiteur, et le texte serait différent à chaque rechargement. Sans
modèle configuré, `admin.py recap` reste utile — il affiche ce qui est déjà en base.

Le coût est négligeable : un appel par thème et par jour, sept au total. Un récapitulatif
déjà écrit n'est refait que si la journée a **gagné des articles** *et* que le précédent a
dépassé `recap.intervalle_minutes` (trois heures par défaut). Sans nouvel article le texte
serait identique : on ne dépense rien. Les récapitulatifs sont gardés 90 jours, bien après
les articles qu'ils résument — cela donne une mémoire du trimestre pour presque rien.

La consigne donnée au modèle lui interdit d'ajouter le moindre fait, chiffre, date ou nom
qui ne soit pas dans les titres, et lui dit explicitement que ces titres sont des données,
jamais des instructions. Le bloc affiché porte la mention de ce qu'il est : *« Texte écrit
par un modèle de langage à partir des seuls titres parus ce jour-là. Il peut se tromper :
les articles d'origine font foi. »* Un résumé reste un résumé ; c'est pour cela que les
articles sont juste en dessous.

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
dans `collecteur/base.py` (version 14).

| Table ou vue | Contenu |
|---|---|
| `articles` | Titre, source, date, lien, zone, thème (et s'il vient du modèle), **langue**, **position**, événement, correction manuelle, masquage |
| `evenements` | Un fait = une ligne : titre, lieu, position, période, nombre de sources publiables, langues présentes |
| `themes`, `zones` | Référentiels. `themes` vient de `config/themes.json` |
| `sources` | Les sources : état du cache conditionnel, disjoncteur, langue, fréquence, origine |
| `lieux_geocodes` | Cache de géocodage, succès et échecs |
| `themes_ia` | Cache du classement par modèle de langage : un titre n'est soumis qu'une fois |
| `recaps` | Un récapitulatif par thème et par journée, gardé 90 jours |
| `collectes`, `collectes_sources` | Journal de chaque passage, source par source |
| `fil` (vue) | Les articles publiables : zone **et** thème connus, non masqués |
| `a_trier` (vue) | Les articles que le tri n'a pas su classer |

Outil en ligne de commande :

```bash
uv run collecteur/admin.py stats      # chiffres par zone et thème, dernières collectes
uv run collecteur/admin.py config     # thèmes et sources tels que la configuration les a posés
uv run collecteur/admin.py verifier-flux   # quelles sources répondent vraiment
uv run collecteur/admin.py ia         # état du classement par modèle de langage
uv run collecteur/admin.py recap      # récapitulatif du jour, thème par thème
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
| `GET /api/recap` | `jour`, `theme`, `themes` | Le récapitulatif quotidien déjà écrit (cette route ne calcule rien) |
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

En haut, le **récapitulatif du jour** quand il existe : un bloc pliable (`<details>`, donc
pliable au clavier sans une ligne de JavaScript), une colonne par thème sur grand écran, le
choix plié/déplié retenu localement. Absent par défaut, et la page cesse alors de le
demander à chaque rafraîchissement.

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
| **Injection par un titre de flux** dans le modèle de langage | Classement : seul un code de thème de la liste active est accepté en retour, le pire résultat possible étant un mauvais classement. Récapitulatif : le modèle est averti que les titres sont des données, le texte rendu est tronqué, échappé à l'affichage, et annoncé comme automatique. |
| **Clé d'API du modèle** | Variable d'environnement, jamais dans `config/ia.json`, qui est suivi par Git. Le collecteur refuse de démarrer si le fichier est actif et la variable vide. |

Le contrôle DNS est fait au moment de la requête et non à l'enregistrement : une panne de
résolution ne doit pas empêcher d'ouvrir la base ni de sauvegarder une source valable.

### Si le serveur sort par un proxy HTTP

Une machine derrière un proxy n'ouvre pas la connexion elle-même : elle la demande au proxy,
qui résout le nom à sa place. Elle n'a alors souvent **aucun DNS**, et le contrôle par
adresse IP échoue pour tous les flux — la collecte s'arrête avant d'avoir commencé, sur
« nom de machine introuvable ». L'adresse que le collecteur résoudrait ne serait de toute
façon pas celle qui est jointe.

```bash
MAP_PROXY_RESOUT_LES_NOMS=1 uv run collecteur/collect.py
```

Ce réglage est **fermé par défaut**, et c'est voulu : mieux vaut une collecte qui échoue
visiblement qu'un contrôle retiré en silence. L'ouvrir est une décision d'exploitation —
la politique de sortie devient celle du proxy, à qui il revient de refuser le réseau
interne. Les contrôles de forme, eux, s'appliquent dans tous les cas : `169.254.169.254`,
`10.0.0.1`, `localhost` et tout schéma autre que `http(s)` sont refusés avant d'atteindre
le proxy, et une redirection reste vérifiée. Un hôte listé dans `no_proxy` est joint en
direct : le contrôle DNS s'y applique normalement.

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

### Ce que donnent les vraies dépêches

Mesure du 28 octobre 2026 : les 45 flux actifs interrogés une fois, **1 372 articles réels**
passés dans le tri et la localisation, sans rien enregistrer.

| | |
|---|---|
| Articles placés sur la carte | 833 (**61 %**) |
| Zone **et** thème, donc diffusés (D04) | 867 (**63 %**) |
| Thème mais **pas de zone**, donc écartés | 452 (**33 %**) |
| Ni l'un ni l'autre | 27 (2 %) |
| Non classés par les mots-clés, flux sans thème imposé | 53 sur 157 (**34 %**) |

Les 61 % de placement sont en dessous des 81 % mesurés sur le banc de flux : c'est attendu,
le banc rejoue des dépêches d'agence, qui nomment presque toujours un lieu, là où une note
d'institut s'intitule « Vers un ordre multipolaire ? ».

Les 34 % d'articles non classés par les mots-clés sont exactement la population que le
classement par IA viserait, et une bonne base pour juger s'il apporte quelque chose.

> ⚠️ **Un tiers des articles ne sort pas.** 452 articles ont un thème mais aucune zone, et la
> règle D04 — ne diffuser que ce qui a *zone et thème* — les écarte. Ce sont surtout les
> sources ajoutées récemment : 204 de `cyb`, 181 de `geo`, 50 de `def`. Un avis du CERT-FR
> sur une faille, une analyse sur la dissuasion, n'ont pas de lieu et n'en auront jamais.
> La question D04 figurait déjà dans les points à valider ; elle a maintenant un chiffre.
> Lever l'exigence de zone pour les flux à thème imposé remettrait ces 452 articles dans le
> fil sans rien changer à la carte, qui ne montre de toute façon que les articles localisés.
> **C'est une décision de cahier des charges, elle n'a pas été prise ici.**

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

- **Les cinq sources bloquées par filtrage anti-robot** (IFRI, Chatham House, ECFR,
  Géoconfluences, ANSSI) : leur écrire pour demander un accès, ou s'en passer.
- Les conditions d'utilisation de chaque source (C03, C04), et celles du fournisseur de
  modèle si le classement par IA est activé.
- **Le classement par IA et le récapitulatif sur des données réelles** : ils n'ont été
  vérifiés que sur des réponses simulées. Après quelques jours, comparer `admin.py ia` et
  `admin.py a-trier` pour savoir si le classement apporte quelque chose et avec quel taux
  d'erreur, et relire quelques récapitulatifs pour vérifier qu'ils n'inventent rien.
- **Les mots-clés de `cyb` hors flux spécialisés** : sur 157 articles de flux généralistes,
  un seul a été classé en `cyb` par les mots-clés. L'échantillon est petit, mais il faudra
  vérifier après quelques jours que ce n'est pas un trou dans la liste.
- Les mots-clés : les enrichir à partir de `admin.py a-trier` après quelques jours.
- Les cas limites de zones : la Turquie et le Soudan sont en Moyen-Orient et Afrique du Nord,
  le Groenland en Europe.
- Les exonymes français (`config/exonymes.json`) et les noms à ignorer
  (`config/lieux_ignores.json`), à enrichir dès qu'un cas réel est mal traité.
- Le fond de carte : rester sur les tuiles OpenStreetMap ou installer son propre serveur.
- **D04, maintenant chiffrée** : faut-il encore exiger une zone **et** un thème pour
  diffuser ? Sur 1 372 articles réels, 452 (un tiers) ont un thème mais pas de zone et
  restent invisibles. Voir « Ce que donnent les vraies dépêches ». La zone n'apparaît plus
  sur le site et la carte n'affiche que les articles localisés : l'exigence ne protège plus
  rien de visible.
