# video-stab

Chaîne de traitement des rushes FPV : surveillance d'un dossier réseau → fusion
des enregistrements découpés → derush dans une interface web → stabilisation
Gyroflow. Deux images issues d'un seul Dockerfile (`--target api`, `--target
worker`), déployables sur Portainer.

**Langues** : le code, les commentaires, les docstrings, les messages de log et
tout le texte de l'interface sont **en anglais**. Ce fichier, le README et les
réponses en conversation restent **en français**.

**Interface** : elle doit être claire au point de ne pas avoir besoin de texte. Une
carte porte un titre et des données, pas un paragraphe qui explique l'implémentation.
Ce qui mérite une explication est soit une infobulle, soit un dialogue destructif (le
seul endroit où la prose est due, parce que deux options destructives diffèrent d'une
façon que les boutons ne portent pas). Balayé le 2026-08-20 : neuf paragraphes de
`CardDescription`, la légende clavier permanente du derush et deux lignes d'indice
retirés, parce qu'ils décrivaient le fonctionnement à quelqu'un qui le connaît déjà.

**Aucun tiret cadratin, jamais**, ni dans le code, ni dans les commentaires, ni dans la
doc, ni dans l'interface. Point, virgule, deux-points ou parenthèses. Un `-` simple pour
la valeur absente dans un tableau.

## Les quatre faits à ne pas redécouvrir

1. **ffmpeg ne peut ni fusionner ni couper un rush DJI sans détruire le gyro.**
   Le flux `djmd` a un codec `none`, refusé par mp4 (`Could not find tag for
   codec none`) comme par mkv (`Only audio, video and subtitles are supported`).
   → La fusion passe par **`mp4_merge`** (le même outil que Gyroflow utilise en
   interne), qui réécrit le `stbl` et conserve toutes les pistes. 4.4 s pour 4 Go.
   → Le **derush reste de la métadonnée** : on ne coupe jamais un master, on
   passe les bornes à Gyroflow via `trim_ranges_ms`.

   → **Cherché à fond le 2026-08-19, il n'existe aucun outil prêt à l'emploi pour
   découper un rush en gardant le gyro.** Mesuré : Gyroflow n'a pas de mode « couper
   sans stabiliser » (vérifié dans son `--help`), `mp4_merge` ne fait que fusionner,
   `ffmpeg -c copy` vers mp4 est refusé même avec `-copy_unknown` et `-tag:d djmd`, et
   vers `.mov` **le muxage réussit** mais le tag des pistes de données devient `stts` au
   lieu de `djmd`/`dbgi` : le `detected_source` de Gyroflow tombe de `DJI O4P` à `None`,
   zéro quaternion. `exiftool` n'expose rien, la charge utile étant des samples du
   `mdat`. Restent deux pistes non testées : GPAC/`MP4Box -splitx`, absent des paquets
   Ubuntu 25.04 (le `.deb` officiel est un 0.7.2-DEV de 2018 qui ne s'installe pas, donc
   il faudrait compiler), et l'issue amont `gyroflow/gyroflow#1000` « mp4-split »,
   ouverte et non implémentée. C'est ce qui force le clip de benchmark à être un vrai
   rush court plutôt qu'un extrait fabriqué.

2. **On fusionne AVANT de stabiliser, jamais l'inverse.** Le lissage et
   l'adaptive zoom sont calculés sur toute la courbe gyro ; stabiliser deux parts
   séparément produirait une couture visible à la jonction.

3. **`--preset` de Gyroflow accepte un JSON partiel de projet et porte
   `trim_ranges_ms`.** Pas besoin de générer puis patcher un `.gyroflow` : un
   template + les bornes du cut, en une commande. Régler `output_width`/
   `output_height` suffit à changer de format (un 1080x1920 demandé sur une
   source 3840x2880 fait dériver un crop 1620x2880 tout seul).

4. **Le warping de Gyroflow prend OpenCL d'abord, wgpu (Vulkan) ensuite, le CPU en
   dernier**, et il choisit seul : `render()` lit dans son log ce qu'il a pris.
   L'image embarque les ICD OpenCL des trois fabricants (rusticl/Mesa pour AMD, NEO
   pour Intel, un `nvidia.icd` pointant sur la lib que le runtime injecte, pocl en
   repli CPU) et les pilotes Vulkan de Mesa. Attention : **un ICD installé n'est pas
   un device**. Sur une machine à cinq ICD et zéro GPU utilisable, le seul device
   énuméré est le CPU. Voir la section matériel.

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

## Matériel : sonder, jamais croire

**La machine de dev n'est pas fixe.** Les perfs ci-dessus viennent d'un portable
AMD ; le poste du 2026-08-17 est tout autre, et toute phrase « le GPU fait X » doit
donc nommer la machine :

| | machine AMD (perfs ci-dessus) | poste 2026-08-17 |
|---|---|---|
| CPU | Ryzen AI 9 HX 370 | Intel i7-7700K |
| GPU | Radeon 890M (amdgpu-dkms) | GeForce RTX 3090 (nvidia 535.261.03) |
| décodage retenu | VAAPI | **NVDEC (`cuda`)** |
| OpenCL | rusticl | **la plateforme NVIDIA, via le `nvidia.icd` de l'image** |
| accès conteneur | `/dev/dri` mappé | `runtime: nvidia` |

**Sur NVIDIA, mapper `/dev/dri` ne sert à rien.** Le nœud render appartient au
pilote `nvidia` : libva y lit le nom du pilote DRM, cherche `nvidia_drv_video.so`
(absent de l'image, et de toute façon la voie NVIDIA passe par le shim
`nvidia-vaapi-driver`), et renvoie `unknown libva error`. Un GPU NVIDIA se donne
au conteneur par `runtime: nvidia` + `NVIDIA_DRIVER_CAPABILITIES=compute,video,
utility`, ce que Docker ne peut pas décider seul, d'où `docker-compose.nvidia.yml`.

**`nvidia-smi` peut mentir sur l'état du GPU.** Diagnostic complet du 2026-08-17,
qui a coûté une soirée et qu'il ne faut pas refaire. Symptôme : `cuInit(0)` renvoie
`CUDA_ERROR_UNKNOWN`, **sur l'hôte, hors de tout conteneur**, alors que module noyau
et espace utilisateur sont tous deux en 535.261.03, que `/dev/nvidia*` sont présents
en `rw-rw-rw-`, que le journal noyau ne montre aucune erreur NVRM et que
`nvidia-smi` répond parfaitement. OpenCL n'énumérait le 3090 qu'une fois sur quatre.

Cause : **un processus d'inférence tué avait laissé un contexte CUDA fuité.**
`nvidia_uvm` gardait un refcount de 2 sans utilisateur nommé, ce qui bloquait à la
fois `rmmod nvidia_uvm` (« Module is in use ») et tout nouveau `cuInit`. Ce qui a
mis sur la piste : ce même `nvidia-smi` listait un processus python en train
d'utiliser 3358 Mio, preuve que CUDA fonctionnait pour lui. **Redémarrage, et tout
remarche du premier coup.**

Deux leçons. `nvidia-smi` interroge la couche de gestion, pas la pile de calcul :
son silence ne vaut rien comme diagnostic. Et le vrai signal utile était le
refcount du module, pas les versions ni les permissions.

Après reboot, mesuré dans le conteneur (source 3840x2880 HEVC 10 bits 60p) :

| source | décodage NVDEC | décodage CPU | gain |
|---|---|---|---|
| synthétique 16 Mb/s | 3.29x | 2.84x | 1.16x |
| synthétique 814 Mb/s | 1.40x | 0.33x | **4.3x** |

Passe proxy complète sur la source lourde : **1.37x contre 0.31x**. Le gain grandit
avec le débit, parce que le décodage domine : en NVDEC, la passe entière (2.19 s) ne
coûte presque rien de plus que le décodage seul (2.15 s). Un vrai rush est à ~135
Mb/s, donc entre les deux lignes du tableau. **Ne pas comparer ces chiffres à ceux
de la machine AMD** : le contenu synthétique encadre, il ne prédit pas.

**Deux impasses vérifiées, pour ne pas les réexplorer.** VDPAU décode bien (4,3x le
CPU sur l'hôte) mais exige une session X11 : `Cannot open the X11 display` dans un
conteneur, donc inutilisable pour un service headless. Et sur un pilote empaqueté
par Debian (bibliothèques dans `/usr/lib/x86_64-linux-gnu/nvidia/current`), le
runtime injecte l'ICD Vulkan mais **pas** le `libnvidia-glsi` dont il dépend, même
avec `NVIDIA_DRIVER_CAPABILITIES=all` : Vulkan reste indisponible dans le conteneur
sur cette machine, alors qu'il marche sur l'hôte.

### Le rendu Gyroflow est limité par le CPU, pas par le GPU

Mesuré le 2026-08-19 sur le poste RTX 3090 + i7-7700K, source O3 réelle 3840x2160
h264, sortie 1080p, 3000 images :

| | valeur |
|---|---|
| débit avec démarrage de Gyroflow | 19,7 img/s |
| débit en régime établi | **22,7 img/s** |
| `processing_device` rapporté | `OpenCL` |
| CPU du conteneur pendant le rendu | **676 %** sur 800 % (8 threads) |
| GPU pendant le rendu | **13 %** |

Le warp passe bien par OpenCL sur le 3090, et pourtant c'est le CPU qui sature. D'où
un résultat qui a l'air absurde : **un RTX 3090 rend à la même vitesse qu'un iGPU
Radeon 890M** (23 img/s dans le tableau plus haut). Il n'y a rien d'absurde, les deux
mesures sont simplement bornées par autre chose que le GPU, et le 890M était accompagné
d'un Ryzen AI 9 HX 370 (12 cœurs, 2024) là où le 3090 l'est d'un i7-7700K (4 cœurs,
2017).

Conséquence pour la répartition des jobs : **« envoyer les rendus à la machine qui a
le gros GPU » est un mauvais modèle**. La bonne question est quelle machine a le
meilleur CPU pour ce travail, et seule une **mesure du vrai débit** répond, jamais un
a priori tiré des capacités matérielles. Un GPU reste nécessaire (sans lui, ~8,7 img/s
mesuré), mais il n'est pas ce qui départage deux machines qui en ont un.

D'où le modèle de `services/capabilities.py` : **on sonde en exécutant**.

- **décodage** : NVDEC puis VAAPI, chacun essayé en décodant réellement un
  échantillon HEVC 10 bits : le codec des rushes, et précisément là où le support
  matériel se dégrade (une puce qui décode le HEVC 8 bits peut refuser le Main10).
  Le premier qui sort en 0 gagne ; `VS_HWACCEL` peut en épingler un. Un timeout
  compte comme un échec : un décodage qui pend est pire qu'un décodage lent.
- **OpenCL** : `clinfo --json`, en cherchant un device de **type GPU**. Compter les
  fichiers ICD était le test d'avant, et il mentait : l'image en livre cinq, et sur
  une machine dont la pile était coincée ils énuméraient le seul CPU.
- **Vulkan** : `vulkaninfo --summary`, même question, parce que Gyroflow essaie
  OpenCL *puis* wgpu. Filtrer sur `deviceType` est indispensable, sinon le
  rastériseur logiciel (`llvmpipe`, `lavapipe`) passe pour un GPU : il s'annonce
  `PHYSICAL_DEVICE_TYPE_CPU`, c'est ce qui le démasque.
- **schéma de `clinfo --json`** (vérifié sur 3.0.25, pas deviné) : `platforms` et
  `devices` sont deux listes **parallèles**, et les devices d'une plateforme pendent
  d'une clé `online` au lieu d'être imbriqués dans la plateforme.
- un **`nvidia.icd` orphelin est inoffensif** : le loader n'arrive pas à ouvrir la
  lib, saute le vendeur, et `clinfo` sort quand même en 0. C'est ce qui permet de le
  livrer inconditionnellement dans l'image plutôt que d'avoir une variante par
  fabricant.

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
  quaternions**, donc sur un fichier O4 son mode par défaut est vide, et le mode
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
  que pour 0,144 de la rotation, contre 0,999 pour `x` et 0,994 pour `y` : seul
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
  ne suffit pas, à 2000 °/s ils fixent encore l'échelle du graphe. On les écarte
  au-delà de la pleine échelle du capteur, en publiant le compte (`dropped`).
- ce qui part au navigateur porte **deux vues** sur la même télémétrie, 340 Ko pour
  4 min : la vitesse angulaire en **enveloppe min/max par bucket** (6000 buckets,
  la décimation effacerait justement les pics qu'on cherche) et les composantes du
  quaternion en **ligne simple**, l'orientation étant lisse à 2 kHz (mesuré : saut
  maximal de 0,0066 entre buckets voisins, et **aucun basculement de signe** dans
  les données DJI, donc rien à recoller). Produit pendant l'étape proxy, ou à la
  demande au premier appel ; `CHART_FORMAT` force la reconstruction des graphes
  déjà sur le disque quand la forme du JSON change.

## Étalonnage : un second encodage, et un piège de filtre

Gyroflow ne fait **rien** en couleur (ses params : `fov_scale`,
`lens_correction_amount`, `background_mode`, `adaptive_zoom_*`, aucune LUT). Donc
l'étalonnage ne peut pas être embarqué dans la passe de stabilisation.

Mesuré sur un clip réel de 10 s en 1080p60 :

| sortie | vitesse |
|---|---|
| HEVC 10-bit `medium` | 0.17x temps réel |
| HEVC 10-bit `superfast` | 0.26x |
| **H.264 8-bit `veryfast`** | **0.71x**, le choix retenu |
| une image filtrée en JPEG | **0.32 s**, d'où l'aperçu live |

L'aperçu est une vraie image ffmpeg, pas une réimplémentation en shader : ce qu'on
voit traverse exactement les filtres du rendu final, aucune parité à maintenir.

**`colorlevels` est un piège.** Il accepte le YUV autant que le RGB, et sur une
image YUV ses points « rouge/vert/bleu » tombent sur **Y/U/V** : décaler le point
noir de la chroma, dont le neutre est au milieu de la plage et non à zéro, rend
l'image entièrement noire. Pire, ça ne se produit qu'**une fois sur deux** : avec
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
non compensé : un correctif en dur se casserait au prochain changement de version
ou de fps.

## Détection des splits

La détection intégrée à Gyroflow ne marche pas sur les noms O3/O4 : son motif
`/(DJI_\d+_(\d+)\.MP4)$/` visait les DJI Action et échoue sur le suffixe `_D`.
La nôtre (`services/grouping.py`) :

- index caméra consécutifs **et** `start(n+1) - (start(n) + durée(n)) <= 1 s`
  (mesuré : 0,36 s d'écart sur une vraie paire)
- **deux sources pour l'heure de départ** : le nom (exact, UTC), puis le
  `creation_time` du conteneur. Aucune des deux, aucun repli : le fichier est
  **refusé** avec la raison. Voir ci-dessous.
- **et rien d'autre.** Le timing est tout le test.

### L'écart sépare, à condition de le serrer

Mesuré le 2026-08-19 sur les 179 paires consécutives de la collection O3 réelle :

| | taille de la part 1 | durée | écart |
|---|---|---|---|
| **51 vraies découpes** | 3,763 à 3,770 Go | 193,7 à 196,8 s | 0,12 à **0,79 s** |
| **9 paires collées à tort** | jusqu'à **1,398 Go** | variable | **1,11** à 1,96 s |

Les deux populations ne se chevauchent pas : **0,79 s d'un côté, 1,11 s de l'autre**.
Une tolérance à **1 s** tombe dedans, garde les 51 vraies et écarte les 9 fausses.
C'était 2 s, et à 2 s il fallait un second signal pour rattraper les 9.

Ce second signal était la taille de la part 1, avec un seuil à 3 Go : une part qui n'a
pas approché la limite de fichier s'est arrêtée parce qu'on a arrêté d'enregistrer.
**Retiré le 2026-08-20**, choix de florian : le seuil appartient à la caméra et à la
carte, pas à nous, et il faisait dépendre les paires O4 d'un nombre mesuré sur des
fichiers O3. Le facteur de séparation était plus confortable (2,7 contre 1,4 pour
l'écart), mais un nombre confortable et faux ailleurs ne vaut pas un nombre juste et
serré. Il n'y a aucun recours manuel (voir plus bas) : deux parts séparées à tort se
recollent en supprimant les deux séquences, ce qui rend leurs clips libres et laisse le
scan suivant les regrouper. Deux vols collés à tort, eux, n'ont pas de recours.

À ne pas confondre avec la vraie cause du bug du 2026-08-20 : sur la paire O4 qui a
déclenché tout ça, la taille passait déjà (3,76 Go). Voir ci-dessous.

### Le scan qui tombe entre deux uploads

Bug mesuré le 2026-08-20, et il n'était pas dans le groupage.

```
13:29:08.607  Received  0034_D.MP4  (3588.6 Mo)
13:29:15.597  Ingested  0034_D  → 1 sequence created   ← scan périodique (30 s)
13:29:20.734  Received  0035_D.MP4  (2372.1 Mo)        ← upload 2 encore en vol
13:29:20.822  Ingested  0035_D  → 1 sequence created
```

Deux parts séparées de 0,361 s sont devenues deux séquences parce que le scan des 30 s
est tombé **pendant** l'upload de la seconde. Il a ingéré la part 1 seule, l'a groupée,
et le worker l'a fusionnée avant que la part 2 arrive : une part unique est un
hardlink, donc c'est fait en 0,3 s. La part 2 a trouvé une séquence déjà `MERGED`, que
le groupage ne touche jamais automatiquement.

Corrigé : le scan **planifié** n'ingère rien tant qu'un upload est en vol, ni pendant
les `UPLOAD_SETTLE_S` (10 s) qui suivent le dernier, parce qu'un uploadeur enchaîne ses
fichiers et lit 2 Mio de chacun pour le contrôle de doublon, donc il existe un creux de
quelques centaines de millisecondes où rien n'est sur le fil et où le lot n'est pas
fini. Un scan **demandé à la main** n'est jamais retenu.

Contre-épreuve, avec le scan forcé à 2 s pour rendre la course certaine :

```
13:58:47.205  Received  0034_D          ← les scans de 13:58:49 et 13:58:51
13:58:50.545  Received  0035_D             n'ont rien ingéré
13:59:01.189  Ingested  0034_D  ┐ un seul scan, 10,6 s après le dernier upload
13:59:01.242  Ingested  0035_D  ┘
13:59:01.278  Scan: 2 clip(s) ingested, 1 sequence(s) created
```

Une séquence, 2 parts, 369,786 s (222,639 + 147,147 exactement), fusionnée et proxyée.

### Trois nommages, et un seul porte un horodatage

Relevé le 2026-08-19 sur la collection O3 réelle de florian (622 Go, 194 masters) :

| forme | exemple | reconnu comme | profil |
|---|---|---|---|
| horodaté | `DJI_20260703172854_0020_D.MP4` | `dji_goggles` | 3840x2880 hevc |
| index seul | `DJI_0327.MP4` | `dji_legacy` (ajouté le 2026-08-19) | 3840x2160 h264 |
| fusion à la main | `DJI_0044_0045_joined.MP4` | `unknown`, et c'est voulu | 3840x2160 h264 |

**L'ancien nommage O3 n'était pas lu du tout** : 13 masters sur 15 échantillonnés
le portent, et le groupage se rabattait donc sur le seul timing. Attention, il n'y a
là **aucun horodatage** : `recorded_at` vient du repli de `probe()`, soit
`mtime - durée`. C'est juste comme instant absolu (le `mtime` est un epoch), et
précis parce que le fichier est écrit en temps réel pendant l'enregistrement.

Le `_joined` reste volontairement `unknown` : c'est un enregistrement entier, rien ne
doit jamais s'y chaîner, et sans index seul le timing peut en décider.

### Le `mtime` ne voyage pas avec les octets

Piège coûteux, trouvé le 2026-08-19 en testant sur les vrais fichiers. L'heure de
départ venait du nom, et à défaut de `mtime - durée`. Or **l'ancien nommage O3 ne
porte aucun horodatage**, donc pour 13 masters sur 15 le `mtime` était la seule
source. Et le `mtime` est détruit par n'importe quelle copie : `cp` sans `-p`, un
`rsync` sans `-t`, et surtout **le glisser-déposer de l'interface**, puisque HTTP ne
transporte pas de date de fichier.

Effet mesuré : la vraie paire `DJI_0330` + `DJI_0331`, séparée de **0,4 s**, copiée
avec un `cp` ordinaire, ressortait à **78 s d'écart** et n'était jamais chaînée. Les
deux parts d'un même vol devenaient deux séquences, donc deux fusions, donc une
couture au montage.

La bonne source était dans le fichier depuis le début : **`creation_time` du
conteneur mp4**. Contre-épreuve sur les fichiers dont le nom porte l'heure vraie, et
c'est exact à la seconde près :

| fichier | nom (UTC) | `creation_time` |
|---|---|---|
| `DJI_20260703172854_0020_D.MP4` | 17:28:54 | `2026-07-03T17:28:54.000000Z` |
| `DJI_0330.MP4` | rien | `2025-08-17T09:51:19.000000Z` |

Donc du vrai UTC, et il survit à la copie. Un garde-fou quand même : une caméra dont
l'horloge n'a jamais été réglée écrit une date epoch, pire que rien puisque tous les
rushes paraîtraient contigus. En dessous de l'an 2000, on ignore.

### Deviner faux est pire que ne pas savoir

Deux refus francs plutôt que deux heuristiques, choix de florian le 2026-08-19.

**Pas d'heure de départ utilisable → fichier refusé.** Le repli `mtime - durée` a été
**supprimé**, pas corrigé : il mentait sur tout fichier copié, et un mensonge
silencieux sur le timing produit un groupage faux qui ne se voit qu'au montage. Le
clip finit en `FAILED` avec la raison écrite, visible dans l'interface. Une horloge
de caméra jamais réglée (date epoch) tombe dans le même cas, puisqu'on l'ignore.

**Sortie déjà stabilisée → écartée sur le nom.** Gyroflow nomme sa sortie
`..._stabilized`, avec parfois le format en suffixe (`_stabilized_16x9`,
`_joined_stabilized`). Le sous-chaîne `stabilized` suffit, et le contrôle passe
**avant** l'empreinte, donc sans lire le fichier. Le fichier part dans
`inbox/.stabilized/`, à côté de `.duplicates/`, jamais supprimé, et le scan en publie
le compte.

Lire la piste gyro serait plus robuste (mesuré : 15 masters sur 15 en ont, 2 sorties
sur 2 n'en ont pas), mais ces fichiers ne sont pas censés arriver dans l'inbox et un
contrôle de nom ne coûte pas une sonde. Conséquence assumée : un fichier sans gyro
qui passerait quand même échouera à l'étape de stabilisation, avec l'erreur de
Gyroflow.

## Dispatcher et workers

Depuis le 2026-08-19, **la base appartient au dispatcher seul**. Un worker ne
l'ouvre jamais : il s'enregistre en HTTP, réclame un job, reçoit une **spec
autoportante** et repose les faits qu'il a mesurés. C'est ce qui permettra de poser
un worker sur une autre machine, et ça a déplacé du code :

| | avant | maintenant |
|---|---|---|
| scan de l'inbox, groupage | worker | **API** (elle seule voit le volume qui compte) |
| purge des parts après fusion | worker | **API** (sa copie est celle qui fait foi) |
| hash des paramètres d'étalonnage, bornes de coupe | worker | **API** (elle doit les stocker de toute façon) |
| sonde matérielle, exécution | worker | worker |

Quatre choses à ne pas défaire :

1. **Les workers tirent, le dispatcher ne pousse jamais.** Il n'y a donc aucune
   découverte (un worker reçoit une URL et s'annonce), aucun port entrant côté
   worker, aucune sonde de santé (un worker qui cesse de demander est parti) et
   aucun moyen de le surcharger, puisque le travail ne bouge que quand il en
   demande.
2. **Un job est tenu par un bail** (`lease_expires_at`, 60 s, renouvelé toutes les
   2 s). Un bail que personne ne renouvelle retourne en file : c'est le seul réessai
   automatique du système, et c'est ce qui rend un worker éteignable en pleine passe
   (mesuré : SIGKILL sur le worker, requeue à +66 s, reprise et fin du job).
   Plafonné à `MAX_ATTEMPTS` pour qu'un job qui tue son worker ne fasse pas tourner
   la file indéfiniment. La cadence de 2 s est celle de la **barre de progression**,
   pas celle du bail : le battement porte les deux, et à 15 s la barre avançait par
   sauts visibles.
3. **Le revers du bail, c'est le clôturage.** Un worker qui n'arrive plus à
   renouveler doit tuer son propre enfant : deux ffmpeg écrivant le même fichier de
   sortie est le seul scénario qui vaille qu'on se donne du mal. D'où le
   `ok: false` en réponse au battement de cœur, et `procs.terminate_all()` derrière.
4. **L'attribution est atomique** : un `UPDATE ... WHERE state='queued'`
   conditionnel. Un `SELECT` puis un `UPDATE` donnerait la même fusion de 4 Go à
   deux machines.

Le battement de cœur est **découplé de la progression** : le callback de
l'executor n'écrit qu'une variable, un thread poste. Donc un dispatcher lent ne
ralentit pas un rendu, et un rendu silencieux garde son bail. Un *process* bloqué
est un autre problème, déjà traité par les timeouts de `procs.run_with_progress`.

Et **c'est le battement qui prouve qu'un worker est là**, pas la demande de job.
Corrigé le 2026-08-20 : `last_seen_at` n'était écrit qu'à l'enregistrement et à la
prise d'un job. Un worker dont tous les créneaux sont pris cesse de demander du
travail, donc il passait hors ligne au bout de `ONLINE_S` et l'interface affichait
« no worker » au-dessus d'un proxy à 52 %. Un battement venant d'un worker qui a perdu
le bail ne compte pas : c'est justement celui à qui on dit d'arrêter.

Le nom d'un worker est son identité (`VS_WORKER_NAME`) et doit être **stable** :
le hostname d'un conteneur a l'air stable et ne l'est pas, il change à chaque
recréation et laisse une ligne `worker` orpheline derrière lui.

### Le benchmark au démarrage : ranger les machines, pas prédire les durées

Chaque worker mesure ses quatre débits **en exécutant les quatre vraies étapes** sur
0,5 s de rush O3 embarqué dans l'image (`docker/bench/clip.mp4`, 9 Mo). Un vrai rush
parce qu'un rendu exige une piste gyro et que personne ne sait en fabriquer ni en
découper une. Mesuré le 2026-08-19 sur le poste RTX 3090 + i7-7700K :

| | valeur |
|---|---|
| fusion `mp4_merge` | 114 Mo/s |
| proxy | 61 img/s |
| rendu | 28,5 img/s |
| étalonnage | 27,8 img/s |
| lien vers le dispatcher | 2981 Mo/s (deux conteneurs, même hôte) |
| **coût total** | **4,4 s** |

4,4 s, donc **aucun cache** : mesurer à nouveau coûte moins cher que raisonner sur la
péremption d'un nombre stocké.

**Ces chiffres classent les machines, ils ne prédisent pas les durées.** Le même code
donne 28,5 img/s sur le clip et 22,7 img/s sur une séquence réelle de 272 s, 61 img/s
en proxy contre 0,9x le temps réel. Un clip aussi court ne sort jamais du cache de
pages et passe une part visible de sa vie dans le démarrage des processus. Sans
importance pour choisir entre deux machines, et `dispatch.observe` remplace l'estimation
par la vérité dès que de vrais jobs finissent : moyenne mobile (α = 0,3) sur le débit
réel, en ignorant les jobs de moins de 300 images ou 200 Mo, qui mesureraient le même
biais de démarrage que le benchmark.

D'où **deux colonnes** sur `worker` : `rates` (le benchmark, réécrit à chaque
enregistrement) et `observed` (ce que les vrais jobs ont prouvé, jamais touché par un
enregistrement). Un redémarrage de conteneur ne doit pas jeter le seul chiffre qui
vienne du vrai travail.

Trois détails qui ont été mesurés, pas devinés :

- **le débit se chronomètre entre la première et la dernière ligne de progression**,
  pas depuis le lancement : Gyroflow passe 1,4 s d'un rendu de 3,4 s à charger ses
  12344 profils d'objectif, et compter ce temps annoncerait une machine 40 % plus lente.
- **plus court est plus répétable.** 0,5 s (30 images) donne 27,2 puis 26,8 img/s sur
  deux passes ; un clip de 1,8 s donne 38,8 puis 36,4. Le court est à la fois plus léger
  (9 Mo contre 25) et plus stable.
- **la fusion mesurée est optimiste et on le sait** : 20 Mo ne quittent jamais le cache
  de pages là où une vraie fusion de 4 Go est de l'I/O pur. Elle classe quand même, et
  elle sert de seul test que `mp4_merge` fonctionne sur cette machine.
- **un débit inconnu reste inconnu.** Une étape de benchmark qui échoue laisse son
  débit à `null`, et le dispatcher traite ça comme une inconnue, jamais comme lent.
  Le gating des capacités reste dans `capabilities.py` ; ce module ne fait que classer.

### Qui voit quel volume : on compare, on ne configure pas

Le dispatcher écrit un `.volume-id` (un uuid) dans son `data_dir` au démarrage. Le
worker lit le sien et l'envoie à l'enregistrement ; l'égalité **est** le test de
« partageons-nous les fichiers ». Pas un drapeau à régler par worker, parce que le sens
qui casse est silencieux : un worker à qui on a dit à tort qu'il partage le volume va
chercher des fichiers qui ne sont pas là et rate tous ses jobs.

### Un worker ailleurs : les fichiers voyagent, le coût décide

Vérifié de bout en bout le 2026-08-19, ce PC en worker distant contre le dispatcher
local, avec son propre volume de travail :

| | mesuré |
|---|---|
| détection du volume | `shares_data=False`, sans rien configurer |
| master récupéré | 450,9 Mo en HTTP |
| rendu | 1470 images en 59,3 s, OpenCL sur le 3090 |
| sorties renvoyées | 114,4 Mo + le `.gyroflow.json` |
| deuxième rendu du même master | **aucun transfert d'entrée**, seuls 88,8 Mo repartent |
| proxy distant | **quatre** fichiers renvoyés : proxy, filmstrip, poster, graphe gyro |
| fusion distante | part récupérée, hardlink, master renvoyé |
| ce que les vrais jobs ont appris | rendu 24,9 img/s contre 28,0 au benchmark |

Les quatre types de job ont tourné à distance, et le proxy est le plus instructif :
**trois de ses quatre sorties ne sont nommées par aucun champ du résultat**.

**Adressés par chemin relatif, pas par hash de contenu.** Le hash serait la réponse
de manuel et c'est la mauvaise ici : il faudrait lire 4 Go pour nommer un fichier dont
le dispatcher connaît déjà le nom. Les chemins sont des identités sûres dans ce dépôt
parce que rien n'est jamais réécrit sur place (un master fusionné porte le stem de la
séquence, un étalonnage le hash de son look, un rendu son template et son cut). Donc
le cache du worker vérifie un chemin et une taille, et il a raison.

**Ce qui remonte, c'est tout ce que le job a écrit, pas ce que le résultat nomme.** La
seule étape proxy écrit un poster, un filmstrip et un graphe gyro qu'aucun champ du
résultat ne mentionne, et l'interface les lit tous. La détection se fait par
**instantané avant/après**, pas par comparaison d'horodatage : la granularité du mtime
appartient au système de fichiers, et les deux façons de se tromper sont mauvaises
(rater une sortie laisse le dispatcher avec un chemin sans fichier, prendre une entrée
pour une sortie renvoie un master de 4 Go d'où il vient).

**La fonction de coût, en une ligne** : `secondes = ampleur / débit + (à récupérer +
ratio_sortie x total_entrée) / lien`. Deux détails qui ont demandé une mesure :

- **le retour se compte à part de l'aller.** Version d'avant : un facteur x2 sur les
  octets *manquants*. Trou réel : un worker qui a déjà le master ne payait plus rien,
  alors qu'il doit toujours renvoyer le rendu. Les ratios viennent de deux rushes
  réels : un master 4K de 451 Mo donne 114 Mo en 1080p (0,3) et 19 Mo de proxy (0,05) ;
  fusion et étalonnage rendent ce qu'on leur a donné (1,0).
- **inconnu reste inconnu.** Un débit que le benchmark n'a pas pu mesurer prend la
  moyenne de ce que les autres ont mesuré, jamais zéro : un worker exclu en silence de
  tous les jobs d'un type serait pire qu'un worker mal classé. Idem pour un lien non
  mesuré.

**La règle d'attribution.** Un worker prend le job pour lequel il est le moins cher.
Si un autre est nettement meilleur et qu'il est en ligne avec de la place, il laisse :
tout le monde interroge le dispatcher une fois par seconde, donc l'autre se le verra
proposer dans la seconde. `SKIP_MARGIN` de 1,2 parce que les deux workers se notent
mutuellement sur des données légèrement différentes (chacun connaît son cache
exactement et celui de l'autre à son dernier poll) : à quasi-égalité, les deux prennent
et l'attribution atomique tranche, alors que les deux qui s'effacent serait un blocage
sans vainqueur.

Trois limites assumées, pas résolues :

1. **rien n'est réservé à un worker simplement occupé**, même trois fois plus rapide et
   sur le point de finir : le savoir demanderait de prédire la fin d'un job en cours, et
   donner le travail à qui est libre ne laisse jamais une machine à l'arrêt.
2. **le benchmark et l'observé se comparent alors qu'ils ne sont pas commensurables.**
   Un worker qui a fini de vrais jobs paraît ~13 % plus lent qu'une machine identique
   qui n'en a pas fait (mesuré : 24,9 contre 28,0). Sous la marge de 20 %, donc sans
   effet sur les décisions, et l'écart s'efface à mesure que tout le monde accumule.
3. **avec un seul worker tout ceci se réduit à « prendre le prochain job »**, puisque
   l'unique candidat est toujours le moins cher. C'est le déploiement normal ici.

Un dernier point trouvé en le regardant se produire : **un worker arrêté proprement
compte comme parti tout de suite**. Avec le worker local stoppé et un rendu en file, le
distant s'effaçait correctement devant une machine moins chère qui n'existait plus, et
le job attendait 59 s que le bail expire. L'expiration de bail est le bon garde-fou pour
un worker qui a disparu, et la mauvaise réponse pour un worker qui a dit au revoir :
`release()` le vieillit donc au-delà de `ONLINE_S`.

**Ce qui n'est pas gardé côté worker** : `tmp/`, `db/` et `inbox/` ne voyagent jamais,
et l'éviction ne touche que les répertoires de rushes. Elle n'existe que sur un worker
qui ne partage pas le volume, garanti par construction (le `Workspace` n'est instancié
que dans ce cas) : sur le volume du dispatcher, elle supprimerait les originaux.

### Deux images, un Dockerfile

La scission ne vaut que dans un sens, et elle rapporte plus qu'il n'y paraît. Mesuré :
**656 Mo pour l'API contre 1,98 Go** pour l'image unique d'avant, soit **1,33 Go de
moins, les deux tiers**. Additionner les gros morceaux (Gyroflow 165 Mo, Vulkan Mesa
90 Mo, pilotes VA 18 Mo, deux des trois copies de LLVM) n'en explique que la moitié :
les pilotes traînent une longue queue de dépendances transitives. Ce qui est propre à
l'API pèse **508 Ko** de front compilé.

Il reste 138 Mo de LLVM dans l'image API, tirés par les dépendances de **ffmpeg**
lui-même. Impossible de s'en débarrasser sans retirer ffmpeg, dont l'aperçu
d'étalonnage a besoin.

| | API | worker |
|---|---|---|
| Python, dépendances, code | oui | oui |
| **ffmpeg** | **oui** (aperçu d'étalonnage, analyse) | oui |
| front compilé | oui | non |
| OpenCL, Vulkan, pilotes VA | non | oui |
| Gyroflow, `mp4_merge` | non | oui |

Un seul fichier et deux cibles, pas deux fichiers : la couche `base` est construite
une fois et partagée dans le registre comme dans le cache CI. Et la CI les construit
**dans un seul job, l'un après l'autre**, pour que le second réutilise `base` et
`frontend` du premier ; une matrice les ferait en parallèle et construirait deux fois
les étages communs.

Conséquences à ne pas oublier :

- **l'API ne sonde plus le matériel.** `/api/status` n'a plus de bloc `capabilities`,
  il a une liste `workers` où chacun publie ce qu'il a mesuré chez lui. L'entête et
  les « hardware notes » de l'interface lisent ça, et une note est attribuée à la
  machine qui s'en plaint.
- **les overrides GPU ne s'appliquent qu'au worker.** Donner la carte à l'API
  injecterait des bibliothèques de pilote dans un processus qui n'a rien à en faire.
- **une seule image worker pour toutes les machines.** Le choix se fait en sondant au
  démarrage, pas à la construction : la VM sans GPU et le poste à RTX 3090 font
  tourner les mêmes octets.

## L'interface : une barre latérale, et une page par geste

Refonte du 2026-08-20. La barre de gauche porte quatre choses, dans cet ordre : le nom,
le compte de workers (cliquable, le détail des capacités est dans un dialogue et non
plus dans une infobulle), l'arborescence des rushes, puis les quatre pages. shadcn de
base uniquement, `dropdown-menu` ajouté pour les actions par ligne.

**Les dossiers vont à deux niveaux, pas plus.** Un site, et une sortie dedans. La règle
vit dans l'API et pas dans le modèle : une contrainte sur une table qui se référence
elle-même ne voit pas le grand-parent. Un dossier ne contient aucune image, donc le
supprimer ne perd rien : ses rushes retournent dans Global, ses sous-dossiers à la
racine. La couleur se choisit à
la création, parmi les six jetons de `lib/colors.ts`, avec un tirage au sort en
présélection ; l'API **refuse** un jeton hors palette, parce que le front décide de son
apparence et qu'un mot qu'il ne connaît pas donnerait une pastille invisible.

**Un rush est toujours dans un dossier.** Celui par défaut s'appelle **Global** et
n'est pas une ligne en base : c'est ce à quoi ressemble `folder_id = null`. Donc il n'y
a rien à protéger contre un renommage, un recoloriage, une suppression ou un glisser,
puisque aucun de ces gestes n'existe pour lui. Toujours premier, et gris, parce que
c'est le seul dossier dont personne n'a choisi la couleur.

**On déplace au glisser-déposer**, un rush comme un sous-dossier, et **réordonner se
distingue d'imbriquer par la cible** : lâcher sur une ligne veut dire « dedans », lâcher
sur l'interstice entre deux lignes veut dire « à côté ». Les interstices n'existent que
pendant le glisser d'un dossier. Les rangs (`folder.position`) sont **recalculés
densément à chaque écriture**, création comprise : incrémenter n'est juste que si l'état
l'était déjà, et un trou laissé par un départ est précisément ce sur quoi la prochaine
insertion atterrit. Un rang n'a besoin d'être unique **qu'entre frères**.

Trois pièges du glisser-déposer HTML5, tous mesurés le 2026-08-20 :

- `dataTransfer` **cache ses valeurs pendant `dragover`**, or c'est là qu'une cible doit
  décider si elle s'allume. Ce qui est soulevé vit donc dans un état React.
- **`dragleave` se déclenche sur un parent dès que le pointeur entre dans son enfant.**
  Le surlignage s'éteignait donc à l'instant où il s'allumait, alors que le lâcher
  marchait, parce que `dragover` remonte de l'enfant. On ne l'éteint que si
  `relatedTarget` n'est pas contenu dans la cible.
- **`dragenter` doit être accepté aussi**, pas seulement `dragover` : c'est l'entrée qui
  déclare l'élément zone de dépôt, et un pointeur qui entre puis s'arrête ne reçoit
  jamais de `dragover`. La ligne d'insertion restait noire sous un lâcher qui marchait.

Les lâchers illégaux ne s'allument pas et ne changent rien : un dossier dans lui-même,
dans un enfant, un dossier qui a des enfants dans un autre, ou n'importe quel dossier
dans Global.

**Toute écriture de l'arborescence s'affiche avant d'être confirmée.** Créer, renommer,
recolorier, supprimer, déplacer : le cache est écrit tel que la réponse le sera, la
requête part derrière, et un refus remet l'arbre d'avant avec le message. `onSettled`
refait la requête dans les deux cas, ce qui corrige une prédiction légèrement fausse
sans que personne le voie. Mesuré le 2026-08-20 avec la réponse retenue par le test :
dossier visible en **70 ms**, pastille changée en **29 ms**, rush déplacé en **136 ms**,
le tout requête toujours en vol ; et un refus fait disparaître le dossier fantôme.

**Aucun compteur dans l'arborescence**, ni le total en tête ni le contenu de chaque
dossier : un nombre à côté d'une liste qu'on voit ne dit rien. Le chevron suffit à dire
qu'un dossier replié contient quelque chose.

La contrepartie assumée : la règle de placement du serveur est **répétée côté client**
(`place()` dans `RushTree.tsx`), sinon un lâcher ne peut pas atterrir tout de suite. Les
deux ne peuvent divergir que jusqu'au refetch, ce qui rend la duplication supportable.
Et un dossier tout juste créé porte un **id négatif** le temps de l'aller-retour.

**Le rush sélectionné est dans l'URL**, `/derush/12` ou `/stabilisation/12`, et il suit
quand on change d'étape. Attention : `useParams` dans l'élément d'une route parente ne
voit pas le `:id` de l'enfant, il rend le match de la route parente. D'où
`lib/routing.ts`, qui lit le chemin. La page couleur ne prend pas de rush, son `:id` est
un rendu.

**Pas de page merge, et aucun groupage à la main.** Elle a existé une journée, le
2026-08-20 : l'écart entre deux rushs consécutifs, joindre, dégrouper. Retirée le même
jour, choix de florian, le groupage automatique est le seul chemin. Partis avec elle :
`POST /sequences/regroup`, `POST /sequences/{id}/split`, et le soin qu'ils prenaient de
garder le dossier de la première part. Si un groupe est faux, le seul recours est de
supprimer les séquences en cause, ce qui rend leurs clips libres (les masters restent
sur le disque) et laisse le prochain scan les regrouper. Il rend le même verdict, donc
ça répare une paire séparée à tort, jamais deux vols collés.

**La pastille de couleur n'appartient qu'aux dossiers.** Un rush en portait une aussi,
posée depuis la page derush. Retirée le 2026-08-20 : dans une liste où le nom et la
durée sont déjà là, la couleur d'un rush ne distinguait rien, alors que celle d'un
dossier est ce qui le fait reconnaître d'un coup d'œil. La colonne `sequence.color`
**reste en base** : elle est NOT NULL sans défaut DDL, donc la retirer du modèle
casserait chaque insertion sur une base existante.

### L'upload ne dépend plus de la page qui l'a lancé

Refonte du 2026-08-21. Le transfert, lui, n'en a jamais dépendu : **un
`XMLHttpRequest` en vol n'est pas annulé quand React démonte le composant qui l'a
ouvert**, et rien n'appelait `abort()` au démontage. Ce qui se perdait en changeant de
page, c'était tout ce qui l'entoure, parce que la file vivait dans l'état de la page
import : la liste, le pourcentage, le bouton Stop, et le fait même qu'un upload
tournait. D'où `lib/upload.tsx`, monté dans le layout, et la page devenue une vue.

**Un onglet en arrière-plan continue, et ce n'est pas de la chance** : Chrome bride les
timers et arrête `requestAnimationFrame` dans un onglet caché, il ne bride pas les
requêtes réseau en vol. Or rien dans cette boucle n'attend un timer, ce sont des
promesses et des événements XHR de bout en bout.

Mesuré côté serveur (les octets arrivés dans `inbox/`, pas l'affichage), montée bridée
à 8 Mo/s par CDP :

| | mesuré |
|---|---|
| onglet au premier plan | 23,9 Mo en 3 s |
| onglet « caché » | 24,0 Mo en 3 s |
| `Page.setWebLifecycleState: frozen` | 24,0 Mo en 3 s |
| après navigation vers `/derush` | 15,9 Mo en 2 s |

**Honnêteté sur la deuxième ligne** : ni en headless ni sous X, `bring_to_front()` sur
un autre onglet n'a fait passer `document.hidden` à vrai, donc l'onglet n'était
probablement pas vraiment considéré comme caché et le gel forcé a pu être refusé. Ce qui
est prouvé, c'est la navigation ; pour l'arrière-plan, l'argument reste l'absence de
timer dans le chemin de l'upload.

Fermer l'onglet, en revanche, tue le transfert : d'où un `beforeunload` tant qu'un
upload tourne, et la ligne « Keep this tab open while uploading » a disparu, la barre de
la sidebar disant mieux que ça continue ailleurs.

Deux détails : **un Stop doit atteindre tous les lots en vol**, pas seulement le dernier
(un `Set<AbortController>` au lieu d'une ref unique, sinon deux dépôts successifs
laissent le premier incoupable), et la barre latérale n'affiche l'upload que pendant :
un indicateur permanent à zéro serait du mobilier.

### Pourquoi l'upload est lent, et où il ne l'est pas

Diagnostiqué le 2026-08-21, laptop de florian vers proxima. Trois suspects, mesurés
un par un, et deux sont innocents.

| mesure | valeur |
|---|---|
| 1 Go poussé dans l'API depuis proxima (localhost) | **611 Mo/s** |
| RTT proxima vers la box (192.168.1.254) | **0,20 ms** |
| RTT proxima vers le laptop (192.168.27.65) | **21,7 ms** |
| lien ethernet de proxima | 1000 Mb/s |
| débit déduit de deux vrais uploads (3,5 Go en 383 s) | **~9 Mo/s** |

**`/mnt/Stockage` n'est pas dans la boucle.** `VS_DATA_PATH=./data`, donc l'inbox est
sur le NVMe racine (`/dev/nvme0n1p2`), pas sur le disque à plateaux. Le HDD ne peut pas
être le coupable de quelque chose qu'il ne touche pas.

**L'API et le disque non plus** : 611 Mo/s en local, soit **66 fois** ce qui est observé.
La route d'upload est déjà écrite pour ça (streaming direct vers un `.partial`, pas de
multipart, pas de `fsync`).

**Le laptop n'est pas sur le LAN.** La box répond en 0,20 ms, le laptop en 21,7 ms, et sa
route passe par `192.168.1.254` : le trafic entre par le VPN de la Freebox depuis
l'extérieur. Le plafond est donc soit le débit montant de la connexion où se trouve le
laptop, soit le CPU de la box qui chiffre le tunnel.

Deux conséquences pratiques. Sur le LAN sans VPN, le lien gigabit donnerait ~110 Mo/s,
soit dix fois mieux, et le NVMe suivrait sans peine. Et côté logiciel, la seule piste
réelle est le **parallélisme** : à 21,7 ms de RTT un flux TCP unique est borné par la
fenêtre et s'effondre à la moindre perte, alors que deux ou trois transferts simultanés
remplissent le tunnel. Le code est volontairement séquentiel, et son commentaire dit
pourquoi (« saturer le lien et éparpiller les écritures ») : c'est juste sur un LAN et
faux sur un tunnel distant.

### La barre latérale se tire, et les noms coupés se lisent

320 px par défaut au lieu de 256, et une poignée de 4 px sur le bord droit, bridée entre
200 et 560 px (mesuré : 150 demandés donnent 200, 900 donnent 560). La largeur va dans
`localStorage` par `usePersistentState`, donc elle survit au rechargement. Écrire à
chaque `pointermove` est assumé : c'est du localStorage, pas une requête.

Tout nom en `truncate` porte un `title`, dans l'arbre comme dans la barre du haut. Pas
de mesure de `scrollWidth` pour ne l'afficher que s'il est vraiment coupé : le `title`
natif ne coûte rien et la page import faisait déjà ça pour les noms de fichiers.

### La barre du haut : l'état en texte, le type en badge

Le badge portait l'état (« running »), identique sur toutes les lignes, et le type était
en texte gras. Inversé le 2026-08-21 : « Running task » se lit en texte normal, et le
badge porte `stabilize` / `proxy` / `merge` / `color`, qui est ce qui distingue une ligne
d'une autre. La barre a aussi perdu son `max-w-7xl` centré : elle prend toute la largeur,
alignée sur le `px-6` du contenu en dessous.

## Le derush : le mot « sequence » est pris deux fois

Refonte du 2026-08-21. Ce que l'interface appelle une **sequence** est un `Cut` dans le
code, l'API et la base, parce que `Sequence` y désigne déjà le rush fusionné qu'on est
en train de découper. Renommer la table serait un renommage sur un nom occupé, et les
deux mots ne se croisent jamais à l'écran : **le code dit `cut`, seul l'affiché dit
« sequence »**. C'est écrit en tête de `Derush.tsx` et dans la docstring de `Cut`.

**Deux boutons, un seul vivant à la fois.** « Start sequence » est actif tant que rien
n'est commencé, « End sequence » seulement après, et un « Cancel » n'apparaît que
pendant. Les deux sont en `variant` par défaut, donc blancs en thème sombre, parce que
c'est le geste de la page. Le clavier suit (I, O, Escape).

**Aucun bouton de sauvegarde.** Fermer une sequence, en redimensionner une, la renommer,
la supprimer : chaque geste écrit. Un redimensionnement n'écrit qu'**au relâchement**,
pas à chaque `pointermove`, via une ref qui retient la liste que le drag a produite. Et
aucun toast de succès : à ce rythme, ce serait du bruit ; seul un échec parle.

La contrepartie de l'écriture immédiate est que **la suppression demande confirmation**,
le seul geste du derush qui ne se refait pas d'un clic. Le dialogue ne porte une phrase
que si la sequence a une stab (« le fichier reste sur le disque ») : dire ça d'une
sequence dont personne n'a rien produit serait rassurer sur un fichier qui n'existe pas.

**Les sequences sont un tableau**, dans une colonne de 24 rem qui passe à 30 rem quand
l'écran le permet : nom, in, out, longueur et les deux actions sur une seule ligne, tout
en `text-sm`. Deux lignes par entrée avec des tailles mélangées ont été essayées le
2026-08-21 et lues comme du désordre. Ici les icônes sont **toujours dessinées**, à la
différence de l'arbre à gauche où elles sortent au survol : une liste de trois lignes
n'a pas de place à gagner, et une icône qui n'apparaît qu'au survol est une icône dont
personne ne sait qu'elle est là.

### Un cut garde son id, sinon deux choses cassent

`replace_cuts` supprimait la totalité des cuts et les réinsérait, donc leurs id
changeaient à chaque sauvegarde. Deux conséquences, toutes deux mesurées le 2026-08-21 :

1. **`render.cut_id` est une vraie clé étrangère.** Supprimer un cut sur lequel un rendu
   pointe échoue en `FOREIGN KEY constraint failed`, donc en 500. Le bug existait déjà :
   stabiliser une zone puis en marquer une autre suffisait à le déclencher.
2. `dispatch._prepare_render` relit le cut par son id pour fabriquer le `trim_ranges_ms`.
   Un id périmé donnait `cut N not found`.

Maintenant `CutIn` porte un `id` optionnel : présent, c'est une mise à jour ; absent,
une création ; ce que l'appelant ne renvoie pas est supprimé. Un id qui appartient à une
autre séquence n'est **pas** adopté.

Pour les rendus d'un cut qu'on supprime, deux traitements et non un :

| état du rendu | traitement | pourquoi |
|---|---|---|
| `done` / `failed` | `cut_id = NULL` | le fichier vaut quelque chose, seul le lien part |
| `queued` / `running` | rendu supprimé | son sujet a disparu, et un job en vol s'arrête au battement suivant |

Détacher un rendu en file serait le désastre silencieux : un `cut_id` nul veut dire « le
rush entier » pour `prepare`, donc un rendu de dix secondes reviendrait long de quatre
minutes.

### Le numéro de frame avait deux menteurs

Bug rapporté le 2026-08-21 : avancer image par image demandait souvent deux clics pour
une frame, puis oscillait entre deux, et cliquer sur le début d'une sequence sautait au
bon endroit avant de revenir en arrière. Deux causes, mesurées séparément, aucune dans
le fichier : les PTS du proxy sont exactement à `N x 1001/60000` avec un `start_time` à
zéro, vérifié à l'`ffprobe`.

**`requestVideoFrameCallback` rapporte `mediaTime` arrondi à la microseconde.** La frame
4 d'un flux 60000/1001 revient donc à 0,066733 s au lieu de 0,06673333, soit **3,99998
frames** : un `floor` tombe sur 3. La tolérance de `secondsToFrame` était de 1e-6 frame,
trente fois trop petite pour cet arrondi. À 1e-3 frame (une milliseconde) elle le
couvre, et reste mille fois trop petite pour laisser passer la frame suivante. C'est ce
qui produisait le double clic : le seek posait N, le callback écrasait par N-1, donc le
clic suivant redemandait N.

**Et le callback arrive après le seek qu'il précède.** Il est planifié pour la peinture
suivante, donc une frame décodée avant un seek peut atterrir après lui et remettre le
playhead d'où il venait. Mesuré sur 20 clics rapides sur « frame suivante », quatre
passes : **18, 19, 20 et 20 frames** avec la seule tolérance corrigée, **20 les quatre
fois** quand le callback ne parle qu'en lecture. D'où la règle : **en pause la frame est
celle qu'on a demandée, en lecture celle que la vidéo affiche**.

Le seek reste au **milieu** de la frame visée (`N + 0,5`), sans quoi l'arrondi du
décodeur retombe sur la précédente. Contre-épreuve que le compteur ne mentait que sur le
numéro : capture du canvas après un saut à la frame 6645, PSNR de **45,9 dB** contre
cette frame extraite par ffmpeg et **32,5 dB** contre chacune de ses deux voisines.

### « Dérushé » est une marque, jamais un compte

Un rush porte un booléen `derushed` qu'on pose à la main, depuis le bouton de l'entête
du derush, et qui se voit dans l'arbre (le nom passe en gris, une coche à droite de la
durée). **Il ne se déduit pas du nombre de sequences** : un rush qui ne valait rien est
dérushé dès qu'on l'a regardé, et il n'a aucune sequence pour le prouver. C'est le
compagnon des deux icônes ci-dessous : ensemble ils disent ce qu'il reste à faire.

La colonne est non nullable avec un défaut constant, donc l'auto-migration la crée avec
son `DEFAULT` et remplit les lignes existantes. Vérifié au redémarrage :
`Column added: sequence.derushed`, et zéro `NULL` derrière.

### Renommer un rush, et depuis où

Le nom d'un rush est celui de sa première part, or **un rush peut être plusieurs
fichiers** : la colonne de la page import porte donc le label en tête et les fichiers
sources en dessous, en petit. La redondance quand personne n'a renommé est assumée, elle
est le prix d'une hiérarchie lisible (voici le rush, voici ce dont il est fait).

Le renommage passe par `components/RenameDialog.tsx`, partagé avec la liste des
sequences du derush. Le champ vit dans un enfant monté avec le dialogue, sinon un nom
tapé puis abandonné revient à la réouverture suivante.

### Ce que l'arbre de gauche montre d'un rush

Un rush qui porte des sequences se déplie, et chaque sequence affiche **deux icônes,
allumées ou éteintes** : un éclair pour « une stab existe », une goutte pour « une stab
étalonnée existe ». Pas la palette, déjà l'icône du recoloriage d'un dossier dans le
même arbre. Les deux drapeaux (`rendered`, `graded`) sont calculés par l'API sur
`CutOut` plutôt que recoupés côté front, et ils comptent **un fichier produit**, pas un
travail demandé : un rendu en cours laisse l'icône éteinte.

Le détail n'est demandé qu'au dépliage, sous la clé `["sequence", id]`, celle de la page
derush : ouvrir un rush là-bas et le déplier ici ne coûte qu'une requête pour les deux.

## Les templates Gyroflow : sept réglages sur les quatre-vingt-dix

Un template est un **projet Gyroflow partiel** (`data/templates/*.json`), passé en
`--preset`. Pour savoir ce que Gyroflow comprend vraiment, on ne devine pas : `gyroflow
<fichier> --export-project 1` écrit le projet complet, et c'est de là que sortent les
défauts de `GYROFLOW_DEFAULTS` (mesuré sur 1.6.3, format version 3, quatre-vingt-dix
champs).

Sept sont éditables dans l'interface, choix de florian le 2026-08-21 : dimensions, codec,
débit, `smoothness`, `horizon_lock_amount`, `lens_correction_amount`, `fov` et le
`adaptive_zoom_center_offset` sur ses **deux** axes (un 16:9 pris dans du 4:3 recadre
verticalement, un 9:16 recadre horizontalement : n'exposer que Y aurait rendu le vertical
inutilisable).

Écartés, et pourquoi : la **méthode de lissage** (chaque méthode a ses propres
`smoothing_params`, donc un formulaire qui change de forme), tout le bloc
`synchronization` (le gyro DJI arrive déjà synchronisé), `frame_readout_time` (déduit du
profil d'objectif), les `additional_rotation/translation`, `background_*` (ne sert que
si on dézoome hors cadre), `video_speed` (change la durée), `use_gpu` (verrouillé à
`false`, voir plus haut), `interpolation`, `pad_with_black`, l'audio.

**Un enregistrement patche, il ne réécrit pas.** Le fichier porte des choses que le
formulaire ne montre pas (`smoothness_pitch/yaw/roll`, `adaptive_zoom_window`,
`max_zoom`, `encoder_options`) et les réécrire depuis le formulaire les perdrait.
`smoothness` en particulier vit dans une **liste de `{name, value}`**, pas dans un objet.

**L'aspect est calculé, jamais stocké.** Il était dans `$meta` et pouvait contredire les
dimensions écrites à côté. Retiré des deux templates livrés.

**Un template livré ne se supprime pas, il se réinitialise.** Son fichier est dans
l'image ; `seed_templates()` le recopie dans `data/templates` s'il manque, donc supprimer
la copie éditée **est** le retour à l'original, et l'interface le dit (l'icône est une
flèche de retour, pas une poubelle). Ceux créés à la main se suppriment pour de bon.

**Le décalage de cadre est une fraction, et il est violent.** Vérifié par deux rendus
réels du clip de bench en 1080x1920 : entre `[0.0, 0.0]` et `[-0.5, 0.0]`, la même frame
montre une partie complètement différente de la source. D'où le pas de 0,05 sur le
slider. Les bornes dures de l'API sont -1 à 1 ; Gyroflow écrête de lui-même au bord du
recadrage possible.

**Ni surcharge par rendu, ni par sequence** : un rendu prend le template tel quel, et le
champ `overrides` de l'API reste inutilisé. Deux variantes d'un look se font en
dupliquant un template, ce qui a l'avantage qu'un rendu se reproduit à l'identique.

Deux détails de l'API : `output_width` / `output_height` doivent être **pairs** (le 4:2:0
sous-échantillonne la chroma par deux, x264 refuse une hauteur impaire), et il n'existe
pas de « défaut Gyroflow » pour les dimensions ni le débit, que Gyroflow dérive du
fichier source. Le bouton « Gyroflow defaults » ne les touche donc pas.

## L'auto-migration doit remplir, pas seulement ajouter

`db.py` compare le schéma déclaré à la base et ajoute les colonnes manquantes en
`ALTER TABLE ADD COLUMN`, sans Alembic. Le piège coûte un démarrage : **sans clause
`DEFAULT`, SQLite remplit les lignes existantes avec `NULL`**, et une colonne que le
modèle déclare entière se relit alors à `None`. L'échec ne se voit pas à l'écriture mais
à la sortie, en `ValidationError` sur le schéma de réponse, donc en 500 sur une route
qui n'a rien à se reprocher. Vu le 2026-08-20 avec `folder.position`.

Corrigé en deux temps : la colonne est créée avec son défaut scalaire, **et** un passage
de remplissage met à jour les `NULL` restants de toute colonne non nullable qui a un
défaut constant, ce qui répare une base déjà abîmée par la version d'avant, au simple
redémarrage.

Deux détails : le littéral est rendu par SQLAlchemy en le liant **au type de la
colonne**, sinon un membre d'enum n'a aucun rendu (`No literal value renderer is
available for literal value <GradeState.DRAFT: 'draft'>`, qui empêchait l'API de
démarrer) et une chaîne partirait sans guillemets. Et un défaut **appelable**
(`created_at`) ne peut pas s'écrire en DDL : la colonne reste nulle sur les lignes
antérieures, et c'est assumé.

## Conventions

- Les marks de derush sont stockés en **numéros de frame**, jamais en ms : à
  60000/1001 fps l'arrondi dérive de plusieurs images sur un rush de 4 minutes.
  Conversion au dernier moment, dans `framing.py`.
- Les valeurs techniques d'une séquence (durée, fps, frame_count) sont celles
  **mesurées sur le fichier fusionné**, pas la somme des estimations des parts.
- SQLite perd les fuseaux : normaliser avec `timeutil.as_utc()` avant toute
  comparaison de dates relues (cf. `BaseSchema` côté API).
- Un job planté ne doit jamais tuer le worker : `worker.run_job` isole et
  rapporte l'échec au dispatcher.
- Front : shadcn/ui de base uniquement, composants copiés dans
  `frontend/src/components/ui/`.
- **Fin de tâche = commit puis push sur `master`.** Ni branche ni PR sur ce projet :
  dès qu'une tâche est terminée *et vérifiée* (tests passés, build ou service
  relancé quand c'est pertinent), committer et pousser directement. L'autorisation
  est donnée une fois pour toutes, inutile de la redemander à chaque fois.

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
