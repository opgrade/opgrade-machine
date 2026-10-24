# OpGrade X — opgrade-machine

Logiciel embarqué de la machine de gradation de cartes de collection **OpGrade X**
(Raspberry Pi 5, caméra HQ IMX477, éclairage contrôlé).

- Analyse complète, architecture, feuille de route, risques et questions : [docs/ANALYSE.md](docs/ANALYSE.md)
- Règles de développement : [CLAUDE.md](CLAUDE.md)

## Démarrage rapide (PC de développement, mode simulation)

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv -e ".[dev]"
.venv/Scripts/python -m pytest       # Linux/Pi : .venv/bin/python
.venv/Scripts/python -m opgrade      # puis ouvrir http://127.0.0.1:8000
```

Au premier lancement, l'écran propose de créer le compte **administrateur** (uniquement depuis la
machine elle-même). Sur un Pi sans écran : `python -m opgrade.auth create-admin --username patron`.

En simulation, un panneau « Simulation » permet de retourner la carte virtuelle ou d'en placer
une nouvelle avec un centrage aléatoire. Les analyses simulées sont marquées comme telles.

## Flux V1

1. Choisir/créer le client → 2. Saisir la carte (type, nom, série, année) →
3. Capturer le recto → **la machine demande de retourner la carte** → capturer le verso
(refusé si la carte n'a pas été retournée) → 4. Analyse automatique du centrage →
5. L'opérateur saisit coins / bordures / surface → note finale 1–10 expliquée.

## Sur le Raspberry Pi (non encore validé sur matériel)

```bash
sudo apt install python3-picamera2 python3-gpiozero
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
OPGRADE_SIMULATION=0 OPGRADE_HOST=0.0.0.0 .venv/bin/python -m opgrade
```

Code propriétaire — © OpGrade. Tous droits réservés.
