# Compatibilité et validation

## Versions déclarées

| Élément | Version ou contrainte du lot | Portée |
| --- | --- | --- |
| Grocy | **4.7.1 uniquement** | Cible déclarée par les neuf manifestes. |
| CORE Grocyste | `1.0.0` | Les addons demandent `>=1.0.0 <2.0.0`. |
| API Grocyste | `v1` | Chemin par défaut `/__grocyste/v1`. |
| Python | `>=3.12` ; image `3.12.14` | CORE et outils. CI prévue sous Python 3.12 Linux. |
| Node.js | 24 ; image `24.21.0` | SDK, construction des assets et runtime live. |
| RecipeScaling, RecipeLive, Budgets, Equivalents, SharedTimers, SafeImport | `1.0.0` chacun | Versions de leurs manifestes, dépôts séparés. |
| StatNerd | `4.4.2` | Version du manifeste adapté. |
| ProductHelper | `4.0.43` | Version du manifeste adapté. |
| ReceiptScanner | `1.0.4` | Version du manifeste adapté. |

Une version déclarée désigne un contrat à vérifier. Elle ne prouve ni l’existence d’une release GitHub ni le fonctionnement d’une instance migrée. Toute autre version Grocy est hors de ce périmètre jusqu’à qualification explicite.

## Niveau de preuve de cette documentation

Les manifestes des neuf addons et le code du CORE ont été relus pour établir le catalogue, les dépendances et les limites documentées. Les tests Python et JavaScript existent dans le dépôt. **La présente documentation ne prétend pas que les tests, la migration de production ou les neuf parcours fonctionnels ont tous réussi.** Le rapport final de livraison doit fournir les résultats exacts avant d’étendre cette affirmation.

| Vérification | État documenté | Preuve à rattacher au rapport de livraison |
| --- | --- | --- |
| Identités, versions, dépendances et capacités des neuf addons | Manifestes relus | Commits exacts et manifestes finaux signés. |
| Tests du CORE et du SDK | Suites disponibles, résultats non attestés ici | Commandes, environnement, compte de tests et code de sortie. |
| Construction et paquetage des neuf addons | Neuf archives signées préparées localement ; publication en attente | Empreintes, signatures et résultat de vérification des archives. |
| Installation, réexécution, désactivation, retour de paquet | Contrats et tests présents, qualification réelle à compléter | Résultats sur Grocy 4.7.1 isolé, générations et absence de doublons. |
| Migration et retour du chargeur | Reçus et contrôles implémentés, exécution à compléter | Comparaison des personnalisations et des empreintes métier avant/après. |
| Parcours navigateur des neuf addons | À qualifier individuellement | Parcours exact, résultat observé, navigateur et version Grocy. |
| Grocy natif et Mon Grocy Android | Contrats à préserver, validation à compléter | Parcours natifs et live réellement exercés. |
| Publication GitHub | Dépôts et releases de destination désignés | URLs, tags, commits et empreintes des assets publics. |

La CI exécute les suites du dépôt ; elle ne valide pas une production et n’a besoin d’aucune clé privée de publication. Un échec ou un parcours non exercé doit rester visible dans le rapport final. Le catalogue d’addons utilisables en production doit être limité aux parcours qui y sont effectivement vérifiés.

Le fichier `catalog.json` présente les résultats automatisés déclarés pour chaque addon, avec `normalGrocyQualification: pending` tant que son parcours réel n’a pas été qualifié. La valeur `tested` n’est donc pas une validation de production. Les archives et le catalogue signé se préparent localement avec la clé publique de `trust/catalog.pub` ; les URLs de release ne constituent pas encore une preuve de disponibilité publique.

Les dépendances effectives sont : RecipeLive → RecipeScaling et SharedTimers ; Budgets → RecipeScaling ; Equivalents → Budgets ; ReceiptScanner → StatNerd ; ProductHelper → StatNerd et ReceiptScanner. Les manifestes sont la référence pour leurs plages de versions.

## Navigateur et confiance

Le SDK utilise les primitives modernes du navigateur : `fetch`, `Headers`, `Response`, `crypto.randomUUID`, cookies de même origine et, lorsqu’il est disponible, `AbortSignal.timeout`. L’installation publique attend HTTPS. Un navigateur différent de celui du rapport n’est pas présenté comme validé par défaut.

Tous les scripts d’addons partagent le contexte JavaScript de la page Grocy. Il n’y a pas d’iframe isolante ou de sandbox entre addons. Les signatures vérifient l’origine et l’intégrité d’un paquet ; les capacités serveur et les droits Grocy contrôlent ses opérations, sans transformer un addon arbitraire en code de confiance.

## Grocy natif et Android

Grocy reste propriétaire des données métier : produits, stock, journaux, unités, conversions, recettes, courses et plan de repas. Les appels des addons passent par l’API native avec le compte connecté. La migration du chargeur ne modifie pas ces données et n’ajoute pas de données métier de démonstration.

Le lot ne contient **aucun APK** et ne demande pas de reconstruire Mon Grocy Android. SharedTimers conserve les contrats de sessions/minuteurs live et les métadonnées natives compatibles prévues par le code. La préservation d’un contrat n’est pas une attestation de test du client Android : un rapport doit préciser les parcours réellement exécutés.

L’installation peut configurer une identité ou une clé technique et des métadonnées de minuteurs via les interfaces natives Grocy. Ces objets techniques sont distincts de recettes, produits, achats ou stock importés.

## Prix, unités et données manquantes

Budgets et Equivalents calculent à partir des références existantes. Une référence de prix absente, une quantité non convertible ou une équivalence non prouvée reste inconnue. Le résultat peut être un sous-total connu, une fourchette ou un coût partiel, avec les raisons de couverture insuffisante ; il ne faut pas le présenter comme un prix complet. Aucun prix ni conversion n’est inventé pour remplir le catalogue.

SafeImport sépare lecture, extraction, proposition, revue, application, vérification et retour. Installer cet addon n’importe aucune recette. L’application d’une proposition dépend de ses références, de son empreinte exacte et des droits du compte ; une incohérence doit être résolue avant mutation.

## Limites de reprise

Une coupure après écriture peut laisser un résultat incertain malgré l’idempotence. Ne créez pas une nouvelle clé pour contourner cet état : comparez l’effet réel au journal. Le retour d’un paquet exige une version précédente connue et compatible. Le retour de migration retire Grocyste et conserve les personnalisations étrangères ; il ne réactive pas l’ancien runtime NerdCore et ne restaure pas toute la base Grocy automatiquement.
