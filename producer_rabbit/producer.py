"""
Producteur RabbitMQ distant — Benchmark Kafka vs RabbitMQ
Mode normal  : 1 message toutes les SEND_INTERVAL_MS (défaut 500ms)
Mode turbo   : TURBO_MODE=true → plusieurs threads, débit maximal
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
import pika
from pika.exceptions import AMQPConnectionError

# ─── Chargement de la configuration ──────────────────────────
ENV_PATH = Path(__file__).parent / ".env"
if not ENV_PATH.exists():
    print(f"[ERREUR] Fichier .env introuvable : {ENV_PATH}")
    print("[AIDE]  Copiez .env.example en .env et renseignez CENTRAL_IP")
    sys.exit(1)

load_dotenv(ENV_PATH)

CENTRAL_IP       = os.getenv("CENTRAL_IP")
RABBITMQ_PORT    = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER    = os.getenv("RABBITMQ_USER", "admin")
RABBITMQ_PASS    = os.getenv("RABBITMQ_PASS", "password123")
SEND_INTERVAL_MS = int(os.getenv("SEND_INTERVAL_MS", "500"))
TURBO_MODE       = os.getenv("TURBO_MODE", "false").lower() == "true"
TURBO_THREADS    = int(os.getenv("TURBO_THREADS", "4"))

if not CENTRAL_IP:
    print("[ERREUR] Variable CENTRAL_IP manquante dans le fichier .env")
    sys.exit(1)

QUEUE_SALES     = "sales_events"
QUEUE_HEARTBEAT = "producer_heartbeats"
PRODUCER_ID     = f"rabbit-{socket.gethostname()}-{str(uuid.uuid4())[:8]}"

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
        "pipeline":       "rabbitmq",
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


def make_pika_params():
    return pika.ConnectionParameters(
        host=CENTRAL_IP,
        port=RABBITMQ_PORT,
        credentials=pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS),
        heartbeat=600,
        blocked_connection_timeout=300,
    )


def connect_rabbitmq(max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            conn    = pika.BlockingConnection(make_pika_params())
            channel = conn.channel()
            channel.queue_declare(queue=QUEUE_SALES,     durable=True)
            channel.queue_declare(queue=QUEUE_HEARTBEAT, durable=True)
            return conn, channel
        except (AMQPConnectionError, Exception) as e:
            print(f"[ERREUR] Tentative {attempt}/{max_retries} — Impossible de joindre RabbitMQ "
                  f"({CENTRAL_IP}:{RABBITMQ_PORT}): {e}")
            if attempt < max_retries:
                print("[INFO]  Retry dans 10s...")
                time.sleep(10)
    print(f"[ERREUR] Connexion échouée après {max_retries} tentatives.")
    print(f"[AIDE]  Vérifiez que CENTRAL_IP={CENTRAL_IP} est correct dans .env")
    sys.exit(1)


def heartbeat_sender(channel):
    """Envoie un heartbeat toutes les 5 secondes."""
    while True:
        try:
            hb = {
                "producer_id": PRODUCER_ID,
                "pipeline":    "rabbitmq",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "type":        "heartbeat",
            }
            channel.basic_publish(
                exchange="",
                routing_key=QUEUE_HEARTBEAT,
                body=json.dumps(hb).encode("utf-8"),
                properties=pika.BasicProperties(delivery_mode=2),
            )
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
    """
    Thread turbo RabbitMQ — chaque thread a sa propre connexion
    (RabbitMQ BlockingConnection n'est pas thread-safe).
    Envoie en mode non-persistant (delivery_mode=1) pour maximiser le débit.
    """
    try:
        conn    = pika.BlockingConnection(make_pika_params())
        channel = conn.channel()
        channel.queue_declare(queue=QUEUE_SALES, durable=True)
        print(f"[RABBIT-TURBO-{thread_id}] ✅ Connecté — envoi en cours...")
    except Exception as e:
        print(f"[RABBIT-TURBO-{thread_id}] ❌ Connexion échouée: {e}")
        return

    while True:
        try:
            event = create_event()
            channel.basic_publish(
                exchange="",
                routing_key=QUEUE_SALES,
                body=json.dumps(event).encode("utf-8"),
                # delivery_mode=1 = non-persistant (pas de fsync disque = plus rapide)
                properties=pika.BasicProperties(delivery_mode=1),
            )
            counter.add(1)
        except (AMQPConnectionError, Exception) as e:
            print(f"[RABBIT-TURBO-{thread_id}] ❌ Connexion perdue: {e}. Reconnexion...")
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(3)
            try:
                conn    = pika.BlockingConnection(make_pika_params())
                channel = conn.channel()
                channel.queue_declare(queue=QUEUE_SALES, durable=True)
            except Exception as e2:
                print(f"[RABBIT-TURBO-{thread_id}] ❌ Reconnexion échouée: {e2}")
                time.sleep(5)


def main_turbo():
    """Mode turbo — plusieurs threads, débit maximal."""
    print("=" * 60)
    print(f"[RABBIT-TURBO] 🚀 Mode TURBO activé")
    print(f"[RABBIT-TURBO] Producer ID : {PRODUCER_ID}")
    print(f"[RABBIT-TURBO] PC Central  : {CENTRAL_IP}:{RABBITMQ_PORT}")
    print(f"[RABBIT-TURBO] Threads     : {TURBO_THREADS}")
    print(f"[RABBIT-TURBO] Mode        : non-persistant (delivery_mode=1)")
    print("=" * 60)

    counter = TurboCounter()

    # Thread heartbeat
    _, hb_channel = connect_rabbitmq()
    t_hb = threading.Thread(target=heartbeat_sender, args=(hb_channel,), daemon=True)
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
        now     = time.time()
        total   = counter.count
        rate    = (total - prev) / max(now - prev_time, 0.001)
        elapsed = now - start_time
        print(f"[RABBIT-TURBO] ⚡ {total:>10,} msgs total | {rate:>8,.0f} msg/s | {elapsed:.0f}s écoulées")
        prev      = total
        prev_time = now


def main_normal():
    """Mode normal — 1 message toutes les SEND_INTERVAL_MS."""
    print("=" * 60)
    print(f"[RABBIT-PRODUCER] Démarrage")
    print(f"[RABBIT-PRODUCER] Producer ID : {PRODUCER_ID}")
    print(f"[RABBIT-PRODUCER] PC Central  : {CENTRAL_IP}:{RABBITMQ_PORT}")
    print(f"[RABBIT-PRODUCER] Intervalle  : {SEND_INTERVAL_MS}ms")
    print("=" * 60)

    connection, channel = connect_rabbitmq()
    print(f"[RABBIT-PRODUCER] ✅ Connecté à RabbitMQ sur {CENTRAL_IP}:{RABBITMQ_PORT}")

    t = threading.Thread(target=heartbeat_sender, args=(channel,), daemon=True)
    t.start()

    count = 0
    while True:
        try:
            event = create_event()
            channel.basic_publish(
                exchange="",
                routing_key=QUEUE_SALES,
                body=json.dumps(event).encode("utf-8"),
                properties=pika.BasicProperties(delivery_mode=2),
            )
            count += 1
            print(f"[RABBIT-PRODUCER] #{count} | {event['product_name']} x{event['quantity']} "
                  f"= {event['total_amount']}€ | {event['region']}")

            interval_s = SEND_INTERVAL_MS / 1000.0
            jitter     = interval_s * 0.1
            time.sleep(interval_s + random.uniform(-jitter, jitter))

        except (AMQPConnectionError, Exception) as e:
            print(f"[ERREUR] Connexion perdue: {e}. Reconnexion...")
            try:
                connection, channel = connect_rabbitmq()
            except SystemExit:
                break


if __name__ == "__main__":
    if TURBO_MODE:
        main_turbo()
    else:
        main_normal()
