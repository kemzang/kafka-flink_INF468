# Requirements Document

## Introduction

Ce document décrit les exigences pour l'extension du pipeline Kafka/Flink existant en une architecture distribuée de benchmark académique comparant **Kafka/Flink** et **RabbitMQ** en contexte Big Data. L'objectif est de déployer 4 PCs distants (producteurs) qui envoient des événements de ventes fictives vers 1 PC central hébergeant l'ensemble de l'infrastructure, et d'exposer un dashboard web temps réel permettant de comparer les deux technologies selon des métriques objectives (débit, latence, messages perdus, scalabilité).

Le projet s'appuie sur le pipeline existant : producteur Python, job Flink simulé, stockage MongoDB, et étend l'architecture avec RabbitMQ, un worker Python dédié, un dashboard Flask/Chart.js, et des scripts d'installation automatique pour les PCs distants.

---

## Glossaire

- **PC_Central** : Machine hébergeant l'ensemble des services via Docker Compose (Kafka, RabbitMQ, Flink, MongoDB, Dashboard).
- **PC_Distant** : Machine distante (Windows, Linux ou macOS) exécutant un producteur Kafka ou RabbitMQ via script d'installation.
- **Kafka_Producer** : Script Python s'exécutant sur un PC_Distant et publiant des événements dans le topic Kafka `sales_events`.
- **RabbitMQ_Producer** : Script Python s'exécutant sur un PC_Distant et publiant des événements dans la queue RabbitMQ `sales_events`.
- **Flink_Pipeline** : Job Python simulant le comportement Apache Flink — consomme Kafka, enrichit les événements, les stocke dans MongoDB.
- **RabbitMQ_Worker** : Service Python s'exécutant sur le PC_Central, consommant la queue RabbitMQ et stockant les événements dans MongoDB.
- **Dashboard** : Application web Flask exposant les métriques comparatives en temps réel via une interface navigateur.
- **Metrics_Store** : Collection MongoDB `benchmark_metrics` stockant les métriques horodatées des deux pipelines.
- **Env_File** : Fichier `.env` présent sur chaque PC_Distant, contenant l'IP du PC_Central et les paramètres de connexion.
- **Heartbeat** : Signal périodique envoyé par chaque producteur pour indiquer qu'il est actif.
- **Verdict_Engine** : Composant du Dashboard calculant et affichant automatiquement quel pipeline est supérieur selon les métriques.

---

## Requirements

### Requirement 1 : Architecture réseau distribuée

**User Story :** En tant que chercheur, je veux que 4 PCs distants envoient des données vers le PC central, afin de simuler une charge distribuée réaliste en réseau local.

#### Acceptance Criteria

1. THE PC_Central SHALL héberger Kafka, RabbitMQ, Flink_Pipeline, RabbitMQ_Worker, MongoDB et Dashboard dans un unique fichier Docker Compose.
2. WHEN un Kafka_Producer démarre sur un PC_Distant, THE Kafka_Producer SHALL se connecter au PC_Central en utilisant l'adresse IP et le port définis dans l'Env_File.
3. WHEN un RabbitMQ_Producer démarre sur un PC_Distant, THE RabbitMQ_Producer SHALL se connecter au PC_Central en utilisant l'adresse IP et le port définis dans l'Env_File.
4. THE PC_Central SHALL exposer le port 9092 pour Kafka, le port 5672 pour RabbitMQ, et le port 5000 pour le Dashboard, accessibles depuis le réseau local.
5. IF un PC_Distant ne peut pas atteindre le PC_Central dans les 30 secondes suivant le démarrage, THEN THE Kafka_Producer ou RabbitMQ_Producer SHALL afficher un message d'erreur explicite et retenter la connexion toutes les 10 secondes.

---

### Requirement 2 : Configuration sans IP codée en dur

**User Story :** En tant qu'utilisateur déployant un PC_Distant, je veux configurer l'IP du PC_Central via un fichier `.env`, afin de ne pas modifier le code source entre chaque déploiement.

#### Acceptance Criteria

1. THE Env_File SHALL contenir au minimum les variables `CENTRAL_IP`, `KAFKA_PORT`, `RABBITMQ_PORT`, `PRODUCER_TYPE` (valeur `kafka` ou `rabbitmq`), et `SEND_INTERVAL_MS`.
2. WHEN un producteur démarre, THE Kafka_Producer ou RabbitMQ_Producer SHALL lire exclusivement l'Env_File pour construire les chaînes de connexion, sans aucune valeur d'IP codée en dur dans le code source.
3. IF l'Env_File est absent au démarrage du producteur, THEN THE Kafka_Producer ou RabbitMQ_Producer SHALL afficher un message d'erreur indiquant le chemin attendu du fichier et s'arrêter.
4. THE Env_File SHALL fournir des valeurs par défaut documentées pour chaque variable afin de permettre un démarrage rapide.

---

### Requirement 3 : Producteurs Kafka distants

**User Story :** En tant que chercheur, je veux que 2 PCs distants envoient des événements de ventes via Kafka, afin de mesurer les performances du pipeline Kafka/Flink sous charge distribuée.

#### Acceptance Criteria

1. WHEN le Kafka_Producer est actif, THE Kafka_Producer SHALL générer et envoyer des événements de ventes fictives au format JSON vers le topic `sales_events` du PC_Central.
2. THE Kafka_Producer SHALL inclure dans chaque événement un champ `producer_id` identifiant de manière unique le PC_Distant émetteur, un champ `pipeline` avec la valeur `kafka`, et un champ `sent_at` avec l'horodatage ISO 8601 de l'envoi.
3. WHEN un événement est envoyé avec succès, THE Kafka_Producer SHALL enregistrer localement l'horodatage d'envoi pour le calcul de latence.
4. THE Kafka_Producer SHALL envoyer un Heartbeat toutes les 5 secondes vers un topic Kafka dédié `producer_heartbeats` contenant le `producer_id` et l'horodatage.
5. WHILE le Kafka_Producer est actif, THE Kafka_Producer SHALL respecter l'intervalle d'envoi défini par `SEND_INTERVAL_MS` dans l'Env_File, avec une tolérance de ±10%.

---

### Requirement 4 : Producteurs RabbitMQ distants

**User Story :** En tant que chercheur, je veux que 2 PCs distants envoient des événements de ventes via RabbitMQ, afin de mesurer les performances du pipeline RabbitMQ sous charge distribuée comparable.

#### Acceptance Criteria

1. WHEN le RabbitMQ_Producer est actif, THE RabbitMQ_Producer SHALL générer et envoyer des événements de ventes fictives au format JSON vers la queue `sales_events` du PC_Central, avec le même schéma de données que le Kafka_Producer.
2. THE RabbitMQ_Producer SHALL inclure dans chaque événement un champ `producer_id` identifiant de manière unique le PC_Distant émetteur, un champ `pipeline` avec la valeur `rabbitmq`, et un champ `sent_at` avec l'horodatage ISO 8601 de l'envoi.
3. THE RabbitMQ_Producer SHALL déclarer la queue `sales_events` avec `durable=True` pour assurer la persistance des messages.
4. THE RabbitMQ_Producer SHALL envoyer un Heartbeat toutes les 5 secondes vers une queue dédiée `producer_heartbeats` contenant le `producer_id` et l'horodatage.
5. WHILE le RabbitMQ_Producer est actif, THE RabbitMQ_Producer SHALL respecter l'intervalle d'envoi défini par `SEND_INTERVAL_MS` dans l'Env_File, avec une tolérance de ±10%.

---

### Requirement 5 : Pipeline Kafka/Flink sur le PC Central

**User Story :** En tant que chercheur, je veux que le Flink_Pipeline traite les événements Kafka et enregistre les métriques de performance, afin de disposer de données objectives pour la comparaison.

#### Acceptance Criteria

1. WHEN un événement est consommé depuis le topic `sales_events`, THE Flink_Pipeline SHALL calculer la latence de traitement en millisecondes comme la différence entre l'horodatage de réception et le champ `sent_at` de l'événement.
2. THE Flink_Pipeline SHALL stocker chaque événement traité dans la collection MongoDB `sales_raw` avec les champs d'enrichissement existants (`tva_amount`, `amount_ttc`, `processed_at`, `processor`).
3. THE Flink_Pipeline SHALL mettre à jour la collection Metrics_Store toutes les 5 secondes avec le débit courant (messages traités dans les 5 dernières secondes), la latence moyenne, et le total cumulé pour le pipeline `kafka`.
4. IF un événement reçu est malformé ou ne peut pas être désérialisé, THEN THE Flink_Pipeline SHALL incrémenter un compteur `messages_perdus` dans la collection Metrics_Store et continuer le traitement.
5. THE Flink_Pipeline SHALL maintenir les fenêtres temporelles de 30 secondes (Tumbling Window) existantes pour l'agrégation par catégorie et par région.

---

### Requirement 6 : Pipeline RabbitMQ Worker sur le PC Central

**User Story :** En tant que chercheur, je veux que le RabbitMQ_Worker traite les événements RabbitMQ et enregistre les métriques de performance, afin de disposer de données comparables à celles du pipeline Kafka.

#### Acceptance Criteria

1. WHEN un message est consommé depuis la queue `sales_events`, THE RabbitMQ_Worker SHALL calculer la latence de traitement en millisecondes comme la différence entre l'horodatage de réception et le champ `sent_at` du message.
2. THE RabbitMQ_Worker SHALL stocker chaque événement traité dans la collection MongoDB `rabbitmq_raw` avec les mêmes champs d'enrichissement que le Flink_Pipeline.
3. THE RabbitMQ_Worker SHALL mettre à jour la collection Metrics_Store toutes les 5 secondes avec le débit courant, la latence moyenne, et le total cumulé pour le pipeline `rabbitmq`.
4. IF un message reçu est malformé ou ne peut pas être désérialisé, THEN THE RabbitMQ_Worker SHALL incrémenter un compteur `messages_perdus` dans la collection Metrics_Store et acquitter le message pour éviter le blocage de la queue.
5. THE RabbitMQ_Worker SHALL utiliser `basic_qos(prefetch_count=10)` pour limiter la charge de traitement simultanée.

---

### Requirement 7 : Collecte et stockage des métriques

**User Story :** En tant que chercheur, je veux que toutes les métriques de performance soient stockées dans MongoDB avec horodatage, afin de pouvoir analyser l'évolution dans le temps et générer des graphiques.

#### Acceptance Criteria

1. THE Metrics_Store SHALL stocker pour chaque pipeline (`kafka` et `rabbitmq`) et chaque intervalle de 5 secondes : `pipeline`, `timestamp`, `throughput_per_sec`, `avg_latency_ms`, `total_processed`, `messages_lost`, `active_producers`.
2. THE Metrics_Store SHALL conserver au minimum les 1000 derniers enregistrements par pipeline pour permettre l'affichage de l'historique sur le Dashboard.
3. WHEN un Heartbeat est reçu depuis un producteur, THE Flink_Pipeline ou RabbitMQ_Worker SHALL mettre à jour le compteur `active_producers` du pipeline correspondant dans la collection Metrics_Store.
4. IF aucun Heartbeat n'est reçu d'un producteur pendant plus de 15 secondes, THEN THE Flink_Pipeline ou RabbitMQ_Worker SHALL décrémenter le compteur `active_producers` du pipeline correspondant.
5. THE Metrics_Store SHALL indexer les documents par `pipeline` et `timestamp` pour garantir des requêtes en moins de 100ms.

---

### Requirement 8 : Dashboard web temps réel

**User Story :** En tant que chercheur, je veux accéder depuis n'importe quel PC du réseau local à un dashboard web affichant les métriques des deux pipelines côte à côte, afin de visualiser la comparaison en temps réel.

#### Acceptance Criteria

1. THE Dashboard SHALL être accessible depuis n'importe quel navigateur du réseau local via `http://<IP_PC_Central>:5000` sans authentification.
2. THE Dashboard SHALL afficher deux colonnes côte à côte : une colonne « Kafka / Flink » et une colonne « RabbitMQ », chacune présentant les métriques suivantes : débit (msg/s), latence moyenne (ms), total messages traités, messages perdus, et nombre de producteurs actifs.
3. THE Dashboard SHALL rafraîchir les métriques affichées toutes les 2 secondes via des appels API REST vers le backend Flask, sans rechargement complet de la page.
4. THE Dashboard SHALL afficher un graphique linéaire comparatif du débit (msg/s) des deux pipelines sur les 60 derniers points de mesure, en utilisant Chart.js.
5. THE Dashboard SHALL afficher un graphique linéaire comparatif de la latence moyenne (ms) des deux pipelines sur les 60 derniers points de mesure.
6. WHERE les deux pipelines ont traité au moins 100 messages chacun, THE Verdict_Engine SHALL afficher un verdict automatique indiquant quel pipeline est supérieur pour chaque métrique (débit, latence, fiabilité).
7. THE Dashboard SHALL indiquer visuellement (indicateur coloré vert/rouge) si chaque producteur distant est actif ou inactif, basé sur la réception des Heartbeats.

---

### Requirement 9 : API REST du Dashboard

**User Story :** En tant que développeur, je veux que le Dashboard expose une API REST, afin que le frontend puisse récupérer les métriques sans couplage fort avec le backend.

#### Acceptance Criteria

1. THE Dashboard SHALL exposer un endpoint `GET /api/metrics` retournant les dernières métriques des deux pipelines au format JSON, avec un temps de réponse inférieur à 200ms.
2. THE Dashboard SHALL exposer un endpoint `GET /api/metrics/history?pipeline=<kafka|rabbitmq>&limit=<n>` retournant les `n` derniers enregistrements de métriques pour le pipeline spécifié.
3. THE Dashboard SHALL exposer un endpoint `GET /api/producers` retournant la liste des producteurs actifs avec leur `producer_id`, `pipeline`, et l'horodatage du dernier Heartbeat reçu.
4. THE Dashboard SHALL exposer un endpoint `GET /api/verdict` retournant le verdict comparatif calculé par le Verdict_Engine au format JSON.
5. IF une requête API reçoit un paramètre invalide, THEN THE Dashboard SHALL retourner une réponse HTTP 400 avec un message d'erreur descriptif au format JSON.

---

### Requirement 10 : Scripts d'installation automatique pour PCs distants

**User Story :** En tant qu'utilisateur non-expert, je veux exécuter un seul script pour installer et démarrer le producteur sur mon PC distant, afin de rejoindre le benchmark sans configuration manuelle complexe.

#### Acceptance Criteria

1. THE PC_Distant SHALL fournir un script `start.sh` (Linux/macOS) et un script `start.bat` (Windows) qui installent les dépendances Python, créent l'Env_File si absent, et démarrent le producteur approprié selon la valeur de `PRODUCER_TYPE`.
2. WHEN le script `start.sh` ou `start.bat` est exécuté pour la première fois, THE PC_Distant SHALL vérifier la présence de Python 3.8 ou supérieur et afficher un message d'erreur explicite si Python n'est pas installé.
3. THE script `start.sh` SHALL être exécutable sans droits administrateur sur Linux et macOS.
4. THE script `start.bat` SHALL fonctionner sur Windows 10 et Windows 11 sans nécessiter PowerShell ou WSL.
5. WHEN le script démarre le producteur, THE PC_Distant SHALL afficher dans la console le `producer_id` généré, le type de pipeline (`kafka` ou `rabbitmq`), l'IP du PC_Central utilisée, et la confirmation de connexion réussie.
6. IF la connexion au PC_Central échoue lors du démarrage du script, THEN THE PC_Distant SHALL afficher un message d'aide indiquant de vérifier l'Env_File et la connectivité réseau, et retenter la connexion 3 fois avant de s'arrêter.

---

### Requirement 11 : Sérialisation et désérialisation des événements

**User Story :** En tant que développeur, je veux que les événements soient sérialisés et désérialisés de manière fiable entre les producteurs et les workers, afin d'éviter toute perte de données due à des erreurs de format.

#### Acceptance Criteria

1. THE Kafka_Producer SHALL sérialiser chaque événement en JSON UTF-8 avant envoi, et THE Flink_Pipeline SHALL désérialiser chaque message JSON UTF-8 reçu en dictionnaire Python.
2. THE RabbitMQ_Producer SHALL sérialiser chaque événement en JSON UTF-8 avant envoi, et THE RabbitMQ_Worker SHALL désérialiser chaque message JSON UTF-8 reçu en dictionnaire Python.
3. FOR ALL événements valides générés par un producteur, la sérialisation puis la désérialisation SHALL produire un objet équivalent à l'original (propriété round-trip).
4. IF un message reçu ne peut pas être désérialisé depuis JSON, THEN THE Flink_Pipeline ou RabbitMQ_Worker SHALL enregistrer le message brut dans une collection MongoDB `dead_letter_queue` avec l'horodatage et la source du message.

---

### Requirement 12 : Verdict Engine — Comparaison automatique

**User Story :** En tant que chercheur, je veux que le système calcule automatiquement un verdict objectif montrant les avantages de Kafka, afin d'étayer mon étude académique avec des données mesurées.

#### Acceptance Criteria

1. THE Verdict_Engine SHALL calculer le verdict en comparant les moyennes des 60 derniers points de mesure pour chaque métrique (débit, latence, messages perdus).
2. WHEN le débit moyen de Kafka dépasse celui de RabbitMQ, THE Verdict_Engine SHALL marquer Kafka comme supérieur pour la métrique « Débit » avec le ratio calculé (ex. : « Kafka 2.3× plus rapide »).
3. WHEN la latence moyenne de Kafka est inférieure à celle de RabbitMQ, THE Verdict_Engine SHALL marquer Kafka comme supérieur pour la métrique « Latence » avec la différence en millisecondes.
4. WHEN le nombre de messages perdus de Kafka est inférieur à celui de RabbitMQ, THE Verdict_Engine SHALL marquer Kafka comme supérieur pour la métrique « Fiabilité ».
5. THE Verdict_Engine SHALL afficher un score global sur 3 points (un point par métrique remportée) pour chaque pipeline, avec un résumé textuel des avantages observés.
6. WHERE les deux pipelines ont traité au moins 100 messages chacun, THE Dashboard SHALL afficher le verdict du Verdict_Engine de manière proéminente en haut de la page.
