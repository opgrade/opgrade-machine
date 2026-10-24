# Mise en service sur Raspberry Pi 5

> Statut : procédure **préparée et testée en simulation**, pas encore exécutée sur le matériel.
> Chaque étape a une vérification ; noter les écarts pour corriger ce document.

Matériel visé : Raspberry Pi 5, caméra **Arducam IMX477** (12,3 MP, 4056 × 3040), objectif
**CS 6 mm 1/2,3"**, écran tactile 7", éclairages LED via MOSFET (voir docs/ANALYSE.md §3).

## 1. Système

1. Raspberry Pi OS **Bookworm 64 bits** (Raspberry Pi Imager), utilisateur graphique `pi`.
2. Mises à jour et paquets :

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-picamera2 python3-gpiozero python3-lgpio python3-venv git curl chromium-browser
```

## 2. Caméra

1. Pi **éteint** : brancher la caméra sur le port CAM0/CAM1 avec un câble **22 → 15 broches**
   (le Pi 5 a des connecteurs étroits), contacts du bon côté.
2. Vérifier la détection :

```bash
rpicam-hello --list-cameras
```

   Attendu : `imx477 [4056x3040]`. Si rien n'apparaît avec le module Arducam, ajouter dans
   `/boot/firmware/config.txt` : `camera_auto_detect=0` puis `dtoverlay=imx477` (ou
   `dtoverlay=imx477,cam0` selon le port), et redémarrer.
3. Photo de test : `rpicam-still -o test.jpg` puis l'ouvrir.

**Distance** : avec l'objectif 6 mm, la carte remplit le champ à **~10 cm** (grand-angle, forte
distorsion corrigée par la calibration). Vérifier que l'objectif fait la mise au point à cette
distance ; sinon, bague d'extension (C/CS) ou objectif 12/16 mm (recommandé, voir ANALYSE §14).

## 3. Logiciel

```bash
sudo useradd --system --home /var/lib/opgrade --groups video,gpio opgrade
sudo mkdir -p /opt/opgrade /var/lib/opgrade && sudo chown opgrade:opgrade /var/lib/opgrade
sudo git clone https://github.com/opgrade/opgrade-machine.git /opt/opgrade
cd /opt/opgrade
# --system-site-packages : picamera2 et gpiozero viennent d'apt.
sudo python3 -m venv --system-site-packages .venv
sudo .venv/bin/pip install -e .
sudo cp deploy/opgrade.env.example /etc/opgrade.env && sudo chmod 640 /etc/opgrade.env && sudo chgrp opgrade /etc/opgrade.env
```

> **À surveiller** : `pip` peut installer un NumPy 2 dans le venv alors que `picamera2` (apt) est
> compilé pour le NumPy 1.x du système. Si `import picamera2` échoue avec une erreur de NumPy :
> `sudo .venv/bin/pip install "numpy<2"` (le code d'OpGrade fonctionne avec NumPy 1.x).

## 4. Vérification du matériel

```bash
sudo -u opgrade env $(cat /etc/opgrade.env | grep -v '^#' | xargs) /opt/opgrade/.venv/bin/python -m opgrade.hardware.check
```

- chaque éclairage doit s'allumer à tour de rôle (regarder la machine) ;
- `diffuse` : luminosité moyenne 80–200, `clipped_%` < 1 ; sinon ajuster
  `OPGRADE_CAMERA_EXPOSURE_US` / `OPGRADE_CAMERA_GAIN` dans `/etc/opgrade.env` ;
- photos dans `/var/lib/opgrade/hardware_check/` : **vérifier que les couleurs ne sont pas
  inversées** (rouge/bleu) sur `diffuse.jpg`.

## 5. Service et écran

```bash
sudo cp deploy/opgrade.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now opgrade
systemctl status opgrade          # doit être « active (running) »
mkdir -p ~/.config/labwc && cp /opt/opgrade/deploy/kiosk-autostart ~/.config/labwc/autostart
```

Au redémarrage, l'écran affiche **Configuration initiale** : créer le compte administrateur.
(Sans écran : `sudo -u opgrade … python -m opgrade.auth create-admin --username patron`.)

## 6. Calibration (Administration → Calibration)

1. **Mise au point** : poser la mire, bouton « Mesurer la netteté » ; tourner la bague de
   l'objectif jusqu'au maximum, puis la bloquer (vis de blocage).
2. **Mire** : télécharger le PDF, imprimer à **100 %**, mesurer la barre de 50 mm et une case au
   pied à coulisse, coller sur un support rigide et plat.
3. **Capture à plat** : mire posée à plat à l'emplacement de la carte → échelle (px/mm) et
   contrôle d'inclinaison (< 1 % de variation, sinon caler la caméra).
4. **14 captures ou plus** : mire déplacée dans tout le champ (coins compris) et inclinée de
   10 à 20° (cale) dans des directions variées.
5. **Calculer** : erreur de reprojection visée < 0,5 px.
6. **Balance des blancs** : poser une carte blanche ou grise 18 % sur tout le plateau, éclairage
   diffus → gains de couleur et uniformité de l'éclairage (≥ 85 %).
7. **Activer** la calibration : chaque gradation enregistre ensuite l'identifiant de la
   calibration utilisée.

Refaire la calibration après tout démontage de la caméra, changement d'objectif, de mise au point
ou d'éclairage.

## 7. Premiers tests à faire (ANALYSE §10)

1. Résolution utile : px/mm affiché après calibration ; défauts visibles sur 10 cartes connues.
2. Répétabilité : même carte repositionnée 20 fois → écart-type du centrage < 0,5 point.
3. Reflets : cartes holographiques, avec et sans polariseurs croisés.
4. Temps : durée capture → note, et durée de la lecture du numéro (OCR) sur le Pi.
