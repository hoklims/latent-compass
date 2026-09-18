Reprends l’implémentation de Latent Compass jusqu’au résultat demandé : choisir les observations utiles à une décision sous budget, avec un routage gouverné, puis permettre le retrait des index uniquement lorsque les preuves le permettent. Poursuis le travail effectif ; ne t’arrête pas à un nouveau plan.

## Dépôt et point de départ

- Worktree à utiliser : `E:\latent-compass-hok798`.
- Branche : `codex/hok-798-active-diagnosis`.
- Dernier commit de code vérifié : `9742953a9c5d397cc7df88df1a0fc4bd48bba9ab`, sur la base `6ec94e86f5b6c429fd264708f28d5105b19fbafa`.
- PR existante, ouverte en brouillon : https://github.com/hoklims/latent-compass/pull/4
- Un commit documentaire de reprise peut suivre ce commit de code : vérifie l’état réel avant d’écrire. Ne réinitialise aucun checkout et conserve les modifications d’autrui. Les anciens auteurs délégués sont arrêtés.

## À lire d’abord

1. Les instructions applicables au worktree, puis `HANDOFF.md`, `GOVERNANCE.md` et `CONTRIBUTING.md`.
2. `docs/adr/0011-experimental-active-diagnosis.md`, `docs/active-diagnosis.md`, `docs/source-session.md`, `docs/source-observations.md`, `docs/lab-operations.md`.
3. La PR #4 et les issues Linear HOK-798 à HOK-806, descriptions et dépendances comprises.
4. Sur cette machine, les preuves de la session précédente sont dans `C:\Users\Hokli\Documents\Codex\2026-09-18\fai\work\` : `audit-terminal.md`, `lc-final-epoch.json`, `lc-mutant-final-receipts.json`, `lc-mutants.json`, `run_lab_mutants.py`, `lc-ci-final.json` et `lc-ci-final.log`. Ces preuves portent sur le commit indiqué, pas automatiquement sur tes futures modifications.

## État réel

Le socle expérimental `latent_compass.lab` est implémenté : modèle fini explicite, calcul rationnel exact sous budget/horizon, abstention et preuves obligatoires, rejeu complet, mémoire de justifications invalidable, lectures confinées, boucle source→observation, contrôle d’avis/budgets, comparaison descriptive et migration à blanc. Il n’est pas branché sur les hôtes actifs.

Au commit de code : CI Windows et Ubuntu verte ; Windows 1 154 tests réussis / 3 cas POSIX ignorés, Ubuntu 1 133 réussis / 24 cas Windows ignorés ; Ruff, mypy, build et wheel vérifiés. Dix mutations sémantiques ont été détectées puis restaurées au vert. Au checkpoint de reprise : Ruff et 36 tests CLI/intégration sont également repassés.

La revue finale n’a établi aucun défaut fonctionnel majeur supplémentaire, mais son verdict est **PROOF_WEAK**. Il manque le reçu formel complet, l’attestation effective des restrictions du réviseur et la couverture de certaines obligations, notamment les nouveaux contrats de décision que le classifieur n’a pas reconnus. Aucun `ALLOW`, aucune fusion, aucune activation ni suppression d’index.

## Prochaine action unique

Termine le cadrage exécutable de **HOK-799** : inventorie en lecture seule les providers, MCP, hooks, caches et consommateurs du périmètre personnel effectivement accessible ; nomme les dépôts/hôtes couverts et les inconnues. Raccorde cet inventaire à l’ADR 0011 et aux scénarios d’acceptation. Ne réouvre pas sans motif l’architecture du modèle fini déjà décidée. Documente les choix routiniers toi-même ; demande seulement une information indispensable qu’aucune source accessible ne permet de résoudre.

Une fois cette dépendance traitée, poursuis les parties autorisées du lot : HOK-800 pour les adaptateurs Git/symboles/contrôles ciblés manquants, HOK-801/802 pour leur intégration au diagnostic, HOK-803 pour le banc des hôtes réels, puis HOK-804 pour l’expérience préenregistrée. HOK-805/806 restent conditionnés par leurs preuves et autorisations propres. La correction des preuves formelles fait partie de la livraison ; elle ne remplace pas les capacités fonctionnelles manquantes.

## Contraintes

- Linear personnel uniquement : workspace/team Hoklims/HOK, compte `hoklims@gmail.com`, connecteur `linear_personal`. Projet réutilisé `2d29f7e2-f682-4f08-a582-db96a5ed31e3`, « Latent Compass — Diagnostic actif et routage gouverné » ; milestone `1600ae66-8f78-4833-9c81-81914858f5ce`. Aucun doublon ni réactivation des anciennes issues annulées.
- Garde les contrats, stores et CLI historiques compatibles ; les nouvelles capacités restent isolées. Un seul auteur par checkout. Ne modifie pas les anciens worktrees `E:\latent-compass`, `E:\latent-compass-hok800`, `E:\latent-compass-hok803` ou `E:\latent-compass-merged` pour reprendre ce lot.
- Les coûts, mondes et probabilités sont des hypothèses du modèle ; aucune optimalité universelle, efficacité réelle ou authenticité ne découle d’un seal. Une lecture vide ne prouve pas une absence hors de sa portée. Aucun résultat empirique inventé ni ancien holdout réutilisé.
- Le conseiller ne gagne aucune autorité d’exécution, de promotion ou de modification du juge Semctx. Stores Codex/Claude et identités personnelles/professionnelles restent séparés. Prépare les intégrations dans un banc isolé ; ne change pas les hooks/configurations actifs pour démontrer un succès.
- Ne contourne pas `proof-integrity-review` : garde la politique active intacte, fournis les témoins nécessaires, utilise un réviseur neuf et réellement restreint, puis le gate exigé avant toute conclusion de readiness. Une consigne « lecture seule » à un agent disposant d’outils d’écriture n’est pas une restriction effective. Comparer les empreintes d’un epoch committé aux objets Git, pas aux octets CRLF du worktree.
- HOK-804, HOK-247 avec ACTIVATE applicable et HOK-740 précèdent le pilote HOK-805. Ne suppose pas livrés HOK-737/740 ou HOK-501. L’autorisation du pilote et celle d’une destruction irréversible restent distinctes. Ne transforme pas cette reprise en autorisation de merge, d’activation ou de purge.
- Conserve des états Linear fidèles aux preuves. Le parent et HOK-799 à HOK-803 sont In Progress ; HOK-804 à HOK-806 sont Backlog au checkpoint. Mets à jour les capacités livrées, les restes et la PR, puis relis les objets modifiés. N’étiquette pas tout Done sur la seule CI.

## Vérification

Utilise Python 3.13 et les dépendances uv figées. Tests ciblés pendant les corrections ; avant publication du candidat, exécute les gates du dépôt :

    uv run --frozen ruff format --check .
    uv run --frozen ruff check .
    uv run --frozen mypy
    uv run --frozen pytest -o addopts='' -q
    uv build --no-sources

Démo : `uv run --frozen python examples/lab_source_demo.py`. Revalide le wheel hors checkout et la CI des deux OS au SHA final. Sous Windows, n’exécute pas de shim POSIX sans extension et ne confonds pas import/type-check Linux avec une exécution Linux.

## Décisions encore ouvertes

Le périmètre opérationnel exact, l’inventaire complet et les paramètres de l’expérience réelle ne sont pas encore fixés. Résous-les dans HOK-799/HOK-804 à partir des sources ; ne fabrique ni budget, ni seuil, ni observation pour fermer une issue.

Commence par vérifier branche, HEAD et arbre de travail, lis les références, puis termine HOK-799 et poursuis la prochaine tranche vérifiable sans demander une nouvelle permission pour les travaux locaux déjà autorisés.
