# Installation et reprise

## Périmètre

Ce guide concerne le CORE Grocyste 1.0.0 sur un hôte Linux, avec Grocy **4.7.1** exécuté dans Docker et accessible derrière une origine HTTPS. Le code cible une installation reproductible depuis ce dépôt ; l’état public des releases et les parcours effectivement qualifiés figurent dans le [rapport de compatibilité](compatibilite.md).

L’installation configure les services, le catalogue et le chargeur. Elle ne crée pas de recettes, de produits, de stock, de courses ou de repas. Le provisionnement d’une identité technique ou des métadonnées de minuteurs passe par les interfaces natives Grocy. Les clés, sauvegardes et reçus restent sur l’hôte de l’instance.

## Préparer l’hôte

- Grocy 4.7.1, son dossier de données réel et son réseau Docker doivent être identifiés.
- Docker Engine avec le plugin `docker compose` et Python 3.10 ou ultérieur doivent être disponibles sur l’hôte. Un accès administrateur à l’hôte, utilisé par `sudo`, est nécessaire pour les dossiers et services système. L’installateur et la migration utilisent uniquement la bibliothèque standard Python ; aucun paquet pip n’est installé sur l’hôte. La construction de l’image apporte Python 3.12, Node.js 24 et les dépendances du runtime, dont `cryptography`.
- Une origine HTTPS publique exacte doit servir Grocy et `/__grocyste` sur la même origine.
- La release doit contenir `trust/catalog.pub`, `catalog.signed.json` et `catalog.signed.sig`, avec les neuf addons du lot. Les archives peuvent être dans le cache local ou disponibles aux URL GitHub du catalogue signé ; la clé privée de publication n’est jamais requise pour installer.
- Un compte administrateur Grocy doit pouvoir appairer l’instance depuis les réglages. Le parcours de secours accepte une clé dans un fichier privé ; aucune clé ne doit être copiée dans `.env`, une ligne de commande ou un dépôt.

Le fichier [`.env.example`](../.env.example) décrit les valeurs non secrètes de Compose :

| Variable | Usage | Exemple générique |
| --- | --- | --- |
| `GROCY_URL` | URL interne accessible sur le réseau Docker. | `http://grocy` |
| `PUBLIC_ORIGIN` | Origine HTTPS exacte du navigateur, sans chemin. | `https://grocy.example.org` |
| `GROCYSTE_BASE_PATH` | Préfixe public du CORE. | `/__grocyste` |
| `GROCY_DOCKER_NETWORK` | Réseau Docker déjà utilisé par Grocy. | `grocy_default` |
| `GROCYSTE_HOME` | Racine des états, paquets et socket. | `/opt/grocyste` |

Ce sont des exemples à adapter, pas les coordonnées d’une installation publique. Le chemin de base est communiqué au chargeur lors de la migration ; un changement ultérieur demande une configuration cohérente.

## Commande d’installation

Le point d’entrée unique est `scripts/install.sh`, qui délègue à `scripts/install.py`. Exécutez-le depuis une copie vérifiée de la release, sur l’hôte Grocy. Pour le cas Docker/Caddy reconnu :

```sh
sudo ./scripts/install.sh --origin https://grocy.example.org --caddyfile /opt/grocy/Caddyfile
```

Adaptez ces deux valeurs. L’outil détecte le dossier `data` dans le montage `/config` du conteneur nommé `grocy`, utilise `http://grocy` comme URL interne et attend le réseau `grocy_default`. Il crée les états et la configuration d’instance, puis construit l’image. Dans cette image, il vérifie le catalogue signé et installe les neuf addons avec leurs versions explicites ; les archives locales sont réutilisées, sinon le gestionnaire télécharge les archives du catalogue et vérifie leurs signatures et empreintes.

Si ce site utilise déjà le service live historique `mon-grocy-live`, sa reprise doit être demandée explicitement dans la même commande :

```sh
sudo ./scripts/install.sh --origin https://grocy.example.org --caddyfile /opt/grocy/Caddyfile --legacy-live-container mon-grocy-live
```

Sans cette option, un site Caddy ciblant ce service historique est refusé avant toute écriture. Une installation neuve ou une autre instance ne recherche ni n’arrête ce conteneur global : l’option engage la vérification de son identité et la conservation de son état avant la bascule.

L’installateur garde un verrou système pendant le parcours complet. Après un éventuel appairage par fichier privé, les services démarrent. Il contrôle l’API et les octets du chargeur, le socket du gestionnaire et le service live, puis configure Caddy et vérifie l’accès HTTPS de même origine. Les anciens services reconnus qui écrivent le chargeur sont retirés avant la sauvegarde et le reçu de migration final. Cette préparation tardive tient compte des changements survenus pendant la construction ; une divergence lors de l’activation reste bloquante.

Pour Caddy, l’outil inspecte les montages et les métadonnées Compose du conteneur indiqué. Il sauvegarde le fichier d’origine, valide le candidat dans Caddy puis publie atomiquement le fichier sur l’hôte. Un montage de répertoire permet un rechargement. Un montage du fichier `/etc/caddy/Caddyfile` exige la recréation du seul service Caddy identifié : projet, répertoire, fichiers Compose, configuration de montage et identité du conteneur doivent correspondre. La commande utilise `--no-deps`, `--no-build` et `--pull never` ; les autres services ne sont pas recréés. Caddy peut être indisponible brièvement pendant cette étape. Ces options sont décrites dans la [documentation Docker Compose](https://docs.docker.com/reference/cli/docker/compose/up/).

Les options permettent de décrire une autre implantation sans autodétection :

| Option | Effet |
| --- | --- |
| `--home /opt/grocyste` | Racine d’installation, états, paquets et reçus. |
| `--data /chemin/grocy/data` | Dossier réel contenant `grocy.db`. |
| `--grocy-url http://grocy` | URL interne accessible depuis les services. |
| `--network grocy_default` | Réseau Docker existant. |
| `--origin https://grocy.example.org` | Origine HTTPS de même origine que Grocy. |
| `--base-path /__grocyste` | Chemin du CORE et du chargeur. |
| `--caddyfile /opt/grocy/Caddyfile` | Fichier Caddy existant, bloc du site ciblé. |
| `--caddy-container grocy-caddy` | Conteneur inspecté pour valider puis recharger ou recréer le seul service Caddy. |
| `--legacy-live-container mon-grocy-live` | Reprendre explicitement le propriétaire live historique reconnu et conserver ses sessions avant la bascule. |
| `--packages /chemin/paquets` | Cache facultatif des archives signées ; les versions restent celles du catalogue signé. |
| `--prepare-only` | Préparer configuration et sauvegarde sans construire l’image, démarrer les services, modifier Caddy ou activer le chargeur. |
| `--admin-key-file /chemin/prive/cle` | Secours : clé administrateur de provisionnement dans un fichier `0600`. |
| `--existing-key-file /chemin/prive/cle` | Secours : clé de service déjà créée dans un fichier `0600`. |

Consultez `./scripts/install.sh --help` pour les options de la release installée. Les deux options de fichier de clé sont exclusives. L’installateur prépare une copie temporaire privée lisible par l’utilisateur du conteneur et la supprime après le provisionnement, même en cas d’échec. Le projet Compose est dérivé du répertoire d’état, afin de conserver un seul propriétaire lors d’un changement de dossier de release. Une réexécution réutilise les paquets reconnus ; une divergence nécessite une réconciliation. Une commande de téléchargement public ne peut être annoncée comme utilisable avant publication des assets et de leur clé de confiance.

Sans `--caddyfile`, l’outil écrit un fragment `proxy-fragment.caddy` sous `GROCYSTE_HOME`. Si la route HTTPS n’est pas déjà prête, il retourne `prepared-awaiting-proxy` et conserve le chargeur actuel. Intégrez et validez le routage `/__grocyste/*` de même origine dans le proxy réellement utilisé, puis réexécutez l’installation. Ce mode ne configure pas automatiquement Nginx, Apache ou un autre fournisseur de proxy. Grocy installé comme PHP natif demande `--data`, `--grocy-url` et un réseau Docker permettant aux services de joindre cette URL ; ce parcours demande sa propre qualification.

Si un Caddyfile monté comme fichier ne possède pas les métadonnées Compose nécessaires, ou si le service comporte plusieurs conteneurs, l’outil conserve le candidat validé sur l’hôte et retourne `prepared-awaiting-caddy-recreate`. Recréez uniquement le conteneur Caddy depuis sa configuration réelle, puis réexécutez l’installation. Le chargeur reste inchangé dans cet état et l’installation n’est pas annoncée complète.

## Configuration des services

Compose sépare le CORE du gestionnaire. Le CORE est lié à `127.0.0.1:8788`, lit les paquets et accède à Grocy via son réseau. Le gestionnaire écrit les paquets, communique par socket Unix et ne reçoit aucun identifiant Grocy. Les conteneurs utilisent un compte non privilégié, une racine en lecture seule et des volumes dédiés.

Sur l’hôte, conservez séparément :

| Chemin sous `GROCYSTE_HOME` | Contenu |
| --- | --- |
| `state/` | État compagnon et secrets du service. |
| `state/secrets/grocy.json` | Clé de service privée, mode `0600`, propriétaire `1000:1000`. |
| `state/config/instance.json` | Métadonnées de l’instance, fichier `0600`, propriétaire `1000:1000`. |
| `state/live/` | État propre aux sessions live. |
| `packages/` | Catalogue signé, paquets, registre actif et historique. |
| `run/` | Socket privé du gestionnaire. |

La clé publique doit être installée à l’emplacement attendu par le gestionnaire. Une absence de clé ou une signature invalide doit interrompre le parcours ; remplacer la clé de confiance n’est pas un moyen de contourner un échec de vérification. Les fichiers du catalogue et de signature sont couplés : `catalog.signed.json` et `catalog.signed.sig`.

Le fichier `state/config/instance.json` est créé avant le démarrage. Le service live monte le répertoire `state/config` en lecture seule, pour voir les remplacements atomiques de ce fichier sans avoir accès au répertoire des secrets. Une configuration héritée dans `state/instance.json` est copiée et conservée ; les métadonnées existantes sont préservées.

Le service live nécessite un paquet SharedTimers signé, actif et compatible ainsi qu’une configuration explicite de l’instance. Le script `scripts/live-bridge.mjs` vérifie l’entrée runtime déclarée avant de la charger. Sans ce service, les fonctions live doivent rester annoncées comme indisponibles.

Lorsque la reprise de l’ancien propriétaire live reconnu est explicitement demandée, l’installateur conserve deux copies privées de son état : avant l’arrêt puis après l’arrêt confirmé et la désactivation du redémarrage automatique. Sessions et commandes sont reprises dans le nouvel état compagnon. Les anciens minuteurs locaux déjà annulés sont archivés ; tout minuteur local dans un autre état exige une réconciliation. La migration de cet état n’écrit aucun minuteur dans Grocy : les minuteurs natifs restent la référence. Une identité de conteneur, un montage ou une destination déjà utilisée qui ne correspondent pas au contrat font échouer cette reprise.

## Migration du chargeur

L’outil de migration sait reconnaître les assemblages NerdCore du lot et l’inclusion de budget attendue. Il refuse les chargeurs modifiés, doublons ou compositions ambiguës. La préparation sauvegarde les personnalisations et les ressources actives, fait une sauvegarde SQLite cohérente en lecture seule et calcule les empreintes des tables métier suivies.

Pour examiner ce contrat séparément de l’installateur, les sous-commandes Python sont :

```sh
python -m grocyste.migration prepare --data /chemin/grocy/data --receipts /chemin/prive/recus --base-path /__grocyste
python -m grocyste.migration activate /chemin/prive/recus/IDENTIFIANT
python -m grocyste.migration rollback /chemin/prive/recus/IDENTIFIANT
```

Ces chemins sont illustratifs. L’activation n’est possible que si le reçu, le chargeur et les empreintes correspondent toujours à la préparation. La publication du chargeur conserve son propriétaire et son mode Grocy. Gardez les reçus et sauvegardes hors de GitHub. L’installateur prépare le reçu final après construction et contrôles de santé ; les commandes manuelles servent au diagnostic et à la reprise d’une installation identifiée.

Avant l’activation, l’installateur retire uniquement les quatre services reconnus `nerdcore`, `nerdstats`, `product-helper` et `nerdcore-update-api` rattachés aux données de l’instance ciblée. Des services aux mêmes noms mais liés à une autre instance sont ignorés. Il vérifie leurs identités et blocs Compose, conserve des sauvegardes privées, retire les références explicites à ces services dans les listes de dépendances et vérifie que tous les autres réglages et dépendances restent identiques, désactive leur redémarrage, les arrête et retire leurs conteneurs. La route historique d’administration doit répondre `410`. L’ancien secret perd ainsi ses vérificateurs actifs ; ce retrait ne constitue pas une rotation cryptographique du secret. Si ces services sont absents d’une installation neuve, le reçu indique cette absence sans prétendre prouver l’invalidation d’un secret historique.

Le retour du chargeur retire Grocyste et conserve les personnalisations étrangères, avec Grocy natif. Il ne réactive ni NerdCore, ni les anciens services, ni la route d’administration historique. Les sauvegardes privées ne doivent pas être restaurées automatiquement pour les recréer. Une composition ambiguë ou modifiée exige une réconciliation ciblée avant poursuite.

## Utiliser et vérifier

Connectez-vous à Grocy puis ouvrez **Réglages → Grocyste — Assaisonnements**, ou `/stocksettings?grocyste=1`. Avec un compte administrateur, cliquez sur **Relier cette instance Grocy** si l’appairage n’a pas été effectué par le parcours de secours. Le serveur vérifie ce compte puis provisionne le service et les métadonnées de minuteurs via les interfaces natives Grocy. Rechargez la page après confirmation.

Le panneau montre le registre connu et les addons chargés. Les boutons de gestion sont réservés au compte administrateur et restent contrôlés par le serveur.

L’installation d’un paquet passe par l’identifiant et la version explicites du catalogue signé. Une dépendance active ne peut pas être désactivée tant qu’un addon l’utilise. Le retour d’un paquet exige une version précédente connue et compatible ; rechargez la page après une modification du registre pour actualiser les scripts déjà chargés.

**Désinstaller** retire l’addon du registre, sous contrôle administrateur. Une dépendance encore utilisée bloque ce retrait. Le cache des versions et l’historique du registre sont conservés pour permettre une reprise contrôlée ; ce bouton n’efface ni les données métier Grocy ni les documents du compagnon. Rechargez la page pour ne plus exécuter le script déjà chargé.

Le rapport de livraison doit constater le chargement de chaque addon retenu, l’authentification d’un compte ordinaire et d’un administrateur, les parcours Grocy natifs, les contrats Android et live réellement utilisés, la réexécution sans doublons et les retours testés. Publiez uniquement les parcours attestés.

## Réagir à un refus ou une coupure

| Situation | Action |
| --- | --- |
| Signature, empreinte ou version incompatible | Conserver le registre courant et vérifier les assets et la clé publique de la release. |
| Personnalisation ou données modifiées après préparation | Examiner les différences et préparer un nouveau reçu cohérent. |
| Conflit de document ou génération | Recharger l’état actuel, puis réévaluer la modification. |
| Réponse perdue après mutation | Vérifier l’effet réel et réconcilier l’opération ; ne pas relancer avec une nouvelle clé d’idempotence. |
| Version précédente absente | Conserver la version courante ou désactiver l’addon si aucune dépendance active ne l’exige ; un retour automatique n’est pas disponible. |
| Service live absent | Vérifier SharedTimers et sa configuration ; ne pas présenter le live comme opérationnel. |
| Contrôle HTTP local ou HTTPS échoué | Corriger services ou routage puis réexécuter ; aucun chargeur de ce lot n’est activé. |
| Caddy sans métadonnées Compose suffisantes | Recréer ce seul conteneur avec sa configuration réelle, vérifier HTTPS puis réexécuter l’installation. |
| Recréation Caddy échouée | Examiner la configuration validée et la sauvegarde privée ; ne pas réactiver l’ancien accès d’administration NerdCore. |

La sauvegarde du reçu est une preuve de migration et une ressource de reprise ciblée. Elle ne doit pas être restaurée par-dessus une base ayant reçu de nouveaux changements métier sans examen de ces changements.
