# CLAUDE.md — OpGrade X (opgrade-machine)

Logiciel embarqué de la machine **OpGrade X** : gradation de cartes de collection (Pokémon,
sport, Magic, Yu‑Gi‑Oh!…) sur Raspberry Pi 5 avec caméra HQ. Note de 1 à 10, explicable.

- Spécification complète : [chatgpt.md](chatgpt.md) (vision produit) et [Start.md](Start.md).
- Analyse, architecture, feuille de route, risques : [docs/ANALYSE.md](docs/ANALYSE.md).

## Langue

- Code, identifiants, noms de fichiers : **anglais**.
- Interface utilisateur, messages à l'opérateur, documentation : **français**.

## Stack

Python 3.11 (version de Raspberry Pi OS Bookworm) · FastAPI · SQLAlchemy 2 + SQLite (local) ·
OpenCV/NumPy · interface web vanilla HTML/JS (Chromium kiosque) · pytest.
Sur le Pi : `picamera2` vient d'apt (`python3-picamera2`), pas de pip.

## Commandes

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest            # Windows ; .venv/bin/python sur Linux/Pi
.venv/Scripts/python -m opgrade           # serveur sur http://127.0.0.1:8000 (simulation par défaut)
.venv/Scripts/python -m opgrade.auth create-admin --username patron   # 1er admin (ou via l'écran)
.venv/Scripts/python -m opgrade.identify.build_index tcgdex   # index visuel (aussi scryfall, ygoprodeck)
.venv/Scripts/python scripts/eval_visual.py --catalog tcgdex  # précision de la reconnaissance (réseau)
.venv/Scripts/python scripts/eval_visual.py --catalog tcgdex --index-size 30000 --ocr --ambiguous-only  # OCR sur réimpressions
```

Variables d'environnement (préfixe `OPGRADE_`) : `OPGRADE_SIMULATION` (1/0),
`OPGRADE_DATA_DIR`, `OPGRADE_MACHINE_ID`, `OPGRADE_HOST`, `OPGRADE_PORT`.

## Architecture (src/opgrade)

| Module | Rôle |
|---|---|
| `hardware/` | HAL : interfaces `Camera`, `Lighting` (`base.py`) ; `sim.py` (simulateur) ; `pi.py` (picamera2/GPIO) |
| `orchestration/` | Machine à états d'une session de gradation (`state_machine.py`) et service qui l'exécute (`service.py`) |
| `vision/` | Détection/redressement de la carte, contrôle qualité image, centrage, détection de retournement |
| `grading/` | Règles versionnées (`src/opgrade/rules/grading_rules.yaml`) et moteur de calcul de la note |
| `identify/` | Identification : recherche texte en ligne (`catalogs.py` : TCGdex, Scryfall, YGOPRODeck, Pokémon TCG API) et **reconnaissance par image** hors ligne (`visual.py` : empreintes pHash + index `data/card_index.db` ; `build_index.py` le construit depuis `sources.py`), **numéro imprimé lu par OCR** (`ocr.py`, RapidOCR) pour départager les réimpressions ; `recognize.py` enchaîne les deux. Catalogue choisi par `CardType.catalog`. Les cartes Pokémon TCG Pocket (numériques) sont exclues |
| `auth/` | Comptes et rôles (operator, admin) : scrypt, sessions, verrouillage, journal `audit_events` ; CLI `python -m opgrade.auth` |
| `customers/` | Commandes/lots (`orders.py` : statuts reçue → en cours → gradée → retournée) et Loi 25 (`privacy.py` : consentement versionné, export, anonymisation, conservation ; avis dans `rules/privacy.yaml`) |
| `data/` | Modèles SQLAlchemy, session DB, données de base (types de cartes), mini-migration (ajout de colonnes) |
| `api/` | Application FastAPI et routes |
| `web/` | Interface tactile (statique) |

## Décisions produit (détail : docs/ANALYSE.md §14)

- Note finale en **entiers 1–10**. Cartes analysées **hors protection** uniquement.
- Formats V1 : 63×88, 63,5×88,9, 59×86 et cartes épaisses.
- Clients : comptoir, boutiques et envois postaux → prévoir commandes/lots.
- Identification automatique tôt (TCGdex, Scryfall, YGOPRODeck) ; sport en saisie manuelle.
  Elle n'est jamais bloquante : catalogue hors ligne ou en erreur → saisie manuelle.
- Interface et rapports **FR + EN**. Loi 25 : données client minimales + consentement.
- Matériel : Pi 5, Arducam IMX477, objectif CS 6 mm, écran tactile 7".
- Référence de qualité : cartes déjà gradées PSA/BGS/CGC (pas d'expert interne).

## Règles du projet (non négociables)

1. **Le métier ne dépend jamais du matériel** : orchestration/grading n'importent que
   `hardware.base`. Toute nouvelle pièce matérielle = une interface + une implémentation sim + une
   implémentation réelle.
2. **La simulation suit exactement le même flux** que la machine réelle ; elle ne saute aucune
   étape. Toute analyse simulée a `simulated = True`.
3. **Aucune analyse sans les deux faces.** Le verso est refusé s'il ressemble au recto.
4. **Traçabilité** : chaque session enregistre `rules_version`, `model_version`, `machine_id`,
   `simulated`. Ne jamais modifier une règle de gradation sans incrémenter sa `version`.
5. **Pas de note « inventée »** : si un critère n'est pas mesuré automatiquement, il est saisi
   par l'opérateur (`source = "operator"`) et la session est marquée `needs_review`.
6. Une fonctionnalité n'est « faite » que si elle est **testée** (pytest) — et validée sur le
   matériel réel quand elle touche la capture.
7. Ne jamais sacrifier la sécurité des cartes à la vitesse (mécanique V2).
8. Tables : clé `id` entière + `uuid` (pour la future synchro PostgreSQL sans doublon).
9. **Accès** : toute route `/api` exige une connexion (dépendance globale `auth_gate`) sauf
    `PUBLIC_PATHS` (`api/auth.py`). Une route sensible (données client en masse, Loi 25,
    configuration, comptes) ajoute `Depends(require_admin)` et écrit dans `audit_events`.
    L'auteur d'une action est toujours `current_user(request)`, jamais une valeur envoyée.
10. **Loi 25** : ne collecter aucun nouveau renseignement personnel sans l'ajouter à l'avis
   (`rules/privacy.yaml`, nouvelle `version`), à l'export (`export_client`) et à l'anonymisation
   (`anonymize_client`). Aucun renseignement personnel dans `consent_records` ni dans les logs.

## État actuel (V1 — phase 0)

Fait : simulation caméra/éclairage, base SQLite, clients/types/cartes, machine à états avec
retournement manuel guidé, contrôle qualité image, mesure du centrage, moteur de notation,
identification par nom/numéro/série dans les catalogues en ligne, reconnaissance de la carte par
la caméra (index visuel local), capture « silhouette » sur plateau rétroéclairé, API + UI, tests.

OCR : ne tourne que si plusieurs éditions sont visuellement proches (ou pour Yu‑Gi‑Oh!), car il
coûte ~1,5 s sur PC (plus sur le Pi). Une confirmation par numéro ne doit jamais être « devinée » :
`number_matches` exige une correspondance exacte du numéro normalisé (ex. « 4/102 »).

Acquisition (`GradingService.acquire`) : si `Hardware.has_backlight`, photo en rétroéclairage pour
localiser la carte, puis photo éclairée pour l'analyse. Ne pas détecter sur fond noir seul quand un
rétroéclairage existe (les bordures noires disparaissent).

Les tests de `identify/` simulent HTTP (`httpx.MockTransport`) avec des réponses calquées sur les
vraies API ; revérifier en direct quand on touche un adaptateur.

À faire ensuite (voir docs/ANALYSE.md §9) : capture réelle picamera2 + calibration ChArUco,
utilisateurs/rôles + journal d'audit, rapport PDF + QR, multi-éclairage, administration.
