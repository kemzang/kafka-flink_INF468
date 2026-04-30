# Design Document — kafka-rabbitmq-benchmark

## Overview

Ce document décrit l'architecture technique du benchmark académique **Kafka/Flink vs RabbitMQ**. L'objectif est d'étendre le pipeline Kafka/Flink existant pour y ajouter un pipeline RabbitMQ symétrique, un dashboard web temps réel Flask/Chart.js, et des producteurs distants autonomes, le tout orchestré via Docker Compose sur un PC central.

### Objectifs techniques

- Comparer objectivement Kafka/Flink et RabbitMQ sur trois métriques : débit (msg/s), latence (ms), fiabilité (messages perdus).
- Permettre à 4 PCs distants (2 Kafka + 2 RabbitMQ) de se connecter au PC central via un simple fichier `.env`.
- Exposer un dashboard web accessible depuis n'importe quel navigateur du réseau local.
- Calculer automatiquement un verdict comparatif via le Verdict Engine.

### Contraintes

- Stack : Python 3.8+, Flask, Docker Compose, Chart.js, kafka-python, pika, pymongo.
- Aucune IP codée en dur dans le code source des producteurs distants.
- Le PC central héberge l'intégralité de l'infrastructure dans un seul `docker-compose.yml`.
- Les producteurs distants s'installent et démarrent via un seul script (`start.sh` / `start.bat`).

---

## Architecture

### Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────────┐
│                          PC CENTRAL (Docker Compose)                │
│                                                                     │
│  ┌──────────┐   sales_events   ┌──────────────────┐                │
│  │  Kafka   │◄─────────────────│  Flink Pipeline  │──► sales_raw   │
│  │ (KRaft)  │   (topic)        │  (flink_job.py)  │──► metrics     │
│  └──────────┘                  └──────────────────┘                │
│       ▲                                                             │
│       │ producer_heartbeats                                         │
│       │                                                             │
│  ┌──────────┐   sales_events   ┌──────────────────┐                │
│  │ RabbitMQ │◄─────────────────│ RabbitMQ Worker  │──► rabbitmq_raw│
│  │          │   (queue)        │ (worker.py)      │──► metrics     │
│  └──────────┘                  └──────────────────┘                │
│       ▲                                                             │
│       │ producer_heartbeats                                         │
│       │                                                             │
│  ┌──────────┐                  ┌──────────────────┐                │
│  │ MongoDB  │◄─────────────────│ Flask Dashboard  │──► :5000       │
│  │          │  benchmark_metrics│  (app.py)        │               │
│  └──────────┘                  └──────────────────┘                │
└─────────────────────────────────────────────────────────────────────┘
         ▲                              ▲
         │ :9092 (Kafka)                │ :5672 (RabbitMQ)
         │                              │
┌────────┴──────────┐        ┌──────────┴────────────┐
│  PC Distant 1 & 2 │        │  PC Distant 3 & 4     │
│  producer_kafka/  │        │  producer_rabbit/     │
│  (kafka-python)   │        │  (pika)               │
└───────────────────┘        └───────────────────────┘
```

### Flux de données

1. **Producteurs distants** génèrent des événements de ventes JSON et les envoient vers le PC central (Kafka topic `sales_events` ou RabbitMQ queue `sales_events`).
2. **Flink Pipeline** consomme le topic Kafka, enrichit les événements, les stocke dans `sales_raw`, et écrit les métriques dans `benchmark_metrics` toutes les 5 secondes.
3. **RabbitMQ Worker** consomme la queue RabbitMQ, enrichit les événements, les stocke dans `rabbitmq_raw`, et écrit les métriques dans `benchmark_metrics` toutes les 5 secondes.
4. **Flask Dashboard** interroge MongoDB via l'API REST et sert le frontend Chart.js.
5. **Heartbeat system** : chaque producteur envoie un signal toutes les 5 secondes ; les workers maintiennent le compteur `active_producers`.

### Décisions d'architecture

| Décision | Choix | Rationale |
|---|---|---|
| Métriques centralisées | MongoDB `benchmark_metrics` | Permet l'historique, les requêtes temporelles, et la comparaison côte à côte |
| Métriques push vs pull | Push depuis les workers (toutes les 5s) | Évite le polling MongoDB depuis le dashboard à haute fréquence |
| Refresh dashboard | Polling REST toutes les 2s | Simple, compatible tous navigateurs, pas de WebSocket nécessaire |
| Producteurs distants | Scripts standalone + `.env` | Aucune dépendance Docker sur les PCs distants |
| Heartbeat | Topic/queue dédiés | Isolation du canal de monitoring du canal de données |

---

## Components and Interfaces

### 1. docker-compose.yml (étendu)

Services ajoutés au `docker-compose.yml` existant :

| Service | Image / Build | Ports exposés | Dépendances |
|---|---|---|---|
| `rabbitmq` | `rabbitmq:3.13-management` | 5672, 15672 | — |
| `rabbitmq-worker` | `./rabbitmq_worker/` | — | rabbitmq, mongodb |
| `dashboard` | `./dashboard/` | 5000 | mongodb |

Le service `producer` existant (Kafka interne) est conservé pour les tests locaux. Les producteurs distants sont des scripts standalone hors Docker.

**Variable d'environnement clé pour Kafka (accès externe)** : `KAFKA_ADVERTISED_LISTENERS` doit inclure `PLAINTEXT_HOST://0.0.0.0:9092` pour être accessible depuis les PCs distants. La valeur actuelle `PLAINTEXT_HOST://localhost:9092` doit être remplacée par `PLAINTEXT_HOST://${CENTRAL_IP}:9092` via une variable d'environnement du docker-compose.

### 2. rabbitmq_worker/ (nouveau service)

```
rabbitmq_worker/
├── Dockerfile
├── requirements.txt
└── worker.py
```

**Interface** :
- Consomme la queue RabbitMQ `sales_events` (durable).
- Consomme la queue RabbitMQ `producer_heartbeats`.
- Écrit dans MongoDB : `rabbitmq_raw`, `benchmark_metrics`, `dead_letter_queue`.
- Utilise `basic_qos(prefetch_count=10)`.

**Variables d'environnement** :
- `RABBITMQ_HOST` (défaut : `rabbitmq`)
- `RABBITMQ_PORT` (défaut : `5672`)
- `MONGO_URI`

### 3. dashboard/ (nouveau service)

```
dashboard/
├── Dockerfile
├── requirements.txt
├── app.py
└── templates/
    └── index.html
```

**Interface REST** :

| Endpoint | Méthode | Description | Réponse |
|---|---|---|---|
| `/api/metrics` | GET | Dernières métriques des 2 pipelines | `{kafka: {...}, rabbitmq: {...}}` |
| `/api/metrics/history` | GET | Historique filtré par pipeline et limite | `[{...}, ...]` |
| `/api/producers` | GET | Producteurs actifs avec dernier heartbeat | `[{producer_id, pipeline, last_heartbeat}]` |
| `/api/verdict` | GET | Verdict comparatif calculé | `{kafka_score, rabbitmq_score, details, summary}` |

**Paramètres de `/api/metrics/history`** :
- `pipeline` : `kafka` ou `rabbitmq` (requis)
- `limit` : entier positif, défaut 60, max 1000

**Codes d'erreur** :
- `400` : paramètre invalide (pipeline inconnu, limit non entier, limit ≤ 0)
- `500` : erreur MongoDB

### 4. producer_kafka/ (producteur distant Kafka)

```
producer_kafka/
├── producer.py
├── requirements.txt
├── .env.example
├── start.sh
└── start.bat
```

**Interface** :
- Lit la configuration depuis `.env` (variables : `CENTRAL_IP`, `KAFKA_PORT`, `PRODUCER_TYPE=kafka`, `SEND_INTERVAL_MS`).
- Publie dans le topic Kafka `sales_events` sur `{CENTRAL_IP}:{KAFKA_PORT}`.
- Publie des heartbeats dans le topic `producer_heartbeats` toutes les 5 secondes.
- Génère un `producer_id` unique au démarrage (format : `kafka-{hostname}-{uuid4[:8]}`).

### 5. producer_rabbit/ (producteur distant RabbitMQ)

```
producer_rabbit/
├── producer.py
├── requirements.txt
├── .env.example
├── start.sh
└── start.bat
```

**Interface** :
- Lit la configuration depuis `.env` (variables : `CENTRAL_IP`, `RABBITMQ_PORT`, `PRODUCER_TYPE=rabbitmq`, `SEND_INTERVAL_MS`).
- Publie dans la queue RabbitMQ `sales_events` (durable) sur `{CENTRAL_IP}:{RABBITMQ_PORT}`.
- Publie des heartbeats dans la queue `producer_heartbeats` toutes les 5 secondes.
- Génère un `producer_id` unique au démarrage (format : `rabbit-{hostname}-{uuid4[:8]}`).

### 6. Flink Pipeline (étendu)

Le `flink_job.py` existant est étendu pour :
- Ajouter les champs `producer_id`, `pipeline`, `sent_at` dans le traitement.
- Calculer la latence : `latency_ms = (datetime.utcnow() - datetime.fromisoformat(event['sent_at'].rstrip('Z'))).total_seconds() * 1000`.
- Écrire dans `benchmark_metrics` toutes les 5 secondes.
- Consommer le topic `producer_heartbeats` pour maintenir `active_producers`.
- Envoyer les messages malformés dans `dead_letter_queue`.

### 7. Verdict Engine (composant du Dashboard)

Fonction Python dans `dashboard/app.py` :

```python
def compute_verdict(kafka_metrics: list[dict], rabbitmq_metrics: list[dict]) -> dict
```

- Prend les 60 derniers points de chaque pipeline.
- Calcule les moyennes de `throughput_per_sec`, `avg_latency_ms`, `messages_lost`.
- Retourne un dict avec `kafka_score` (0-3), `rabbitmq_score` (0-3), `details` (par métrique), `summary` (texte).

---

## Data Models

### Collection `benchmark_metrics`

Document MongoDB inséré toutes les 5 secondes par chaque worker :

```json
{
  "_id": ObjectId,
  "pipeline": "kafka",
  "timestamp": ISODate("2024-01-15T10:30:00Z"),
  "throughput_per_sec": 42.5,
  "avg_latency_ms": 12.3,
  "total_processed": 1250,
  "messages_lost": 0,
  "active_producers": 2
}
```

Index : `{ pipeline: 1, timestamp: -1 }` (compound, pour les requêtes d'historique).
Rétention : les workers suppriment les documents au-delà des 1000 derniers par pipeline après chaque insertion.

### Collection `sales_raw` (existante, enrichie)

Champs ajoutés aux champs existants :

```json
{
  "producer_id": "kafka-PC-LABO-a1b2c3d4",
  "pipeline": "kafka",
  "sent_at": "2024-01-15T10:30:00.123Z",
  "latency_ms": 15.7,
  "tva_amount": 24.13,
  "amount_ttc": 149.13,
  "processed_at": "2024-01-15T10:30:00.139Z",
  "processor": "kafka-flink-pipeline-v1.0"
}
```

### Collection `rabbitmq_raw` (nouvelle)

Même schéma que `sales_raw` avec `pipeline: "rabbitmq"` et `processor: "rabbitmq-worker-v1.0"`.

### Collection `dead_letter_queue` (nouvelle)

```json
{
  "_id": ObjectId,
  "raw_message": "<bytes bruts ou string>",
  "source": "kafka",
  "received_at": ISODate("2024-01-15T10:30:00Z"),
  "error": "JSONDecodeError: Expecting value: line 1 column 1"
}
```

### Schéma d'un événement de vente (producteurs distants)

```json
{
  "event_id": "uuid4",
  "producer_id": "kafka-PC-LABO-a1b2c3d4",
  "pipeline": "kafka",
  "sent_at": "2024-01-15T10:30:00.123Z",
  "timestamp": "2024-01-15T10:30:00.123Z",
  "customer_id": "CUST-4521",
  "customer_name": "Jean Dupont",
  "product_id": "P001",
  "product_name": "Laptop Dell XPS",
  "category": "Informatique",
  "quantity": 2,
  "unit_price": 1230.00,
  "total_amount": 2460.00,
  "region": "Douala",
  "payment_method": "carte_bancaire",
  "is_premium": false
}
```

### Schéma d'un Heartbeat

```json
{
  "producer_id": "kafka-PC-LABO-a1b2c3d4",
  "pipeline": "kafka",
  "timestamp": "2024-01-15T10:30:00.123Z",
  "type": "heartbeat"
}
```

### Réponse `/api/metrics`

```json
{
  "kafka": {
    "pipeline": "kafka",
    "timestamp": "2024-01-15T10:30:00Z",
    "throughput_per_sec": 42.5,
    "avg_latency_ms": 12.3,
    "total_processed": 1250,
    "messages_lost": 0,
    "active_producers": 2
  },
  "rabbitmq": {
    "pipeline": "rabbitmq",
    "timestamp": "2024-01-15T10:30:00Z",
    "throughput_per_sec": 18.2,
    "avg_latency_ms": 28.7,
    "total_processed": 890,
    "messages_lost": 3,
    "active_producers": 2
  }
}
```

### Réponse `/api/verdict`

```json
{
  "kafka_score": 3,
  "rabbitmq_score": 0,
  "sufficient_data": true,
  "details": {
    "throughput": {
      "winner": "kafka",
      "kafka_avg": 42.5,
      "rabbitmq_avg": 18.2,
      "ratio": "Kafka 2.3× plus rapide"
    },
    "latency": {
      "winner": "kafka",
      "kafka_avg_ms": 12.3,
      "rabbitmq_avg_ms": 28.7,
      "diff_ms": 16.4
    },
    "reliability": {
      "winner": "kafka",
      "kafka_lost": 0,
      "rabbitmq_lost": 3
    }
  },
  "summary": "Kafka remporte les 3 métriques : débit 2.3× supérieur, latence 16.4ms plus faible, 0 messages perdus vs 3."
}
```

### Fichier `.env` (producteurs distants)

```dotenv
# IP du PC Central hébergeant Kafka et RabbitMQ
CENTRAL_IP=192.168.1.100

# Ports des brokers
KAFKA_PORT=9092
RABBITMQ_PORT=5672

# Type de producteur : kafka ou rabbitmq
PRODUCER_TYPE=kafka

# Intervalle d'envoi en millisecondes (défaut : 500ms)
SEND_INTERVAL_MS=500
```


---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1 : Configuration chargée exclusivement depuis l'Env_File

*Pour tout* couple `(CENTRAL_IP, PORT)` valide écrit dans un fichier `.env`, le producteur (Kafka ou RabbitMQ) doit construire sa chaîne de connexion en utilisant exactement ces valeurs — sans aucune valeur d'IP ou de port codée en dur dans le code source.

**Validates: Requirements 1.2, 1.3, 2.2**

---

### Property 2 : Schéma d'événement valide pour tout événement généré

*Pour tout* événement de vente généré par un producteur Kafka ou RabbitMQ, l'événement doit contenir les champs obligatoires `event_id`, `producer_id`, `pipeline`, `sent_at` (ISO 8601 valide), `timestamp`, `customer_id`, `product_id`, `total_amount`, `region`, et `payment_method`, avec `pipeline` égal à `"kafka"` ou `"rabbitmq"` selon le producteur.

**Validates: Requirements 3.1, 3.2, 4.1, 4.2**

---

### Property 3 : Sérialisation round-trip des événements

*Pour tout* événement de vente valide (dictionnaire Python), la sérialisation en JSON UTF-8 suivie de la désérialisation doit produire un objet équivalent à l'original — aucun champ ne doit être perdu, modifié ou ajouté.

**Validates: Requirements 11.1, 11.2, 11.3**

---

### Property 4 : Respect de l'intervalle d'envoi

*Pour toute* valeur de `SEND_INTERVAL_MS` configurée dans l'Env_File, l'intervalle effectif entre deux envois consécutifs du producteur doit rester dans la plage `[SEND_INTERVAL_MS × 0.9, SEND_INTERVAL_MS × 1.1]` (tolérance ±10%).

**Validates: Requirements 3.5, 4.5**

---

### Property 5 : Calcul correct de la latence de traitement

*Pour tout* événement reçu par le Flink_Pipeline ou le RabbitMQ_Worker, la latence calculée en millisecondes doit être égale à `(received_at − sent_at).total_seconds() × 1000`, avec `received_at` l'horodatage de réception et `sent_at` le champ ISO 8601 de l'événement. La latence doit toujours être ≥ 0.

**Validates: Requirements 5.1, 6.1**

---

### Property 6 : Enrichissement correct des événements

*Pour tout* événement avec un champ `total_amount` valide, les champs d'enrichissement calculés doivent satisfaire : `tva_amount = round(total_amount × 0.1925, 2)` et `amount_ttc = round(total_amount × 1.1925, 2)`. Cette propriété doit tenir pour tout montant positif, y compris les valeurs décimales et les montants élevés.

**Validates: Requirements 5.2, 6.2**

---

### Property 7 : Agrégation correcte des métriques

*Pour tout* lot de N événements traités en T secondes avec des latences `[l₁, l₂, ..., lₙ]`, le document de métriques produit doit satisfaire : `throughput_per_sec = N / T` et `avg_latency_ms = mean([l₁, ..., lₙ])`. Le champ `total_processed` doit être monotone croissant entre deux snapshots consécutifs.

**Validates: Requirements 5.3, 6.3**

---

### Property 8 : Rétention des métriques (au plus 1000 enregistrements par pipeline)

*Pour tout* nombre N d'insertions dans `benchmark_metrics` pour un pipeline donné, après chaque insertion la collection doit contenir au plus 1000 documents pour ce pipeline. Si N > 1000, les documents les plus anciens (par `timestamp`) doivent être supprimés en premier.

**Validates: Requirements 7.1, 7.2**

---

### Property 9 : Structure complète des documents de métriques

*Pour tout* snapshot de métriques inséré dans `benchmark_metrics`, le document doit contenir exactement les champs : `pipeline` (string, `"kafka"` ou `"rabbitmq"`), `timestamp` (datetime), `throughput_per_sec` (float ≥ 0), `avg_latency_ms` (float ≥ 0), `total_processed` (int ≥ 0), `messages_lost` (int ≥ 0), `active_producers` (int ≥ 0).

**Validates: Requirements 7.1**

---

### Property 10 : Réponse structurée de l'API `/api/metrics`

*Pour tout* état de la base de données, la réponse de `GET /api/metrics` doit contenir les clés `"kafka"` et `"rabbitmq"`, chacune avec les 7 champs de métriques requis. Si aucune métrique n'est disponible pour un pipeline, les valeurs numériques doivent être `0` ou `null` mais les clés doivent toujours être présentes.

**Validates: Requirements 9.1**

---

### Property 11 : Filtrage correct de l'historique des métriques

*Pour tout* appel à `GET /api/metrics/history?pipeline=P&limit=N` avec P ∈ `{"kafka", "rabbitmq"}` et N entier positif, la réponse doit contenir au plus N documents, tous avec `pipeline == P`, triés par `timestamp` décroissant.

**Validates: Requirements 9.2**

---

### Property 12 : Validation des entrées de l'API (rejet des paramètres invalides)

*Pour tout* appel à un endpoint de l'API avec un paramètre invalide (pipeline inconnu, limit non entier, limit ≤ 0, ou paramètre requis absent), la réponse doit avoir le code HTTP 400 et un corps JSON contenant une clé `"error"` avec un message descriptif non vide.

**Validates: Requirements 9.5**

---

### Property 13 : Détermination correcte du vainqueur par le Verdict Engine

*Pour tout* couple de métriques `(kafka_val, rabbitmq_val)` pour une métrique donnée (débit, latence, messages perdus), le Verdict Engine doit désigner le bon vainqueur : pour le débit, `winner = "kafka"` si et seulement si `kafka_avg_throughput > rabbitmq_avg_throughput` ; pour la latence, `winner = "kafka"` si et seulement si `kafka_avg_latency < rabbitmq_avg_latency` ; pour la fiabilité, `winner = "kafka"` si et seulement si `kafka_lost < rabbitmq_lost`. En cas d'égalité stricte, aucun vainqueur n'est désigné.

**Validates: Requirements 12.1, 12.2, 12.3, 12.4**

---

### Property 14 : Cohérence du score global du Verdict Engine

*Pour tout* résultat de verdict, `kafka_score + rabbitmq_score ≤ 3`, chaque score est un entier dans `[0, 3]`, et chaque score est égal au nombre de métriques pour lesquelles ce pipeline a été désigné vainqueur.

**Validates: Requirements 12.5**

---

## Error Handling

### Stratégie générale

Chaque composant adopte une stratégie de **fail-safe** : les erreurs sont isolées, journalisées, et le traitement continue sans interrompre le pipeline.

### Producteurs distants (Kafka et RabbitMQ)

| Scénario d'erreur | Comportement attendu |
|---|---|
| Fichier `.env` absent au démarrage | Afficher le chemin attendu, arrêter le processus avec code de sortie 1 |
| Variable manquante dans `.env` | Afficher la variable manquante, arrêter le processus avec code de sortie 1 |
| Connexion impossible au PC_Central | Afficher un message d'erreur, retenter toutes les 10s pendant 30s, puis afficher un message d'aide et s'arrêter après 3 tentatives |
| Erreur d'envoi d'un événement | Logger l'erreur, incrémenter un compteur local d'erreurs, continuer la production |
| Erreur d'envoi du heartbeat | Logger l'erreur, continuer — le heartbeat suivant sera tenté dans 5s |

### Flink Pipeline

| Scénario d'erreur | Comportement attendu |
|---|---|
| Message Kafka non désérialisable (JSON invalide) | Insérer dans `dead_letter_queue` avec `raw_message`, `source="kafka"`, `received_at`, `error` ; incrémenter `messages_lost` ; continuer |
| Champ `sent_at` absent ou malformé | Utiliser `latency_ms = null` ; stocker l'événement normalement |
| Erreur d'écriture MongoDB | Logger l'erreur, retenter 3 fois avec backoff exponentiel (1s, 2s, 4s), puis continuer |
| Connexion MongoDB perdue | Tenter une reconnexion automatique via le client pymongo (reconnect automatique) |

### RabbitMQ Worker

| Scénario d'erreur | Comportement attendu |
|---|---|
| Message RabbitMQ non désérialisable | Insérer dans `dead_letter_queue` ; incrémenter `messages_lost` ; **acquitter le message** (`basic_ack`) pour éviter le blocage de la queue |
| Connexion RabbitMQ perdue | Tenter une reconnexion avec backoff exponentiel (5s, 10s, 20s, max 60s) |
| Erreur d'écriture MongoDB | Même stratégie que le Flink Pipeline |

### Dashboard Flask

| Scénario d'erreur | Comportement attendu |
|---|---|
| Paramètre API invalide | Retourner HTTP 400 avec `{"error": "<message descriptif>"}` |
| Erreur MongoDB lors d'une requête API | Retourner HTTP 500 avec `{"error": "Database error"}` (sans exposer les détails internes) |
| Aucune donnée disponible pour un pipeline | Retourner des valeurs nulles/zéro plutôt qu'une erreur 404 |
| Données insuffisantes pour le verdict (< 100 messages) | Retourner `{"sufficient_data": false}` dans `/api/verdict` |

### Dead Letter Queue

Tous les messages non désérialisables sont archivés dans la collection MongoDB `dead_letter_queue` avec le schéma suivant :

```json
{
  "_id": ObjectId,
  "raw_message": "<string ou bytes encodés en base64>",
  "source": "kafka | rabbitmq",
  "received_at": ISODate,
  "error": "<message d'erreur Python>"
}
```

---

## Testing Strategy

### Approche duale

La stratégie de test combine des **tests unitaires** (exemples concrets, cas limites, comportements d'erreur) et des **tests basés sur les propriétés** (vérification de propriétés universelles sur des entrées générées aléatoirement). Les deux approches sont complémentaires.

### Bibliothèque de tests basés sur les propriétés

**Hypothesis** (Python) est utilisé pour les tests basés sur les propriétés. Chaque test de propriété est configuré pour un minimum de **100 itérations** (`settings(max_examples=100)`).

```python
from hypothesis import given, settings
from hypothesis import strategies as st
```

### Organisation des tests

```
tests/
├── unit/
│   ├── test_config_loader.py        # Properties 1
│   ├── test_event_schema.py         # Properties 2, 3
│   ├── test_send_interval.py        # Property 4
│   ├── test_latency_computation.py  # Property 5
│   ├── test_enrichment.py           # Property 6
│   ├── test_metrics_aggregation.py  # Properties 7, 8, 9
│   ├── test_api_endpoints.py        # Properties 10, 11, 12
│   ├── test_verdict_engine.py       # Properties 13, 14
│   └── test_error_handling.py       # Tests unitaires (exemples)
└── integration/
    ├── test_kafka_pipeline.py       # Tests d'intégration Kafka → MongoDB
    └── test_rabbitmq_pipeline.py    # Tests d'intégration RabbitMQ → MongoDB
```

### Tests basés sur les propriétés (Hypothesis)

Chaque test de propriété doit être annoté avec un commentaire de traçabilité :

```python
# Feature: kafka-rabbitmq-benchmark, Property N: <texte de la propriété>
@given(...)
@settings(max_examples=100)
def test_property_N_description(...)
```

**Exemples de tests de propriétés :**

```python
# Feature: kafka-rabbitmq-benchmark, Property 3: Sérialisation round-trip des événements
@given(st.fixed_dictionaries({
    "event_id": st.uuids().map(str),
    "producer_id": st.text(min_size=1, max_size=50),
    "pipeline": st.sampled_from(["kafka", "rabbitmq"]),
    "sent_at": st.datetimes().map(lambda d: d.isoformat() + "Z"),
    "total_amount": st.floats(min_value=0.01, max_value=10000.0, allow_nan=False),
    "region": st.text(min_size=1, max_size=30),
}))
@settings(max_examples=100)
def test_serialization_round_trip(event):
    serialized = json.dumps(event).encode('utf-8')
    deserialized = json.loads(serialized.decode('utf-8'))
    assert deserialized == event
```

```python
# Feature: kafka-rabbitmq-benchmark, Property 6: Enrichissement correct des événements
@given(st.floats(min_value=0.01, max_value=100000.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=100)
def test_event_enrichment(total_amount):
    event = {"total_amount": total_amount}
    enriched = enrich_event(event)
    assert enriched["tva_amount"] == round(total_amount * 0.1925, 2)
    assert enriched["amount_ttc"] == round(total_amount * 1.1925, 2)
```

```python
# Feature: kafka-rabbitmq-benchmark, Property 13: Détermination correcte du vainqueur
@given(
    st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=1000.0, allow_nan=False),
)
@settings(max_examples=100)
def test_verdict_throughput_winner(kafka_throughput, rabbitmq_throughput):
    result = compute_verdict_metric("throughput", kafka_throughput, rabbitmq_throughput)
    if kafka_throughput > rabbitmq_throughput:
        assert result["winner"] == "kafka"
    elif rabbitmq_throughput > kafka_throughput:
        assert result["winner"] == "rabbitmq"
    else:
        assert result["winner"] is None
```

### Tests unitaires (exemples)

Les tests unitaires couvrent les comportements spécifiques non capturés par les propriétés :

- **Connexion échouée** : mock de la connexion réseau, vérification du retry (3 tentatives, messages d'erreur).
- **Fichier `.env` absent** : vérification du message d'erreur et du code de sortie.
- **Message malformé → dead_letter_queue** : envoi d'un JSON invalide, vérification de l'insertion dans `dead_letter_queue`.
- **Heartbeat** : mock du temps, vérification de l'envoi toutes les 5s et du décrément de `active_producers` après 15s sans heartbeat.
- **Queue RabbitMQ durable** : mock pika, vérification de `queue_declare(durable=True)`.
- **`basic_qos(prefetch_count=10)`** : mock pika channel, vérification de l'appel.
- **Verdict avec données insuffisantes** : vérification de `sufficient_data=False` quand < 100 messages.

### Tests d'intégration

Les tests d'intégration vérifient le câblage entre les composants. Ils utilisent 1 à 3 exemples représentatifs et ne sont **pas** des tests basés sur les propriétés :

| Test | Description | Exemples |
|---|---|---|
| Kafka → Flink → MongoDB | Envoyer un événement Kafka, vérifier qu'il apparaît dans `sales_raw` avec les champs d'enrichissement | 1 événement valide, 1 événement malformé |
| RabbitMQ → Worker → MongoDB | Envoyer un message RabbitMQ, vérifier qu'il apparaît dans `rabbitmq_raw` | 1 événement valide, 1 événement malformé |
| Index MongoDB | Vérifier que l'index `{pipeline: 1, timestamp: -1}` existe sur `benchmark_metrics` | 1 vérification |
| Dashboard `/api/metrics` | Vérifier la réponse avec des données réelles en base | 1 appel avec données, 1 appel sans données |

### Couverture cible

| Composant | Tests de propriétés | Tests unitaires | Tests d'intégration |
|---|---|---|---|
| Config loader | Property 1 | `.env` absent, variable manquante | — |
| Producteurs | Properties 2, 3, 4 | Retry connexion, heartbeat, démarrage | — |
| Flink Pipeline | Properties 5, 6, 7 | Message malformé, dead_letter_queue | Kafka → MongoDB |
| RabbitMQ Worker | Properties 5, 6, 7 | Message malformé, ack, prefetch | RabbitMQ → MongoDB |
| Metrics Store | Properties 8, 9 | Heartbeat, timeout producteur | Index MongoDB |
| Dashboard API | Properties 10, 11, 12 | Erreur MongoDB, données insuffisantes | `/api/metrics` live |
| Verdict Engine | Properties 13, 14 | Données insuffisantes, égalité | — |
