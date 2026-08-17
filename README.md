# video-stab

Traitement des rushes FPV DJI (O3/O4), de la carte SD au clip stabilisé, dans une
interface web. Une image Docker, deux services, déployable sur Portainer.

```
inbox/     dépôt réseau surveillé (dump de la carte SD)
  ↓        ingestion : attente de stabilité de taille, ffprobe, empreinte
raw/       masters intacts — jamais réencodés ni coupés
  ↓        fusion des parts d'un même vol (mp4_merge, sans perte, gyro conservé)
merged/    un fichier continu par séquence
  ↓        proxy H.264 lisible dans le navigateur + pellicule
proxies/   ce qu'on regarde pour derusher
  ↓        marks in/out enregistrés en frames
out/       rendus stabilisés par Gyroflow (une passe, zones gardées uniquement)
  ↓        étalonnage : un second encodage, sur les seuls clips gardés
graded/    clips étalonnés en H.264, prêts à partager
```

L'interface suit ces étapes : **1 Import**, **2 Derush**, **3 Stabilize**,
**4 Color**. Le texte de l'interface, le code et les commentaires sont en anglais.

## Pourquoi cet ordre

Un rush DJI porte son gyro dans un flux `djmd` que **ffmpeg ne sait pas
réécrire** : toute fusion ou coupe faite avec ffmpeg détruit la télémétrie et
rend la stabilisation impossible. La fusion passe donc par `mp4_merge`, qui
travaille au niveau des boxes mp4, et **le derush n'est que de la métadonnée** —
c'est Gyroflow qui coupe, pendant qu'il stabilise. Une seule passe lourde,
uniquement sur les zones gardées, sans intermédiaire de plusieurs gigaoctets.

Fusionner avant de stabiliser n'est pas un détail : le lissage se calcule sur
toute la courbe gyro. Deux parts stabilisées séparément laisseraient une couture
visible à la jonction.

## Déploiement

```bash
cp .env.example .env      # ajuster VS_DATA_PATH, VS_PORT
docker compose up -d --build
```

Avec un GPU sur l'hôte (recommandé, environ 3x plus rapide sur la
stabilisation) :

```bash
getent group render       # relever le GID, à mettre dans VS_RENDER_GID
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Sans GPU la stack tourne telle quelle : Gyroflow retombe sur pocl/CPU et ffmpeg
décode au CPU. L'interface affiche en haut à droite ce qui a réellement été
détecté.

**Portainer** : coller le contenu de `docker-compose.yml` en stack (y ajouter les
sections `devices`/`group_add` de `docker-compose.gpu.yml` si l'hôte a un GPU),
et renseigner les variables d'environnement de `.env.example`.

Interface sur `http://<hôte>:8080`.

### Le volume de données

Un seul bind mount, `/data`. Garder `inbox/` et `raw/` sur le **même système de
fichiers** : l'ingestion devient un `rename()` instantané au lieu d'une copie de
plusieurs gigaoctets. Compter environ 2x la taille des rushes tant que
`VS_PURGE_PARTS_AFTER_MERGE` est à `false` (masters + fusionnés), plus environ
8 Mb/s de proxy.

## Utilisation

1. Vider la carte SD dans `inbox/`, de deux façons au choix :
   - **copie directe** dans le dossier réseau. Le worker scanne toutes les 30 s
     et n'ingère un fichier qu'après plusieurs scans à taille identique —
     inotify n'est pas fiable sur NFS/SMB et un rush de 4 Go met du temps à
     arriver.
   - **glisser-déposer** dans la zone du tableau de bord. Les fichiers partent
     l'un après l'autre avec une progression par fichier, et sont ingérés dès la
     fin de l'envoi (pas d'attente : le fichier est écrit sous `.partial` et
     renommé seulement une fois complet, donc sa complétude est certaine).
     Un rush déjà connu est reconnu à l'empreinte et écarté dans
     `inbox/.duplicates/` au lieu d'être traité deux fois.
2. Les parts d'un même vol sont regroupées automatiquement, fusionnées, puis un
   proxy est généré. La séquence passe à *prête*.
3. Derush : `←`/`→` image par image, `maj` pour une seconde, `espace` lecture,
   sélecteur de vitesse. `I` pose un début, `O` ferme la zone, `ctrl+S`
   enregistre. Sous le lecteur, la pellicule et la **courbe gyro** (vitesse
   angulaire X/Y/Z en °/s) partagent la même timeline, zoomable à la molette
   jusqu'à ×24 — c'est là qu'on repère les passages calmes et les secousses.
4. Choisir un template et lancer le rendu. Un fichier par zone.
5. Étalonner si besoin : six curseurs, un auto-niveaux mesuré sur le clip, un
   aperçu image fixe qui traverse exactement les filtres du rendu final. La
   sortie est un **nouveau fichier** H.264 ; le rendu stabilisé reste intact.

## Templates

Deux à l'installation, copiés dans `data/templates/` où ils sont éditables sans
reconstruire l'image :

| id | sortie | recadrage |
|---|---|---|
| `h_1080` | 1920×1080 | crop 16:9 dans le 4:3 source |
| `v_1080` | 1080×1920 | crop 9:16, offset réglable |

Un template est un JSON partiel de projet Gyroflow (`stabilization` + `output`) ;
l'application y injecte les bornes du cut et les chemins de sortie. Le projet
généré est conservé dans `projects/` : chaque rendu est rejouable à l'identique.

## Identité de contenu : ne rien recalculer deux fois

Chaque séquence porte le hash de ses parts (empreintes ordonnées), et ce hash est
dans le nom des fichiers produits : `DJI_..._0044_D__934e00ad7607.mp4`. Une
séquence supprimée puis reformée retrouve donc sa fusion et son proxy sur le
disque et repasse à *prête* sans rien réencoder — mesuré à 186 ms au lieu de sept
minutes. Même principe pour l'étalonnage, dont le fichier porte en plus le hash du
look. C'est aussi pourquoi supprimer une séquence ne supprime que sa ligne en
base : `keep_derived=false` pour effacer les dérivés, `keep_raw=false` pour tout
purger.

## Ce qui reste ouvert

- Le niveau de lissage et l'offset de recadrage vertical sont dans les templates ;
  ils gagneraient à être réglables par zone dans l'UI.
- Le regroupement automatique ne touche jamais une séquence déjà fusionnée. Une
  part arrivée en retard forme sa propre séquence, à recoller via
  `POST /api/sequences/regroup`.
- Pas d'authentification : à placer derrière un reverse proxy si l'accès sort du
  réseau local.

## Piège : l'upload derrière un reverse proxy

Le glisser-déposer envoie le fichier entier dans une requête. En accès direct sur
le LAN, aucune limite. Mais devant un proxy, les plafonds par défaut sont très
en dessous d'un rush de 4 Go :

| couche | défaut | à faire |
|---|---|---|
| nginx / Nginx Proxy Manager | **1 Mo** | `client_max_body_size 0;` et remonter `proxy_read_timeout` |
| Cloudflare (proxy orange) | **100 Mo**, non contournable hors Enterprise | ne pas faire passer l'upload par Cloudflare |

Autrement dit : pour déposer des rushes depuis le navigateur, viser l'app **en
direct sur le LAN** (ou via un tunnel type Tailscale), pas la chaîne
Cloudflare → box → proxy. La copie de fichiers dans le dossier réseau, elle,
n'est concernée par aucune de ces limites.
