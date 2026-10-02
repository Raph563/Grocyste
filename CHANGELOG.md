# Changements

## 1.0.0 — livraison en préparation, 2 octobre 2026

Cette entrée décrit le code du lot. Les dates de publication, résultats de tests et preuves de migration doivent être rattachés au rapport de livraison avant de qualifier une release de validée en production.

- Création de Grocyste — Seasonings enabler comme CORE commun remplaçant NerdCore : service Python, SDK navigateur et panneau d’administration intégré aux réglages Grocy.
- Authentification rattachée à la session Grocy ; appels métier avec les droits de l’utilisateur et contrôle serveur des opérations administrateur.
- Gestionnaire séparé pour paquets Ed25519, empreintes SHA-256, résolution des dépendances et générations atomiques du registre ; désactivation protégée et retour à une version précédente connue.
- État compagnon pour sessions, réglages révisionnés, cache de recherche, mémoire de reçus, candidats Course U et journal des opérations ; coffre serveur pour clés de fournisseurs.
- Adaptateurs de transport pour les addons historiques StatNerd, ProductHelper et ReceiptScanner, sans diffusion du jeton d’administration NerdCore aux addons.
- Catalogue réparti sur neuf dépôts : RecipeScaling, RecipeLive, Budgets, Equivalents, SharedTimers, SafeImport, StatNerd, ProductHelper et ReceiptScanner.
- Migration du chargeur avec reçu privé, sauvegarde SQLite cohérente en lecture seule, empreintes des tables métier et conservation des personnalisations reconnues. Le retour de migration retire Grocyste et laisse Grocy natif ; il ne réactive pas l’ancien runtime NerdCore.
- Déploiement Docker Compose et point d’entrée d’installation unique ; documentation française et CI Python 3.12 / Node.js 24.

Compatibilité déclarée : Grocy **4.7.1**. Aucun APK ni jeu de données métier n’est publié par ce lot. Les coûts partiels et références inconnues conservent leur qualification.
