# Phase 1 — Audit de MAP

Mesures faites le 27/10/2026 sur 4 vCPU / 16 Gio, Python 3.12, SQLite 3.45.1, uvicorn 1 worker.
Flux servis par `bancs/serveur_flux.py` (100 flux, 28 Kio chacun, latences 80–600 ms, 3 lents, 2 morts).
Tous les chiffres sont reproductibles avec `bancs/` (voir `bancs/README.md`).

---

## 1. Architecture actuelle

```
sources (table SQLite)
   │
   ├─ rss (feedparser) ─┐
   ├─ gdacs (GeoJSON) ──┼──► collecteur/collect.py  ──►  donnees/map.sqlite
   └─ reliefweb (POST) ─┘    séquentiel, 1 passe         articles, sources,
                             tri par mots-clés           collectes, vues fil/a_trier
                             (regles.json, pays.json)          │ lecture seule
                                                               ▼
                                     navigateur ◄── JSON ── serveur/app.py (FastAPI)
                                     site/index.html         + serveur/admin.py (Basic auth)
                                     (SVG, vanilla JS)
```

**Dépendances** : 3 directes (`fastapi`, `uvicorn`, `feedparser`), `sqlite3` de la bibliothèque
standard, `urllib` pour le réseau. Front : aucune dépendance JS, un seul fichier de 241 lignes.
**C'est un socle sain** : séparation collecteur / API / front déjà faite, pas de framework inutile,
61 tests qui passent en 2,8 s, administration complète. Rien à jeter.

**Flux de données** : le collecteur tourne à la demande ou en tâche planifiée, écrit dans SQLite
(WAL), purge les articles de plus de 30 jours (D05). L'API lit la base en lecture seule et sert
le front, qui ne contient aucune donnée.

---

## 2. Mesures de référence

### Ingestion — 100 flux, 3 800 articles

| | temps mur | CPU | mémoire max |
|---|---|---|---|
| 100 flux (dont 3 lents + 2 morts) | **94,8 s** | 14,3 s (15 %) | 35 Mio |
| 95 flux sains | **45,7 s** | 12,6 s (28 %) | 35 Mio |

Découpe des 45,7 s (`bancs/profil_cpu.py`) :

| étape | durée | part | par article |
|---|---|---|---|
| 1. téléchargement (séquentiel) | 33,01 s | **73,5 %** | 8,69 ms |
| 2. parsing feedparser | 1,09 s | 2,4 % | 0,29 ms |
| 3. tri par mots-clés | 10,74 s | **23,9 %** | 2,83 ms |
| 4. normalisation + SQLite | 0,09 s | 0,2 % | 0,02 ms |

### API — latences p50 / p95 (ms) et taille des réponses

| route | 5 000 art. | 50 000 art. | 200 000 art. | taille | gzip | cache |
|---|---|---|---|---|---|---|
| `/api/referentiel` | 2,9 / 3,4 | 3,0 / 4,0 | 3,2 / 4,1 | **87,6 Kio** | non | aucun |
| `/api/synthese` | 8,5 / 11,0 | 148,8 / 184,9 | **740,0 / 819,9** | 1,4 Kio | non | aucun |
| `/api/articles` (150) | 6,7 / 8,1 | 66,0 / 89,6 | **307,0 / 375,5** | 30,4 Kio | non | aucun |
| `/api/collectes` | 1,8 / 3,4 | 2,2 / 3,2 | 1,7 / 2,3 | 0,2 Kio | non | aucun |
| `/` (HTML) | 1,6 / 2,1 | 1,6 / 2,3 | 1,4 / 3,5 | 14,0 Kio | non | ETag |

### Front — premier chargement

7 requêtes · **165 Kio non compressés** · FCP 64 ms · fil affiché **357 ms** (en local, sans
latence réseau) · 1 364 nœuds DOM · 1 feuille de style externe bloquant le rendu.
Avec 300 ms de latence simulée sur la ressource externe : **660 ms**.

---

## 3. Goulots d'étranglement (profilés, pas supposés)

| # | Problème | Preuve | Coût mesuré |
|---|---|---|---|
| 1 | **Téléchargements strictement séquentiels** (`for s in …: collecter_source`) | profil : 33 s sur 45 s dans `urlopen` | 73,5 % de l'ingestion |
| 2 | **Réessais bloquants, pas de circuit breaker** : 3 essais × `sleep(6)` puis `sleep(12)` dans le thread principal | 94,8 s − 45,7 s | **49 s pour 2 flux morts** |
| 3 | **Tri : 487 regex exécutées par article** | `cProfile` : 1 850 885 appels à `re.findall`, 8,68 s de `tottime` | 2,65 ms/article |
| 4 | **`/api/synthese` balaie deux fois la base** (GROUP BY zone, puis GROUP BY source) | 372 ms + 366 ms = 739 ms mesurés isolément | = les 740 ms de la route |
| 5 | **Aucun index composite** : `idx_articles_date` seul, les filtres zone/thème/masque se font en balayage | `EXPLAIN QUERY PLAN` | count 275 ms à 200 k |
| 6 | **Pas de compression ni de cache HTTP** ; `/api/referentiel` relit et resérialise `carte.json` à chaque appel | en-têtes de réponse | 87,6 Kio statiques, 65 % de la page |
| 7 | **Le front demande deux fois les mêmes données** (`document.fonts.ready.then(actualiser)`) | trace Playwright : 2× `/api/synthese` + 2× `/api/articles` | 32 Kio et 2 requêtes API de trop |
| 8 | **Feuille de style Google Fonts bloquant le rendu**, sur 2 origines externes | `<link rel=stylesheet>` dans `<head>` | +300 ms mesurés en simulation |
| 9 | **Aucun en-tête conditionnel** (`ETag` / `If-Modified-Since`) sur les flux | `collect.http()` | 2,67 Mio retéléchargés à chaque passage |
| 10 | **Pas de limite de taille au téléchargement, pas de garde SSRF** : `r.read()` sans borne, redirections suivies | `collecteur/collect.py:133` | risque, pas de coût |

---

## 4. Gains mesurés sur prototypes

Ce ne sont pas des estimations : chaque ligne a été exécutée (`bancs/proto_*.py`, `bancs/bench_sql.py`).

| Correctif | Avant | Après | Gain |
|---|---|---|---|
| Téléchargement concurrent (32 simultanés) | 33,01 s | 2,03 s | **16×** |
| Tri en un balayage (automate de jetons) — *sortie strictement identique, vérifiée sur 2 405 textes* | 2,65 ms/art. | 0,58 ms/art. | **4,6×** |
| **Ingestion complète, 95 flux** | **44,9 s** | **5,2 s** | **8,6×** |
| En-têtes conditionnels (95/95 réponses 304) | 2 670 Kio | 0 Kio | bande passante ÷ ∞ |
| Index `(zone, theme, masque, date_pub DESC)` — count à 200 k | 275 ms | 9,1 ms | **30×** |
| Index composite — page d'articles à 200 k | 7,97 ms | 0,26 ms | **30×** |
| Table de synthèse pré-agrégée (930 lignes) | 739 ms | 0,25 ms | **~3 000×** |
| gzip sur `/api/referentiel` | 87,5 Kio | 32,7 Kio | −63 % |
| gzip sur `index.html` | 14,0 Kio | 5,4 Kio | −61 % |

Avec ces seuls correctifs, les objectifs de la phase 4 sont atteints **sans changer de langage** :
ingestion de 100 flux ≈ 5 s (objectif < 30 s), API < 10 ms au p95 à 200 000 articles (objectif < 100 ms).

---

## 5. Recommandation sur le langage

Même travail, mêmes flux, mêmes règles de tri (`bancs/proto_py.py` et `bancs/go/`) :

| | Python actuel | **Python optimisé** (32 simultanés) | **Go** (32 simultanés) |
|---|---|---|---|
| téléchargement | 33,01 s | 2,03 s | 1,70 s |
| parsing XML | 1,09 s | 1,12 s | 0,07 s |
| tri par mots-clés | 10,74 s | 2,08 s | 0,45 s |
| **total** | **44,92 s** | **5,23 s** | **2,23 s** |
| mémoire max | 35 Mio | 56 Mio | 20 Mio |

**Recommandation : rester en Python et l'optimiser.** Les mesures, pas une préférence :

1. **Le gain réel de Go est de 3 s par collecte.** Pour une collecte horaire, 72 s de CPU par jour.
   C'est le prix d'un second langage à maintenir, d'une seconde chaîne de build et de déploiement,
   et de la perte de `feedparser` — dont la tolérance aux flux cassés est précisément ce que
   demande le cahier des charges. `encoding/xml` en Go **rejette** les flux mal formés que
   `feedparser` sait réparer (il n'y a pas d'équivalent Go de qualité comparable).
2. **La partie où Go gagne n'est pas celle qui coûte.** Le téléchargement — 73 % du temps actuel —
   est limité par le réseau : 2,03 s en Python contre 1,70 s en Go, soit 0,33 s d'écart.
   Les deux langages attendent le même réseau.
3. **Le prochain goulot n'est ni Python ni Go : c'est le géocodage.** La politique de Nominatim
   impose 1 requête/seconde. Avec un cache persistant, un démarrage à froid sur 100 flux
   (~500 lieux distincts) demande ~500 s d'attente réglementaire. Aucun langage n'y change rien :
   ce qui compte est le cache, pas la vitesse d'exécution.
4. **Rust** : plus rapide encore sur les 2,5 s de CPU, pour un coût de développement très
   supérieur sur un projet d'étudiants. Hors de proportion.
5. Le front reste en JS/TS sans framework, comme aujourd'hui.

**Où Go resterait justifié** : si le volume passait à plusieurs milliers de flux rafraîchis toutes
les 5 minutes, ou si l'hébergement imposait un binaire unique sans environnement Python.
À retester alors avec `bancs/`, pas à décider d'avance.

---

## 6. Écarts entre l'existant et les objectifs demandés

À lire avant la phase 3 : plusieurs points du cahier des charges **n'existent pas du tout**, et
deux entrent en conflit avec des exigences du projet actuel.

| Objectif demandé | État actuel |
|---|---|
| Thèmes définis en fichier de configuration | **Codés en dur** dans `collecteur/base.py` (`THEMES`), avec une table et une clé étrangère. Idem pour les 6 zones. |
| Page par thème, activation/désactivation par l'utilisateur | N'existe pas : une seule page, filtres en mémoire. |
| Flux avec langue et fréquence de rafraîchissement | La table `sources` n'a ni l'une ni l'autre. |
| Carte interactive avec points géographiques | **N'existe pas.** La carte est un SVG figé de 6 zones (`donnees/carte.json`), coloré par volume. Aucune coordonnée nulle part. |
| Géocodage + cache persistant | N'existe pas. |
| Regroupement d'articles sur un même point | **Fonctionne à l'inverse aujourd'hui** : deux articles de même titre normalisé → le second est **écarté** (`enregistrer()` renvoie `"doublon"`), ses sources sont perdues. Il faudra les conserver et les grouper. |
| Extrait court de l'article | Non stocké — **et l'exigence C03 du projet l'interdit** explicitement. À arbitrer. |
| Mises à jour quasi temps réel (SSE / polling) | N'existe pas. |
| Compression, cache HTTP, pagination par bbox | N'existe pas. |

**Ce qui existe déjà et qu'il ne faut pas perdre** : worker d'ingestion séparé, purge
automatique à 30 jours (D05), journal des collectes, classement manuel, masquage, administration
web complète, tolérance aux sources en panne, 61 tests.
