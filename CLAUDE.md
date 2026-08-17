# video-stab

Chaîne de traitement des rushes FPV : surveillance d'un dossier réseau → fusion
des enregistrements découpés → derush dans une interface web → stabilisation
Gyroflow. Une image Docker, deux services (`api`, `worker`), déployable sur
Portainer.

**Langues** : le code, les commentaires, les docstrings, les messages de log et
tout le texte de l'interface sont **en anglais**. Ce fichier, le README et les
réponses en conversation restent **en français**.

## Les quatre faits à ne pas redécouvrir

1. **ffmpeg ne peut ni fusionner ni couper un rush DJI sans détruire le gyro.**
   Le flux `djmd` a un codec `none`, refusé par mp4 (`Could not find tag for
   codec none`) comme par mkv (`Only audio, video and subtitles are supported`).
   → La fusion passe par **`mp4_merge`** (le même outil que Gyroflow utilise en
   interne), qui réécrit le `stbl` et conserve toutes les pistes. 4.4 s pour 4 Go.
   → Le **derush reste de la métadonnée** : on ne coupe jamais un master, on
   passe les bornes à Gyroflow via `trim_ranges_ms`.

2. **On fusionne AVANT de stabiliser, jamais l'inverse.** Le lissage et
   l'adaptive zoom sont calculés sur toute la courbe gyro ; stabiliser deux parts
   séparément produirait une couture visible à la jonction.

3. **`--preset` de Gyroflow accepte un JSON partiel de projet et porte
   `trim_ranges_ms`.** Pas besoin de générer puis patcher un `.gyroflow` : un
   template + les bornes du cut, en une commande. Régler `output_width`/
   `output_height` suffit à changer de format (un 1080x1920 demandé sur une
   source 3840x2880 fait dériver un crop 1620x2880 tout seul).

4. **Le warping de Gyroflow passe par OpenCL, pas Vulkan.** L'image embarque
   rusticl/Mesa (GPU si `/dev/dri` est mappé) et pocl (repli CPU).

## Perfs mesurées (Ryzen AI 9 HX 370 / Radeon 890M, source 3840x2880 HEVC 10-bit 60p)

| étape | avec GPU | CPU pur |
|---|---|---|
| fusion `mp4_merge` | 4.4 s pour 4 Go (I/O pur) | idem |
| proxy H.264 1280x960 | 0.79–0.92x temps réel | 0.53x |
| rendu Gyroflow 1080p | **23 img/s** (OpenCL rusticl) | **~8.7 img/s** |

Le décodage VAAPI marche, mais **`scale_vaapi` et `h264_vaapi` bloquent ou
segfaultent** sur iGPU AMD + Mesa : on se sert du GPU pour décoder uniquement,
jamais pour scaler ou encoder. Idem côté Gyroflow, `use_gpu: false` en sortie
(l'encodage GPU y passe par AMF/NVENC, souvent absent → images corrompues).

### Le gel du GPU n'était pas VAAPI, c'était le SIGKILL

Diagnostiqué le 2026-08-14 dans le journal du boot fautif. La machine de dev
n'utilise pas l'amdgpu du noyau mais **celui d'AMD en DKMS** (pile ROCm, d'où les
modules renommés `amdttm`, `amdkcl`, `amd_sched`). Signature du blocage :

```
[drm:amddrm_sched_entity_push_job [amd_sched]] *ERROR* Trying to push to a killed entity
... 122 s plus tard ...
INFO: task kworker/u100:2 blocked   Workqueue: ttm ttm_bo_delayed_delete [amdttm]
  dma_fence_default_wait            ← attente d'un fence jamais signalé
```

Un ffmpeg **tué au SIGKILL avec des jobs VAAPI en vol** laisse un fence orphelin ;
tous les kworkers TTM s'empilent derrière, le GPU devient inutilisable et le
conteneur impossible à supprimer. Le déclencheur était de notre côté :
`docker compose down` → SIGTERM au worker, qui laissait son job continuer → délai
de grâce Docker de **10 s** → SIGKILL. Un proxy dure des minutes.

Corrigé : `procs.terminate_all()` tient un registre des enfants et le handler
SIGTERM du worker leur transmet le signal, avec `stop_grace_period: 60s` côté
compose. Mesuré ensuite : 600 frames en 18,0 s en VAAPI contre 28,2 s en CPU
(1,57x), et un SIGTERM en pleine passe VAAPI ne laisse **rien** dans le journal
noyau. Risque résiduel : un crash de ffmpeg emprunte le même chemin de teardown.

**Pourquoi Gyroflow n'a jamais planté** : il ne touche pas au bloc vidéo. Le
warping passe par les rings de calcul via OpenCL, et l'encodage est en CPU
(`use_gpu: false`). Or les fences de compute sont précisément ce que la pile DKMS
d'AMD est faite pour valider ; le décodage vidéo en est le coin négligé.

## Visualisation du gyro : passer par Gyroflow, pas par le fichier

`gyroflow <src> --export-metadata 2:out.json` est la seule entrée praticable
(`djmd` est propriétaire, c'est le telemetry-parser de Gyroflow qui sait le lire).

- le **type 3** (« camera data », celui qui porte les angles d'Euler et les séries
  stabilisées) **panique** : il exige un contexte de stabilisation. Seul le type 2
  marche, et `--export-metadata-fields` ne s'y applique pas.
- **ce que Gyroflow trace lui-même, vérifié dans son source** (`src/ui/components/
  TimelineGyroChart.rs`) : quatre modes, gyro X/Y/Z (défaut), accéléromètre,
  magnétomètre, quaternions x/y/z/w + quaternions lissés. Les trois premiers lisent
  `raw_imu`, et `gyro_source/mod.rs::raw_imu()` **n'en dérive rien depuis les
  quaternions** — donc sur un fichier O4 son mode par défaut est vide, et le mode
  quaternions est le seul qu'il puisse afficher. Couleurs des axes brutes :
  `#8f4c4c` / `#4c8f4d` / `#4c7c8f` / `#8f4c8f`, identiques en thème clair et
  sombre. Il normalise l'échelle sur **tout le fichier** (`normalize_height`), pas
  sur la fenêtre visible.
- le DJI O4 **ne fournit pas d'IMU brut** : `raw_imu` revient vide et la charge
  utile est faite de **quaternions d'orientation à ~2000 Hz** (477 083 pour 4 min,
  61 Mo de JSON en 3,5 s). Le signal gyroscopique se reconstruit en dérivant :
  rotation relative entre deux échantillons ÷ dt = vitesse angulaire.
- l'ordre des composantes suit nalgebra, `[x, y, z, w]`.
- **les axes sont identifiés, sur O4P** (2026-08-14) : `x` = tangage, `y` = roulis,
  `z` = lacet. Trois mesures concordantes. La gravité exprimée dans le repère du
  drone tranche le lacet sans ambiguïté : tourner autour de `z` n'incline la gravité
  que pour 0,144 de la rotation, contre 0,999 pour `x` et 0,994 pour `y` — seul
  l'axe vertical se comporte ainsi. Ensuite l'image : au pic de 534 °/s sur `x`,
  l'horizon **descend sans pivoter**, c'est un flip donc du tangage ; sur une plage
  soutenue de `y` il **pivote**, donc du roulis. `y` est aussi l'axe le plus souvent
  isolé (878 échantillons contre 213), ce à quoi ressemble un vol FPV.
  `imu_orientation` est `None` dans l'export : la métadonnée ne donne rien, il fallait
  mesurer. Les noms ne sont revendiqués que pour les sources listées dans
  `AXIS_NAMES` ; ailleurs on garde X/Y/Z, une autre caméra ayant son propre repère.
- la **vue quaternions ne se renomme pas** : x/y/z/w sont les composantes d'une
  rotation, pas des angles.
- **les derniers échantillons du fichier sont du rebut** : mesuré, 6 échantillons
  à 58 000 °/s (160 tours par seconde) sur la toute dernière milliseconde. Écrêter
  ne suffit pas — à 2000 °/s ils fixent encore l'échelle du graphe. On les écarte
  au-delà de la pleine échelle du capteur, en publiant le compte (`dropped`).
- ce qui part au navigateur porte **deux vues** sur la même télémétrie, 340 Ko pour
  4 min : la vitesse angulaire en **enveloppe min/max par bucket** (6000 buckets —
  la décimation effacerait justement les pics qu'on cherche) et les composantes du
  quaternion en **ligne simple**, l'orientation étant lisse à 2 kHz (mesuré : saut
  maximal de 0,0066 entre buckets voisins, et **aucun basculement de signe** dans
  les données DJI, donc rien à recoller). Produit pendant l'étape proxy, ou à la
  demande au premier appel ; `CHART_FORMAT` force la reconstruction des graphes
  déjà sur le disque quand la forme du JSON change.

## Étalonnage : un second encodage, et un piège de filtre

Gyroflow ne fait **rien** en couleur (ses params : `fov_scale`,
`lens_correction_amount`, `background_mode`, `adaptive_zoom_*` — aucune LUT). Donc
l'étalonnage ne peut pas être embarqué dans la passe de stabilisation.

Mesuré sur un clip réel de 10 s en 1080p60 :

| sortie | vitesse |
|---|---|
| HEVC 10-bit `medium` | 0.17x temps réel |
| HEVC 10-bit `superfast` | 0.26x |
| **H.264 8-bit `veryfast`** | **0.71x** — le choix retenu |
| une image filtrée en JPEG | **0.32 s** — d'où l'aperçu live |

L'aperçu est une vraie image ffmpeg, pas une réimplémentation en shader : ce qu'on
voit traverse exactement les filtres du rendu final, aucune parité à maintenir.

**`colorlevels` est un piège.** Il accepte le YUV autant que le RGB, et sur une
image YUV ses points « rouge/vert/bleu » tombent sur **Y/U/V** : décaler le point
noir de la chroma, dont le neutre est au milieu de la plage et non à zéro, rend
l'image entièrement noire. Pire, ça ne se produit qu'**une fois sur deux** — avec
un autre filtre RGB dans la chaîne, ffmpeg insère une conversion et les mêmes
paramètres se comportent normalement. L'étirement de niveaux passe donc par
`lutyuv=y='...'`, qui ne touche que la luma (ce qu'on veut : pas de balance des
blancs inventée sur une image moitié ciel, moitié herbe sèche).

L'auto-niveaux ne pousse jamais un côté déjà écrêté : mesuré, remonter le point
blanc d'un plan dont le ciel touche le plafond le brûle complètement.

## Biais connu : Gyroflow rend +3 frames

Mesuré : une plage de 600 frames exactes (`trim_ranges_ms` de 100100 à 110110)
ressort en **603 frames**, une de 300 en 303. Gyroflow planifie le bon compte
(`Rendering progress: 599/599`) puis l'encodeur en écrit deux ou trois de plus.
50 ms, et **dans le bon sens** : on ne perd jamais l'instant marqué. Volontairement
non compensé — un correctif en dur se casserait au prochain changement de version
ou de fps.

## Détection des splits

La détection intégrée à Gyroflow ne marche pas sur les noms O3/O4 : son motif
`/(DJI_\d+_(\d+)\.MP4)$/` visait les DJI Action et échoue sur le suffixe `_D`.
La nôtre (`services/grouping.py`) :

- index caméra consécutifs **et** `start(n+1) - (start(n) + durée(n)) < 2 s`
  (mesuré : 0.36 s d'écart sur une vraie paire)
- **le timestamp du nom est en UTC**, le `mtime` est l'heure *locale* de fin
  d'écriture → deux signaux indépendants
- une taille de part ≈ 3.763 Go est un indice de découpe, jamais une preuve

## Conventions

- Les marks de derush sont stockés en **numéros de frame**, jamais en ms : à
  60000/1001 fps l'arrondi dérive de plusieurs images sur un rush de 4 minutes.
  Conversion au dernier moment, dans `framing.py`.
- Les valeurs techniques d'une séquence (durée, fps, frame_count) sont celles
  **mesurées sur le fichier fusionné**, pas la somme des estimations des parts.
- SQLite perd les fuseaux : normaliser avec `timeutil.as_utc()` avant toute
  comparaison de dates relues (cf. `BaseSchema` côté API).
- Un job planté ne doit jamais tuer le worker : `process_next_job` isole.
- Front : shadcn/ui de base uniquement, composants copiés dans
  `frontend/src/components/ui/`.

## Développement

```bash
# backend
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cd backend && VS_DATA_DIR=../data ../.venv/bin/uvicorn app.main:app --port 8000
VS_DATA_DIR=../data ../.venv/bin/python -m app.worker

# front (proxy /api vers le port 8000)
cd frontend && npm install && npm run dev
```

Pour tester en local il faut `mp4_merge` dans le PATH (ou `VS_MP4_MERGE_BIN`) :
binaire prêt sur les releases de `gyroflow/mp4-merge`.

`docker/Dockerfile.spike` est l'image jetable qui a servi à valider Gyroflow
headless + OpenCL en conteneur. À garder pour rejouer la validation sur une
nouvelle machine cible.
