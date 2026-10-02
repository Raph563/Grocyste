# Provenance

## CORE Grocyste

Grocyste — Seasonings enabler est développé pour le projet Grocy maintenu par Raph563. Ce dépôt regroupe le CORE Python, le SDK navigateur, la gestion de paquets et les outils de migration de la livraison 1.0.0 préparée le 2 octobre 2026. Il remplace le rôle central de NerdCore et conserve des adaptateurs explicites pour ses anciens appels utilisés par les addons.

Le CORE est distribué sous **GPL-3.0-or-later**. Le texte intégral GPL v3 de `LICENSE` est repris à l’identique du texte déjà fourni dans le dépôt RecipeScaling ; la mention « ou ultérieure » vient de la déclaration de licence du CORE et de ses en-têtes SPDX, et ne modifie pas le texte officiel de la licence.

## Origine des assaisonnements

Les moteurs de RecipeScaling, RecipeLive, Budgets, Equivalents et SharedTimers proviennent du projet Grocy de Raph563, dans l’état préparé pour ce lot. SafeImport reprend les moteurs génériques de l’importeur audité. Leurs fichiers de provenance restent dans leurs dépôts respectifs ; les données d’une instance et les sources privées de recettes ne font pas partie de leurs paquets.

StatNerd, ProductHelper et ReceiptScanner conservent l’historique de leurs dépôts d’origine. Leur adaptation Grocyste porte notamment sur le transport réseau, le chargeur et le paquetage. Les notices des bibliothèques tierces restent auprès des fichiers vendus dans chaque dépôt. La licence du CORE ne remplace pas celles des addons ou de leurs dépendances.

| Composant | Dépôt de destination | Référence du manifeste du lot |
| --- | --- | --- |
| CORE | [Raph563/Grocyste](https://github.com/Raph563/Grocyste) | `1.0.0` |
| RecipeScaling | [Raph563/Grocyste-RecipeScaling](https://github.com/Raph563/Grocyste-RecipeScaling) | `1.0.0` |
| RecipeLive | [Raph563/Grocyste-RecipeLive](https://github.com/Raph563/Grocyste-RecipeLive) | `1.0.0` |
| Budgets | [Raph563/Grocyste-Budgets](https://github.com/Raph563/Grocyste-Budgets) | `1.0.0` |
| Equivalents | [Raph563/Grocyste-Equivalents](https://github.com/Raph563/Grocyste-Equivalents) | `1.0.0` |
| SharedTimers | [Raph563/Grocyste-SharedTimers](https://github.com/Raph563/Grocyste-SharedTimers) | `1.0.0` |
| SafeImport | [Raph563/Grocyste-SafeImport](https://github.com/Raph563/Grocyste-SafeImport) | `1.0.0` |
| StatNerd | [Raph563/StatNerd](https://github.com/Raph563/StatNerd) | `4.4.2` |
| ProductHelper | [Raph563/ProductHelper](https://github.com/Raph563/ProductHelper) | `4.0.43` |
| ReceiptScanner | [Raph563/ReceiptScanner](https://github.com/Raph563/ReceiptScanner) | `1.0.4` |

Ces liens identifient les dépôts de publication prévus ; leur présence sur GitHub et les références immuables des releases doivent être confirmées par le rapport de publication. Une version de manifeste ne prouve ni un tag public ni un test réussi.

## Ce qui établit une release

Le paquet signé contient un manifeste canonique, sa signature Ed25519 et la liste exacte des fichiers avec leur taille et leur SHA-256. Le catalogue signé associe une version à une URL de release et une empreinte d’archive. La clé publique de confiance est fournie séparément dans `trust/catalog.pub` ; la clé privée de publication reste hors du dépôt et hors de la CI.

Pour rendre une livraison reproductible, le rapport final doit fixer les commits du CORE et des neuf addons, les tags et empreintes des archives, la clé publique attendue, les versions de runtime, les commandes de test et leurs résultats. Aucune référence de commit ou réussite de production n’est inventée ici.

## Données privées

Les reçus d’installation et de migration, bases, images d’une instance, clés de fournisseurs, clé de service Grocy et états historiques importés sont des artefacts privés de l’installation. Ils ne sont pas des sources à publier dans GitHub. Les exemples de configuration du dépôt ne contiennent pas de secret et ne constituent pas un catalogue métier.
