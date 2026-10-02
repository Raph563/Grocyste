# Qualification et livraison 1.0.0

Qualification du 2 octobre 2026, pour **Grocy 4.7.1**. Le CORE et les neuf paquets du catalogue sont installés sur une instance existante après essais isolés. Les rapports contenant des données privées restent hors de GitHub ; leurs empreintes sont consignées dans le dossier privé de livraison.

| Contrôle exécuté | Résultat |
| --- | --- |
| CORE, Linux / Python 3.12 | 165 tests réussis |
| SDK navigateur | 10 tests réussis |
| Neuf addons et constructions | 164 tests réussis, 9 constructions réussies |
| Authentification et entrées HTTP réelles | 78 contrôles réussis |
| Résilience des paquets | 14 scénarios réussis |
| Interruptions de migration du chargeur | 4 scénarios réussis |
| Migration du propriétaire live historique | 4 contrôles réussis |
| Interfaces sur Grocy Docker et PHP natif | 8 parcours réussis sur chaque environnement |
| Interfaces historiques et SafeImport | 5 parcours réussis |
| Désinstallation, réinstallation et dépendance requise | 3 parcours réussis |
| Installation complète et réexécution au laboratoire | Réussies |
| Contrôles après bascule réelle | 26 contrôles réussis |
| Réexécution ciblée en production | Chargeur et 9 paquets sans effet |

La sauvegarde générale cohérente a été réellement restaurée : 2 616 fichiers restaurés, toutes leurs empreintes comparées et douze tables métier identiques. La migration réelle conserve ensuite ces douze empreintes métier, le schéma et les vues/triggers ProductHelper, les personnalisations, les sessions live et les identités des conteneurs Grocy et du service Android. Aucune mutation métier n’est effectuée par cette bascule. Une personnalisation ajoutée concurremment a bloqué la première préparation ; sa source a été sauvegardée et son inclusion conservée lors de la reprise vérifiée.

Les quatre anciens services de recomposition sont retirés, avec sauvegarde privée. Leurs références Compose sont supprimées en préservant les autres réglages et dépendances. Les anciennes routes d’administration répondent `410` : le secret historique n’a plus de vérificateur actif et aucun retour arrière ne doit recréer ces services. Les trois services Grocyste fonctionnent avec UID 1000, un système de fichiers racine en lecture seule, sans socket Docker ni montage de la base Grocy. La clé de service reste côté serveur.

Les versions et empreintes des paquets restent immuables après qualification. La signature Ed25519 protège le catalogue et les fichiers des paquets ; les signatures ne remplacent pas la revue des sources et ne constituent pas une sandbox JavaScript. Les dix dépôts et releases sont publics. Les 23 assets ont été téléchargés sans authentification et leurs tailles/empreintes comparées ; les neuf paquets signés ont été installés par le gestionnaire depuis un cache vide. L’archive CORE vérifiée porte SHA-256 `ed00f6302763a775ac355897f2358a0ad3ac46289fb8c10e3e3352b3f3be8679`.

## Limites conservées

Les huit échecs historiques du catalogue privé restent reproduits et exclus des fonctions annoncées. Aucun correctif de ce catalogue, import métier de démonstration, nouvel APK ou nouveau correctif SQL ProductHelper n’est livré. La qualification décrit des parcours précis : elle ne couvre pas une application Android réelle, les appels IA facturés, LDAP/reverse proxy en conditions réelles, chaque navigateur ou une charge prolongée. Les essais de corruption, de crash et de sécurité ont été exécutés sur les laboratoires uniquement.

L’audit Python de 43 distributions ne remonte aucun avis connu à la date du contrôle. Les limites de l’audit des composants JS/WASM, dont le moteur OCR natif, et le défaut Grocy amont reproduit séparément sont décrits dans le [rapport de sécurité](security-validation.md). Aucun défaut critique ou haut attribué à Grocyste ne reste ouvert dans la couverture exécutée. Ce constat n’est pas une garantie d’absence de défaut hors de cette couverture.

Voir aussi la [compatibilité et les parcours vérifiés](compatibilite.md), le [guide d’installation](installation.md), le [catalogue](../catalog.json) et le [suivi de livraison](../HANDOFF.md).

Le premier workflow GitHub RecipeScaling a échoué car son test de contrat lisait un checkout voisin RecipeLive absent. Le correctif du contexte CI récupère la révision RecipeLive qualifiée ; les sources et octets des paquets signés, ainsi que les tags publiés, restent identiques. La réussite des workflows de suivi est vérifiée séparément.
