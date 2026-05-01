"""
Producteur Kafka distant — Benchmark Kafka vs RabbitMQ
Mode normal  : 1 message toutes les SEND_INTERVAL_MS (défaut 500ms)
Mode turbo   : TURBO_MODE=true → plusieurs threads, batching lz4, débit maximal
Configuration via fichier .env
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

# ─── Chargement de la configuration ──────────────────────────
ENV_PATH = Path(__file__).parent / ".env"
if not ENV_PATH.exists():
    print(f"[ERREUR] Fichier .env introuvable : {ENV_PATH}")
    print("[AIDE]  Copiez .env.example en .env et renseignez CENTRAL_IP")
    sys.exit(1)

load_dotenv(ENV_PATH)

CENTRAL_IP       = os.getenv("CENTRAL_IP")
KAFKA_PORT       = os.getenv("KAFKA_PORT", "9092")
SEND_INTERVAL_MS = int(os.getenv("SEND_INTERVAL_MS", "500"))
TURBO_MODE       = os.getenv("TURBO_MODE", "false").lower() == "true"
TURBO_THREADS    = int(os.getenv("TURBO_THREADS", "4"))

if not CENTRAL_IP:
    print("[ERREUR] Variable CENTRAL_IP manquante dans le fichier .env")
    sys.exit(1)

BOOTSTRAP_SERVERS = f"{CENTRAL_IP}:{KAFKA_PORT}"
TOPIC_SALES       = "sales_events"
TOPIC_HEARTBEAT   = "producer_heartbeats"
PRODUCER_ID       = f"kafka-{socket.gethostname()}-{str(uuid.uuid4())[:8]}"

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
REGIONS         = ["Yaoundé", "Douala", "Bafoussam", "Garoua", "Bamenda", "Ngaoundéré"]
PAYMENT_METHODS = ["carte_bancaire", "mobile_money", "espèces", "virement"]


def create_event():
    product      = random.choice(PRODUCTS)
    quantity     = random.randint(1, 5)
    unit_price   = round(product["base_price"] * random.uniform(0.85, 1.15), 2)
    total_amount = round(unit_price * quantity, 2)
    now          = datetime.now(timezone.utc).isoformat()
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


def connect_kafka(turbo=False, max_retries=3):
    """Connexion Kafka — mode turbo active le batching lz4."""
    for attempt in range(1, max_retries + 1):
        try:
            if turbo:
                producer = KafkaProducer(
                    bootstrap_servers=BOOTSTRAP_SERVERS,
                    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                    key_serializer=lambda k: k.encode("utf-8") if k else None,
                    # ── Optimisations débit maximal ──
                    batch_size=65536,        # 64KB par batch
                    linger_ms=5,             # attend 5ms pour remplir le batch
                    compression_type="gzip", # compression native Python (pas de dépendance externe)
                    acks=1,                  # leader ack seulement
                    buffer_memory=67108864,  # 64MB buffer
                    retries=3,
                )
            else:
                producer = KafkaProducer(
                    bootstrap_servers=BOOTSTRAP_SERVERS,
                    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                    key_serializer=lambda k: k.encode("utf-8") if k else None,
                    retries=3,
                    retry_backoff_ms=1000,
                )
            producer.bootstrap_connected()
            return producer
        except (NoBrokersAvailable, KafkaError, Exception) as e:
            print(f"[ERREUR] Tentative {attempt}/{max_retries} — Impossible de joindre Kafka ({BOOTSTRAP_SERVERS}): {e}")
            if attempt < max_retries:
                print("[INFO]  Retry dans 10s...")
                time.sleep(10)
    print(f"[ERREUR] Connexion échouée après {max_retries} tentatives.")
    print(f"[AIDE]  Vérifiez que CENTRAL_IP={CENTRAL_IP} est correct dans .env")
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


# ─── Compteur partagé pour le mode turbo ─────────────────────
class TurboCounter:
    def __init__(self):
        self._lock  = threading.Lock()
        self._count = 0

    def add(self, n=1):
        with self._lock:
            self._count += n

    @property
    def count(self):
        with self._lock:
            return self._count


def turbo_worker(thread_id, counter):
    """Thread turbo — envoie le maximum de messages possible."""
    producer = connect_kafka(turbo=True)
    print(f"[KAFKA-TURBO-{thread_id}] ✅ Connecté — envoi en cours...")
    local = 0
    while True:
        try:
            event = create_event()
            producer.send(TOPIC_SALES, key=event["region"], value=event)
            local += 1
            if local % 500 == 0:
                producer.flush()
                counter.add(500)
        except KafkaError as e:
            print(f"[KAFKA-TURBO-{thread_id}] ❌ Erreur: {e}")
            time.sleep(2)
        except Exception as e:
            print(f"[KAFKA-TURBO-{thread_id}] ❌ Inattendue: {e}")
            time.sleep(2)


def main_turbo():
    """Mode turbo — plusieurs threads, batching lz4, débit maximal."""
    print("=" * 60)
    print(f"[KAFKA-TURBO] 🚀 Mode TURBO activé")
    print(f"[KAFKA-TURBO] Producer ID : {PRODUCER_ID}")
    print(f"[KAFKA-TURBO] PC Central  : {BOOTSTRAP_SERVERS}")
    print(f"[KAFKA-TURBO] Threads     : {TURBO_THREADS}")
    print(f"[KAFKA-TURBO] Batching    : 64KB + compression lz4")
    print("=" * 60)

    counter = TurboCounter()

    # Thread heartbeat (utilise une connexion normale)
    hb_producer = connect_kafka(turbo=False)
    t_hb = threading.Thread(target=heartbeat_sender, args=(hb_producer,), daemon=True)
    t_hb.start()

    # Threads turbo
    for i in range(TURBO_THREADS):
        t = threading.Thread(target=turbo_worker, args=(i+1, counter), daemon=True)
        t.start()

    # Reporter toutes les 5s
    prev       = 0
    prev_time  = time.time()
    start_time = time.time()
    while True:
        time.sleep(5)
        now      = time.time()
        total    = counter.count
        rate     = (total - prev) / max(now - prev_time, 0.001)
        elapsed  = now - start_time
        print(f"[KAFKA-TURBO] ⚡ {total:>10,} msgs total | {rate:>8,.0f} msg/s | {elapsed:.0f}s écoulées")
        prev      = total
        prev_time = now


def main_normal():
    """Mode normal — 1 message toutes les SEND_INTERVAL_MS."""
    print("=" * 60)
    print(f"[KAFKA-PRODUCER] Démarrage")
    print(f"[KAFKA-PRODUCER] Producer ID : {PRODUCER_ID}")
    print(f"[KAFKA-PRODUCER] PC Central  : {BOOTSTRAP_SERVERS}")
    print(f"[KAFKA-PRODUCER] Intervalle  : {SEND_INTERVAL_MS}ms")
    print("=" * 60)

    producer = connect_kafka(turbo=False)
    print(f"[KAFKA-PRODUCER] ✅ Connecté à Kafka sur {BOOTSTRAP_SERVERS}")

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
    if TURBO_MODE:
        main_turbo()
    else:
        main_normal()
