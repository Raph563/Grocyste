# Qualification sécurité et résilience

SPDX-License-Identifier: GPL-3.0-or-later

État au 2 octobre 2026 : des contrôles automatisés et des exercices réels ont
été exécutés dans un laboratoire isolé. Ce document décrit leurs résultats et
leurs limites ; il ne constitue pas une certification ni une preuve d'absence
de vulnérabilités. Aucune bascule du CORE ni retraite de services historiques
en production n'a été réalisée pendant ces exercices.

## Périmètre et conservation des preuves

Les services de test utilisent une instance Grocy 4.7.1 neuve, un clone privé,
un proxy HTTPS local et des états compagnons distincts. Les scripts de
qualification refusent les chemins et origines extérieurs au laboratoire.
Les exercices de paquets utilisent des clés synthétiques, un système de
fichiers jetable et aucun accès réseau. Les archives destinées à la publication
sont vérifiées avec leur clé publique réelle, en lecture seule.

Les rapports détaillés restent dans le répertoire privé `lab/security` du VPS.
Les cookies, mots de passe, clés API, bases Grocy, reçus contenant des chemins
privés et données de courses ne doivent pas être commis. Les scripts publics
dans `tests/integration` suffisent à reproduire les scénarios avec des fixtures
privées séparées. Une suite verte ne couvre pas tous les états possibles.

| Exercice exécuté | Résultat | Preuve privée / script public |
| --- | --- | --- |
| Suite Python Linux finale, uploads et messages du socket compris | 165 tests réussis, un avertissement attendu ZIP dupliqué | `retirement-dependencies-linux-suite.log` ; `tests/` |
| Audit de l'environnement Python aux versions et hashes figés | 43 distributions, aucune vulnérabilité connue retournée, exit 0 | `pip-audit-final.json` |
| Bibliothèques navigateur livrées : provenance + advisory inventory | 208 fichiers vérifiés, six versions npm ; OSV et endpoint npm sans advisory directe | `vendor-audit-final.json` ; `audit_vendor_dependencies.py` |
| HTTP réel via proxy HTTPS laboratoire | 78 contrôles réussis ; nettoyage confirmé | `http-auth.json` ; `qualify_http_security.py` |
| Migration exacte chargeur + budget PHP historique | Migration, rejeu, dérive refusée, retour natif et restauration du clone réussis | `migration-*/report.json` ; `qualify_migration.py` |
| Retraite des quatre anciens services sur conteneurs jetables | Interruption/reprise/rejeu réussis ; service témoin inchangé | `legacy-*/report.json` ; `qualify_legacy_retirement.py` |
| Faute réelle et concurrence sur tmpfs jetable | 14 scénarios réussis ; neuf archives finales revalidées | `package-resilience-release-image.json` ; `qualify_package_resilience.py` |
| Interruption réelle de migration du chargeur | Quatre scénarios SIGKILL/reprise/dérive réussis | `migration-crash-release-image.json` ; `qualify_migration_crash.py` |
| Reprise du live historique sélectionné | Quatre contrôles réussis ; aucun minuteur natif écrit | `live-migration.json` ; `qualify_live_migration.py` |
| Deux suites historiques catalogue | 11 tests : 3 réussis, exactement 8 échecs connus reproduits | `legacy-catalog-test.tap` |

Cette suite inclut la désinstallation ciblée, la publication concurrente de
la clé du coffre, le refus d'une restauration sans clé et les cas de
reprise d'activation interrompue. Le socket privé restitue le refus de dépendance
dans un message français. Les résultats d'interface sont détaillés dans le
rapport de compatibilité.

## Authentification et droits : contrôles réellement exécutés

Le script HTTP utilise deux comptes temporaires et une ressource de test
qu'il nettoie. Il vérifie l'accès anonyme refusé aux données privées, l'appairage
par cookie Grocy, les sessions opaques et les attributs `Secure`, `HttpOnly` et
`SameSite=Strict`. Un en-tête de clé API ne remplace pas le cookie utilisateur.
Les origines absentes, étrangères ou utilisant un autre port, les requêtes
signalées cross-site, les jetons CSRF absents ou incorrects et un changement
de cookie Grocy sont refusés.

Le compte limité ne peut pas utiliser les opérations administrateur, le coffre
ni les appels avec le compte service. Le changement de permissions Grocy est
pris en compte par la session CORE existante. La déconnexion Grocy et la
révocation de la session CORE ferment effectivement l'accès. Les chemins de
clés API et de sessions Grocy sont interdits au proxy du CORE, y compris pour
l'administrateur. Les requêtes avec clés JSON dupliquées, valeurs non finies,
traversées de chemins, secrets dans les settings et payloads excessifs sont
refusées. Les réponses privées ont des en-têtes de cache et de contenu adaptés.

Une mutation native sur une seule ressource synthétique a été exécutée via le
CORE : rejouer la même clé d'idempotence rend le même résultat et ne crée pas
de deuxième ressource ; réutiliser cette clé avec un autre corps rend 409.
Les opérations dont l'issue réseau est incertaine restent à réconcilier ; leur
rejeu automatique est refusé. Ceci n'est pas une transaction distribuée entre
Grocy et le compagnon, et une réponse perdue exige une vérification explicite.

Le même exercice a refusé HTML, SVG et un contournement d'upload par JSON,
puis confirmé l'absence du fichier natif correspondant. Une vraie image PNG
synthétique a été envoyée, rejouée avec la même clé, relue octet pour octet et
supprimée ; le GET final a répondu 404. Ces contrôles vérifient l'enveloppe et
les chemins exercés, sans prétendre décoder exhaustivement tous les codecs.

Le bootstrap est couvert par les tests du runtime et par un appairage navigateur
réel dans le laboratoire. Le compte service est identifié avant capture de sa
clé officielle et reçoit ses droits ADMIN après cette capture. Sa clé reste
côté serveur. Les tests de reprise ne justifient pas de recréer aveuglément un
compte après une réponse perdue ; les comptes possédés sont suivis et nettoyés.

## Réseau externe et credentials

Le proxy externe refuse les schémas non HTTPS, identifiants dans les URL,
adresses IP locales ou privées, ports inattendus et en-têtes contenant une clé
de fournisseur en clair. Les destinations et chemins autorisés sont bornés.
Les redirections sont revalidées et la résolution DNS est contrôlée avant
connexion. Les tests unitaires couvrent la destination, la résolution, les
limites et les en-têtes ; les essais HTTP réels couvrent les refus évidents de
SSRF et de credentials en clair.

Le coffre chiffre les credentials des fournisseurs dans la base du compagnon
avec une clé privée distincte. Une course réelle à sa première utilisation a
été reproduite : un second worker pouvait voir le fichier de clé encore vide.
Le correctif verrouille les lecteurs et écrivains entre processus et publie
la clé complète atomiquement. Le test de régression reproduit le défaut avant
correction et passe ensuite. Une clé existante corrompue ne doit pas être
régénérée silencieusement, sous peine de rendre les anciens credentials
illisibles. Si la clé a disparu alors que des credentials chiffrés existent,
toute lecture ou nouvelle écriture est refusée jusqu'à une restauration
explicite de la clé privée. Le scénario réel confirme que les ciphertexts
restent récupérables après remise de la clé sauvegardée. Aucun appel facturé
à un fournisseur AI réel n'a été qualifié ici.

## Paquets et publication

Le gestionnaire n'exécute aucun script d'installation fourni par un addon. Les
tests rejettent une mauvaise signature, la substitution de manifeste, une
empreinte incorrecte, des fichiers non déclarés, doublons ZIP, liens symboliques,
traversées et chemins ambigus. Les versions, dépendances, cycles et mises à jour
qui casseraient une dépendance active sont contrôlés. Une cible inconnue ne
devient jamais une mise à jour globale. La corruption du registre est refusée
sans réinitialiser silencieusement l'état ; les fichiers d'une version déjà
installée sont revérifiés.

La désinstallation retire seulement l'entrée ciblée du registre, sous verrou,
et refuse les dépendances encore requises par un addon actif. Les paquets
installés, archives et historiques restent disponibles ; leur contenu est
revérifié à la réinstallation. Les tests exécutent le vrai socket Unix et son
journal de job, ainsi que les contrôles ADMIN/CSRF et le rejeu idempotent du
runtime. Un `SIGKILL` juste avant publication de la désinstallation préserve
la génération précédente ; la reprise, réinstallation, mise à jour et rollback
conservent des générations cohérentes sans modifier les autres addons.

Le script de résilience a effectivement envoyé `SIGKILL` à deux processus
gestionnaires, après extraction et juste avant le remplacement atomique du
registre. Le registre précédent est resté intact et les reprises ont produit
une seule nouvelle génération. Sur un tmpfs limité à 8 MiB, une réserve de
7 606 272 octets a provoqué `ENOSPC` réel ; un répertoire non accessible en
écriture a provoqué `EACCES` réel pour uid 1000. Dans les deux cas, l'ancienne
génération a été conservée et l'installation a réussi après correction de la
condition. Ces exercices couvrent ces points précis, pas une perte électrique
du système de fichiers hôte.

Six processus ont installé six addons sans perdre de génération, enregistré
leurs credentials lors du premier usage sans clé partielle et effectué
soixante mises à jour de document sans perte. Parmi six tentatives d'une même
opération, une seule a été admise ; les autres ont été refusées tant qu'elle
était pending, puis le rejeu après état incertain a été bloqué. Un registre
JSON dupliqué, une clé de coffre corrompue et une archive signée excessivement
compressée ont été refusés sans réinitialisation silencieuse. Les neuf archives
réelles du cache ont été authentifiées ; une copie avec capacités modifiées
sans nouvelle signature et une copie avec payload modifié ont été refusées.

Les signatures authentifient les octets et leur éditeur. Elles ne prouvent pas
qu'un programme signé est exempt de bugs, de comportements malveillants ou de
capacités excessives. La chaîne de confiance reste limitée à la protection de
la clé de signature et à la revue des releases. Le téléchargement depuis les
releases publiques ne peut être qualifié avant que les neuf assets signés et
leurs URL existent effectivement sur GitHub. L'installation depuis le cache
privé local a été utilisée dans le laboratoire.

## Migration historique et retour natif

La fixture utilise le chargeur de migration réellement observé, SHA-256
`87b88f2ac0ee463a68bbc4ad0fde5921450bee77d4749d939e139f03a45c43e6`, et le budget PHP
actif SHA-256
`37d0902fd23131d30a65e004b9ecf79c11a90716f67332e52c1dc0dc48cc14ab`.
Les quatre loaders historiques et la ligne qui inclut ce budget sont retirés
au profit du chargeur CORE unique. Les modifications externes du loader sont
détectées et empêchent le rollback de les écraser.

Les empreintes des douze tables métier protégées, du schéma SQLite, des triggers
et des vues restent identiques dans le clone de sécurité. La sauvegarde SQLite
est contrôlée par `integrity_check`, puis restaurée dans un fichier distinct
et ses empreintes sont recomparées. Le clone source reste intact. Aucun import
métier ni entretien SQL n'a été exécuté par ce scénario.

Le rollback qualifié retourne à un chargeur Grocy natif neutre. Il ne restaure
pas les anciennes lignes PHP ou JS ni la route privilégiée. Les sauvegardes
historiques restent privées et servent à l'audit. Il ne faut pas réinjecter un
ancien `compose.before`, recréer un ancien conteneur ou restaurer un ancien
proxy : cela réouvrirait les vérificateurs et services retirés.

La retraite des quatre services a été exercée sur de vrais conteneurs Docker
avec noms préfixés laboratoire. L'adaptateur ne traduit que les métadonnées
d'identité nécessaires au contrat ; les commandes de mutation utilisent des
IDs capturés de ces seuls conteneurs. Un HTTP 410 synthétique est exigé avant
retrait, une interruption est injectée avant le premier arrêt, puis la reprise
retire exactement les quatre services et laisse un cinquième service témoin
inchangé. Ce test ne prouve pas la fermeture de tous les chemins d'un proxy de
production. La suppression des anciens vérificateurs rend l'ancien token
inutilisable à cet endroit ; ce n'est pas une rotation cryptographique de
toutes ses copies historiques.

La migration du propriétaire live et l'activation permanente du proxy ont leurs
propres contrôles. Les états de minuteurs dont l'origine ou la situation exige
une réconciliation bloquent l'activation. Un ancien live n'est sélectionné que
par une option explicite ou un reçu possédé. Une instance étrangère et ses
conteneurs ne doivent pas être affectés par une installation de laboratoire.

## Reproductibilité et audit Python

Les images de base Python 3.12.14 et Node 24.21.0 sont référencées par digest de
manifeste. Les fichiers `requirements-bootstrap.lock.txt`,
`requirements.lock.txt` et `requirements-dev.lock.txt` fixent toutes les
distributions observées dans les images auditées, avec les SHA-256 des roues
non retirées fournis par l'API de PyPI. L'installation Docker exige ces hashes
et des roues binaires. Le générateur public `lock_audited_images.py` documente
l'origine des versions et hashes.

Le premier audit trouvait des advisories dans le pip hérité de l'image. Après
mise à niveau vers pip 26.2.1, l'audit des 43 distributions ne retournait aucune
vulnérabilité connue. Un audit Python ne couvre pas les paquets Debian, PHP,
Grocy, les bibliothèques JS/WASM embarquées ni de nouvelles advisories publiées
ultérieurement. Les paquets apt et les métadonnées de build ne sont pas figés
par ce mécanisme : il ne garantit pas une image entière identique bit à bit.

Sources primaires : [API JSON PyPI](https://docs.pypi.org/api/json/),
[release pip 26.2.1](https://pypi.org/project/pip/),
[inspection des manifests Docker](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/).

## Bibliothèques JS et WASM embarquées

Le contrôle de provenance a vérifié les tailles et SHA-256 des 208 fichiers
déclarés par ReceiptScanner et StatNerd. Les six versions npm vérifiées sont
Chart.js 2.9.4, Tesseract.js 5.1.1, Tesseract.js-core 5.1.1, pdfjs-dist 4.10.38
et les paquets de modèles `@tesseract.js-data/eng` et `fra` 1.0.0.
Les requêtes OSV et le endpoint d'advisories npm ne retournent aucune advisory
pour ces versions exactes. Une fixture npm séparée, sans exécution de scripts,
a résolu leurs dépendances actuelles ; `npm audit --omit=dev --json` retourne
zéro vulnérabilité sur le graphe de 36 dépendances décrit par ses métadonnées.
Ce graphe résolu aujourd'hui n'est pas une preuve de la composition historique
des bundles précompilés.

Les lockfiles amont aux tags exacts de Chart.js et Tesseract.js ont aussi été
interrogés : parmi vingt couples paquet/version runtime, Moment 2.24.0 remonte
deux advisories High. Ce Moment n'est pas embarqué dans l'asset livré
`Chart.min.js` : sa configuration de build déclare Moment externe et le fichier
vendored importe le global existant. `Chart.bundle.min.js`, qui inclurait Moment,
n'est pas livré. Le Moment éventuellement présent dans Grocy appartient au
runtime natif et ne doit pas être confondu avec une nouvelle dépendance de
l'addon. Les avis décrivent respectivement une traversée de fichiers dans le
runtime Node et un ReDoS sur de longues dates. [Build Chart.js 2.9.4](https://raw.githubusercontent.com/chartjs/Chart.js/v2.9.4/rollup.config.js),
[GHSA-8hfj-j24r-96c4](https://github.com/advisories/GHSA-8hfj-j24r-96c4),
[GHSA-wc69-rhjr-hc9g](https://github.com/advisories/GHSA-wc69-rhjr-hc9g).

La pollution de prototype connue de Chart.js est corrigée en 2.9.4. La faille
d'exécution JS lors du chargement d'un PDF décrite par Mozilla touche les
versions jusqu'à 4.1.392 et est corrigée en 4.2.67 ; la version livrée 4.10.38
est hors de cette plage. Ces observations portent sur ces advisories précises.
[Avis Chart.js](https://github.com/advisories/GHSA-h68q-55jf-x68w),
[avis Mozilla PDF.js](https://github.com/mozilla/pdf.js/security/advisories/GHSA-wgrm-67xf-hhpq).

Deux advisories Moderate publiées en 2026 concernent le moteur Tesseract natif
avant 5.5.3 et le chargement de fichiers `.traineddata` malveillants. La version
npm `tesseract.js-core` ne permet pas, à elle seule, d'attester la version de
tous les composants C++ compilés dans les WASM. La mitigation recommandée par
les mainteneurs est d'utiliser des modèles de confiance : l'addon fixe les
chemins worker/core/lang vers les assets locaux déclarés et signés et ne
propose pas de chargement de modèles utilisateur. Cela borne ce vecteur, sans
constituer un audit exhaustif des codecs ou de Leptonica embarqués.
[GHSA-7j76-5rq5-5jg8](https://github.com/tesseract-ocr/tesseract/security/advisories/GHSA-7j76-5rq5-5jg8),
[GHSA-x3vq-7rr7-5x3h](https://github.com/tesseract-ocr/tesseract/security/advisories/GHSA-x3vq-7rr7-5x3h).

## Huit échecs historiques du catalogue

L'exécution Node 24.21.0 de `catalog-price.test.mjs` et
`catalog-product.test.mjs` dans une copie publique du checkout historique
reproduit les deux échecs prix et les six échecs produits. Ce sont les échecs
déjà documentés avant Grocyste ; ils ne sont pas comptés comme réussites du
nouveau CORE et n'ont pas été corrigés en modifiant les assertions.

Les deux cas prix concernent la sélection de remise immédiate et la fermeture
des schémas/bornes UTC. Les six cas produits concernent les rubriques
historiques, les photos préparées, le poids variable, la preuve GTIN,
la réconciliation identité/SKU/GTIN/URL et les schémas/unités compatibles.
La plupart échouent sur la validation du nom de fichier de la photo préparée.
Ces fonctions ne justifient aucune exécution d'import catalogue en production.

## Limites matérielles

Les addons classiques partagent le même contexte JavaScript que Grocy et le
SDK. Leurs identifiants et capacités ne constituent pas une frontière de
sandbox contre un script malveillant : ce script peut appeler un autre scope,
modifier le DOM ou utiliser le cookie natif de l'utilisateur. Le passage par
le CORE est le contrat d'implémentation des addons livrés, contrôlé par leur
code et leur signature. Pour isoler des éditeurs non fiables, il faudrait une
origine séparée, une sandbox et un protocole de messages explicitement borné.

Le baseline natif Grocy de laboratoire contient un défaut amont confirmé : un
utilisateur limité peut supprimer la clé API appartenant à un autre utilisateur
via `/api/objects/api_keys/<id>`. Le proxy CORE refuse ce chemin, mais le défaut
natif reste accessible aux clients Grocy directs. Aucun correctif de Grocy
upstream ni scan agressif sur la production n'a été exécuté dans ce lot.

Les accès d'une véritable application Android, les variantes Grocy autres que
4.7.1, les interruptions du noyau ou d'alimentation, les systèmes de fichiers
distants, un attaquant ayant root sur l'hôte et les attaques de charge prolongées
ne sont pas couverts. Les tests de fermeture d'anciens services ne valent que
pour les identités et chemins explicitement reconnus. Toute dérive bloque la
bascule et requiert une action ciblée. Les données et secrets réels de
production ne sont pas nécessaires pour reproduire ces contrôles.

La qualification finale ajoute un défaut reproduit sur la configuration réelle : Caddy dépendait encore de services de recomposition à retirer. Le retrait gère leurs références explicites en liste, puis compare toute la configuration Compose résolue des services restants. Un aperçu en lecture de la configuration active a confirmé cette égalité ; une syntaxe de dépendance non reconnue reste bloquante. Le nom de projet Compose est également stable lorsque le dossier de sources change, ce qui évite la création de deux propriétaires du même état.

La bascule réelle et ses 26 contrôles, réalisée ensuite sans test destructif sur la production, est décrite dans le [rapport de livraison](livraison-1.0.0.md). Le rejeu ciblé conserve le chargeur, le registre, la clé de service, l’état live et les douze tables métier.
