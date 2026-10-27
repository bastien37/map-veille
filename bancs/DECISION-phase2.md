# Phase 2 — Décision sur le langage et le stockage

Contexte fixé par vos réponses : **VPS / serveur dédié**, **~100 flux et 8–10 thèmes**
(~100 000 articles sur 30 jours), flux **en français et en anglais**, et la carte à points
**remplace** la carte des 6 zones.

---

## 1. Les quatre options, sur les quatre critères demandés

Toutes les lignes « gain mesuré » viennent de `bancs/` : même machine, mêmes 95 flux,
mêmes règles de tri, mêmes 3 800 articles.

| | **Python optimisé** | **Go** | **Rust** | Node |
|---|---|---|---|---|
| **Gain mesuré** — ingestion totale | **5,23 s** | **2,23 s** | non mesuré (≤ Go) | non mesuré |
| dont téléchargement (réseau) | 2,03 s | 1,70 s | ≈ identique | ≈ identique |
| dont parsing XML | 1,12 s | 0,07 s | — | — |
| dont tri par mots-clés | 2,08 s | 0,45 s | — | — |
| mémoire | 56 Mio | 20 Mio | ~15 Mio | ~80 Mio |
| **Maintenance** | 1 langage, code déjà écrit et testé (61 tests) | 2 langages, 2 chaînes de build, 2 jeux de tests | idem + courbe d'apprentissage forte | 2 langages pour un gain nul |
| **Écosystème RSS** | `feedparser` : référence, **répare les flux cassés** | `encoding/xml` **rejette** les flux mal formés ; pas d'équivalent de qualité | idem Go | `feedparser`-like correct |
| **Écosystème géocodage** | clients Nominatim/Photon matures, `shapely`, `rtree` | corrects | corrects | corrects |
| **Déploiement** | déjà en place (uv + requirements.txt) | binaire unique, plus simple en soi… mais s'ajoute à Python qui reste pour l'API | idem | idem |

## 2. Recommandation : rester en Python, l'optimiser

Trois raisons, dans l'ordre de poids :

**a) L'écart réel est de 3 s par collecte.** 5,23 s contre 2,23 s. Pour une collecte horaire,
72 s de CPU par jour sur un VPS. L'objectif du cahier des charges est 100 flux en moins de
30 secondes : Python optimisé passe avec un facteur 6 de marge. Payer un second langage pour
gagner 3 s sur un budget de 30 s n'est pas défendable.

**b) Là où Go gagne vraiment, il perd sur la robustesse.** Son avance tient au parsing
(1,12 s → 0,07 s). Or le cahier des charges demande explicitement de gérer les **flux
malformés** : c'est le cœur de métier de `feedparser`, et `encoding/xml` fait l'inverse — il
refuse ce que `feedparser` répare. Remplacer un parseur tolérant par un parseur strict pour
gagner une seconde irait contre l'objectif n° 2.

**c) Le prochain goulot n'est pas le CPU.** Sur VPS vous pouvez auto-héberger Photon, ce qui
lève la limite de 1 req/s de Nominatim public — mais le géocodage reste des appels réseau,
donc du temps d'attente que la concurrence résout et que le langage ne change pas. Le levier,
là encore, est le **cache persistant**, pas la vitesse d'exécution.

**Quand rouvrir la question** : plusieurs milliers de flux, ou un rafraîchissement toutes les
5 minutes. Le banc `bancs/` est fait pour re-trancher sur mesures, pas sur opinion.

## 3. Stockage : SQLite suffit, et l'index spatial n'est pas (encore) utile

Mesuré avec `bancs/bench_bbox.py` sur le volume cible — 35 000 points, 100 000 articles,
9 thèmes, 30 jours, base de 8,2 Mio. Requête type de la carte : bbox + thème + période,
regroupée par point, limitée à 500 points.

| variante | Europe de l'Ouest | Afrique | monde entier |
|---|---|---|---|
| **aucun index spatial** | **7,71 ms** | **13,19 ms** | **11,21 ms** |
| index B-tree `(lat, lon)` | 7,25 ms | 13,50 ms | 153,09 ms |
| table virtuelle R*Tree | 9,20 ms | 15,58 ms | 198,23 ms |

Deux enseignements, contraires à ce que j'attendais :

1. **SQLite tient largement l'objectif sans rien ajouter** : 7–13 ms contre 100 ms visés au p95.
   À ce volume la base entière (8 Mio) tient en cache, et un balayage filtré bat un parcours
   d'index.
2. **Ajouter un index spatial dégrade la vue « monde »** (11 ms → 153 ms avec le B-tree,
   198 ms avec le R*Tree) : le planificateur choisit l'index alors qu'il n'est pas sélectif, et
   perd le meilleur plan. Il ne faut donc **pas** créer ces index « par précaution ».

**Recommandation : SQLite, sans SpatiaLite ni R*Tree, sans PostGIS.** Un index composite sur
`(theme, date_pub)` et la table de synthèse pré-agrégée suffisent. PostgreSQL/PostGIS
ajouterait un service à administrer sur le VPS pour un gain nul à ce volume. Le banc
`bancs/bench_bbox.py` reste dans le dépôt : si le volume décuple, on re-mesure et on ajoute le
R*Tree à ce moment-là, sur preuve.

## 4. Migration incrémentale — le site reste fonctionnel à chaque étape

Aucune étape ne casse l'étape précédente ; chacune est un commit livrable, testé.

| # | Étape | Visible pour l'utilisateur | Risque |
|---|---|---|---|
| 1 | Tri en un balayage (équivalence déjà prouvée) + index composite + table de synthèse | API 30× à 3 000× plus rapide | nul (sortie identique, tests existants) |
| 2 | Téléchargement concurrent + `ETag`/`If-Modified-Since` + backoff non bloquant + circuit breaker | collecte 95 s → ~5 s | faible |
| 3 | Compression gzip/brotli, cache HTTP, `/api/referentiel` mis en cache, suppression du double appel du front | page ÷ 3 en poids | faible |
| 4 | Thèmes et flux en fichier de configuration (langue, fréquence) ; migration des 5 thèmes actuels | aucun changement visible | moyen (migration de schéma) |
| 5 | Extraction de lieux + géocodage + cache persistant ; coordonnées stockées, **sans toucher au front** | aucun | moyen |
| 6 | Regroupement des articles par lieu + similarité de titre + fenêtre de temps | les doublons cessent d'être perdus | moyen |
| 7 | Nouvelle carte à points (MapLibre/Leaflet, clustering, chargement par bbox) **à côté** de la carte actuelle | les deux cartes coexistent | faible |
| 8 | Retrait de la carte des 6 zones, une fois la nouvelle validée par vous | la carte zones disparaît | — |
| 9 | Pages par thème, activation/désactivation, SSE ou polling léger | nouvelles fonctions | faible |
| 10 | Sécurité (limite de taille, garde SSRF), accessibilité, README | — | faible |

## 5. Ce qui sera supprimé — à confirmer avant l'étape 8

Vous avez choisi de **remplacer** la carte des 6 zones. Concrètement, disparaîtraient :

- le SVG des 6 zones et son fichier de tracé `donnees/carte.json` (88 Kio) ;
- la coloration par volume (échelle « peu → beaucoup ») et les étiquettes de zone ;
- le filtre par zone sur le site public et la liste de zones du mode téléphone ;
- selon votre arbitrage, **le découpage en zones lui-même** (`ZONES` dans `base.py`, la table
  `zones`, `collecteur/pays.json`, les 363 mots-clés de zone de `regles.json`, la colonne
  `articles.zone`, les filtres `zone` de l'API et de l'administration, la vue `fil`, et
  environ 15 tests).

**Je ne supprime rien de la seconde liste sans votre accord explicite.** Deux options :
garder la zone comme simple attribut de classement (utile pour l'administration et comme
filtre secondaire, coût quasi nul) ou la retirer entièrement. Je recommande de la **garder en
base** et de ne retirer que l'affichage : le géocodage échouera sur une partie des articles,
et la zone reste alors le seul rattachement géographique disponible pour eux.

## 6. Ce que je ne touche pas

Worker d'ingestion séparé, purge à 30 jours (D05), journal des collectes, classement manuel,
masquage, administration web, tolérance aux sources en panne, API `/api/collectes`.
