# Idées

Ce qui est demandé et pas encore fait. Une entrée par idée, la plus récente en haut,
avec ce qu'on en sait déjà : ce n'est pas une feuille de route avec des dates, c'est un
carnet pour que rien ne se perde entre deux sessions.

Quand une idée est faite, elle sort d'ici et va dans `CLAUDE.md`, où vivent les faits.

---

## Choisir le dossier de destination à l'upload

**Demandé le 2026-08-26.** Sur la page import, pouvoir dire dans quel dossier les rushes
vont atterrir, en choisissant un dossier existant ou en en créant un au passage. Sans ça
tout arrive dans Global et il faut ranger à la main, rush par rush, après coup.

**Ce qui rend ça moins trivial qu'il n'y paraît** : l'upload et l'ingestion sont
découplés. L'upload dépose des fichiers dans `inbox/` et s'arrête là ; c'est le **scan**
qui crée les clips et les sequences, plus tard, et il ne sait pas qui a déposé quoi ni où
la personne voulait que ça aille. L'intention doit donc survivre de l'un à l'autre.

Deux façons, et la moins chère pourrait suffire :

- **Côté client, après coup.** La réponse du scan nomme déjà les sequences créées, donc
  la page pourrait leur poser le dossier en une requête chacune. Rien à stocker côté
  serveur. Casse dans deux cas : si c'est le scan **périodique** qui ingère (donc si
  l'onglet est fermé, ou si l'upload a duré plus longtemps que le prochain tic), et si
  deux personnes déposent en même temps.
- **Côté serveur, porté par l'upload.** `POST /upload/begin` prendrait un `folder_id`,
  retenu pour le nom de fichier résolu, et `ingest_and_group` le lirait en créant la
  sequence. Plus robuste, et ça demande un endroit pour ranger cette intention (une
  colonne sur une table de dépôts en cours, ou un fichier à côté du `.partial`).

**À ne pas oublier** : les dossiers vont à **deux niveaux au maximum** (un site, une
sortie dedans), la règle vit dans l'API, et une création depuis la page import doit la
respecter comme les autres. Et Global n'est pas une ligne en base, c'est `folder_id =
null` : « aucun dossier » reste donc un choix valide et doit rester le défaut.
