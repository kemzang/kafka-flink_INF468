"""
Producteur Kafka distant — Benchmark Kafka vs RabbitMQ
À exécuter sur les PCs distants (PC1, PC2)
Configuration via fichier .env (pas d'IP codée en dur)
"""
import json
import os
import random
import socket
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from faker import Faker
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable, KafkaError

# ─── Chargement de la configuration ──────────────────────────────────────────
ENV_PATH = Path(__file__).parent / ".env"

if not ENV_PATH.exists():
    print(f"[ERREUR] Fichier .env introuvable : {ENV_PATH}")
    print("[AIDE]  Copiez .env.example en .env et renseignez CENTRAL_IP")
    sys.exit(1)

load_dotenv(ENV_PATH)

CENTRAL_IP       = os.getenv("CENTRAL_IP")
KAFKA_PORT       = os.getenv("KAFKA_PORT", "9092")
SEND_INTERVAL_MS = int(os.getenv("SEND_INTERVAL_MS", "500"))

if not CENTRAL_IP:
    print("[ERREUR] Variable CENTRAL_IP manquante dans le fichier .env")
    sys.exit(1)

BOOTSTRAP_SERVERS = f"{CENTRAL_IP}:{KAFKA_PORT}"
TOPIC_SALES       = "sales_events"
TOPIC_HEARTBEAT   = "producer_heartbeats"

# Identifiant unique de ce producteur
PRODUCER_ID = f"kafka-{socket.gethostname()}-{str(uuid.uuid4())[:8]}"

fake = Faker("fr_FR")

PRODUCTS = [
    {"id": "P001", "name": "Laptop Dell XPS",  "category": "Informatique", "base_price": 1200.00},
    {"id": "P002", "name": "iPhone 15 Pro",    "category": "Téléphonie",   "base_price": 1100.00},
    {"id": "P003", "name": "Samsung TV 55\"",  "category": "Électronique", "base_price": 800.00},
    {"id": "P004", "name": "Nike Air Max",      "category": "Vêtements",    "base_price": 150.00},
    {"id": "P005", "name": "Livre Python",      "category": "Livres",       "base_price": 35.00},
    {"id": "P006", "name": "Café Arabica 1kg",  "category": "Alimentaire",  "base_price": 25.00},
    {"id": "P007", "name": "Casque Sony WH",    "category": "Électronique", "base_price": 350.00},
    {"id": "P008", "name": "Montre Garmin",     "category": "Sport",        "base_price": 450.00},
]
REGIONS          = ["Yaoundé", "Douala", "Bafoussam", "Garoua", "Bamenda", "Ngaoundéré"]
PAYMENT_METHODS  = ["carte_bancaire", "mobile_money", "espèces", "virement"]


def create_event():
    product        = random.choice(PRODUCTS)
    quantity       = random.randint(1, 5)
    unit_price     = round(product["base_price"] * random.uniform(0.85, 1.15), 2)
    total_amount   = round(unit_price * quantity, 2)
    now            = datetime.now(timezone.utc).isoformat()
    return {
        "event_id":       str(uuid.uuid4()),
        "producer_id":    PRODUCER_ID,
        "pipeline":       "kafka",
        "sent_at":        now,
        "timestamp":      now,
        "customer_id":    f"CUST-{random.randint(1000, 9999)}",
        "customer_name":  fake.name(),
        "product_id":     product["id"],
        "product_name":   product["name"],
        "category":       product["category"],
        "quantity":       quantity,
        "unit_price":     unit_price,
        "total_amount":   total_amount,
        "region":         random.choice(REGIONS),
        "payment_method": random.choice(PAYMENT_METHODS),
        "is_premium":     random.random() < 0.2,
    }


def connect_kafka(max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            producer = KafkaProducer(
                bootstrap_servers=BOOTSTRAP_SERVERS,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                retries=3,
                retry_backoff_ms=1000,
            )
            # Test de connexion
            producer.bootstrap_connected()
            return producer
        except (NoBrokersAvailable, KafkaError, Exception) as e:
            print(f"[ERREUR] Tentative {attempt}/{max_retries} — Impossible de joindre Kafka ({BOOTSTRAP_SERVERS}): {e}")
            if attempt < max_retries:
                print(f"[INFO]  Retry dans 10s...")
                time.sleep(10)
    print(f"[ERREUR] Connexion échouée après {max_retries} tentatives.")
    print(f"[AIDE]  Vérifiez que CENTRAL_IP={CENTRAL_IP} est correct dans .env")
    print(f"[AIDE]  Vérifiez que le PC central est démarré et accessible sur le réseau")
    sys.exit(1)


def heartbeat_sender(producer):
    """Envoie un heartbeat toutes les 5 secondes."""
    while True:
        try:
            hb = {
                "producer_id": PRODUCER_ID,
                "pipeline":    "kafka",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "type":        "heartbeat",
            }
            producer.send(TOPIC_HEARTBEAT, value=hb)
        except Exception as e:
            print(f"[WARN] Heartbeat échoué: {e}")
        time.sleep(5)


def main():
    print("=" * 60)
    print(f"[KAFKA-PRODUCER] Démarrage")
    print(f"[KAFKA-PRODUCER] Producer ID : {PRODUCER_ID}")
    print(f"[KAFKA-PRODUCER] Pipeline    : kafka")
    print(f"[KAFKA-PRODUCER] PC Central  : {BOOTSTRAP_SERVERS}")
    print(f"[KAFKA-PRODUCER] Intervalle  : {SEND_INTERVAL_MS}ms")
    print("=" * 60)

    producer = connect_kafka()
    print(f"[KAFKA-PRODUCER] ✅ Connecté à Kafka sur {BOOTSTRAP_SERVERS}")

    # Thread heartbeat
    t = threading.Thread(target=heartbeat_sender, args=(producer,), daemon=True)
    t.start()

    count = 0
    while True:
        try:
            event = create_event()
            producer.send(TOPIC_SALES, key=event["region"], value=event)
            count += 1
            print(f"[KAFKA-PRODUCER] #{count} | {event['product_name']} x{event['quantity']} "
                  f"= {event['total_amount']}€ | {event['region']}")

            # Intervalle ±10%
            interval_s = SEND_INTERVAL_MS / 1000.0
            jitter     = interval_s * 0.1
            time.sleep(interval_s + random.uniform(-jitter, jitter))

        except KafkaError as e:
            print(f"[ERREUR] Envoi Kafka: {e}")
            time.sleep(5)
        except Exception as e:
            print(f"[ERREUR] Inattendue: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
