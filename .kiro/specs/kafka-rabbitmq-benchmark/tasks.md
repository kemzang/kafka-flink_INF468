# Plan d'implémentation : kafka-rabbitmq-benchmark

## Vue d'ensemble

Extension du pipeline Kafka/Flink existant pour y ajouter un pipeline RabbitMQ symétrique, un dashboard web Flask/Chart.js, des producteurs distants autonomes, et une suite de tests basés sur les propriétés (Hypothesis). Tout est orchestré via Docker Compose sur le PC central.

## Tâches

- [x] 1. Mettre à jour `docker-compose.yml` — ajout de RabbitMQ, rabbitmq-worker et dashboard
  - Ajouter le service `rabbitmq` (image `rabbitmq:3.13-management`, ports 5672 et 15672, healthcheck)
  - Ajouter le service `rabbitmq-worker` (build `./rabbitmq_worker/`, dépend de rabbitmq et mongodb)
  - Ajouter le service `dashboard` (build `./dashboard/`, port 5000, dépend de mongodb)
  - Remplacer `KAFKA_ADVERTISED_LISTENERS: PLAINTEXT_HOST://localhost:9092` par `PLAINTEXT_HOST://${CENTRAL_IP:-localhost}:9092` pour l'accès réseau distant
  - Tous les nouveaux services rejoignent le réseau `bigdata_net`
  - _Requirements: 1.1, 1.4_

- [x] 2. Créer le service `rabbitmq_worker/`
  - [x] 2.1 Créer `rabbitmq_worker/Dockerfile`
    - Image de base `python:3.11-slim`, copier `requirements.txt` et `worker.py`, `CMD ["python", "worker.py"]`
    - _Requirements: 6.1, 6.5_

  - [x] 2.2 Créer `rabbitmq_worker/requirements.txt`
    - Dépendances : `pika==1.3.2`, `pymongo==4.7.2`, `python-dotenv==1.0.1`
    - _Requirements: 6.1_

  - [x] 2.3 Créer `rabbitmq_worker/worker.py`
    - Lire `RABBITMQ_HOST`, `RABBITMQ_PORT`, `MONGO_URI` depuis les variables d'environnement
    - Se connecter à RabbitMQ avec backoff exponentiel (5s, 10s, 20s, max 60s) en cas d'échec
    - Déclarer les queues `sales_events` et `producer_heartbeats` avec `durable=True`
    - Appeler `basic_qos(prefetch_count=10)` sur le channel
    - Consommer `sales_events` : désérialiser JSON, calculer `latency_ms`, enrichir (`tva_amount`, `amount_ttc`, `processed_at`, `processor="rabbitmq-worker-v1.0"`), insérer dans `rabbitmq_raw`
    - Consommer `producer_heartbeats` : mettre à jour `active_producers` dans `benchmark_metrics`
    - Écrire dans `benchmark_metrics` toutes les 5 secondes (débit, latence moyenne, total, messages perdus, active_producers) ; supprimer les documents au-delà des 1000 derniers par pipeline
    - Messages non désérialisables → insérer dans `dead_letter_queue`, incrémenter `messages_lost`, `basic_ack`
    - Si aucun heartbeat d'un producteur depuis 15s → décrémenter `active_producers`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4, 11.2, 11.4_

  - [ ]* 2.4 Écrire les tests unitaires pour `rabbitmq_worker/worker.py`
    - Tester : message malformé → `dead_letter_queue` + `basic_ack`, `basic_qos(prefetch_count=10)`, queue durable, heartbeat timeout → décrément `active_producers`
    - _Requirements: 6.4, 6.5, 7.4_

- [ ] 3. Checkpoint — S'assurer que le worker RabbitMQ démarre correctement
  - S'assurer que tous les tests passent, poser des questions à l'utilisateur si nécessaire.

- [x] 4. Mettre à jour `flink_job/flink_job.py` — métriques, latence, heartbeat
  - [x] 4.1 Ajouter le calcul de `latency_ms` dans `process_event`
    - `latency_ms = (datetime.utcnow() - datetime.fromisoformat(event['sent_at'].rstrip('Z'))).total_seconds() * 1000`
    - Si `sent_at` absent ou malformé → `latency_ms = None`, continuer normalement
    - Ajouter `producer_id`, `pipeline`, `sent_at`, `latency_ms` dans le document inséré dans `sales_raw`
    - _Requirements: 5.1, 5.2_

  - [x] 4.2 Ajouter l'écriture dans `benchmark_metrics` toutes les 5 secondes
    - Utiliser un thread ou un compteur basé sur le temps pour déclencher l'écriture toutes les 5s
    - Document : `pipeline="kafka"`, `timestamp`, `throughput_per_sec`, `avg_latency_ms`, `total_processed`, `messages_lost`, `active_producers`
    - Supprimer les documents au-delà des 1000 derniers pour le pipeline `kafka` après chaque insertion
    - _Requirements: 5.3, 7.1, 7.2_

  - [x] 4.3 Consommer le topic `producer_heartbeats` et gérer `active_producers`
    - Ajouter un consumer sur le topic `producer_heartbeats` (thread séparé ou consumer group dédié)
    - Mettre à jour `active_producers` dans `benchmark_metrics` à chaque heartbeat reçu
    - Décrémenter `active_producers` si aucun heartbeat reçu depuis 15s
    - _Requirements: 5.3, 7.3, 7.4_

  - [x] 4.4 Gérer les messages malformés → `dead_letter_queue`
    - Entourer la désérialisation JSON d'un try/except
    - En cas d'échec : insérer dans `dead_letter_queue` (`raw_message`, `source="kafka"`, `received_at`, `error`), incrémenter `messages_lost`, continuer
    - _Requirements: 5.4, 11.1, 11.4_

  - [ ]* 2.5 Écrire les tests de propriétés pour le calcul de latence (Property 5)
    - **Property 5 : Calcul correct de la latence de traitement**
    - **Validates: Requirements 5.1, 6.1**
    - Fichier : `tests/unit/test_latency_computation.py`

  - [ ]* 2.6 Écrire les tests de propriétés pour l'enrichissement (Property 6)
    - **Property 6 : Enrichissement correct des événements**
    - **Validates: Requirements 5.2, 6.2**
    - Fichier : `tests/unit/test_enrichment.py`

  - [ ]* 2.7 Écrire les tests de propriétés pour l'agrégation des métriques (Properties 7, 8, 9)
    - **Property 7 : Agrégation correcte des métriques**
    - **Property 8 : Rétention des métriques (≤ 1000 enregistrements par pipeline)**
    - **Property 9 : Structure complète des documents de métriques**
    - **Validates: Requirements 5.3, 6.3, 7.1, 7.2**
    - Fichier : `tests/unit/test_metrics_aggregation.py`

- [x] 5. Créer le producteur distant Kafka `producer_kafka/`
  - [x] 5.1 Créer `producer_kafka/requirements.txt`
    - Dépendances : `kafka-python==2.0.2`, `Faker==24.11.0`, `python-dotenv==1.0.1`
    - _Requirements: 3.1_

  - [x] 5.2 Créer `producer_kafka/.env.example`
    - Variables : `CENTRAL_IP=192.168.1.100`, `KAFKA_PORT=9092`, `PRODUCER_TYPE=kafka`, `SEND_INTERVAL_MS=500`
    - Inclure des commentaires explicatifs pour chaque variable
    - _Requirements: 2.1, 2.4_

  - [x] 5.3 Créer `producer_kafka/producer.py`
    - Vérifier la présence du fichier `.env` au démarrage ; si absent → afficher le chemin attendu et quitter avec code 1
    - Lire `CENTRAL_IP`, `KAFKA_PORT`, `SEND_INTERVAL_MS` depuis `.env` exclusivement (pas de valeur codée en dur)
    - Générer un `producer_id` unique : `kafka-{hostname}-{uuid4[:8]}`
    - Se connecter à Kafka sur `{CENTRAL_IP}:{KAFKA_PORT}` avec retry (3 tentatives, 10s entre chaque) ; si échec → afficher message d'aide et quitter
    - Générer des événements de ventes avec les champs `producer_id`, `pipeline="kafka"`, `sent_at` (ISO 8601)
    - Envoyer dans le topic `sales_events` avec la région comme clé
    - Envoyer un heartbeat toutes les 5s dans le topic `producer_heartbeats`
    - Respecter `SEND_INTERVAL_MS` ± 10% entre chaque envoi
    - Afficher dans la console : `producer_id`, type de pipeline, IP du PC central, confirmation de connexion
    - _Requirements: 1.2, 1.5, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 10.5, 10.6_

  - [x] 5.4 Créer `producer_kafka/start.sh`
    - Vérifier Python 3.8+ (sinon afficher erreur et quitter)
    - Créer `.env` depuis `.env.example` si absent
    - Installer les dépendances via `pip install -r requirements.txt`
    - Démarrer `producer.py`
    - Exécutable sans droits administrateur
    - _Requirements: 10.1, 10.2, 10.3_

  - [x] 5.5 Créer `producer_kafka/start.bat`
    - Même logique que `start.sh` pour Windows 10/11, sans PowerShell ni WSL
    - _Requirements: 10.1, 10.2, 10.4_

  - [ ]* 5.6 Écrire les tests de propriétés pour le chargement de la configuration (Property 1)
    - **Property 1 : Configuration chargée exclusivement depuis l'Env_File**
    - **Validates: Requirements 1.2, 1.3, 2.2**
    - Fichier : `tests/unit/test_config_loader.py`

  - [ ]* 5.7 Écrire les tests de propriétés pour le schéma d'événement et la sérialisation (Properties 2, 3)
    - **Property 2 : Schéma d'événement valide pour tout événement généré**
    - **Property 3 : Sérialisation round-trip des événements**
    - **Validates: Requirements 3.1, 3.2, 4.1, 4.2, 11.1, 11.2, 11.3**
    - Fichier : `tests/unit/test_event_schema.py`

  - [ ]* 5.8 Écrire les tests de propriétés pour l'intervalle d'envoi (Property 4)
    - **Property 4 : Respect de l'intervalle d'envoi (±10%)**
    - **Validates: Requirements 3.5, 4.5**
    - Fichier : `tests/unit/test_send_interval.py`

- [x] 6. Créer le producteur distant RabbitMQ `producer_rabbit/`
  - [x] 6.1 Créer `producer_rabbit/requirements.txt`
    - Dépendances : `pika==1.3.2`, `Faker==24.11.0`, `python-dotenv==1.0.1`
    - _Requirements: 4.1_

  - [x] 6.2 Créer `producer_rabbit/.env.example`
    - Variables : `CENTRAL_IP=192.168.1.100`, `RABBITMQ_PORT=5672`, `PRODUCER_TYPE=rabbitmq`, `SEND_INTERVAL_MS=500`
    - _Requirements: 2.1, 2.4_

  - [x] 6.3 Créer `producer_rabbit/producer.py`
    - Même logique que `producer_kafka/producer.py` mais pour RabbitMQ (pika)
    - Lire `CENTRAL_IP`, `RABBITMQ_PORT`, `SEND_INTERVAL_MS` depuis `.env`
    - Générer un `producer_id` : `rabbit-{hostname}-{uuid4[:8]}`
    - Déclarer la queue `sales_events` avec `durable=True`
    - Déclarer la queue `producer_heartbeats` avec `durable=True`
    - Envoyer des heartbeats toutes les 5s dans `producer_heartbeats`
    - Même schéma d'événement que le producteur Kafka avec `pipeline="rabbitmq"`
    - Retry connexion : 3 tentatives, 10s entre chaque, message d'aide si échec
    - _Requirements: 1.3, 1.5, 2.2, 2.3, 4.1, 4.2, 4.3, 4.4, 4.5, 10.5, 10.6_

  - [x] 6.4 Créer `producer_rabbit/start.sh` et `producer_rabbit/start.bat`
    - Même logique que les scripts Kafka
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [ ] 7. Checkpoint — S'assurer que les producteurs distants fonctionnent
  - S'assurer que tous les tests passent, poser des questions à l'utilisateur si nécessaire.

- [x] 8. Créer le service `dashboard/`
  - [x] 8.1 Créer `dashboard/Dockerfile` et `dashboard/requirements.txt`
    - Image de base `python:3.11-slim`
    - Dépendances : `Flask==3.0.3`, `pymongo==4.7.2`, `python-dotenv==1.0.1`
    - _Requirements: 8.1_

  - [x] 8.2 Créer `dashboard/app.py` — backend Flask
    - Lire `MONGO_URI` depuis les variables d'environnement
    - Créer l'index MongoDB `{ pipeline: 1, timestamp: -1 }` sur `benchmark_metrics` au démarrage
    - Implémenter `GET /api/metrics` : retourner les dernières métriques des 2 pipelines (valeurs nulles/zéro si aucune donnée) ; temps de réponse < 200ms
    - Implémenter `GET /api/metrics/history?pipeline=<kafka|rabbitmq>&limit=<n>` : valider les paramètres (400 si invalide), retourner les N derniers documents triés par timestamp décroissant
    - Implémenter `GET /api/producers` : retourner les producteurs actifs avec `producer_id`, `pipeline`, `last_heartbeat`
    - Implémenter `GET /api/verdict` : appeler `compute_verdict()`, retourner `{"sufficient_data": false}` si < 100 messages par pipeline
    - Implémenter `compute_verdict(kafka_metrics, rabbitmq_metrics)` : comparer les moyennes des 60 derniers points, calculer `kafka_score` et `rabbitmq_score` (0-3), générer `details` et `summary`
    - Retourner HTTP 400 + `{"error": "..."}` pour tout paramètre invalide
    - Retourner HTTP 500 + `{"error": "Database error"}` pour toute erreur MongoDB (sans exposer les détails)
    - Servir `index.html` sur la route `/`
    - _Requirements: 8.1, 8.3, 9.1, 9.2, 9.3, 9.4, 9.5, 12.1, 12.2, 12.3, 12.4, 12.5_

  - [x] 8.3 Créer `dashboard/templates/index.html` — frontend Chart.js
    - Deux colonnes côte à côte : « Kafka / Flink » et « RabbitMQ »
    - Chaque colonne affiche : débit (msg/s), latence moyenne (ms), total messages traités, messages perdus, producteurs actifs
    - Indicateurs colorés vert/rouge pour l'état de chaque producteur (basé sur les heartbeats)
    - Graphique linéaire comparatif du débit (60 derniers points) via Chart.js
    - Graphique linéaire comparatif de la latence (60 derniers points) via Chart.js
    - Verdict Engine affiché en haut de page (si données suffisantes)
    - Rafraîchissement automatique toutes les 2s via `fetch()` vers `/api/metrics`, `/api/verdict`, `/api/producers` (sans rechargement de page)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [ ]* 8.4 Écrire les tests de propriétés pour les endpoints API (Properties 10, 11, 12)
    - **Property 10 : Réponse structurée de l'API `/api/metrics`**
    - **Property 11 : Filtrage correct de l'historique des métriques**
    - **Property 12 : Validation des entrées de l'API (rejet des paramètres invalides)**
    - **Validates: Requirements 9.1, 9.2, 9.5**
    - Fichier : `tests/unit/test_api_endpoints.py`

  - [ ]* 8.5 Écrire les tests de propriétés pour le Verdict Engine (Properties 13, 14)
    - **Property 13 : Détermination correcte du vainqueur par le Verdict Engine**
    - **Property 14 : Cohérence du score global du Verdict Engine**
    - **Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5**
    - Fichier : `tests/unit/test_verdict_engine.py`

- [ ] 9. Créer la suite de tests `tests/`
  - [ ] 9.1 Créer `tests/unit/test_config_loader.py`
    - Tester Property 1 avec Hypothesis : pour tout couple `(CENTRAL_IP, PORT)` écrit dans un `.env` temporaire, vérifier que la chaîne de connexion construite utilise exactement ces valeurs
    - Tester : `.env` absent → message d'erreur + code de sortie 1 ; variable manquante → message d'erreur + code de sortie 1
    - _Requirements: 2.2, 2.3_

  - [ ] 9.2 Créer `tests/unit/test_event_schema.py`
    - Tester Property 2 avec Hypothesis : tout événement généré contient les champs obligatoires avec les bons types et valeurs
    - Tester Property 3 avec Hypothesis : sérialisation JSON UTF-8 puis désérialisation → objet identique à l'original
    - _Requirements: 3.1, 3.2, 4.1, 4.2, 11.3_

  - [ ] 9.3 Créer `tests/unit/test_send_interval.py`
    - Tester Property 4 avec Hypothesis : pour toute valeur de `SEND_INTERVAL_MS`, l'intervalle effectif reste dans `[SEND_INTERVAL_MS × 0.9, SEND_INTERVAL_MS × 1.1]`
    - _Requirements: 3.5, 4.5_

  - [ ] 9.4 Créer `tests/unit/test_latency_computation.py`
    - Tester Property 5 avec Hypothesis : pour tout couple `(received_at, sent_at)` valide, `latency_ms = (received_at - sent_at).total_seconds() * 1000` et `latency_ms >= 0`
    - _Requirements: 5.1, 6.1_

  - [ ] 9.5 Créer `tests/unit/test_enrichment.py`
    - Tester Property 6 avec Hypothesis : pour tout `total_amount > 0`, `tva_amount == round(total_amount * 0.1925, 2)` et `amount_ttc == round(total_amount * 1.1925, 2)`
    - _Requirements: 5.2, 6.2_

  - [ ] 9.6 Créer `tests/unit/test_metrics_aggregation.py`
    - Tester Property 7 avec Hypothesis : pour tout lot de N événements en T secondes, `throughput_per_sec == N/T` et `avg_latency_ms == mean(latencies)` ; `total_processed` est monotone croissant
    - Tester Property 8 avec Hypothesis : après N insertions, la collection contient au plus 1000 documents par pipeline (les plus anciens supprimés en premier)
    - Tester Property 9 avec Hypothesis : tout document inséré dans `benchmark_metrics` contient exactement les 7 champs requis avec les bons types
    - _Requirements: 5.3, 6.3, 7.1, 7.2_

  - [ ] 9.7 Créer `tests/unit/test_api_endpoints.py`
    - Tester Property 10 avec Hypothesis : `GET /api/metrics` retourne toujours les clés `"kafka"` et `"rabbitmq"` avec les 7 champs de métriques
    - Tester Property 11 avec Hypothesis : `GET /api/metrics/history?pipeline=P&limit=N` retourne au plus N documents, tous avec `pipeline == P`, triés par timestamp décroissant
    - Tester Property 12 avec Hypothesis : tout appel avec paramètre invalide retourne HTTP 400 + `{"error": "..."}` non vide
    - Utiliser le client de test Flask (`app.test_client()`) avec une base MongoDB mockée
    - _Requirements: 9.1, 9.2, 9.5_

  - [ ] 9.8 Créer `tests/unit/test_verdict_engine.py`
    - Tester Property 13 avec Hypothesis : pour tout couple `(kafka_val, rabbitmq_val)`, le vainqueur est correctement déterminé pour chaque métrique (débit, latence, fiabilité) ; en cas d'égalité stricte → `winner = None`
    - Tester Property 14 avec Hypothesis : `kafka_score + rabbitmq_score <= 3`, chaque score ∈ `[0, 3]`, chaque score = nombre de métriques remportées
    - Tester : `sufficient_data=False` quand < 100 messages par pipeline
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5_

  - [ ] 9.9 Créer `tests/unit/test_error_handling.py`
    - Tester (exemples unitaires) : retry connexion (3 tentatives, messages d'erreur), message malformé → `dead_letter_queue`, heartbeat envoyé toutes les 5s, queue RabbitMQ `durable=True`, `basic_qos(prefetch_count=10)`, verdict avec données insuffisantes
    - _Requirements: 1.5, 6.4, 6.5, 7.4, 10.6_

  - [ ] 9.10 Créer `tests/integration/test_kafka_pipeline.py`
    - Tester (exemples) : 1 événement valide Kafka → vérifier présence dans `sales_raw` avec champs d'enrichissement ; 1 événement malformé → vérifier présence dans `dead_letter_queue`
    - _Requirements: 5.1, 5.2, 5.4, 11.4_

  - [ ] 9.11 Créer `tests/integration/test_rabbitmq_pipeline.py`
    - Tester (exemples) : 1 événement valide RabbitMQ → vérifier présence dans `rabbitmq_raw` ; 1 événement malformé → vérifier présence dans `dead_letter_queue` + `basic_ack`
    - Vérifier que l'index `{ pipeline: 1, timestamp: -1 }` existe sur `benchmark_metrics`
    - _Requirements: 6.1, 6.2, 6.4, 7.5, 11.4_

- [ ] 10. Checkpoint final — S'assurer que tous les tests passent
  - S'assurer que tous les tests passent, poser des questions à l'utilisateur si nécessaire.

## Notes

- Les tâches marquées `*` sont optionnelles et peuvent être ignorées pour un MVP plus rapide
- Chaque tâche référence les exigences spécifiques pour la traçabilité
- Les tests de propriétés utilisent Hypothesis avec `@settings(max_examples=100)`
- Les tests d'intégration nécessitent Docker Compose en cours d'exécution
- La variable `CENTRAL_IP` dans `docker-compose.yml` doit être définie dans un fichier `.env` à la racine du projet avant de démarrer les services
