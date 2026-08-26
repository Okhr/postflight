# postflight

**Renommé le 2026-08-26**, de `video-stab` à **postflight** (`PostFlight` à l'écran).
Ce qui a bougé avec : le préfixe d'environnement `VS_` → `PF_`, les images
(`postflight-api` / `postflight-worker`, `ghcr.io/okhr/postflight-*`), le dépôt GitHub
(`Okhr/postflight`, l'ancienne URL redirige), le préfixe `localStorage` et le nom du
fichier SQLite. `db._adopt_legacy_db` reprend une base restée sous l'ancien nom, **et
déplace les trois fichiers ensemble** : le `-wal` porte des transactions commitées que
le fichier principal n'a pas encore, donc l'emmener seul revient à perdre les dernières
écritures en silence.

Chaîne de traitement des rushes FPV : surveillance d'un dossier réseau → fusion
des enregistrements découpés → derush dans une interface web → stabilisation
Gyroflow. Deux images issues d'un seul Dockerfile (`--target api`, `--target
worker`), déployables sur Portainer.

**Langues** : le code, les commentaires, les docstrings, les messages de log,
tout le texte de l'interface et **le README** sont **en anglais** (le README depuis le
2026-08-26 : c'est la vitrine publique du dépôt). Ce fichier et les réponses en
conversation restent **en français**.

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
  Le premier qui sort en 0 gagne ; `PF_HWACCEL` peut en épingler un. Un timeout
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

L'aperçu était une vraie image ffmpeg, sans parité à maintenir. **Changé le
2026-08-23**, choix de florian : « ça serait pas mal quand même des filtres frontend,
on a pas moyen d'avoir un truc good enough ». Voir la section suivante, l'image fixe
est retirée.

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

### Point noir et point blanc sont deux curseurs, pas un bouton

Demandé par florian le 2026-08-25 : « on va ajouter les points noirs et points blancs
comme curseurs, le bouton il faut le renommer et il va bouger ça, mais on va séparer les
2 curseurs et les mettre en haut au-dessus d'un séparateur parce qu'ils vont pas pouvoir
être move to another clip ».

Ça change la nature du geste. Avant, `auto_levels` était un booléen et la décision se
prenait **au moment du rendu**, invisible : le serveur mesurait le clip, choisissait, et
la sortie l'appliquait. Maintenant `black_point` et `white_point` sont deux paramètres
comme les autres, en fraction de la plage légale (0 et 1 = on ne touche à rien), et le
bouton (« Measure this clip ») **écrit dedans**. Ce qui était un raisonnement caché
devient une valeur qu'on voit et qu'on corrige.

Le partage du code suit cette frontière :

- **`levels(values)`** est de l'arithmétique sur ce que disent les curseurs, donc elle
  existe **des deux côtés** (`grading.levels` et `levelsOf` dans le shader) : l'aperçu
  doit suivre un curseur pendant qu'il glisse, un aller-retour n'est pas envisageable.
- **`suggest_levels(analysis)`** est le jugement (quel côté écrête déjà, reste-t-il assez
  de plage inutilisée pour valoir la peine) et il n'existe **qu'au serveur**, publié en
  `GradeOut.suggested`. Le bouton n'écrit que ce qu'il renvoie.

**Ils ne voyagent pas.** Le dialogue « Copy to » conserve les points de chaque cible :
une plage inutilisée ici est de l'image sur le plan d'à côté. C'est ce qui justifie le
séparateur, et la seule raison pour laquelle la carte a deux groupes.

**Un reset par groupe, pas un pour tout** (florian, le 2026-08-25). Les deux groupes
sont deux décisions : la petite flèche à côté de « Auto range » rend la pleine plage,
« Reset » sous le titre Look rend un look neutre, et ni l'un ni l'autre ne touche au
groupe voisin. Chacun est mort quand son groupe est déjà chez lui. Contre-épreuve :
point noir à 20 % et 7100 K, reset des points, la température ne bouge pas ; reset du
look, les points ne bougent pas.

**Chaque curseur porte son échelle et sa sortie de secours.** Sous la piste, une ligne
de trois nombres : les deux bornes aux extrémités, et le défaut **sous sa propre
position**. Il disparaît quand il coïncide avec une borne, ce qui est le cas des deux
points. Les nombres y sont nus : l'unité est déjà dite une fois, dans la valeur
au-dessus.

**Toutes les plages sont symétriques autour de leur défaut**, y compris 3000-10000 K
dont le milieu est pile 6500. Le contraste allait de 0,5 à 1,6 et faisait exception :
« le fait que le défaut du contraste est pas au centre c'est chelou » (florian, le
2026-08-25). Passé à 0,3-1,7, donc symétrique et plus large des deux
côtés qu'avant, sachant qu'à 1,3 les deux extrémités écrêtent déjà (mesuré : 16 tombe à
0 et 235 monte à 255) : le haut de la plage est expressif, pas précis.

Un **cran sur la piste** a été essayé d'abord et abandonné : dessiné sur la piste il
passe derrière la partie remplie de la barre et disparaît pour toute valeur au-delà du
défaut, et aucune couleur unique ne se lit à la fois sur le blanc du remplissage et sur
le gris sombre du reste.

La **flèche de retour à côté du libellé n'apparaît que sur un curseur déplacé**, ce qui
en fait à la fois le chemin du retour et la marque « celui-là a bougé ». Le bouton
`Reset` global reste, pour tout remettre d'un coup.

Sur un clip qui n'a rien à récupérer, le bouton est **désactivé** et le dit au survol.
Cas réel mesuré le 2026-08-25 : 90 % des images contiennent du noir vrai et 43 % du blanc
vrai, donc les deux côtés sont bloqués et il n'y a pas de proposition. Les curseurs
restent, eux.

### La profondeur de bits a cassé l'analyse, sans rien dire

`signalstats` rapporte dans la profondeur de la **source**, or toutes les constantes ici
sont en plage légale 10 bits (64-940). Tant que les rendus sortaient en 10 bits ça
coïncidait. Le jour où ils sont passés en 8 bits (voir le GOP ouvert plus bas), le même
plan a mesuré `y_high` à **183** sur une échelle 64-940, avec « 100 % des images qui
écrêtent le noir », et le bouton s'est tu sans que rien ne signale d'erreur.

Corrigé en convertissant **avant** de mesurer (`format=yuv420p10le` devant `signalstats`)
plutôt qu'en lisant la profondeur : les nombres veulent alors dire la même chose quelle
que soit la source. Contre-épreuve : la même mire encodée en 8 et en 10 bits mesure à
moins de 20 niveaux près, là où l'erreur d'échelle était d'un facteur quatre.

Deux détails qui ont failli me coûter une heure de plus. L'analyse est **mise en cache**
dans `grade.analysis` et ne se refait pas, donc une base déjà mesurée garde ses chiffres
faux : il faut vider la colonne. Et la source de test `gradients` de ffmpeg **se
réamorce au hasard** à chaque appel : mes deux encodages ne contenaient pas la même
image, et la comparaison ne prouvait rien.

### Color : un arbre, aucun bouton Save, et deux gestes séparés

Refonte du 2026-08-24, sur le constat de florian : « c'est tout pété ça marche, et ça
respecte pas le naming ». La page marchait, elle **parlait une autre langue** : son titre
et sa liste disaient `DJI_20260809144616_0034_D__h_1080__c00.mp4` et `h_1080` sous un
arbre qui dit « Rush 1 » et « dive ». Même défaut que Stabilize avant-hier, au même
endroit du raisonnement (un recoupement côté front à partir d'un nom de fichier).

Elle a maintenant la forme de Stabilize, parce qu'elle répond à la même question un cran
plus loin :

- **L'arbre groupé** (dossier, rush, clip) à la place de la liste plate, chaque clip
  nommé « sequence · profil », le profil en badge dans l'entête. Depuis le 2026-08-25 il
  descend deux crans plus bas (profil, puis grade) et le nom concaténé a disparu avec ça. Deux champs de plus sur
  `RenderOut` suffisent : `folder_id` pour grouper, et `duration_ms`, que la page
  calculait **à 60 fps en dur** sur un rush en 60000/1001.
- **Aucun bouton Save.** Chaque curseur écrit **au relâchement** (`onValueCommit` de
  Radix), comme le derush. Mesuré : zéro requête pendant le glisser, une au relâchement.
- **« Copy to »** ouvre le même arbre avec des cases à trois états et écrit le look sur
  les clips choisis. Il **n'encode rien** : régler un look et dépenser des minutes
  d'encodage sont deux décisions, et florian les veut séparées (« je règle un clip et je
  copie sur les autres »).
- **Le lot est un bouton en tête de la colonne**, « Render N looks », qui prend tous les
  looks réglés dont le fichier n'existe pas. Une requête par clip, comme le lancement de
  Stabilize.
- **La carte « Measured » a disparu** au profit de l'histogramme, passé en bande pleine
  largeur sous les contrôles. C'était trois lignes de prose sur des mesures que la courbe
  montre, et la règle de ce projet dit qu'une carte porte des données, pas un paragraphe.
- **Une raison au survol, jamais en prose.** « Neutral look, nothing to apply. » sous un
  bouton mort est devenu son `title`. Attention : un bouton désactivé ne reçoit pas
  d'événement de pointeur (`disabled:pointer-events-none`), donc le `title` va sur un
  `span` autour.

### Un grade est un niveau, pas un attribut du clip

Demandé par florian le 2026-08-25 : « j'aimerais que le fait de grader d'une certaine
manière ça soit juste un niveau de plus dans la hiérarchie rush/sequence/profile/grade,
ce qui fait qu'on pourrait avoir plusieurs grading en parallèle ». Avant ça
`grade.render_id` était **unique** : un clip portait un look, écrasé sur place.

Six conséquences, dont trois qui n'avaient rien d'évident :

- **Un grade porte un nom**, sans quoi deux feuilles de l'arbre seraient
  indistinguables. Par défaut le premier « Grade N » libre (pas un compte : un grade
  supprimé au milieu rend son numéro), renommable, et le nom est **la clé d'écriture**
  (voir « Copy to » plus bas). Le défaut du modèle est `"Grade 1"`, ce qui sert aussi de
  remplissage à l'unique grade sans nom que chaque clip avait avant.
- **L'analyse a remonté d'un niveau, sur `render`.** Elle mesure le **clip**, pas le
  look : `signalstats` sur un plan de 30 s coûte quelques secondes, et la garder sur le
  grade l'aurait fait tourner une fois par look. `GradeOut.analysis` et `suggested` sont
  donc les mêmes pour tous les grades d'un clip. La colonne `grade.analysis` reste en
  base, vestigiale, l'auto-migration n'enlevant jamais rien ; les valeurs sont
  recalculées à la première ouverture de chaque clip.
- **Le nom du fichier porte l'id du grade** (`..._c00__g4__d53ea42efc.mp4`). Le hash rend
  gratuit le retour à un look déjà produit ; l'id empêche **deux grades réglés
  exactement pareil de partager un fichier**, ce que supprimer l'un aurait retiré à
  l'autre. Vérifié en rendant le même look sur deux clips : deux fichiers, même hash,
  ids différents.
- **`DELETE /grades/{id}` supprime le grade, `DELETE /grades/{id}/file` seulement sa
  sortie.** Deux gestes, parce qu'un grade est maintenant quelque chose qu'on a nommé :
  jeter cent mégaoctets ne doit pas jeter le look qui les a produits. Le premier vit sur
  la ligne de l'arbre, le second dans la carte « Graded file ».
- **« Copy to » écrit par nom.** Chaque cible reçoit un grade portant le nom de la
  source, créé s'il n'existe pas, écrasé s'il existe : appuyer deux fois ne fait pas de
  doublon, et ce que la cible a réglé sous un autre nom n'est pas touché. C'est
  exactement `POST /renders/{id}/grades`, la même route que le « + » de l'arbre (sans
  nom, il invente le prochain « Grade N »).
- **L'index unique se démonte à la main.** SQLite ne modifie pas un index en place et
  `create_all` ne touche jamais un index existant : `db._relax_grade_render_index`
  supprime `ix_grade_render_id` et le recrée simple. Nommé et non générique, parce que
  c'est un changement de schéma qui a eu lieu une fois. Vu au démarrage : `Column added:
  grade.label`, `Column added: render.analysis`, `Index relaxed`.

**La barre de progression d'un grade était morte.** Rapporté par florian le 2026-08-25 :
le battement de cœur recopiait la progression du job sur le `render` et **pas sur le
`grade`**, donc `grade.progress` passait de 0 à 1 d'un coup et la carte sous l'éditeur
affichait une barre vide pendant tout l'encodage. Corrigé dans `heartbeat`, avec un test
qui écrit 0,42 et le relit sur la ligne. Au passage, le titre de la carte suit l'état
(« Encoding », « Waiting for a worker », « Failed », « Graded file ») : il disait
« Graded file » au-dessus d'une barre vide alors que le fichier n'existait pas encore.

**L'analyse se mesure à l'ouverture d'un grade, pas seulement de la liste.** Régression
introduite en déplaçant l'analyse sur le `render` puis rapportée par florian (« où sont
passés les boutons pour aller à la frame la plus sombre ? ») : elle ne tournait que dans
`GET /renders/{id}/grades`, que la page n'appelle pas. Sans analyse, pas de
`darkest_ms`, donc pas de boutons Darkest / Median / Brightest et pas de `suggested`.
`GET /grades/{id}` la mesure donc aussi, ce qui est la route avec laquelle l'éditeur
s'ouvre.

**La goutte suit l'état, pas le nom de fichier.** Un grade retombé en `draft` après un
changement de réglage garde son fichier sur le disque (réutilisable si les curseurs
reviennent exactement dessus), et ce fichier n'est **pas** ce look : la ligne ne l'affiche
donc que sur `done`. Défaut vu sur une capture avant correctif, pas déduit du code.

**Deux encodages du même clip tiennent en parallèle**, mesuré de bout en bout : deux
grades lancés, l'un `running` l'autre `queued`, deux fichiers de 103 et 107 Mo, puis les
deux supprimés avec leurs grades sans toucher aux voisins.

#### Écraser un réglage demande confirmation

Le tampon (poser un look sur le grade ouvert) et la disquette (écrire le réglage courant
dans un look) détruisent des réglages trouvés à l'œil : les deux passent par un dialogue
(florian, même jour). Seulement quand il y a quelque chose à perdre, et le dialogue **dit
les deux états** :

> Apply "Golden hour" to "Sunset" ? It holds 7100 K, which is replaced by contrast 1.08 ·
> sat 1.34 · 5800 K · shadows -0.10. Its black and white points stay.

Un geste qui ne changerait rien n'ouvre rien : le bouton est **désactivé** et le dit au
survol (« This grade already holds it »). `DeleteDialog` a gagné un mot de bouton
paramétrable pour ça, parce qu'écraser n'est pas supprimer.

#### Les deux arbres ont la même anatomie

« On va en profiter pour harmoniser un peu à quoi ressemblent les arbres dans stab et
color » (florian). Ils avaient divergé sur tout ce qui se voit, mesuré avant correctif :
indentation de **16 px contre 12**, `gap-2` contre `gap-1.5`, survol sur toutes les lignes
contre survol des feuilles seulement, ligne de rush cliquable avec chevron contre texte
muet sans chevron, pliage partagé contre `useState` local par dossier.

`components/tree.tsx` livre donc les **primitives** (`INDENT`, `rowClass`, `Indent`,
`Twisty`, `Dot`, `Meta`), pas un arbre configurable : les deux pages ont deux métiers, et
ce qui diffère est **quel niveau est la feuille**. Stabilize s'arrête à la sequence et
résume les profils en badges sur sa ligne (c'est une file de ce qui manque) ; Color
descend en lignes profil puis grade (c'est un éditeur de ce qui existe). Le badge de
profil est le même composant des deux côtés, donc un profil se reconnaît où qu'il soit.
Mesuré après : `gap: 8px`, `padding: 4px`, `font: 14px`, indentation par pas de 16 px,
identiques sur les deux pages.

Un arbre unique et configurable a été écarté : il faudrait lui passer quel niveau est
sélectionnable, quelles cases, quelles actions à droite, ce qui est un sac de réglages
pour prétendre que deux interactions sont une seule.

**Une feuille réserve la place du chevron qu'elle n'a pas.** Rapporté par florian (« la
ligne des grades a l'air plus à gauche que son parent ») et vrai aux pixels : le nom d'un
grade commençait à **x=416** sous un profil à **x=429**, et sur Stabilize « chemin » à 433
sous « Maison 1 » à 437. Une ligne sans enfants n'a pas de chevron, donc son nom
remontait de 20 px et un cran d'indentation n'en fait que 16. `<Twisty />` sans `open`
dessine donc le vide de la bonne taille. Mesuré après : 436 sous 429, et 453 sous 437,
soit un cran complet. Ce défaut existait sur les deux pages avant l'harmonisation.

Restent deux écarts de 4 px, qui sont normaux : le nom d'un dossier est poussé par sa
pastille et celui d'un profil par le fond de son badge. L'œil s'aligne sur la marque
visible, pas sur le glyphe, et depuis la pastille comme depuis le bord du badge le cran
fait bien 16 px.

### Étalonner, changer un réglage, réétalonner

Question de florian le 2026-08-25, et les trois cas ne se comportent pas pareil. Vérifié
en le faisant, pas en lisant le code :

| geste | ce qui se passe |
|---|---|
| réétalonner **sans rien changer** | rien n'est réencodé. Le nom du fichier porte le hash du look, le worker le trouve, répond `reused` et le job se termine en une seconde |
| **changer un réglage** | le grade retombe en `draft`, un encodage en vol est annulé, et le fichier précédent **reste** : il ne correspond plus au look, donc l'interface ne le montre plus |
| **réétalonner ensuite** | un nouvel encodage, et le fichier remplacé est maintenant supprimé |

Cette dernière ligne est neuve. Le nom porte le hash du look pour que **revenir à un look
déjà produit soit gratuit**, et le prix en était cent mégaoctets par look jamais essayé,
inatteignables depuis n'importe où dès que la ligne pointe ailleurs. Mesuré sur le vrai
volume avant correctif : **181 Mo sur 393**, après deux changements de réglage dans
l'après-midi. `_apply_grade` supprime donc le fichier qu'il remplace, et seulement s'il
diffère (sinon il supprimerait la réponse que le worker vient de réutiliser).

Ce qui reste assumé : un look changé et jamais réétalonné garde son fichier, invisible
mais réutilisable si on revient exactement dessus. Le supprimer au changement de réglage
serait pire, la page écrivant à chaque relâchement de curseur.

### Changer le look annule l'encodage en vol

Trouvé en testant la page, pas par raisonnement : j'ai réglé un contraste, lancé le
rendu, puis remis à zéro, et le worker a continué à encoder **un fichier neutre** à 24 %.
`save_grade` ne remettait à `draft` que depuis `done` ou `failed`, donc un job en vol
survivait à un changement de look et écrivait le look d'avant.

Corrigé comme ailleurs : le job part et le worker s'arrête au battement suivant. Le
garde-fou qui compte est l'autre moitié : **on ne compare pas les états mais les
hashes**, et un `PUT` qui ne change rien ne touche à rien. Sans ça, une page qui écrit à
chaque relâchement de curseur tuerait un encodage au premier curseur relâché sur sa
propre valeur.

### Les looks : une bibliothèque, et une porte d'entrée rapide

Demandé le 2026-08-25, « une gestion des looks comme on a une gestion des profils dans
stab », et florian a tranché pour **un mélange** : la carte pour créer et voir, un geste
rapide pour en créer un depuis le clip qu'on vient de régler.

**Une table, pas des fichiers JSON**, contrairement aux profils Gyroflow : ceux-là sont
de vrais fragments de projet que Gyroflow lui-même relit, un look n'est que six nombres
et un nom. La carte est en haut de la page, comme les templates sur Stabilize, avec
quatre actions par ligne : peindre sur le clip ouvert, écrire le réglage courant dans
cette ligne, renommer, supprimer.

**Un look ne porte jamais les points noir et blanc.** Le serveur les jette à l'entrée
(`grading.travelling`), donc ni la carte ni le dialogue n'ont à se souvenir de la règle,
et un look posé sur dix clips ne peut pas y transporter la plage d'un seul. Contre-épreuve
dans le navigateur : point noir à 20 % sur le clip cible, look à 7100 K appliqué, la
température voyage et le point noir ne bouge pas.

**Appliquer est une écriture, pas un état partagé.** La carte est au-dessus de l'éditeur
et ne lui demande rien : ce qu'un clip porte est déjà dans les grades chargés par la
page, et l'éditeur voit la nouvelle valeur parce que sa propre requête est invalidée.
C'est ce qui évite de remonter l'état transitoire des curseurs d'un cran.

**Supprimer un look ne touche aucun clip**, et le dialogue le dit : appliquer copie des
nombres, donc rien ne pend du look. C'est la seule suppression de ce dépôt qui ne
cascade pas, et pour une bonne raison.

### Les instruments : quatre, tous débrayables

Brainstorm du 2026-08-25, parti d'un constat de florian : « l'hist est pas très parlant
c'est quoi exactement ». Il ne l'était pas, et pour cinq raisons cumulées : luma seule
(donc muet sur une dominante et sur le canal qui écrête le premier), aucun repère aux
extrémités, **normalisé sur sa classe la plus haute** (donc un plan de ciel plat et un
plan équilibré se ressemblent), 64 classes seulement, et aucune information de position.

Quatre instruments à la place, tous des bascules mémorisées (`color.scopes`) parce qu'on
règle ses instruments une fois puis on travaille :

| instrument | ce qu'il répond |
|---|---|
| **écrêtage sur l'image** | quoi est brûlé, et **où**. Rouge au blanc, bleu au noir |
| **histogramme R/V/B** | la dominante, et quel canal meurt d'abord |
| **waveform** | où se trouvent les sombres et les clairs dans le cadre |
| **chiffres du plan** | % de pixels écrêtés, min, moyenne, max |

**Pas d'onglets**, choix de conception : lire l'histogramme *et* la waveform en même
temps est la raison d'avoir les deux, et en cacher un derrière l'autre transforme un
coup d'œil en aller-retour. Les scopes allumés s'empilent, chacun sur toute la largeur
de sa colonne.

#### Ils vivent dans la colonne des réglages, épinglés

Ils étaient dans une bande sous l'image, et florian les a trouvés illisibles le
2026-08-25. Mesuré avant de bouger quoi que ce soit, en 1600x900 : la page a **quatre
colonnes** (barre latérale 320, clips 320, image 560, réglages 304), donc chaque scope
héritait de **267x80 px**, et la main était sur un curseur à x=1424 quand l'œil devait
lire un scope à x=835.

Quatre placements ont été chiffrés avant de choisir : colonne de droite (2x l'aire),
bande pleine largeur sous tout (4x, mais sous la ligne de flottaison), surimpression sur
l'image (aire libre, un coin caché), replier la liste des clips (2,4x). **Choix de
florian : la colonne de droite**, et c'est la seule qui règle aussi la diagonale du
regard.

La carte est **épinglée** (`xl:sticky`) parce que la colonne est plus haute que la
fenêtre : sans ça les derniers curseurs se tirent avec les scopes sortis par le haut.
Mesuré après 342 px de défilement : histogramme à y=65, waveform à y=153, curseur
Highlights à y=709, les trois visibles ensemble.

Deux conséquences. Le bouton **Compare passe en icône seule** (son libellé faisait
passer la barre du lecteur sur deux lignes dans une colonne rétrécie ; le `title` disait
déjà « Hold to see it ungraded »). Et l'échantillon voyage par une **poignée impérative**
(`ScopeSink`), pas par un prop : une image atterrit par présentation, et la page ne doit
pas se re-rendre autour.

**Les lignes de bout de graphe sont sorties** (florian, même jour) : elles marquaient
l'écrêtage à chaque extrémité, et le bord de la toile le dit déjà. Les scopes sont passés
à 96 px de haut au même moment, 80 aplatissant tout ce qui n'était pas le pic.

#### La colonne du milieu ne paie plus tout

Deux corrections du même défaut, le 2026-08-25 : **l'image est la seule chose flexible de
cette page**, donc elle encaissait 100 % de ce que la fenêtre perdait, sous deux colonnes
fixes qui n'en perdaient rien.

| largeur de fenêtre | image avant | image après |
|---|---|---|
| 2560 | 1156x650 | 1156x650 (plafonnée par `65vh`) |
| 1920 | 782x440 | **1094x615** |
| 1600 | 462x260 | **838x471** |
| 1440 | 302x170 | **694x390** |
| 1300 | **162x91** | **554x312** |

Ce qui a changé, dans cet ordre :

- **l'arbre des clips est passé sous l'image** (florian) au lieu d'occuper une colonne. Il
  coûte un défilement quand on change de clip, ce qui arrive une fois par clip, et il rend
  à l'image la largeur d'une colonne entière. Son bouton de lot est monté dans la ligne de
  titre : pleine largeur sous l'image, c'était un bouton primaire de 900 px.
- **la colonne des réglages est en `clamp(21rem, 22vw, 26rem)`**, donc elle rétrécit avec
  la fenêtre au lieu d'être fixe, et sans à-coup au redimensionnement (des paliers de
  breakpoint faisaient sauter l'image de 111 px pour 1 px de fenêtre).

Deux détails techniques qui comptent :

- **un seul relevé de pixels** alimente les trois scopes hors image : une copie réduite
  en 256x144, soit 36 000 pixels, lue dans la même tâche que le dessin (avant que le
  drawing buffer soit effacé). Rien n'est calculé pour un instrument éteint.
- **l'écrêtage n'est pas déduit de ces chiffres**, il tourne par pixel dans le shader, à
  pleine résolution. Vu sur une vraie image : le relevé annonçait un maximum de 234
  alors que l'overlay trouvait encore des pixels au plafond, la réduction ayant moyenné
  un pixel brûlé isolé. L'overlay est l'instrument exact, les chiffres un échantillon.
- **et c'est justement pour ça qu'il fallait le dessiner deux fois.** Bug rapporté par
  florian le 2026-08-25 : l'overlay est peint par le shader **dans le buffer que le relevé
  lit ensuite**, donc les trois instruments comptaient son rouge et son bleu au lieu de
  l'image. Mesuré avec la contre-épreuve (ancien code, overlay allumé sur un plan à 79 %
  d'écrêtage) : la moyenne du plan tombait de **235 à 100**, le min de 64 à 53, le max de
  255 à 250, parce qu'un pixel peint en rouge a une luma de 84. Les chiffres étaient les
  plus faux : un pixel noir écrêté peint en bleu porte `b=255`, donc il comptait comme
  écrêté **en haut** et jamais en bas. Corrigé en dessinant la frame sans overlay, en
  relevant, puis en la redessinant avec : les deux dessins sont dans la même tâche, donc
  rien ne compose entre les deux et rien ne clignote. Après correctif, les quatre lectures
  (éteint, éteint, allumé, éteint) sont identiques au caractère près.

Le bouton des points s'appelle **« Auto range »** (florian, même jour), et le titre
« Look » est passé **sous** les deux points : ce qui est au-dessus du séparateur
appartient au clip, ce qui est en dessous voyage.

### L'aperçu en shader : une seconde implémentation, mesurée

Le modèle est celui de Resolve ou Lightroom : un aperçu GPU qui suit les curseurs, un
rendu final qui fait foi. Donc oui, la chaîne couleur existe deux fois, et le fichier
écrit vient toujours de ffmpeg.

**Les formules ont été trouvées par la mesure, pas dans la doc** : une mire de 256 gris
et huit patchs colorés passée dans chaque filtre, valeurs relues, candidats comparés.

| filtre | ce qu'il fait vraiment | écart de la reproduction |
|---|---|---|
| `exposure` | `out = in * 2^EV`, sur les valeurs encodées, sans linéarisation | 0,5 niveau |
| `eq` contrast | `(v - 0,5) * C + 0,5` en luma normalisée | 1,5 niveau |
| `eq` saturation | `(u - 128) * S + 128` sur la chroma | 1,6 niveau |
| `colortemperature` | un gain par canal RGB, sur les valeurs encodées | 1,7 niveau |
| `curves` | spline cubique naturelle sur les quatre points | 1,1 niveau |
| `lutyuv` | l'étirement de luma, que le serveur résout et envoie | exact |

**Le détail qui décide de tout : ffmpeg repasse par du 8 bits entre les filtres, donc
il écrête après chaque étage.** En gardant tout en flottant, le ciel divergeait de 18
niveaux (30,6 dB) ; en écrêtant à chaque étage, 39,1 dB et 2 niveaux d'écart moyen, deux
images indiscernables côte à côte.

**Le jugement reste au serveur, la formule est des deux côtés.** `GradeOut.suggested`
dit où les deux points iraient si on les mesurait ; l'étirement lui-même se calcule dans
le navigateur (`levelsOf`), parce que l'aperçu suit le curseur pendant qu'il glisse. Ce
qui ne doit pas exister deux fois est le raisonnement, pas l'arithmétique.

**Deux bugs de shader que seul le harnais a attrapés** : une LUT en `R32F` ne
s'échantillonne pas en `LINEAR` sans extension, et une texture incomplète se lit comme
zéro, donc la courbe noircissait toute l'image (5,9 dB) ; et à 6500 K l'approximation de
Tanner Helland ne vaut pas exactement `[1, 1, 1]` mais `[1, 0,996, 0,980]`, soit 2 % de
bleu appliqués à un moment où ffmpeg n'insère pas le filtre du tout.

**Le plancher de la mesure est le décodeur, pas le shader.** Comparé dans le navigateur
contre une frame PNG pleine résolution produite par le ffmpeg du conteneur :

| cas | PSNR |
|---|---|
| **contrôle, paramètres neutres** (le shader ne fait rien) | **29,7 dB** |
| exposition +0,4 EV | 31,0 dB |
| contraste + saturation | 27,2 dB |
| température 7400 K | 28,0 dB |
| courbe | 28,9 dB |
| auto-levels | 32,6 dB |
| tout ensemble | 33,2 dB |

Le contrôle neutre est la clé de lecture : à paramètres neutres le shader recopie la
texture, donc ces 29,7 dB sont l'écart entre **le décodeur de Chrome et celui de
ffmpeg**. Il est **affine** (mesuré : pente 0,945, offset +12,9), donc l'aperçu est
légèrement plus clair et moins contrasté que le fichier final. Tous les cas tombent à
±3 dB du contrôle : le shader n'ajoute rien à ce plancher.

**Le clip stabilisé était en H.264 10 bits** (`yuv420p10le`, Gyroflow suit la source),
et c'est devenu bien plus qu'un écart de mesure. Voir ci-dessous.

### Le lecteur que le seek tuait : un GOP ouvert

Rapporté le 2026-08-24, en trois observations qui décrivent exactement le défaut :
« des fois ça marche », « quand je touche aux boutons darkest et tout ça casse », « seul
un changement de clip répare », « le scrub casse aussi ». Lecture depuis le début : bon.
Darkest / Median / Brightest : ce sont des **seeks**. Le scrub aussi. Changer de clip
remonte un `<video>` neuf, d'où la réparation.

**La cause, mesurée en comptant les NAL** : le rendu de Gyroflow ne contient qu'**une
seule IDR**, la première image.

| fichier | images clés | dont IDR |
|---|---|---|
| rendu Gyroflow | 28 | **1** |
| proxy du derush | 31 | 31 |

Les 27 autres sont des I-frames d'un **GOP ouvert** : décoder à partir d'elles réclame
des images qui viennent après. ffmpeg s'en accommode en écrivant `mmco: unref short
failure` et continue ; Chrome refuse le paquet, `PIPELINE_ERROR_DECODE`, et le décodeur
est mort jusqu'au remontage de l'élément.

Réparé par une option d'encodeur, choisie en rendant quatre fois 8 s du même master :

| `encoder_options` | IDR | images clés |
|---|---|---|
| `-preset superfast` (avant) | 1 | 9 |
| `-preset superfast -flags +cgop` | **9** | 9 |
| `-preset superfast -x264-params open-gop=0` | 9 | 9 |
| `-preset superfast -g 60` | 1 | 9 |

`+cgop` est retenu : c'est un drapeau générique de ffmpeg, donc il vaut aussi pour HEVC,
là où `-x264-params` ne parle qu'à x264. Ajouté dans `build_preset`, et seulement si le
template n'a pas déjà fermé son GOP. Contre-épreuve sur un vrai rendu refait par la
chaîne complète : **28 images clés, 28 IDR**, et sur la page les trois marks atterrissent
en donnant trois images distinctes au canvas.

**Trois innocents, écartés par la mesure** avant d'arriver là : nos réponses Range sont
exactes à l'octet (comparées au disque sur quatre plages, dont une ouverte) ; les images
clés ne manquaient pas (28 sur 26 s) ; et le fichier n'est pas corrompu, ffmpeg le décode
de bout en bout avec un simple avertissement.

**Et une fausse piste que j'ai suivie une heure**, écrite ici parce qu'elle est
instructive : le premier fichier examiné était en H.264 **High 10** (`yuv420p10le`,
Gyroflow suivant la profondeur de la source), ce qui expliquait joliment le symptôme. Ça
n'était pas la cause : le fichier 8 bits produit ensuite échouait **exactement pareil**.
Mesuré après coup, Chrome lisait même le 10 bits depuis le début sans broncher. La leçon
tient en une phrase : **un mécanisme plausible qui colle au symptôme n'est pas une
cause**, et la contre-épreuve manquante était triviale, refaire le même essai sur un
fichier 8 bits.

**Le passage en 8 bits est gardé quand même**, pour une raison qui n'a rien à voir : un
livrable en High 10 se lit mal partout (navigateurs, QuickTime, plusieurs montages), et
personne ne demande dix bits à un H.264 destiné au partage. `output.pixel_format` est le
champ, vide par défaut, trouvé comme toujours par `--export-project 1` :

| `output.pixel_format` | sortie sur une source 10 bits |
|---|---|
| `""` (défaut) | `High 10 / yuv420p10le` |
| `"yuv420p"` | `High / yuv420p` |

Forcé pour H.264 seulement ; HEVC et ProRes gardent leur profondeur, et un template qui
nomme un format le garde.

À ne pas confondre avec le derush : ses proxys sont en `yuv420p` avec une IDR par
seconde, donc ce lecteur-là n'a jamais eu ce problème.

**Et un clip en H.265 n'est pas cassé, il est illisible ici.** Rapporté le 2026-08-25 sur
un profil « Instagram » : le fichier est du `hevc / Main 10 / yuv420p10le`, et Chrome sous
Linux n'a pas de décodeur HEVC. Le message disait « Render it again to get one it can
play », ce qui est un mauvais conseil : réencoder avec le même profil redonne le même
fichier. Le lecteur **demande donc au navigateur** (`canPlayType('video/mp4;
codecs="hvc1..."')`, mesuré : `''` pour hvc1 et `probably` pour avc1) plutôt que de
supposer, parce que le support HEVC de Chrome dépend de la plateforme. Quand la réponse
est non, la ligne le dit avant que le lecteur meure, nomme le codec et dit quoi faire
(étalonner un rendu H.264 de la même sequence). ProRes est traité pareil, et n'est lu par
aucun navigateur.

Conséquence assumée : **un clip HEVC ne s'étalonne pas dans cette interface**, faute
d'aperçu. La chaîne ffmpeg saurait le faire, mais régler un look sans le voir n'a pas
de sens.

Et la page le dit maintenant au lieu de figer : l'élément vidéo émet bien un événement
`error` (code 3), donc une ligne apparaît sous l'image. Un remontage automatique a été
écarté parce qu'il ne répare pas, mesuré.


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

Le nom d'un worker est son identité (`PF_WORKER_NAME`) et doit être **stable** :
le hostname d'un conteneur a l'air stable et ne l'est pas, il change à chaque
recréation et laisse une ligne `worker` orpheline derrière lui.

### Plusieurs jobs à la fois : entre workers, pas dans un worker

Question de florian le 2026-08-25. Les nombres, mesurés dans le code plutôt que
racontés :

| | valeur |
|---|---|
| types de job | `merge`, `proxy`, `render`, `grade` |
| ordre de la file | `ORDER BY priority, id`, priorités **1** (scan manuel), **5** (proxy), **10** (fusion), **50** (rendu), **60** (étalonnage) |
| cadence de demande d'un worker | 1 s (`POLL_INTERVAL_S`) |
| jobs simultanés **par processus worker** | **1** |
| bail / battement | 60 s, renouvelé toutes les 2 s |
| réessais | `MAX_ATTEMPTS = 3`, uniquement par expiration de bail |

**Le parallélisme est entre machines, pas dans une machine.** La boucle du worker est
`claim()` puis `run_job()`, qui bloque jusqu'à la fin : un processus ne tient qu'un job.
`PF_WORKER_CONCURRENCY` (défaut 1) est ce que le worker **déclare** au dispatcher, et
`available()` s'en sert pour savoir s'il a de la place ; la boucle, elle, ne sait pas
s'en servir. Deux jobs en même temps veut donc dire deux workers, ou deux conteneurs
worker sur la même machine.

Les priorités disent l'ordre, pas le nombre : fusion et proxy passent devant les rendus
et les étalonnages parce qu'ils débloquent tout le reste, et à priorité égale c'est
l'ordre de création.

**Ce que ça donne à l'écran.** Le flux SSE `/api/jobs/stream` envoie **tous** les jobs
`queued` et `running` chaque seconde, et n'émet que quand la charge utile change. La
barre du haut dessine donc **une ligne par job en cours**, chacune avec son type en
badge, son nom (« Rush 1 · dive »), la machine qui la tient et sa propre progression,
puis une ligne « N jobs queued » pour la file. Ailleurs, chaque page montre sa part : la
carte « Running · N » de Stabilize, un badge à spinner sur la sequence concernée, un
spinner sur chaque grade en cours dans l'arbre de Color.

**Le nom du worker est sur la ligne** depuis le 2026-08-25 (demandé par florian) :
`JobOut.worker_name`, résolu en un select à côté des noms de rush. Rien ne disait où un
job tournait, ce qui va avec un seul worker et rend aveugle à deux.

**Le dialogue des workers détaille les débits, un par ligne et un nombre par ligne**
(même jour) : l'estimation courante, c'est-à-dire ce que les vrais jobs ont mesuré, à
défaut le benchmark, et un `-` quand ni l'un ni l'autre (état réel, que le dispatcher
traite comme inconnu et jamais comme lent). Ni titre de section ni provenance : « 3 jobs
· 47 at start » a existé une heure, et florian l'a fait retirer, ce n'est pas ce qu'on
lit une vitesse pour savoir. Plus une section **Now** qui dit ce que la machine encode à
l'instant, lue dans le même flux SSE que la barre. Les quatre lignes portent les mots des
jobs (`merge`, `proxy`, `stabilize`, `color`), le même vocabulaire que les badges, via
`jobKindLabel`.

**Pourquoi la fusion est en MB/s et pas en img/s** (question de florian) : `mp4_merge` ne
décode rien. Il réécrit le `stbl` et recopie les octets, 4,4 s pour 4 Go, donc ce qui
varie est une taille et pas un nombre d'images. C'est aussi ce que `_magnitude` renvoie
pour un job de fusion, en mégaoctets, là où les trois autres renvoient un compte
d'images : la fonction de coût divise l'ampleur par le débit, et les deux doivent parler
la même unité.

### Deux jobs du même type sur un rush n'étaient pas des doublons

Bug trouvé le 2026-08-25 en répondant à la question ci-dessus, et **il précède les
grades multiples** : `drop_stale_jobs`, qui tourne à chaque démarrage de l'API,
dédoublonnait la file sur `(sequence_id, kind)`. Vrai pour une fusion et un proxy, qui
sont uniques par rush et écriraient sur le même fichier ; faux pour un rendu, qui est par
sequence et par profil, et pour un étalonnage, qui est par look.

Effet mesuré : trois étalonnages en file sur deux rushes redescendent à deux après un
redémarrage, puis à zéro au suivant, **en silence**, en laissant les lignes `queued`
pour toujours avec aucun job derrière. Le même défaut mangeait les rendus d'un rush
stabilisé en deux formats d'un coup.

La clé porte maintenant sur ce que le job écrit : `(sequence_id, kind, render_id,
grade_id)`. Contre-épreuve : avec l'ancienne clé le test `test_two_grades_of_one_rush_
are_not_duplicates` échoue en journalisant « 1 duplicate queued job(s): dropped », avec
la nouvelle il passe, et deux étalonnages du même rush survivent à un redémarrage réel
(2 avant, 2 après, aucune ligne dans le journal).

**Piège de méthode dans cette enquête** : mes lectures directes du fichier SQLite avec
`python3 -c "import sqlite3"` étaient **périmées**, le WAL écrit par le conteneur ne
m'étant pas visible. Elles affichaient une file vide pendant que l'API en rapportait
trois. À lire par l'API, pas par le fichier.

### Un arrêt propre rend le job, il ne le fait pas échouer

Corrigé le 2026-08-25, et le mécanisme existait déjà : `release()` rend tout ce qu'un
worker tient, **sans dépenser de tentative**, parce qu'être éteint n'est pas la faute du
job. Le défaut était **l'ordre**. Sur SIGTERM, `procs.terminate_all()` tue le ffmpeg, la
passe lève une `ProcessError`, et le worker rapportait cet échec ; ce rapport arrivait
avant le `release`, qui ne trouvait alors plus rien en `RUNNING` à rendre. Résultat : un
`docker compose restart worker` laissait un rendu **FAILED** à relancer à la main.

`run_job` reçoit donc l'événement d'arrêt et **ne dit rien** quand il est armé : le job
reste à nous, `release` le remet en file une seconde plus tard, et si le processus est tué
avant d'avoir pu dire au revoir, le bail expire et le reaper fait la même chose. Vérifié
en vrai : étalonnage à 4 %, `restart worker`, le grade repasse en `queued` et le job aussi.

Les deux moitiés de la règle sont testées, parce que la seconde est celle qu'on casse en
corrigeant la première : un job qui casse **de lui-même** doit toujours le dire, sinon il
resterait `RUNNING` jusqu'à l'expiration du bail et serait réessayé pour rien.

### Annuler un job

Demandé le 2026-08-25 (« we cannot cancel a pending or running color job », « the top bar
should allow us to cancel the running job »). Le mécanisme est le plus ancien du dépôt :
**supprimer la ligne du job suffit**. `_held_by` ne le reconnaît plus, le battement suivant
répond `ok: false`, et le worker tue son propre enfant. C'est déjà comme ça qu'un rendu de
cut supprimé s'arrête ; l'annulation ne fait que donner un nom au geste.

Deux routes, chacune adressée par ce que l'appelant a sous la main :

- **`POST /grades/{id}/cancel`**, pour la page Color, qui connaît le grade et pas le job.
  Le look est intact, le grade retombe en `draft`. Ouvert aussi en `queued` : attendre un
  worker est un état dont on veut sortir.
- **`POST /jobs/{id}/cancel`**, pour la barre du haut, qui connaît le job et pas ce qu'il
  produit. Un rendu part **avec sa ligne** (`_purge_render`), puisque c'est cette ligne qui
  remet la sequence dans la file de Stabilize ; un étalonnage laisse son look.

**Une fusion et un proxy ne sont pas annulables**, et le bouton mort le dit au survol :
personne ne les a demandés et le scan suivant les relancerait. C'est un 409 côté API, pas
seulement un bouton désactivé.

**Le dialogue est dans la barre et pas dans Color** : en haut, la croix est à 20 px d'une
barre de progression et un clic de travers jette des minutes d'encodage ; dans la carte, le
bouton s'appelle « Stop », on vient de le chercher, et rien du look n'est perdu.

Au passage, une fuite réparée : **Gyroflow écrit `<sortie>.tmp` et renomme à la fin**, donc
un rendu tué laissait un fichier partiel que plus rien ne nommait (mesuré : 15,5 Mo sur ce
volume, d'un rendu interrompu deux jours plus tôt). `gyroflow.render` le supprime maintenant
dans son chemin d'échec. L'étalonnage, lui, écrivait déjà dans un `.partial.mp4` nettoyé.

### Les colonnes mortes se suppriment, une par une

`db.DEAD_COLUMNS` liste les colonnes dont le sens est mort, avec le jour : `sequence.color`
(le 2026-08-20, quand seuls les dossiers ont gardé une pastille) et `grade.analysis` (le
2026-08-25, l'analyse ayant remonté sur `render`). SQLite sait supprimer une colonne depuis
3.35 et l'image en livre 3.46. Vu au démarrage : `Column dropped: sequence.color`,
`Column dropped: grade.analysis`.

**Nommées une par une, jamais déduites.** Un « supprime ce que le modèle ne déclare plus »
effacerait une colonne le jour où quelqu'un oublie de la déclarer. Et le tout est enveloppé
dans un `try` qui journalise : un rangement de schéma ne doit jamais empêcher un démarrage.

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

**Un job qui n'a rien fait n'est pas une mesure.** Un étalonnage dont le fichier existe
déjà (le hash du look est dans le nom) revient en quelques millisecondes sans encoder,
avec `reused: True`. Replié dans la moyenne, ça donne un débit qui n'en est pas un :
mesuré le 2026-08-25, **neuf étalonnages réutilisés avaient poussé le débit couleur de
cette machine à 406 909 img/s**, ce qui l'aurait rendue la moins chère pour tout job de
couleur à jamais. `observe` écarte donc un résultat `reused`, comme il écartait déjà une
fusion faite en hardlink pour la même raison. Les valeurs déjà empoisonnées ont été
effacées à la main (`grade_fps` et `merge_mbps`, le second mesuré sur des fusions de
test entièrement en cache de pages).

Trouvé en mettant **une ligne par débit** dans le dialogue des workers : la ligne unique
d'avant, « proxy 61 img/s · render 28 img/s · grade 27 img/s · merge 114 MB/s », cachait
l'absurdité. Un affichage qui montre un nombre à la fois est aussi un instrument.

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
| proxy distant | **trois** fichiers renvoyés : proxy, poster, graphe gyro |
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
seule étape proxy écrit un poster et un graphe gyro qu'aucun champ du
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

**`/mnt/Stockage` n'est pas dans la boucle.** `PF_DATA_PATH=./data`, donc l'inbox est
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

### L'upload part en morceaux, parce qu'une requête est plafonnée à 100 Mio

Un rush partait en **une** requête, et ça ne peut pas marcher depuis l'extérieur.
**Mesuré le 2026-08-26** sur la vraie chaîne publique (Cloudflare, la box, le NPM de vm2),
en postant des corps croissants : **104 857 600 octets passent, un octet de plus revient en
413 depuis le bord**, sur le `Content-Length`, en 70 ms, sans que l'origine voie quoi que ce
soit. Donc 100 Mio pile, borne incluse, et non « 100 Mo » comme le disait la doc d'infra.

Deux détails qui ont failli fausser la mesure. **Il y a deux plafonds** : celui du NPM vaut
1 Mo par défaut, et c'est lui qui a refusé mes 99 Mo. Ce qui les rend distinguables est la
**signature du 413**, qui dit `cloudflare` ou `nginx/<version>` : sans elle on croit mesurer
l'un en mesurant l'autre. Et **aucun plan ni aucun transport ne règle ça** (Business 200 Mo,
Enterprise 500 Mo, un tunnel Cloudflare garde le plafond du plan) : il faut découper.

La forme retenue, quatre routes : `POST /upload/begin` réserve la destination et préalloue
le fichier, `PUT /upload/{partial}/chunk?offset=` écrit un morceau, `POST .../finish` le
nomme pour de vrai, `DELETE /upload/{partial}` abandonne. **64 Mio par morceau, trois en
vol.**

- **La concurrence est passée à l'intérieur d'un fichier**, alors que la boucle reste
  séquentielle d'un fichier à l'autre. C'est le second bénéfice du découpage, et il vise le
  vrai problème mesuré plus haut : à ~20 ms de RTT un flux TCP unique est borné par la
  fenêtre, d'où les 9 Mo/s observés sur un lien qui vaut dix fois ça. Trois morceaux d'un
  même rush remplissent le lien aussi bien que quatre rushes, et ils atterrissent dans **un**
  fichier préalloué au lieu d'éparpiller les écritures.
- **C'est le serveur qui décide de la complétude, pas le client**, parce qu'un morceau perdu
  sur un tunnel qui tombe est précisément ce que le client ne sait pas. Chaque morceau dépose
  un **fichier marqueur vide** `inbox/.uploads/<partial>/<offset>-<longueur>`, et `finish`
  refuse en 409 en nommant le premier trou. Un rush de 4 Go renommé avec un trou dedans
  n'échouerait qu'à la fusion ou à la stabilisation, loin de la cause.
- **Un marqueur par fichier et non des lignes ajoutées à un seul** : les morceaux sont écrits
  en parallèle, et l'atomicité de `O_APPEND` n'est pas garantie sur NFS, or c'est là que
  l'inbox va (voir le plan de déploiement). Des noms distincts ne peuvent pas s'entrelacer,
  et un morceau renvoyé réécrit le même nom, donc c'est idempotent.
- 🔴 **`start_upload` ne peut pas réutiliser `unique_destination`**, et c'est un test qui l'a
  trouvé : celui-ci regarde si la **destination** existe, or un fichier en cours de réception
  n'existe pas encore sous son vrai nom. Deux `begin` du même nom renvoyaient donc le **même**
  partiel, et deux uploads de 4 Go auraient écrit le même fichier. Il enjambe maintenant le
  `.partial` aussi, et il crée le fichier en ouverture **exclusive** (`"xb"`) plutôt que de
  vérifier puis créer, ce qui est une course que les deux appelants passent.
- **Le garde-fou du scan reste par morceau**, comme il était par fichier. Un compteur tenu de
  `begin` à `finish` fuirait si le client disparaissait, et **un compteur qui fuit fait taire
  le scan planifié pour la vie du processus** ; les creux entre morceaux sont couverts par
  `UPLOAD_SETTLE_S` (10 s), sans commune mesure avec eux. C'est le bug du 2026-08-20 avec une
  fenêtre plus étroite, pas un bug neuf.
- **Un seul réessai par morceau.** Sur un tunnel qui tombe, perdre 64 Mio ne doit pas coûter
  le fichier entier, et le serveur prend deux fois la même plage comme une seule écriture.

Vérifié de bout en bout : un vrai rush de **3,7 Go en 57 morceaux envoyés à l'envers, trois
en parallèle**, réassemblé **identique octet pour octet** (397 Mo/s en local) ; puis dans le
**vrai navigateur**, trois morceaux, un `finish`, fichier identique et ingéré. Plus le cas
qui compte : morceau du milieu jamais envoyé, `finish` refusé en nommant l'offset, morceau
envoyé, `finish` accepté.

**Deux sondes fausses avant d'être justes**, pour mémoire. Mon attente côté navigateur
cherchait le mot « uploaded » dans la page, or la barre de progression affiche « 0/1
uploaded » : elle concluait au succès avant le premier morceau. Et mon `dd` de découpe
utilisait `count=$((len/1048576+1))` avec `iflag=count_bytes`, donc **65 octets par
morceau** au lieu de 64 Mio. Les deux fois, c'est le serveur qui a dit la vérité en refusant
le `finish`.

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
que si des fichiers en sont sortis, et il en dit le compte : dire quoi que ce soit d'une
sequence dont personne n'a rien produit serait parler d'un fichier qui n'existe pas.

**Les sequences sont un tableau**, dans une colonne de 24 rem qui passe à 30 rem quand
l'écran le permet : nom, in, out, longueur et les deux actions sur une seule ligne, tout
en `text-sm`. Deux lignes par entrée avec des tailles mélangées ont été essayées le
2026-08-21 et lues comme du désordre. Ici les icônes sont **toujours dessinées**, à la
différence de l'arbre à gauche où elles sortent au survol : une liste de trois lignes
n'a pas de place à gagner, et une icône qui n'apparaît qu'au survol est une icône dont
personne ne sait qu'elle est là.

### La pellicule est partie avec la place qu'elle n'avait plus

Retirée le 2026-08-26. Le derush a eu une pellicule sous la timeline, et elle a cessé
d'être dessinée quand `GyroChart` a pris toute la bande : la courbe gyro est ce sur quoi
on scrube, une pellicule à côté aurait pris de la place à l'image. Ce qui restait était
une mécanique complète que rien n'affichait, mesurée : **une passe ffmpeg et 190 à 290 Ko
écrits par proxy**, plus une colonne `sequence.filmstrip_path`, une route
`GET /media/filmstrip/{id}`, un champ `has_filmstrip`, un helper d'URL côté front et deux
réglages. Le tout est parti, la colonne par `DEAD_COLUMNS`.

Le poster reste : il sert de vignette et il est produit par la même passe.

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

### Supprimer un parent supprime ses enfants

Règle posée par florian le 2026-08-24 : « de manière générale, supprimer un parent
supprime aussi toujours ses enfants, le user aura téléchargé les fichiers avant si
besoin ». Elle remplace un traitement par état du rendu, où un rendu `done` gardait son
fichier et perdait seulement son `cut_id`. Ce qui restait était un clip dans `out/` que
**plus aucune vue ne pouvait nommer** : tout l'affichage d'un rendu pend du cut, et le
seul endroit où il réapparaissait était la liste plate de la page Color.

Deux fuites du même genre étaient là depuis le début, trouvées en appliquant la règle :
`delete_render` ne touchait ni la ligne `grade` ni le fichier étalonné, et `grade.render_id`
est unique **mais pas une clé étrangère**, donc rien ne s'en plaignait. D'où
`_purge_render` (le rendu, son grade, son fichier étalonné, leurs jobs) et `_purge_cut`
par dessus, appelés par les trois chemins : la suppression d'un rendu, celle d'un cut,
celle d'un rush.

Un détail qui a coûté un test rouge : **il faut un `flush()` entre les rendus et le cut**.
`render.cut_id` est une vraie clé étrangère et aucune relation ORM ne la déclare, donc
rien n'ordonne les deux `DELETE` et SQLite refuse celui du cut. C'est le même piège qui
rendait `replace_cuts` obligé de traiter les rendus d'abord.

Ce que la règle a fait disparaître : le `cut_id = NULL`, et avec lui le désastre
silencieux qu'il fallait éviter à la main (un `cut_id` nul veut dire « le rush entier »
pour `prepare`, donc un rendu de dix secondes revenait long de quatre minutes). Un rendu
en file dont le cut part est supprimé, et son job en vol s'arrête au battement suivant.

**Le geste vit partout où on voit une sequence** : la ligne de la file Stabilize (icône
toujours dessinée, comme le reste de cette table), l'arbre de gauche (au survol, comme
les actions de dossier juste au-dessus) et le derush. Un seul composant,
`DeleteCutDialog`, parce que ce qui compte est la phrase : une sequence n'est que deux
numéros de frame, donc la perdre ne coûte rien, alors que ce qu'elle a produit est un
fichier. C'est cette asymétrie que le dialogue est là pour dire, avec le compte exact
(`CutOut.files`, les rendus finis plus les étalonnages finis, calculé par l'API et non
recoupé côté front).

Le derush, lui, supprime toujours en réécrivant la liste qu'il édite (`onConfirm`), parce
que c'est ainsi que tous ses autres gestes écrivent et que la ligne doit disparaître avant
la requête. `replace_cuts` passe par le même `_purge_cut`, donc les deux chemins font
exactement la même chose.

**Et un clip stabilisé se télécharge dans ses deux versions.** Le badge du profil porte
« Download stabilized » et « Download graded », la seconde seulement quand un fichier
étalonné existe, ce que la file publie en `QueueRender.grade_id` : deux fichiers, deux
adresses, aucune requête de plus.

#### Les trois actions sont dessinées, pas pliées dans un menu

Choix de florian le 2026-08-24, après le correctif ci-dessous : « j'aimerais plutôt
afficher les trois boutons directement dans le badge avec le texte au hover ». Le badge
porte donc le nom du profil puis trois icônes (goutte, flèche, poubelle), leurs noms au
survol par le `title` natif, comme tout ce qui est tronqué ou muet ailleurs. Un badge qui
ouvre un menu ressemble à une étiquette, et c'est bien ce qui s'était passé.

**Le fichier étalonné ne se télécharge pas d'ici.** Deux flèches côte à côte ne se lisent
pas sans leur texte, et l'endroit où prendre un clip étalonné est la page qui l'a
étalonné. La goutte se **remplit** à la place (`QueueRender.grade_id`), donc la ligne dit
quand même qu'un fichier existe.

**Et la suppression d'un rendu demande confirmation**, elle aussi (florian, même jour).
Elle ne le demandait pas quand elle était au fond d'un menu ; avec une poubelle toujours
visible à 20 px d'un téléchargement, un clic de travers détruit cent mégaoctets. D'où
`DeleteDialog`, la présentation seule, et `DeleteCutDialog` par dessus pour le geste sur
une sequence : le dialogue d'un rendu dit que la sequence reste, donc qu'il suffit de
relancer, ce qui est exactement ce qui distingue les deux suppressions.

**Le fichier servi porte le nom que l'interface donne aux choses**, slugifié, pas celui
du disque (florian, même jour). Sur le volume un rendu s'appelle
`DJI_20260711191722_0025_D__h_1080__c00.mp4`, ce qui est juste là où il vit (le cache du
worker adresse par chemin, et les parts doivent être sans ambiguïté) et illisible dans un
dossier de téléchargements. Ce qui sort reprend la **même forme** que le volume, `__`
entre les champs et `_` à l'intérieur : `rush_1_rush_rush_rush_rush_rush__dive__h_1080p_h264.mp4`,
mesuré dans le navigateur pour 273 Mo, plus `__graded` pour la version étalonnée.

Trois détails. Un profil supprimé depuis ne laisse que son id, et c'est **mieux que
rien** : c'est la seule chose qui distingue deux fichiers d'une même sequence. Un
fragment qui ne donne rien (un libellé fait d'un seul emoji) **disparaît** au lieu de
laisser un `__` orphelin. Et chaque fragment est **plafonné à 60 caractères**, trois
libellés et une extension devant tenir dans un nom de fichier.

Le slug a fait disparaître une mécanique plutôt que de s'y ajouter : l'entête partait en
deux formes (`filename*=UTF-8''` percent-encodé, RFC 5987) pour qu'un libellé accentué
survive à un entête latin-1. Un nom déjà ASCII ne pose pas la question. Il reste un
garde-fou d'une ligne, parce qu'un non-ASCII passerait en mojibake plutôt qu'en erreur.

**Un placeholder nomme le champ, il ne montre pas un exemple.** « Vertical 4K » dans le
nom d'un profil se lisait comme une valeur déjà saisie ; c'est « Profile name »
(florian, même jour).

#### Le badge qui ouvrait un menu invisible

Rapporté par florian le 2026-08-24, « le clic sur le badge ne fait rien », et c'est le
même piège que le playhead : **`Badge` est une fonction simple qui laisse tomber la ref
qu'on lui passe.** Avec `DropdownMenuTrigger asChild`, Radix n'avait donc aucun élément
pour ancrer son popper, et le menu se dessinait nulle part. Il était pourtant bien monté :
mon test lisait `[role=menuitem]` dans le DOM, y trouvait Grade / Download / Delete, et
concluait à tort que ça marchait. **Le DOM n'est pas l'écran.** La capture prise au même
instant ne montrait aucun menu, et personne ne l'avait regardée.

Le trigger **porte** maintenant les classes du badge (`badgeVariants`) au lieu d'en
envelopper un : son propre `<button>` a une ref, et un `<div>` dans un `<button>` n'est
de toute façon pas du balisage valide. Mesuré après coup avec trois lectures
indépendantes : la boîte de l'item (1195, 633) juste sous celle du badge (1203, 601),
`elementFromPoint` au centre de l'item qui renvoie bien « Grade », et l'octet d'écran qui
change. Plus le clavier, que le div n'avait jamais eu : focus, Entrée ouvre, Échap ferme.

À retenir pour tout `asChild` : il exige un composant qui transmet sa ref. Dans ce dépôt
seuls `Button`, les `<a>`/`<button>` nus et les `Link` de react-router en sont.

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

**Et le troisième menteur était la garde elle-même**, trouvé le 2026-08-23 grâce à une
trace envoyée depuis le navigateur qui le voyait, puisqu'aucune mesure faite ici ne le
reproduisait :

```
46495.8   shown 15112        ← la vidéo présente 15112, et `paused` est déjà vrai
46497.0   pause              ← l'événement n'arrive que 1,2 ms plus tard
47938.6   step +1 from 15111 ← le pas part donc d'une frame en retard
47938.6     seek -> 15112    ← il redemande la frame déjà affichée
47975.1     shown 15112      ← rien ne bouge
```

Le dernier callback d'une lecture s'exécute quand `paused` **est déjà vrai**. Filtrer sur
la pause jetait donc la dernière frame de chaque lecture : la position restait en retard
d'une image et le premier pas suivant ne faisait rien. « Une fois sur cinq », soit la
fréquence à laquelle on met en pause avant de faire du pas à pas.

Le bon critère n'est pas la pause mais **un seek en vol** : un callback en retard porte
`seeking = true` **et** une frame différente de la cible, et c'est ce couple qui le
démasque. Tout le reste dit où la vidéo est vraiment, pause comprise. La cible s'oublie
dès qu'elle est atteinte ou dès `seeked`, sans quoi un décodeur qui n'atterrirait jamais
exactement dessus ferait taire le suivi pour toujours.

Leçon de méthode, plus utile que le correctif : mes tests comparaient le compteur à
lui-même après une pause, deux valeurs issues du même état, donc ils passaient tous. La
trace comparait le compteur à la frame que la vidéo **présentait**. **Une sonde qui ne
compare que deux valeurs venant de la même source ne prouve rien.**

L'enregistreur qui a produit cette trace (`lib/playhead-debug.ts`, une route
`POST /api/debug/report`, un tampon en anneau posté quand un geste n'atterrit pas où il
l'a demandé) a été **retiré le même jour**, son travail fait. À refaire à l'identique si
un défaut ne se voit que sur une machine : c'est ce qui a tranché là où quatre séries de
mesures prises ici avaient toutes conclu à tort.

Reste, après ces trois correctifs, une gêne visuelle que florian décrit toujours et
qu'aucune sonde n'attrape : dans sa dernière trace, chaque geste atterrit exactement où
il le demandait. Son verdict le 2026-08-23, et l'affaire est classée là : « je pense que
c'est purement visuel ».

**Ni le fichier ni le décodeur n'y sont pour rien**, vérifié le 2026-08-23 quand le
bug a paru persister. Les deux proxys ont des PTS strictement monotones, tous exactement
à `N x 1001`, un seul écart possible entre voisins, aucun trou (26390 et 22165 frames,
dont un proxy issu d'une fusion de deux parts). Et le navigateur ne se trompe pas non
plus : 300 seeks répartis sur cinq zones du fichier présentent chaque fois la frame
demandée, et son `duration` colle à la milliseconde.

**Un pas relatif part d'une ref, jamais de l'état React.** `frame` a un render de retard
dès qu'un clic arrive avant que React ait commité le précédent, et deux clics dans cette
fenêtre demandent deux fois la même frame. La position vit donc dans une ref écrite par
le seek comme par le callback, et l'état ne sert plus qu'à l'affichage. `currentTime` a
été essayé à sa place et écarté : juste après une pause il peut encore être dans la frame
qui précède celle affichée, et le premier pas ne bougeait alors pas (mesuré : trois pas
donnaient +2). **Et un pas met la lecture en pause**, sinon la frame suivante efface le
seek et l'image a l'air de revenir en arrière.

Le seek reste au **milieu** de la frame visée (`N + 0,5`), sans quoi l'arrondi du
décodeur retombe sur la précédente. Contre-épreuve que le compteur ne mentait que sur le
numéro : capture du canvas après un saut à la frame 6645, PSNR de **45,9 dB** contre
cette frame extraite par ffmpeg et **32,5 dB** contre chacune de ses deux voisines.

### « Dérushé » est une marque, jamais un compte

Un rush porte un booléen `derushed` qu'on pose à la main, depuis la case à cocher de
l'entête du derush, et qui se voit dans l'arbre : **une coche à droite de la durée, et rien
d'autre**. **Il ne se déduit pas du nombre de sequences** : un rush qui ne valait rien
est dérushé dès qu'on l'a regardé, et il n'a aucune sequence pour le prouver. C'est le
compagnon des deux icônes ci-dessous : ensemble ils disent ce qu'il reste à faire.

Le nom passait aussi en gris, retiré le 2026-08-23 : mesuré dans le DOM, les deux noms
font bien 14 px et poids 400, mais à contraste plus faible (`rgb(161,161,170)` contre
`rgb(250,250,250)`) le même texte se lit comme un texte plus petit, ce qui a été rapporté
comme une taille de police incohérente. La coche disait déjà la même chose.

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

## Stabilize : une file, pas un lanceur par rush

Refonte du 2026-08-23, après un brainstorm où le grief de florian était textuel : « je
sais pas trop si je peux faire tous les rendus de toutes les sequences de toutes les
rush en même temps, ou si je peux sélectionner certains profils, ou certains rush de
certains dossiers ». Ce n'était pas un manque de fonction, c'était un manque
d'affordance : la page ne disait pas ce qu'elle permettait.

Le lanceur par rush a donc disparu au profit d'**une file unique, tous rushes
confondus**, dessinée comme l'arbre de la barre latérale (dossier, rush, sequence) avec
des **cases à trois états** : cocher un dossier coche ses rushes, cocher un rush coche
ses sequences, et l'état partiel se voit. Une case « Everything waiting » en tête, parce
que le cas courant est de tout prendre.

Ce qui fait le travail de la page :

- **Tout arrive coché, sauf ce qui a déjà un fichier pour le profil choisi.** Changer de
  profil en haut recoche ces lignes toutes seules : c'est ce qui rend un second format
  gratuit sans jamais refaire deux fois le même travail. Le second format est rare mais
  existe (choix de florian), donc il ne fallait ni l'imposer ni le bloquer.
- **Une sequence porte le nom des profils avec lesquels elle a été rendue**, un badge par
  fichier, avec un spinner tant que le job tourne. Et ce badge **est le chemin vers le
  fichier** : étalonner, télécharger, supprimer. C'est ce qui a permis de retirer la
  table de rendus à neuf colonnes, qui mélangeait deux échelles (un lanceur pour un rush,
  une table pour tous).
- **Le profil est un réglage mémorisé, pas une question.** Le dernier utilisé, dans
  `localStorage`, parce que le profil a été validé dans Gyroflow bien avant cette page.
- **Aucun aperçu du cadrage**, décision de florian : « l'idée que j'avais quand j'ai
  commencé le projet c'est d'aller vite, donc normalement le profil a déjà été testé et
  ajusté dans Gyroflow ». Un aperçu serait une question posée à quelqu'un qui a déjà
  répondu.
- **Pas de rush entier.** Le derush est le passage obligé, donc une sequence marquée est
  la condition d'entrée dans la file. `whole_sequence` reste dans l'API (des rendus à
  `cut_id` nul existent en base et la file doit les ignorer sans les faire disparaître),
  mais l'interface ne l'offre plus.
- **Les templates restent en haut**, choix de florian contre ma proposition de les
  déplacer dans un dialogue.

Quatre choses sont sorties du premier vrai usage de la page, le 2026-08-23, et la
première était la plus grave.

**Un rendu qui échoue ne dit rien de son rush.** `dispatch._fail` marquait la séquence
en `FAILED` quel que soit le job, donc un rendu tué par un redémarrage sortait son rush
de cette file (elle filtre sur `ready`) et affichait le stderr de Gyroflow à la place de
sa durée dans l'arbre, à côté d'un fichier fusionné qui n'avait jamais été en cause.
Seuls **merge et proxy** peuvent invalider un rush, ce sont eux qui le produisent. La
ligne déjà abîmée en base a été remise à `ready` à la main.

**Une sequence qui a déjà un fichier pour le profil choisi n'est plus cochable**, la
case est désactivée et le dit au survol. Relancer écrivait un second fichier du même
look, et c'est ce qui faisait passer un rendu échoué pour un rendu réussi : les deux
existaient sur la même sequence, l'un ayant abouti. Cocher un dossier ne prend que ce
qui est libre.

**Les noms viennent de l'API, pas d'un recoupement côté front.** `RenderOut` et `JobOut`
portent le label du rush et celui de la sequence. Un merge ou un proxy nomme le rush
seul, un rendu ou un étalonnage nomme aussi sa sequence, donc la barre du haut dit
« Rush 1 · dive » là où elle imprimait `DJI_20260809144616_0034_D` sous un arbre qui dit
« Rush 1 ». Deux vocabulaires pour une chose sur un écran, c'était le défaut.

**Une erreur tient sur une ligne.** Gyroflow rend ses trente dernières lignes de
progression : affichées en entier, elles faisaient un mur rouge sur toute la page pour
un seul fait. Première ligne, le reste au survol. Et un rendu en file ou en cours
s'annule : supprimer le rendu supprime son job, et le worker s'arrête au battement
suivant, ce qui existait déjà pour un cut supprimé.

**Deux « riens » différents, deux réponses.** Rapporté par florian le 2026-08-25 : quand
toutes les sequences sont rendues avec le profil choisi, la page affichait « Nothing
marked yet. See Derush. » alors qu'il y avait des sequences partout. Le test portait sur
ce qui reste à faire (`everything`, les cuts libres) au lieu de ce qui existe (`marked`).
Maintenant l'arbre reste affiché avec toutes ses cases désactivées et le compteur dit
« Every sequence is rendered with this profile » ; le message vers Derush ne sort que
s'il n'y a **aucune** sequence. Vérifié sur les deux profils du volume : `h_1080` donne
0 case active et 7 désactivées, `v_1080p_h264` donne « 1 of 1 sequence · 0:43 ».

Deux détails d'implémentation qui ont une raison :

- **`GET /stabilize/queue` répond en une requête**, avec trois SELECT et un assemblage en
  Python. Une route par rush aurait fait du N+1 pour une page dont le sujet est
  précisément l'ensemble.
- **Un rush dont la sonde a échoué n'a pas de fps**, et `frame_to_ms` divise par zéro. La
  file entière serait tombée en 500 à cause d'un seul mauvais rush ; la durée vaut 0 dans
  ce cas. Trouvé par un test, pas en production.
- **Le lancement fait une requête par rush**, puisqu'un rendu se crée contre le rush qui
  possède les cuts, et rapporte l'agrégat (`Promise.allSettled`) : ce qui compte est
  combien de jobs le clic a produits, pas quelle requête a échoué.

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

**Un template livré se supprime pour de bon**, choix de florian le 2026-08-23 contre la
version d'avant, où l'icône était une flèche de retour et où supprimer la copie éditée
valait retour à l'original. Une icône qui veut dire deux choses selon la ligne ne se lit
pas ; maintenant c'est une poubelle partout, et le dialogue dit que ce profil est livré
avec l'application. Deux endroits à traiter pour que la suppression tienne : le fichier
part de `data/templates`, **et** son id va dans `.removed`, sinon `seed_templates()` le
recopie au démarrage suivant ; et `list_templates()` lit **les deux répertoires**, donc
il faut aussi y filtrer, l'original étant toujours dans l'image. Une écriture sous un id
supprimé l'oublie (un label qui retombe sur `h_1080` doit réapparaître, pas rester
invisible). Conséquence assumée : plus de retour à l'original, il faut vider `.removed`.

**Le décalage de cadre est une fraction, et il est violent.** Vérifié par deux rendus
réels du clip de bench en 1080x1920 : entre `[0.0, 0.0]` et `[-0.5, 0.0]`, la même frame
montre une partie complètement différente de la source. D'où le pas de 0,05 sur le
slider. Les bornes dures de l'API sont -1 à 1 ; Gyroflow écrête de lui-même au bord du
recadrage possible.

**Ni surcharge par rendu, ni par sequence** : un rendu prend le template tel quel. Deux
variantes d'un look se font en dupliquant un template, ce qui a l'avantage qu'un rendu se
reproduit à l'identique. Le champ `overrides` existait pour ça et n'a jamais été rempli :
**retiré le 2026-08-26** de bout en bout (colonne, schéma, spec du job, paramètre de
`build_preset`, et `_deep_merge` qui n'existait que pour lui). Le renforcement plutôt que
la perte : il n'y a plus **aucun** endroit où un rendu puisse dévier de son profil.

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

## Sauvegardes de la base : `VACUUM INTO`, et un restore qui attend

Ajouté le 2026-08-26, demandé par florian : de quoi sauvegarder la base, la restaurer, et
le faire tout seul toutes les X heures vers le partage réseau. Réglages seulement, pas
d'écran (`PF_BACKUP_DIR`, vide = `<data>/backups`, donc le volume des vidéos ;
`PF_BACKUP_INTERVAL_H`, 24, 0 coupe ; `PF_BACKUP_KEEP`, 7). Quatre routes,
`GET`/`POST /backups`, `POST /backups/{nom}/restore`, `DELETE /backups/{nom}`.

**Ce n'est pas une copie de fichier, et c'est tout le sujet.** Le `-wal` porte des
transactions commitées que le fichier principal n'a pas encore : un `cp` d'une base
vivante rend une base amputée de ses dernières écritures, **en silence**. `VACUUM INTO`
écrit une copie cohérente depuis une transaction de lecture, et sort **un seul fichier
sans annexes**, ce qui est précisément ce qu'on peut poser sur un partage sans en perdre
la moitié. Trois choses mesurées avant de s'engager :

- **le paramètre lié marche** (`VACUUM INTO ?`), donc aucun échappement de chemin à rater ;
- un snapshot pris **pendant qu'une autre connexion tient une transaction d'écriture
  ouverte** réussit et **exclut la ligne non commitée**. Il n'a donc jamais à attendre que
  la chaîne soit au repos ;
- ça passe par `engine.connect()` sans souci de transaction implicite, vérifié contre la
  variante `AUTOCOMMIT` : les deux marchent, donc on garde la simple.

Chaque snapshot passe un `integrity_check` **avant d'être nommé**, et s'écrit en
`.partial` puis se renomme, comme tout le reste du dépôt : une sauvegarde non vérifiée
est une rumeur.

**Un restore est posé, pas appliqué.** Remplacer le fichier sous un moteur ouvert
entrerait en course avec la requête en cours de transaction. Le snapshot choisi est donc
copié en `db/restore.pending` et échangé par `db._apply_pending_restore` au démarrage
suivant, dans la **même fenêtre** que `_adopt_legacy_db` : avant que quoi que ce soit
n'ait ouvert quoi que ce soit.

**Le seul endroit où un restore peut corrompre au lieu de restaurer** est le `-wal`
sortant laissé derrière : SQLite le rejouerait dans un fichier dont il n'a jamais fait
partie. Les trois fichiers de l'ancienne base partent donc ensemble, et un test ne passe
que si c'est le cas. Rien n'est mis de côté à la main, parce que `stage_restore` a déjà
pris un snapshot complet de l'état remplacé : un demi-fichier sans son WAL serait un
filet de sécurité **pire que rien**, puisqu'il en aurait l'air.

**Deux ordres qui ne sont pas décoratifs.** La copie du snapshot choisi se fait **avant**
le snapshot de sécurité, parce que celui-ci déclenche une passe de rétention qui, sur un
répertoire plein, peut supprimer le fichier depuis lequel on restaure. Et la planification
se juge sur **le snapshot le plus récent du disque**, pas sur le démarrage du processus :
un conteneur qui redémarre deux fois par heure déroulerait sinon une semaine de rétention
en une après-midi. La boucle tique au plus toutes les heures et `_backup_once` décide.

**Un bug que seul un test a attrapé** : `make()` renvoyait `listing()[0]` au lieu du
fichier qu'il venait d'écrire, et le tri par nom place un snapshot taggé **avant** un non
taggé de la même seconde (`-` vaut 0x2D, `.` vaut 0x2E). Donc un restore nommait le
mauvais fichier de sécurité. Le tri porte maintenant sur l'horodatage, le nom ne servant
que d'égalité.

La rétention ne regarde que les noms que ce module écrit (`NAME`), ce qui est aussi ce qui
protège du traversal : un nom venu d'une URL est **refusé s'il ne ressemble pas à un
snapshot**, plutôt qu'assaini. Et un snapshot d'un schéma plus ancien se restaure très
bien, l'auto-migration le rattrape à la remontée.

Vérifié de bout en bout sur la stack : snapshot, renommage d'un dossier, restore,
`restart api`, `Database restored from a staged snapshot`, nom revenu, snapshot de
sécurité contenant l'état remplacé, et les 4 clips / 2 rushes / 4 rendus / 2 grades
intacts.

**`restore` refuse pendant qu'un job tourne** (409, avec le type du job). Une
restauration supprime toutes les lignes `job` : une ligne `queued` qui disparaît se
rattrape, un encodage en cours est des minutes de machine. Les lignes `queued` partent
quand même sans que ça bloque, c'est assumé.

## Le fallback SPA avalait les 404 de l'API

Corrigé le 2026-08-26. Le front est servi par un attrape-tout, `@app.get("/{full_path:
path}")`, qui rend `index.html` pour tout ce que l'API n'a pas déclaré. **`/api/*`
tombait dedans** : une route mal tapée renvoyait **200 et une page HTML**, et le client
échouait ensuite sur un parsing JSON, ailleurs. Le symptôme ne désigne pas la cause, et
c'est une heure perdue pour une faute de frappe.

Pire pour les autres verbes : un `POST` sur un chemin inconnu rendait **405**, parce que
l'attrape-tout était en GET seul et que le chemin correspondait quand même. « Mauvais
verbe » est un diagnostic différent de « mauvais chemin », et il envoie chercher au
mauvais endroit.

Un second attrape-tout sur `/api/{rest:path}`, **tous les verbes**, est donc enregistré
**après les routers et avant le fallback**. Il vit dans `_mount_frontend` parce qu'il
n'existe que pour contrer le fallback : sans front construit il n'y a pas d'attrape-tout,
et un chemin inconnu tombe déjà en 404 tout seul.

`_mount_frontend` prend l'app en paramètre au lieu de fermer sur celle du module, pour
que **l'ordre de la table de routes** soit lisible sur une app jetable. C'est l'ordre qui
décide de tout ici, et sans ça un test devrait le croire sur parole. Mesuré sur le
conteneur : 404 sur les cinq verbes avec le chemin nommé dans le corps, les vraies routes
toujours en 200 (`/api/status`, `/api/media/proxy/1` inclus), et `/derush/1`,
`/color/2/2`, `/stabilisation` qui rendent toujours la page.

## Un worker d'appoint, et son icône de barre

Déployé le 2026-08-26 : le dispatcher et un worker vivent sur **vm4 (xenon)** du homelab,
proxima prête son 3090 par intermittence. `contrib/tray/` porte de quoi l'allumer et
l'éteindre depuis la barre système, ce qui n'existait pas et manquait tous les jours.

Ce que le déploiement a mesuré, et qui vaut mieux que le tableau des capacités :

| worker | décodage | OpenCL | rendu | lien vers le dispatcher |
|---|---|---|---|---|
| **xenon** (vm4, 8 vCPU, sans GPU) | cpu | aucun | **8,4 img/s** | 4028 Mo/s (même hôte) |
| **proxima** (i7-7700K, RTX 3090) | NVDEC | RTX 3090 | **24,6 img/s** | 111 Mo/s (gigabit réel) |

C'est la fonction de coût en situation : proxima rend 2,9x plus vite mais paie le
transfert, xenon est lent et a les fichiers sous la main. Les 8,4 img/s confirment au
passage les ~8,7 mesurés en CPU pur sur une autre machine.

Quatre choses apprises sur l'icône, toutes par l'essai :

- **`env python3` est le mauvais interpréteur.** PyGObject vient des paquets de la
  distribution, et un miniforge en tête de `PATH` n'a pas `gi` du tout. Le shebang
  nomme donc `/usr/bin/python3`. Première version morte exactement là.
- **L'inscription DBus n'est pas l'écran.** Le programme vivait et parlait à
  `org.kde.StatusNotifierWatcher` sans qu'aucune icône soit visible. Ce qui a tranché
  est un **test différentiel** : capture d'écran worker allumé, puis éteint, et c'est
  la seule icône qui change qui est la nôtre. Vérifier une paire d'états vaut mieux que
  chercher une icône dans une barre qui en porte huit.
- **Une icône d'état doit se distinguer de ses voisines, pas seulement d'elle-même.**
  `network-offline-symbolic` était la même forme que l'icône réseau du système deux
  crans à droite, en plus pâle. Remplacé par la vague du logo, et sa variante barrée
  pour l'arrêt : à 16 px l'opacité seule ne se lit pas, et changer de silhouette
  cesserait de dire « PostFlight ».
- **L'état est relu, jamais mémorisé** (`docker inspect` toutes les 3 s) : le worker
  s'allume aussi depuis un shell ou par `restart: unless-stopped` après un reboot, et
  une icône qui ment est pire que pas d'icône.

Et deux pièges de méthode dans lesquels je suis tombé le même jour, tous deux la même
faute : **`pgrep -f` et `pkill -f` correspondent à leur propre ligne de commande**. Le
premier m'a fait déclarer vivant un processus mort, le second a tué le shell qui le
lançait, le motif étant présent dans son propre `bash -c`. Filtrer par PID, ou avec un
motif entre crochets.

## Où vont les idées pas encore faites

`IDEES.md` à la racine, en français comme ce fichier, la plus récente en haut. Une idée
qui se fait en sort et vient ici : ce fichier porte des faits mesurés, l'autre des
intentions. Créé le 2026-08-26.

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
cd backend && PF_DATA_DIR=../data ../.venv/bin/uvicorn app.main:app --port 8000
PF_DATA_DIR=../data ../.venv/bin/python -m app.worker

# front (proxy /api vers le port 8000)
cd frontend && npm install && npm run dev
```

Pour tester en local il faut `mp4_merge` dans le PATH (ou `PF_MP4_MERGE_BIN`) :
binaire prêt sur les releases de `gyroflow/mp4-merge`.

`docker/Dockerfile.spike` est l'image jetable qui a servi à valider Gyroflow
headless + OpenCL en conteneur. À garder pour rejouer la validation sur une
nouvelle machine cible.
