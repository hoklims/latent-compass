Reprends l’implémentation de Latent Compass jusqu’au résultat demandé : choisir les observations utiles à une décision sous budget, avec un routage gouverné, puis permettre le retrait des index uniquement lorsque les preuves le permettent. Poursuis le travail effectif ; ne t’arrête pas à un nouveau plan.

## Dépôt et point de départ

- Worktree à utiliser : `E:\latent-compass-hok798`.
- Branche : `codex/hok-798-active-diagnosis`, base `6ec94e86f5b6c429fd264708f28d5105b19fbafa`.
- PR existante, ouverte en brouillon : https://github.com/hoklims/latent-compass/pull/4
- Le dernier commit de code est celui de `git log -1` sur cette branche ; vérifie l’état réel et la CI de ce SHA avant d’écrire. Ne réinitialise aucun checkout et conserve les modifications d’autrui. Les anciens auteurs délégués sont arrêtés.

## À lire d’abord

1. Les instructions applicables au worktree, puis `HANDOFF.md`, `GOVERNANCE.md` et `CONTRIBUTING.md`.
2. `docs/adr/0011-experimental-active-diagnosis.md` et son amendement, `docs/active-diagnosis.md`, `docs/active-diagnosis-scope.md`, `docs/host-observations.md`, `docs/source-session.md`, `docs/source-observations.md`, `docs/lab-operations.md`.
3. La PR #4 et les issues Linear HOK-798 à HOK-806, descriptions et dépendances comprises.
4. Sur cette machine, les preuves sont hors dépôt, sous le profil de l’opérateur. Session Codex du 2026-09-18 : `Documents/Codex/2026-09-18/fai/work/` (commit `9742953`). Session Claude du 2026-09-19 : `Documents/Claude/2026-09-19/latent-compass-hok798/work/` — epoch, sortie du classifieur, témoins rouges/verts liés aux objets Git, les deux rapports d’audit indépendants et les lanceurs de mutants. Chaque preuve porte sur le SHA qu’elle nomme, pas sur tes futures modifications.

## État réel

Le socle expérimental `latent_compass.lab` est implémenté : modèle fini explicite, calcul rationnel exact sous budget/horizon, abstention et preuves obligatoires, rejeu complet, mémoire de justifications invalidable, lectures confinées, boucle source→observation, contrôle d’avis/budgets, comparaison descriptive et migration à blanc. Il n’est branché sur aucun hôte actif.

Livré depuis la reprise du 2026-09-19 :

- **HOK-799** — périmètre figé dans `docs/active-diagnosis-scope.md` : un poste personnel, Claude Code et Codex, ce dépôt et ses worktrees comme seul périmètre `PERSONAL_LAB`. Inventaire en lecture seule de 25 actifs, scellé en deux révisions sous `evidence/` : la première reste telle que committée, défauts compris ; la `v1.1.0` la corrige sans l’éditer. Seuls deux actifs Graphify du pilote sortent de `KEEP`, en `DISABLE_LATER` avec leurs sept gates manquants. Six scénarios d’acceptation exécutables, neuf inconnues avec responsable.
- **HOK-800** — `latent_compass.lab.host_observations` : une enveloppe unique pour lecture de fichier, recherche littérale, diff Git, navigation symbolique et contrôle ciblé ; sept statuts, limites obligatoires scellées, mode source seule comme témoin, provider retiré refusé sans repli. Le laboratoire n’exécute rien : l’hôte exécute, le laboratoire admet puis recoupe contre le snapshot.
- **HOK-801/802** — `latent_compass.lab.host_session` : pont observation vérifiée → issue du modèle ou `UNKNOWN`. L’identité et la version de l’outil sont scellées dans l’épisode ; un `UNKNOWN` laisse l’état intact et la sonde non consommée.
- **HOK-803** — `examples/lab_host_bench.py` : boucle complète dans un dépôt Git jetable, avec de vrais outils (git, `ast`, processus de contrôle). Exerce outil absent, timeout interrompu, langage non supporté, conseiller absent, kill switch, capacité retirée, avis périmé ou conflictuel, preuve Semctx manquante, avis ignoré, budget épuisé, provider retiré et séparation des deux familles d’agents. `run_delegation` confie le même diagnostic à des épisodes enfants qui réservent sur **un seul** pool : enfant affamé, enfant planifiant sur un reste périmé refusé par le pool, délégation annulée, checkout jamais partagé. Ce sont des épisodes du banc exécutés l’un après l’autre, pas des sous-agents réels.

Vérification locale au dernier commit de code : Ruff, mypy, 1 300 tests réussis et 3 cas POSIX ignorés sous Windows ; build et wheel validés hors checkout. Les 917 tests de la base existent toujours. Vérifie la CI du SHA courant avant de t’en prévaloir.

Deux revues indépendantes en lecture seule ont eu lieu. Celle de HOK-799 a rendu `BOUNDARY_WEAK` ; ses treize constats sont traités. Celle du candidat agrégé a rendu **PROOF_WEAK** sans défaut fonctionnel établi : ses constats corrigeables dans le dépôt sont traités, et un témoin rouge/vert lié aux objets Git existe pour chacun des fichiers de test modifiés. Aucun reçu `PROOF_ADEQUATE`, aucun `ALLOW`, aucune fusion, aucune activation ni suppression d’index.

`PROOF_WEAK` a une cause structurelle hors de ce dépôt : le classifieur de la politique de preuve active ne reconnaît ni `src/latent_compass/lab/`, ni `examples/`, ni `evidence/`. Un changement qui ne touche aucun test ne déclencherait aucun contrôle. Le corriger est une révision de politique, à mener par sa procédure N-1 dans un worktree isolé, jamais depuis ce candidat.

## Prochaine action unique

Obtiens du propriétaire les décisions de la section suivante, puis prépare **HOK-804** : protocole préenregistré avant toute mesure, sur le banc et les contrats existants. Sans ces décisions, poursuis ce qui n’en dépend pas : un exécuteur hôte de navigation symbolique réel pour le banc, et les critères HOK-803 encore ouverts — séparation effective des stores, parité des deux hôtes réels. Ces deux critères demandent de vraies sessions Codex et Claude Code : cadre leur coût et leur isolation avec le propriétaire avant de les lancer.

HOK-805/806 restent conditionnés par leurs preuves et autorisations propres. La correction des preuves formelles fait partie de la livraison ; elle ne remplace pas les capacités fonctionnelles manquantes.

## Décisions du propriétaire en attente

- **U1** — un second dépôt personnel, avec CCC provisionné, rejoint-il le pilote ? Sans lui, CCC ne reçoit aucun verdict : ce dépôt est sous le seuil d’admission de CCC.
- **Planificateur** — `propose()` ne peut pas être informé qu’une sonde est impossible à obtenir ; la boucle s’arrête alors sur la meilleure décision admissible, même quand une autre sonde resterait rentable. Y remédier ajoute une entrée au contrat `1.0.0`.
- **Politique de preuve** — ajouter au classifieur une règle couvrant le noyau de décision et les preuves scellées de ce dépôt.
- **HOK-804** — population, budgets, métrique primaire, marge de non-infériorité et amélioration de coût utile, figés avant la première mesure (U8).
- **Historique Git** — le commit `4abf502` de cette branche contient encore un chemin privé de l’opérateur ; le retirer exige une réécriture d’historique et un push forcé.

## Contraintes

- Linear personnel uniquement : workspace/team Hoklims/HOK, compte `hoklims@gmail.com`. Projet réutilisé `2d29f7e2-f682-4f08-a582-db96a5ed31e3`, « Latent Compass — Diagnostic actif et routage gouverné » ; milestone `1600ae66-8f78-4833-9c81-81914858f5ce`. Aucun doublon ni réactivation des anciennes issues annulées.
- Garde les contrats, stores et CLI historiques compatibles ; les nouvelles capacités restent isolées. Un seul auteur par checkout. Ne modifie pas les anciens worktrees `E:\latent-compass`, `E:\latent-compass-hok800`, `E:\latent-compass-hok803` ou `E:\latent-compass-merged` pour reprendre ce lot.
- Les coûts, mondes et probabilités sont des hypothèses du modèle ; aucune optimalité universelle, efficacité réelle ou authenticité ne découle d’un seal. Une lecture vide ne prouve pas une absence hors de sa portée. Aucun résultat empirique inventé ni ancien holdout réutilisé.
- Une preuve committée ne s’édite jamais sur place : une correction est une révision ajoutée à côté, et un test épingle les seals de chaque révision.
- Le conseiller ne gagne aucune autorité d’exécution, de promotion ou de modification du juge Semctx. Stores Codex/Claude et identités personnelles/professionnelles restent séparés. Prépare les intégrations dans un banc isolé ; ne change pas les hooks/configurations actifs pour démontrer un succès.
- Ne contourne pas `proof-integrity-review` : garde la politique active intacte, fournis les témoins nécessaires, utilise un réviseur neuf et réellement restreint, puis le gate exigé avant toute conclusion de readiness. Une consigne « lecture seule » à un agent disposant d’outils d’écriture n’est pas une restriction effective. Compare les empreintes d’un epoch committé aux objets Git, pas aux octets CRLF du worktree : `git archive` convertit les fins de ligne sauf avec `-c core.autocrlf=false`.
- HOK-804, HOK-247 avec ACTIVATE applicable et HOK-740 précèdent le pilote HOK-805. Ne suppose pas livrés HOK-737/740 ou HOK-501. L’autorisation du pilote et celle d’une destruction irréversible restent distinctes. Ne transforme pas cette reprise en autorisation de merge, d’activation ou de purge.
- Conserve des états Linear fidèles aux preuves. Le parent et HOK-799 à HOK-803 sont In Progress ; HOK-804 à HOK-806 sont Backlog. Mets à jour les capacités livrées, les restes et la PR, puis relis les objets modifiés. N’étiquette pas tout Done sur la seule CI.

## Vérification

Utilise Python 3.13 et les dépendances uv figées. Tests ciblés pendant les corrections ; avant publication du candidat, exécute les gates du dépôt :

    uv run --frozen ruff format --check .
    uv run --frozen ruff check .
    uv run --frozen mypy
    uv run --frozen pytest -o addopts='' -q
    uv build --no-sources

Démos : `uv run --frozen python examples/lab_source_demo.py` et `uv run --frozen python examples/lab_host_bench.py`. Revalide le wheel hors checkout et la CI des deux OS au SHA final. Sous Windows, n’exécute pas de shim POSIX sans extension et ne confonds pas import/type-check Linux avec une exécution Linux.

Commence par vérifier branche, HEAD et arbre de travail, lis les références, puis poursuis la prochaine tranche vérifiable sans demander une nouvelle permission pour les travaux locaux déjà autorisés.
