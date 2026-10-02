# Compatibilité et validation

## Versions déclarées

| Élément | Version ou contrainte du lot | Portée |
| --- | --- | --- |
| Grocy | **4.7.1 uniquement** | Cible déclarée par les neuf manifestes. |
| CORE Grocyste | `1.0.0` | Les addons demandent `>=1.0.0 <2.0.0`. |
| API Grocyste | `v1` | Chemin par défaut `/__grocyste/v1`. |
| Python CORE | `>=3.12` ; image `3.12.14` | Runtime et tests ; CI sous Python 3.12 Linux. |
| Python hôte | `>=3.10` | Installateur Linux utilisant la bibliothèque standard. |
| Node.js | 24 ; image `24.21.0` | SDK, construction des assets et runtime live. |
| RecipeScaling, RecipeLive, Budgets, Equivalents, SharedTimers, SafeImport | `1.0.0` chacun | Versions de leurs manifestes, dépôts séparés. |
| StatNerd | `4.4.2` | Version du manifeste adapté. |
| ProductHelper | `4.0.43` | Version du manifeste adapté. |
| ReceiptScanner | `1.0.4` | Version du manifeste adapté. |

Une version déclarée désigne un contrat à vérifier. Elle ne prouve ni l’existence d’une release GitHub ni le fonctionnement d’une instance migrée. Toute autre version Grocy est hors de ce périmètre jusqu’à qualification explicite.

## Qualification réellement exécutée

Les neuf archives du catalogue signé ont été figées avant les essais finaux, puis installées dans un registre neuf. Les octets d’un identifiant/version restent immuables. Les résultats ci-dessous ont été observés au laboratoire, sans requête de test destructif en production.

| Vérification | Résultat observé |
| --- | --- |
| CORE Python, Linux/Python 3.12 | **165 tests réussis** dans une image construite depuis les sources courantes. |
| SDK navigateur, Node 24 | **10 tests réussis**, dont préfixe sous-répertoire, secrets legacy, événements et tâches incertaines. |
| Neuf addons, Node 24 | **164 tests réussis et neuf constructions réussies** : Scaling 12, Live 27, Budgets 37, Equivalents 12, Timers 12, SafeImport 55, adaptateurs historiques 3 chacun. |
| Authentification, droits et uploads via HTTPS réel | **78 contrôles réussis**, fixtures supprimées et nettoyage confirmé. |
| Paquets, concurrence et fautes réelles | **14 scénarios réussis**, neuf ZIP finaux authentifiés, écritures altérées refusées. |
| Activation du chargeur interrompue | **4 scénarios réussis** : SIGKILL avant/après publication et refus des dérives métier/chargeur. |
| Grocy Docker vierge, navigateur Chromium 145 | **8 parcours réussis**, neuf addons chargés, OCR/PDF locaux, aucun appel externe direct. |
| Grocy PHP natif, hors Docker | **8 parcours réussis** sur l’instance isolée. |
| Grocy et CORE sous `/grocy` | Chargement des neuf addons et chemins de transport vérifiés. |
| Trois addons historiques et SafeImport | **5 parcours réussis** : graphique, formulaire produit, carte achat, migration de credential et import synthétique avec retour. |
| Courses Budgets et Equivalents | Deux parcours réussis sans colonnes de prix natives : prix/familles absents et sous-totaux incomplets explicites. |
| Gestion depuis les boutons réels | Désinstallation SafeImport, réinstallation vérifiée et refus de supprimer une dépendance utilisés : **3 parcours réussis**. |
| Installateur complet Docker/Caddy | Installation et réexécution `installed`, neuf addons, registre/chargeur identiques au rejeu, tables métier inchangées. |
| Propriétaire live | Rechargement de configuration et retour sans écrivains simultanés ; second propriétaire refusé. |
| Reprise du live historique | **4 contrôles réussis** sur un conteneur jetable : sélection explicite, arrêt, conservation de l’état, rejeu sans écraser la progression. |
| Migration du clone actuel | Chargeur et budget reconnus par leurs empreintes ; schéma, vues/triggers et douze tables métier inchangés ; retour natif et restauration réelle réussis. |
| Anciennes compositions | Retraite interrompue/reprise/rejeu sur quatre conteneurs jetables ; cinquième service témoin inchangé. |

Un échec ancien du panneau courses venait d’une archive antérieure ; les octets finaux contiennent le repli vers un panneau compagnon. Un deuxième échec de vérification lisait le total natif plutôt que ce panneau : la lecture correcte a confirmé « Prix absent » et un total partiel. Le défaut réel de réexécution Caddy avec l’environnement vide `/dev/null` est corrigé, et les autres périphériques restent refusés.

Le [catalogue descriptif](../catalog.json) indique `normalGrocyQualification: verified-scoped` avec, pour chaque addon, sa fonction, la version exercée et ses limites. Ce statut atteste les parcours décrits, pas chaque fonction commerciale ni toutes les installations possibles. Les références détaillées restent privées ; les scripts de sécurité reproductibles sont publics. Voir la [qualification sécurité et résilience](security-validation.md).

L’application Android réelle, les appels facturés aux fournisseurs d’IA, la charge prolongée et toutes les versions Grocy autres que 4.7.1 ne sont pas qualifiés. Les contrats natifs des minuteurs sont exercés par navigateur et API, avec conservation de la synchronisation existante.

Les dépendances effectives sont : RecipeLive → RecipeScaling et SharedTimers ; Budgets → RecipeScaling ; Equivalents → Budgets ; ReceiptScanner → StatNerd ; ProductHelper → StatNerd et ReceiptScanner. Les manifestes sont la référence pour leurs plages de versions.

## Maintien de la façade NerdCore

Les appels NerdCore non sensibles utilisés par les adaptateurs livrés sont maintenus pendant **au moins 90 jours après la publication effective de Grocyste 1.0.0**, et **au moins deux versions mineures du CORE**, soit jusqu’à `1.2.0` au minimum. Les deux conditions doivent être satisfaites avant tout retrait annoncé. Ce lot n’implémente aucune expiration automatique à cette échéance ; la date sera calculée à partir de la release effectivement publiée.

Cette façade traduit les appels compatibles vers le SDK Grocyste. `getUpdateToken()` renvoie une chaîne vide et `setUpdateToken()` est un stub sans effet : aucun ancien secret privilégié n’est rétabli ni exposé au navigateur. Chaque addon livré utilise sa fermeture d’adaptation ; les droits restent vérifiés par le CORE et Grocy.

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
