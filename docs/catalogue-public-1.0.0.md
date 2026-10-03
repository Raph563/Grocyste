# Catalogue public : recettes et produits

Le dixième addon, **[Grocyste-Catalog](https://github.com/Raph563/Grocyste-Catalog)**, est livré séparément en version 1.0.0. Il fonctionne avec le CORE 1.0.0 existant et Grocy 4.7.1.

**[Ouvrir le catalogue sans compte](https://raph563.github.io/Grocyste-Catalog/)** · [Release et paquet signé](https://github.com/Raph563/Grocyste-Catalog/releases/tag/v1.0.0) · [Validation](https://github.com/Raph563/Grocyste-Catalog/blob/main/VALIDATION.md)

La collection réunit **5 098 fiches recettes**, dont **5 011 avec ingrédients et préparation libre**, **690 fiches produits**, **85 observations de prix publics datées** et **130 références énergétiques**. Elle comporte 1 302 recettes en français et 3 796 en anglais. Les 70 recettes normales de la collection partagée sont conservées sous forme factuelle, avec leurs ingrédients et leur source lorsqu’elle est connue.

Recherche par nom, ingrédient, catégorie et code-barres ; filtres ; lecture ; lien direct vers une fiche ; téléchargement JSON. Les textes libres conservent leur révision, leur attribution et leur licence **CC BY-SA 4.0**. Le code de l’addon est **GPL v3 ou ultérieure**.

Les stocks, achats, prix payés, tickets, comptes, prescriptions, emplacements domestiques, descriptions commerciales et photos privées ne sont pas publiés. Les instructions et photos provenant de sites commerciaux ne sont pas redistribuées. Les prix publics retenus sont anciens et à revalider ; le magasin personnel n’est pas publié.

L’addon a zéro capacité métier et aucune dépendance. Il ajoute « Grocyste — Catalogue public » au menu, et ses assets restent consultables sans connexion à `/__grocyste/assets/public-catalog/index.html`. Les downloads n’envoient ni cookie ni clé. Les téléchargements de fiches ne constituent pas des propositions SafeImport : les produits et unités de l’instance cible doivent être rapprochés et relus avant toute importation.

Qualification : 8 tests Node 24, 7 régressions d’extraction, 14 contrôles navigateur, 4 contrôles d’intégration Grocy PHP standard et 5 contrôles de cycle de vie. Le paquet est installé depuis un cache vide par le gestionnaire à partir de la release publique ; les six assets et les deux index publics sont récupérés anonymement et vérifiés. Le catalogue est activé sur l’instance existante après sauvegarde SQLite cohérente et contrôle d’intégrité ; les douze tables métier, le schéma, les personnalisations et les neuf addons précédents sont conservés.

Les recettes communautaires ne sont pas testées en cuisine. 179 fiches gardent des modèles wiki à relire ; 17 pages anglaises renvoient à la source pour compléter l’extraction. Aucune conversion automatique, correction des huit échecs du catalogue privé, import massif ni nouvel APK n’est livré. Le modèle de confiance des scripts intégrés reste celui décrit dans l’architecture du CORE.

L’installation ciblée est décrite dans [le guide du nouvel addon](https://github.com/Raph563/Grocyste-Catalog/blob/main/INSTALLATION.md). Les archives du CORE et des neuf premiers addons restent figées ; leur bootstrap ne change pas. Le catalogue signé joint à la nouvelle release ajoute uniquement le dixième paquet aux versions déjà vérifiées.
