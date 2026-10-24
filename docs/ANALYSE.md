# OpGrade X — Analyse complète du projet

> Version 0.1 — 2026-10-24. Document de référence technique. Les chiffres de coûts sont des
> estimations à valider par devis ; aucune précision de gradation n'est garantie tant que la
> validation (section 10) n'est pas faite.

---

## 0. Résumé exécutif

OpGrade X est une machine de bureau fermée (Raspberry Pi 5 + caméra HQ + éclairage contrôlé) qui
photographie le recto et le verso d'une carte, mesure son centrage, détecte ses défauts et produit
une note de 1 à 10 **explicable** et **traçable**.

Le vrai défi n'est pas le logiciel : c'est **la qualité et la reproductibilité de la capture**
(optique, éclairage, positionnement) et **la constitution d'un jeu de données étiqueté par des
experts**. Sans ces deux éléments, aucune IA ne pourra produire une note fiable.

Recommandation stratégique :

1. **V1 = « gradation assistée »** : la machine mesure ce qu'elle sait mesurer de façon fiable
   (centrage, dimensions, premiers indices de défauts) et l'opérateur complète/valide les
   sous-notes coins, bordures et surface. Chaque analyse enrichit le jeu de données.
2. **V2 = « gradation semi-automatique »** : modèles de détection de défauts entraînés sur les
   données accumulées en V1, multi-éclairage, retournement motorisé.
3. Ne jamais annoncer de précision commerciale avant d'avoir mesuré la répétabilité et l'accord
   avec des experts sur un lot de référence.

---

## 1. Architecture générale de la machine

```
┌──────────────────────────── Boîtier fermé (noir mat intérieur) ────────────────────────────┐
│                                                                                            │
│   [Caméra HQ IMX477 + objectif 16 mm, fixée sur potence rigide, à ~20–25 cm]               │
│                │                                                                           │
│   [Anneau / panneau LED diffus]   [LED rasantes ×4 (N/S/E/O) à 10–20°]                     │
│                │                                                                           │
│   ┌────────────▼────────────┐                                                              │
│   │ Plateau de positionnement│  ← butées en L, fond noir mat, gabarit par format          │
│   └─────────────────────────┘                                                              │
│                                                                                            │
│   Raspberry Pi 5 (8 Go) ── carte driver LED (MOSFET/PWM) ── (V2: TMC2209 + moteurs)        │
│        │                                                                                   │
│   Écran tactile 7"   SSD NVMe (via HAT)   Alimentation 27 W officielle + 12/24 V pour LED   │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

Principes :

- **Environnement optique contrôlé** : aucune lumière extérieure, géométrie fixe caméra/plateau.
- **Positionnement mécanique répétable** : butées physiques plutôt que logiciel pour la V1.
- **Tout piloté par le Pi**, avec une couche d'abstraction matérielle (HAL) qui permet la
  simulation et le remplacement des composants.

---

## 2. Architecture logicielle

### 2.1 Choix technologiques

| Couche | Choix | Justification |
|---|---|---|
| Langage cœur | Python 3.11 | Version native de Raspberry Pi OS Bookworm, écosystème vision/IA |
| Caméra | `picamera2` (libcamera) | Pilote officiel, contrôle exposition/gain/AWB manuel |
| Vision | OpenCV + NumPy | Standard, rapide, ouvert |
| IA (V2) | PyTorch pour l'entraînement (PC/serveur GPU) → export ONNX → `onnxruntime` sur le Pi | Entraînement hors machine, inférence légère et indépendante du matériel |
| API | FastAPI | Typage, OpenAPI auto, async, testable |
| Base locale | SQLite (WAL) via SQLAlchemy 2 | Zéro administration, robuste, même ORM que PostgreSQL |
| Base centrale | PostgreSQL | Demandé, standard |
| Interface | Web (HTML/JS) servie localement, Chromium en mode kiosque | Tactile, modifiable sans recompiler, accessible à distance pour l'admin |
| Rapports | HTML → PDF (WeasyPrint) | Mise en page professionnelle, QR code facile |

### 2.2 Modules

```
src/opgrade/
  hardware/       HAL : Camera, Lighting, (V2) Motion, Sensors — implémentations "sim" et "pi"
  orchestration/  Machine à états de la session de gradation (étapes, retournement guidé)
  acquisition/    (dans vision/ pour la V1) capture, contrôle qualité image (netteté, exposition)
  vision/         Détection de la carte, redressement, mesure du centrage, (V2) défauts
  ai/             (V2) Inférence ONNX, registre de modèles, scores de confiance
  grading/        Règles versionnées (YAML), calcul des sous-notes et de la note finale
  reports/        (V1.x) Rapport HTML/PDF, QR code
  data/           Modèles SQLAlchemy, accès base locale, file de synchronisation
  api/            FastAPI : routes opérateur, admin, simulation
  web/            Interface tactile
  sync/           (V2) Synchronisation vers PostgreSQL
```

Règle d'or : **le métier (orchestration, grading) ne connaît que des interfaces**, jamais
`picamera2` directement. Le mode simulation passe par exactement le même chemin que la machine
réelle ; seule l'implémentation HAL change.

### 2.3 Flux d'une analyse (V1, retournement manuel)

```
IDLE → AWAIT_FRONT ──capture──► AWAIT_BACK ("Retournez la carte") ──capture──► READY_TO_ANALYZE
      ──analyse──► AWAIT_REVIEW (opérateur saisit/valide coins, bordures, surface) ──► COMPLETED
Toute étape peut aller vers ERROR (ou CANCELLED) ; une capture invalide reste dans l'état courant.
```

Contrôles obligatoires :

- Une capture est **refusée** si aucune carte n'est détectée, si l'image est floue ou
  sur/sous-exposée.
- La capture du verso est **refusée** si elle ressemble trop au recto (carte non retournée).
- L'analyse est **impossible** tant que les deux faces ne sont pas capturées.

---

## 3. Composants physiques indispensables (V1)

| # | Composant | Exemple | Prix estimé (CAD) |
|---|---|---|---|
| 1 | Raspberry Pi 5 8 Go | officiel | 120 $ |
| 2 | Alimentation 27 W USB‑C | officielle | 20 $ |
| 3 | Refroidisseur actif | officiel | 8 $ |
| 4 | Caméra HQ IMX477 (monture C/CS) | Raspberry Pi HQ ou Arducam | 80–100 $ |
| 5 | Objectif 16 mm (ou 12 mm selon hauteur) faible distorsion | 16 mm 10 MP | 50–80 $ |
| 6 | Câble caméra Pi 5 (22→15 broches) | officiel | 5 $ |
| 7 | Écran tactile 7" | Raspberry Pi Touch Display 2 | 90 $ |
| 8 | SSD NVMe 256–512 Go + HAT M.2 | — | 60–90 $ |
| 9 | Panneau LED diffus haute IRC (≥95) 5000–5600 K | bande LED IRC 95 + diffuseur opale | 40 $ |
| 10 | LED rasantes ×4 (barrettes) | IRC ≥90 | 30 $ |
| 11 | Driver LED : 4–6 canaux MOSFET logiques (PWM) | carte MOSFET IRLZ44N ou PCA9685 + MOSFET | 20 $ |
| 12 | Alimentation 12 V 3 A pour LED | — | 20 $ |
| 13 | Potence/rail caméra rigide (profilé alu 2020) | — | 40 $ |
| 14 | Plateau + gabarits de positionnement imprimés 3D, fond noir mat (flocage) | PETG + flocage | 20 $ |
| 15 | Boîtier fermé (contreplaqué/acrylique noir ou alu) | — | 60–120 $ |
| 16 | Mire de calibration (ArUco/ChArUco imprimée sur support rigide) + carte gris 18 % / ColorChecker | — | 30–150 $ |
| 17 | Visserie, connecteurs, câbles | — | 30 $ |

**Total V1 : ~800–1 100 $ CAD** (hors outillage et cartes de référence).

V2 ajoute : moteurs pas à pas NEMA 17 ×2–3, TMC2209 ×3, fins de course, rails/courroies,
capteur de présence (IR ou ToF), éventuellement 2e caméra HQ. **+400–800 $**.

> Note : le Raspberry Pi 5 ne pilote pas directement des LED de puissance ; il faut un étage
> MOSFET. Le PWM matériel du Pi 5 est limité en nombre de canaux : un PCA9685 (I²C, 16 canaux)
> est recommandé pour les éclairages multiples.

---

## 4. Solutions de capture recto/verso

| Critère | A. 1 caméra + retournement **manuel** | A'. 1 caméra + retournement **motorisé** | B. 2 caméras opposées |
|---|---|---|---|
| Coût | ★★★ le plus bas | ★★ | ★ (2× optique + éclairage du dessous) |
| Risque pour la carte | ★★ (manipulation humaine) | ★ (pince/ventouse = risque) | ★★★ (aucun retournement) |
| Précision | ★★★ même optique pour les 2 faces | ★★★ | ★★ nécessite **vitre** sous la carte (reflets, poussière) et double calibration |
| Complexité | ★★★ | ★ | ★★ |
| Débit | ★ | ★★ | ★★★ |

**Recommandation** :

- **V1 : A (manuel guidé)** — c'est ce que vous avez demandé, et c'est le plus sûr pour valider
  l'optique. Le logiciel guide (« Retournez la carte ») et vérifie le retournement.
- **V2 : à décider après tests**. Ma préférence va vers **A'** avec un plateau basculant qui
  retourne la carte sans la pincer (berceau en V), car B impose une vitre entre la carte et la
  caméra du dessous, ce qui dégrade la détection de micro-rayures — précisément ce qu'on cherche.

---

## 5. Fonctionnement de l'intelligence artificielle

### 5.1 Architecture hybride (pipeline)

1. **Détection et redressement** (vision classique) : contour de la carte sur fond noir →
   homographie → image canonique à échelle fixe (ex. 10 px/mm).
2. **Mesures géométriques** (vision classique, déterministe) : dimensions, centrage
   gauche/droite et haut/bas, rectitude des bords.
3. **Détection de défauts** (IA, V2) :
   - *Coins* : 8 crops (4 recto, 4 verso) → classifieur/régression de l'usure (CNN léger type
     MobileNetV3/EfficientNet-Lite).
   - *Bordures* : bandes périphériques → segmentation des zones blanchies/écaillées.
   - *Surface* : images sous éclairages rasants (4 directions) → détection d'anomalies
     (rayures, plis, bosses). Approche recommandée au départ : **détection d'anomalies non
     supervisée** (type PatchCore) car les données étiquetées seront rares, puis modèles
     supervisés (YOLO/segmentation) quand le jeu de données grandira.
4. **Règles de gradation** (déterministe, versionnées) : transforment mesures + défauts en
   sous-notes puis note finale.
5. **Confiance** : combinaison de la qualité de capture, de la confiance des modèles et de la
   distance aux seuils ; sous un seuil → révision humaine obligatoire.

### 5.2 Pourquoi ne pas « une IA qui regarde la photo et donne une note » ?

- Non explicable (exigence 2.4).
- Demande des dizaines de milliers de cartes notées pour être correcte.
- Ne se corrige pas règle par règle.

L'approche hybride donne une note justifiable dès la V1 (centrage mesuré) et permet d'ajouter
l'IA critère par critère.

### 5.3 Données et apprentissage

- Chaque analyse V1 stocke images brutes + sous-notes de l'opérateur → **jeu de données gratuit**.
- Outil d'annotation (V1.x) : l'expert dessine les zones de défaut sur l'image.
- Séparation stricte entraînement / validation / test **par carte physique** (jamais la même
  carte dans deux ensembles).
- Registre de modèles : version, date, jeu de données, métriques, statut (brouillon / validé /
  actif / retiré). Chaque analyse enregistre `model_version`.
- **Aucun réentraînement automatique** : cycle contrôlé entraîner → tester → approuver → publier.

---

## 6. Système de notation et de validation

### 6.1 Sous-notes (1–10, pas de 0,5)

- **Centrage** : ratio du pire axe (ex. 60/40). Barème recto plus strict que verso
  (fichier `src/opgrade/rules/grading_rules.yaml`, versionné).
- **Coins / Bordures / Surface** : V1 saisis/validés par l'opérateur ; V2 proposés par l'IA.

### 6.2 Note finale

```
moyenne_pondérée = Σ (poids_critère × sous-note)        # poids configurables par profil
plafond          = min(sous-notes) + 1                   # un défaut majeur limite la note
note             = clamp(arrondi_inférieur(min(moyenne_pondérée, plafond)), 1, 10)
```

- Règles éliminatoires possibles (ex. pli traversant → plafond 4, carte altérée → « N/A »).
- Toutes les règles sont dans un fichier YAML avec un `version` enregistré dans chaque analyse.
- La note OpGrade est **propre à OpGrade** et ne prédit pas une note PSA/BGS/CGC.

### 6.3 Validation humaine

- Révision obligatoire si : confiance < seuil, sous-note saisie manuellement (V1), écart > 1
  entre IA et règles, ou carte de grande valeur (option).
- Toute modification est journalisée (qui, quand, ancienne/nouvelle valeur, motif).

---

## 7. Structure des bases de données

### 7.1 Local (SQLite) — implémenté en V1 (partiellement)

```
clients(id, uuid, first_name, last_name, email, phone, notes, created_at)
card_types(id, code, name, category, width_mm, height_mm, thickness_mm, active)   -- profils
cards(id, uuid, client_id→clients, card_type_id→card_types, name, set_name, year,
      card_number, language, notes, created_at)
grading_sessions(id, uuid, card_id→cards, machine_id, state, simulated, operator,
      rules_version, model_version, final_grade, confidence, needs_review,
      created_at, updated_at, completed_at)
captures(id, session_id→grading_sessions, side[front|back], lighting, file_path,
      width_px, height_px, sharpness, quality_ok, created_at)
measurements(id, session_id, side, name, value, unit)            -- ex. centering_lr = 0.55
subgrades(id, session_id, criterion, value, source[auto|operator|model], confidence, details_json)
-- V1.x / V2 :
defects(id, session_id, capture_id, criterion, kind, x, y, w, h, severity, confidence, source)
users(id, username, password_hash, role[operator|admin|superadmin], active)
audit_log(id, user_id, entity, entity_id, action, before_json, after_json, at)
calibrations(id, created_at, px_per_mm, camera_matrix, dist_coeffs, white_balance, ...)
model_versions(id, name, version, kind, metrics_json, status, file_path, created_at)
sync_queue(id, entity, entity_uuid, op, payload_json, attempts, last_error, created_at, synced_at)
```

Images sur disque : `data/images/<session_uuid>/<side>_<lighting>.png`, chemin en base.

### 7.2 Central (PostgreSQL) — V2

Même schéma + `machines(id, uuid, name, site, api_key_hash, last_seen, software_version)`.
Toutes les tables portent un **UUID** généré sur la machine → pas de collision ni de doublon à
la synchronisation (upsert par UUID). Les images sont envoyées vers un stockage objet (S3/MinIO).

---

## 8. Interfaces

### 8.1 Opérateur (tactile, 7")

1. **Accueil** : état machine (Prête / Simulation), bouton « Nouvelle gradation ».
2. **Client** : recherche ou création rapide.
3. **Carte** : type (Pokémon, Magic, Yu‑Gi‑Oh!, Sport…), nom, série, année, numéro.
4. **Capture** : grand bandeau d'instruction (« Placez la carte RECTO vers le haut »,
   « RETOURNEZ la carte »), aperçu de l'image capturée, bouton CAPTURER plein écran.
5. **Résultats** : centrage mesuré (ratios), saisie coins/bordures/surface, note finale, confiance.
6. **Historique** des analyses.

Règles UX : boutons ≥ 48 px, une action principale par écran, couleurs d'état, aucun texte
petit, messages d'erreur actionnables.

### 8.2 Super‑administrateur (V1.x, navigateur sur réseau local, authentifié)

Configuration, calibration guidée, profils de cartes, règles de gradation (édition + version),
modèles IA, utilisateurs/rôles, sauvegardes, journaux, activation du mode simulation, mises à jour.

---

## 9. Feuille de route

| Phase | Contenu | Durée indicative (1 dev temps partiel) |
|---|---|---|
| **0 — Fondations** ✅ commencé | Dépôt, architecture, simulation, base locale, machine à états, centrage, règles, UI minimale | 2–3 sem. |
| **1 — Banc optique** | Montage caméra + LED + plateau ; capture `picamera2` réelle ; calibration ChArUco (px/mm, distorsion) ; contrôle netteté/exposition | 3–5 sem. |
| **2 — V1 utilisable** | Comptes/roles, journal d'audit, rapport PDF + QR, historique, calibration enregistrée, multi-éclairage (diffus + 4 rasants), sauvegarde | 4–6 sem. |
| **3 — Jeu de données** | 300–1 000 cartes analysées et notées par un expert, outil d'annotation des défauts | continu (2–4 mois) |
| **4 — IA défauts (V2)** | Modèles coins/bordures/surface, registre de modèles, confiance | 2–3 mois |
| **5 — Mécanique (V2)** | Retournement motorisé, capteur présence, TMC2209, sécurité | 2–3 mois |
| **6 — Central (V2)** | PostgreSQL, synchronisation, plateforme admin multi‑machines | 1–2 mois |
| **7 — V3/V4** | Industrialisation, boîtiers, portail client, vérification publique | — |

---

## 10. Risques techniques et points à valider en priorité

| Risque | Impact | Mitigation / test prioritaire |
|---|---|---|
| **Résolution insuffisante** pour les micro-défauts. IMX477 = 4056×3040 ; sur une carte 63×88 mm cadrée à ~95 mm de haut → ~32 px/mm (~31 µm/px). Le blanchiment de coin est visible, les micro-rayures fines sont limites. | Élevé | **Test n°1** : photographier 10 cartes connues, vérifier visuellement les défauts. Option : captures zoomées des coins (2e position caméra ou objectif macro en V2). |
| **Reflets** sur cartes holographiques/brillantes | Élevé | Polariseurs croisés (filtre sur objectif + film polarisant sur LED), test A/B |
| **Répétabilité** du positionnement manuel | Moyen | Butées + redressement logiciel ; test : 20 captures de la même carte, écart-type du centrage < 0,5 % |
| **Centrage des cartes sans bordure** (full art) | Moyen | Profil « sans bordure » → centrage non mesurable automatiquement → saisie opérateur + confiance faible |
| **Manque de données étiquetées** | Très élevé | Commencer la collecte dès la V1 ; partenariat avec un expert/grader |
| **Fiabilité des notes vs experts** | Très élevé | Lot de référence (≥ 100 cartes déjà gradées PSA/BGS) ; mesurer l'accord (±1 note) |
| **Performance Pi 5** pour l'IA | Moyen | ONNX + modèles légers ; budget ≤ 10 s par carte ; option Hailo-8L (AI HAT+) |
| **Dommages aux cartes** (V2 mécanique) | Critique | Aucune pince sur la surface imprimée ; tests destructifs sur cartes sans valeur |
| **Juridique** : comparaisons avec PSA/BGS, responsabilité sur la valeur | Moyen | Mentions claires, CGV, ne pas utiliser leurs marques |

**Les 3 premiers tests à faire sur le matériel réel** : (1) résolution utile en px/mm et
visibilité des défauts, (2) répétabilité du centrage sur 20 captures, (3) gestion des reflets.

---

## 11. Coûts et difficultés par version

| Version | Matériel (par machine) | Effort logiciel | Difficulté principale |
|---|---|---|---|
| V1 prototype | 800–1 100 $ | 3–4 mois | Optique/éclairage, calibration |
| V2 semi-auto | 1 500–2 500 $ | 6–9 mois | Données + IA, mécanique sûre |
| V3 pro | 2 500–5 000 $ (petite série) | 9–12 mois | Industrialisation, certification électrique (CSA/UL), QA |
| V4 écosystème | serveur + SaaS | continu | Commercial, sécurité, support |

Coûts annexes à prévoir : cartes de référence déjà gradées (1 000–5 000 $), expert(s) pour
l'étiquetage, impression 3D/usinage, ColorChecker, GPU (location cloud ou carte graphique) pour
l'entraînement.

---

## 12. Critères pour déclarer la V1 fonctionnelle

La V1 est « fonctionnelle » quand **tous** ces critères sont vérifiés par des tests documentés :

1. Une analyse complète (client → carte → recto → retournement guidé → verso → note → rapport)
   se fait sur la machine réelle en **< 3 minutes**, sans ordinateur externe.
2. Le logiciel **refuse** : carte absente, image floue, verso identique au recto, analyse sans
   les deux faces.
3. **Répétabilité du centrage** : 20 captures de la même carte (repositionnée à chaque fois) →
   écart-type < 0,5 point de pourcentage par axe.
4. **Exactitude du centrage** : sur 20 cartes mesurées au pied à coulisse/loupe graduée, écart
   moyen < 1 point de pourcentage.
5. La note finale est calculée par des règles versionnées, et chaque analyse conserve version
   des règles, modèle, calibration et mode (réel/simulé).
6. Les analyses survivent à une coupure de courant (base SQLite + images) et sont exportables.
7. Rapport PDF généré avec photos recto/verso, sous-notes et mesures.
8. Le mode simulation exécute le même flux, et les analyses simulées sont marquées comme telles.
9. Tests automatisés au vert (`pytest`).

---

## 13. Recommandations

1. **Investir d'abord dans l'optique et l'éclairage**, pas dans l'IA. Une bonne image facilite
   tout le reste ; une mauvaise image rend tout impossible.
2. **Fond noir mat + butées** sur le plateau : la détection de la carte devient triviale et
   robuste.
3. **Polariseurs croisés** dès la V1 pour les cartes brillantes.
4. **Collecter des données dès la première carte** : la V1 est aussi un outil de collecte.
5. **Lot de référence** de cartes déjà gradées (PSA/BGS/CGC) pour mesurer l'accord.
6. Garder l'**inférence IA hors du chemin critique** : la machine doit pouvoir noter en mode
   assisté même sans modèle.
7. Ne pas commencer la mécanique motorisée avant d'avoir validé la chaîne optique.

---

## 14. Décisions d'OpGrade (2026-10-24)

| # | Sujet | Décision | Conséquence technique |
|---|---|---|---|
| 1 | Clients | Particuliers au comptoir **et** boutiques/revendeurs **et** envois postaux | Ajouter une notion de **commande/lot** (plusieurs cartes, statut réception → gradée → retournée, n° de suivi) |
| 2 | Échelle | **Entiers 1–10** pour la note finale | Déjà le cas ; sous-notes en demi-points conservées en interne |
| 3 | Formats V1 | Pokémon/Magic 63×88, sport 63,5×88,9, Yu‑Gi‑Oh! 59×86, **cartes épaisses** | Plateau à gabarits interchangeables ; profils avec épaisseur ; cartes épaisses = mise au point/hauteur à valider (risque) |
| 4 | Protection | Analyse **hors protection** uniquement | Instruction opérateur « retirer la carte de sa protection » |
| 5 | Données de référence | Pas d'expert ; **cartes déjà gradées** (PSA/BGS/CGC) | Lot de référence = vérité terrain ; saisir la note officielle + n° de certification ; l'annotation fine des défauts reste à faire en interne |
| 6–7 | Matériel acheté | Raspberry Pi 5, **Arducam IMX477**, objectif **CS 6 mm (1/2,3")**, **écran tactile 7"** | Voir note objectif ci-dessous |
| 8 | Identification | **Reconnaissance automatique tôt** | ✅ Module `identify/` : recherche par nom/numéro/série dans TCGdex (Pokémon, FR+EN), Scryfall (Magic), YGOPRODeck (Yu‑Gi‑Oh!). La Pokémon TCG API, testée le 2026-10-24, échouait sur ~50 % des requêtes : elle reste disponible en alternative. Étape suivante : reconnaissance par l'image capturée. Sport : pas de base ouverte équivalente → saisie manuelle |
| 9 | Langue | **Français + anglais** | Interface et rapports bilingues (fichiers de traduction) |
| 10 | Loi 25 | **Minimum + consentement** | Client = nom + 1 moyen de contact ; date de consentement enregistrée ; suppression/anonymisation sur demande |
| 11 | Licence | **Propriétaire**, dépôt privé | Déjà indiqué dans le README |

### Note sur l'objectif 6 mm

Avec le capteur IMX477 (6,29 × 4,71 mm), un objectif 6 mm doit être à **~10 cm** de la carte
pour la cadrer (~100 mm de champ). C'est un grand-angle : distorsion en barillet marquée,
perspective plus forte sur les bords, peu de place pour l'éclairage rasant, et la distance de
mise au point minimale de certains 6 mm est justement autour de 10–20 cm (à vérifier).

**Recommandation** : l'utiliser pour les premiers essais (la calibration corrige la distorsion),
mais prévoir un objectif **12 mm ou 16 mm** (~19 ou ~25 cm de distance), plus adapté à la
mesure. Résolution attendue dans tous les cas : ~40 px/mm si la carte remplit le champ.
