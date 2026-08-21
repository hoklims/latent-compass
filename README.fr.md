# Latent Compass

> La boîte noire et le banc d’essai hors ligne des décisions stratégiques prises
> par les agents de développement.

[English](README.md) · [Français](README.fr.md)

Les agents de développement prennent sans cesse des décisions lourdes de
conséquences : quelle hypothèse tester, quel fichier modifier, quelle erreur
creuser, quand s’arrêter, quand demander de l’aide. La plupart des systèmes
conservent le patch final. Ils ne conservent pas la décision qui l’a produit.

Cet angle mort est dangereux. Une tâche réussie ne prouve pas que la stratégie
choisie était la bonne. Un échec révèle rarement si une autre piste aurait
fonctionné. Et si l’agent « apprend » ensuite de ces résultats sans frontière
claire entre preuve et autorité, il finit par noter son propre travail, réécrire
son histoire et promouvoir sa propre politique.

Latent Compass est né d’une question plus difficile :

**Un agent peut-il apprendre de ses décisions stratégiques sans obtenir le
pouvoir de les déclarer correctes ?**

Le projet commence par les preuves, pas par l’intelligence. Il capture les
épisodes de décision, valide leurs contrats, les inscrit dans un registre local
détectant les altérations et évalue des politiques figées hors ligne. Un juge
déterministe externe reste l’autorité. Seul un humain peut décider d’une
promotion.

Latent Compass **valide et enregistre**. Il ne tient pas la barre.

## Le problème auquel il répond

Imaginez un agent face à trois directions plausibles :

1. corriger le symptôme visible ;
2. inspecter le contrat qui l’a produit ;
3. s’arrêter parce qu’une décision de produit fait défaut.

L’agent choisit. Quelques heures plus tard, la tâche est verte ou cassée. Que
manque-t-il presque toujours ?

- les alternatives disponibles avant le choix ;
- les preuves associées à chaque alternative ;
- la propension de la politique qui a produit la direction retenue ;
- le coût, les violations, l’information acquise et la réversibilité observés
  ensuite ;
- une trace fiable de ce qui était connu avant le résultat ;
- une autorité distincte capable de dire « continue » ou « arrête ».

Sans ces éléments, « apprendre de l’expérience » revient surtout à reconstruire
une histoire après coup. Latent Compass rend cette histoire réfutable.

## Pourquoi il a été construit ainsi

La première question n’était pas : « Quel modèle faut-il entraîner ? » Elle
était : « Qu’est-ce qu’une boucle d’apprentissage ne doit jamais avoir le droit
de faire ? »

Cinq frontières en découlent :

1. **Capturer avant le résultat.** Les preuves liées aux candidats appartiennent
   à l’instant qui précède le choix, pas à une justification rétrospective.
2. **Refuser l’ambiguïté.** Version absente, probabilité invalide, champ inventé
   ou preuve non prise en charge : le système échoue fermé.
3. **Séparer observation et autorité.** Le composant qui enregistre un verdict
   ne peut pas s’accorder le droit d’agir dessus.
4. **Protéger le holdout.** La validation peut être rejouée. Une preuve holdout
   destinée à étayer une affirmation ne peut pas être consommée jusqu’à produire
   la réponse souhaitée.
5. **Accepter le manque de preuves.** Une abstention ou `KILL_DISCOVERY` vaut
   mieux qu’un gagnant fabriqué.

Le nom résume ce rôle : une boussole indique une direction, elle ne prend pas le
gouvernail.

## Comment ça marche

```text
L’agent atteint un point de décision
            │
            ▼
Latent Compass valide l’épisode et les preuves liées aux candidats
            │
            ▼
Le registre lié à l’hôte conserve une observation immuable et rejouable
            │
            ▼
Le benchmark hors ligne compare des politiques figées sur des données scellées
            │
            ▼
Le juge externe évalue les preuves ─── L’humain garde l’autorité de promotion
```

Cette frontière d’autorité est volontaire. Latent Compass n’appelle jamais le
juge externe, ne lui écrit jamais et ne transforme jamais un résultat de
benchmark en autorisation.

## Ce qui existe aujourd’hui

### Démontré — mis en œuvre et couvert par les tests du dépôt

- **Des contrats d’épisode stricts et versionnés.** Champs inconnus, coercitions
  de type, valeurs non finies, horodatages non canoniques, distributions de
  propension invalides et observabilité exagérée sont refusés.
- **Une frontière d’autorité qui échoue fermé.** Latent Compass n’émet aucun
  avis opérationnel. Même un résultat localement cohérent est refusé comme
  `untrusted_evidence` lorsqu’aucune racine de confiance externe n’est configurée.
- **Un registre append-only lié à l’hôte.** Il prend en charge l’ajout atomique,
  le rejeu, le caviardage structurel, la vérification d’intégrité et les
  ancrages durables.
- **Un benchmark hors ligne reproductible.** Quatre baselines pré-enregistrées
  reçoivent le même budget sur des données de validation scellées. La
  vérification les réexécute au lieu de croire un checksum fourni.
- **Une discipline holdout.** Le runner de validation et le planificateur
  pré-holdout n’ouvrent pas le split holdout. Sa consommation est atomique et
  liée au corpus.
- **Une capture pré-action jugeable.** Les preuves propres à chaque candidat
  sont enregistrées dans un fichier compagnon immuable, sans révéler l’action
  choisie ni le résultat ultérieur.
- **Le confinement.** Les écritures durables restent sous une racine explicite,
  refusent l’écrasement silencieux et ne suivent pas les liens remplacés.

### Expérimental — réel, mais pas encore une preuve de valeur

- La taxonomie de huit familles de métriques et leurs seuils.
- IPS avec plancher de support, les diagnostics SNIPS et le quantile empirique
  pondéré du coût de queue introduit par le contrat de benchmark 1.1.0.
- Le corpus synthétique. Il prouve que le pipeline s’exécute et refuse les
  entrées invalides ; il ne prouve pas qu’une politique est bonne.
- Le caviardage par tombstone et l’ancrage local durable.
- Un labeler sans clé prouvé par des éléments externes. Son workflow privé est
  lié par GitHub OIDC et Sigstore, mais sa rubrique conservatrice a produit une
  abstention, pas une supervision directionnelle exploitable.

### Projeté — ni mis en œuvre ni revendiqué

- Un ranker pairwise entraîné ou un bandit contextuel. C’était la direction
  prévue par HOK-182 ; l’issue a été annulée avant tout entraînement, faute de
  données suffisantes.
- Une calibration destinée à un agent actif.
- Une évaluation canary ou une intégration à Semctx.
- Une promotion automatique ou une autorité d’exécution.
- La moindre amélioration démontrée des résultats d’un agent. **Aucune
  affirmation de ce type n’est formulée.**

Le premier cycle de recherche s’est terminé par `KILL_DISCOVERY`. Cela ne
signifie pas qu’un ranker a échoué : aucun ranker n’a été entraîné. Le projet ne
disposait pas d’assez de supervision directionnelle éligible, de résultats
calibrables ni d’un jeu holdout apte à étayer une affirmation pour autoriser l’étape
suivante. Ce refus fait partie du résultat.

## Un exemple concret

Un agent cherche la cause d’un échec d’autorisation. Avant qu’il agisse, un
producteur enregistre trois candidats :

```text
A — corriger localement la condition en échec
B — inspecter le contrat d’autorité et la provenance de ses preuves
C — s’arrêter parce que la décision de produit nécessaire manque
```

Chaque candidat porte des preuves pré-action bornées sur la réussite, les
violations, le coût, l’information et la réversibilité. Latent Compass valide et
scelle cette projection. Un résultat pourra être rattaché à l’épisode plus tard.

Que fait Latent Compass ?

- Il conserve ce qui était connu avant le choix.
- Il rend les mutations silencieuses détectables.
- Il rejoue des politiques hors ligne sur le cas enregistré.
- Il expose l’incertitude et le manque de support.

Que ne fait-il pas ?

- Il ne prétend pas connaître le meilleur candidat.
- Il ne transforme pas un résultat observé en preuve causale.
- Il n’autorise pas un modèle à se promouvoir parce que son score est bon.

## Démarrage rapide

Nécessite Python 3.13 et [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hoklims/latent-compass
cd latent-compass
uv sync --all-groups
```

Lancez le gate complet du dépôt :

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q && uv build
```

Chaque étape peut être exécutée séparément. `pytest` prouve les contrats
comportementaux ; Ruff et mypy ne le font pas.

## Enregistrer et rejouer un épisode

```bash
uv run latent-compass init --root ./store --store-id store-alpha \
    --host-id host-alpha --agent-family claude --epoch LC-2026-E1
uv run latent-compass validate --episode episode.json
uv run latent-compass append   --root ./store --episode episode.json
uv run latent-compass verify   --root ./store
uv run latent-compass replay   --root ./store
uv run latent-compass export   --root ./store --out ./store/snapshot.json
```

Capturez une projection pré-action jugeable :

```bash
uv run latent-compass pairwise capture --projection ./projection.json \
    --root ./run --out ./run/captures/decision-0001.json
```

`--out` doit rester sous `--root`. Omettez cette option pour écrire uniquement
sur la sortie standard. Les codes de sortie font partie du contrat : `0` succès,
`2` erreur d’usage, `3` refus, `4` échec d’intégrité et `5` erreur de stockage ou
de système de fichiers.

## Lancer le benchmark hors ligne

Le corpus inclus contient des données synthétiques de démonstration. Le runner
de validation s’en sert comme instrument de mesure ; les politiques n’atteignent
jamais un agent actif.

```bash
uv run latent-compass benchmark manifest verify \
    --corpus-dir ./corpus/synthetic-v1 --manifest ./corpus/synthetic-v1/manifest.json
uv run latent-compass benchmark run \
    --spec ./corpus/synthetic-v1/spec.json --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json --corpus-dir ./corpus/synthetic-v1 \
    --root ./run --out ./run/report.json
uv run latent-compass benchmark verify --report ./run/report.json \
    --spec ./corpus/synthetic-v1/spec.json --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json --corpus-dir ./corpus/synthetic-v1
uv run latent-compass benchmark holdout plan --report ./run/report.json \
    --spec ./corpus/synthetic-v1/spec.json --protocol ./corpus/synthetic-v1/protocol.json \
    --manifest ./corpus/synthetic-v1/manifest.json --corpus-dir ./corpus/synthetic-v1 \
    --baseline least-uncertainty --root ./run --out ./run/holdout-plan.json
```

Le runner, le vérificateur de rapport et le planificateur de phase 1 ne lisent
jamais le split holdout. La création et la vérification du manifeste lisent en
revanche les trois splits pour calculer et contrôler leurs sceaux.

## Architecture

```text
vocabulary.py           acteurs, capacités, avis, états du cycle de vie
authority.py            refus, transitions, preuves reproduites
episode.py              contrat versionné des épisodes de décision
pairwise_capture.py     projections pré-action et entrées pairwise aveuglées
governance.py           rétention, minimisation, caviardage, suppression
protocol.py             pré-enregistrement, discipline holdout, continue/kill
ledger.py               registre append-only, chaîne, ancre, rejeu, export
benchmark/              benchmark hors ligne scellé des baselines
cli.py                  point d’entrée unique : lire, valider, enregistrer, refuser
canonical.py            sérialisation canonique et sceaux séparés par domaine
contracts.py            versions, primitives strictes, validation
errors.py               vocabulaire typé des refus
```

Les dépendances suivent une seule direction :

```text
errors → canonical → contracts → vocabulary → {episode, protocol}
       → authority → governance → ledger → cli
                   → benchmark → cli
```

`benchmark` n’importe ni `authority` ni `ledger`. La réciproque est vraie. Un
rapport de benchmark est une preuve, jamais une autorisation.

## Ce que les preuves établissent — et ce qu’elles ne peuvent pas établir

Latent Compass sépare cohérence, intégrité, authenticité et autorité. Ces mots
ne désignent pas la même chose.

- Un contrat valide prouve que l’entrée respecte la forme attendue.
- Un sceau recalculé prouve que le contenu correspond à une préimage connue.
- Une chaîne de hachage et une ancre détectent les altérations prévues par leur
  modèle de menace.
- Un bundle Sigstore identifie un workflow et une entrée immuable.
- Rien de tout cela ne prouve qu’un résultat est vrai, qu’une preuve précède une
  décision ou qu’un acteur est autorisé à promouvoir.

Un administrateur capable de réécrire le registre et son ancre locale peut
fabriquer un historique qui se vérifie parfaitement. Détecter cette attaque
exige une ancre externe que ce paquet ne possède pas. Cette limite est
documentée et testée.

## Carte de la documentation

| Document | Objet |
| --- | --- |
| [Frontière d’autorité](docs/authority-boundary.md) | acteurs, capacités, cycle de vie, provenance, modèle de menace |
| [Contrat d’épisode](docs/episode-contract.md) | schéma d’un épisode de décision |
| [Protocole d’évaluation](docs/evaluation-protocol.md) | pré-enregistrement et discipline holdout |
| [Protocole du benchmark](docs/benchmark-protocol.md) | baselines, estimateurs, métriques et limites |
| [Supervision pairwise](docs/pairwise-supervision.md) | labels, résultats, calibration et gate de données |
| [Projection jugeable](docs/judgeable-projection.md) | fichiers compagnons pré-action et dérivation des paires aveuglées |
| [Labeler indépendant](docs/independent-labeler.md) | identité du workflow externe, preuve, rotation et limites |
| [Provenance du corpus](corpus/synthetic-v1/PROVENANCE.md) | production du corpus synthétique et limites de ce qu’il peut établir |
| [Registre](docs/ledger.md) | chaîne, ancre, rejeu, caviardage et limites |
| [Décisions d’architecture](docs/adr/) | raisons qui ont conduit aux frontières actuelles |
| [Gouvernance](GOVERNANCE.md) | gouvernance des données et du projet |
| [Contribuer](CONTRIBUTING.md) | règles de contribution et gate requis |
| [Sécurité](SECURITY.md) | signalement des vulnérabilités |

## Licence

Apache-2.0. Voir [LICENSE](LICENSE) et [NOTICE](NOTICE).

Ce README n’affiche aucun badge de build, de couverture ou de qualité. Aucune
exécution publique et reproductible ne les a encore mérités.
