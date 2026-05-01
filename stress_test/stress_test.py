"""
Stress Test — Kafka vs RabbitMQ
Injecte massivement des messages dans les deux brokers en parallèle
pour démontrer la supériorité de Kafka en débit brut.

Usage :
    pip install kafka-python-ng pika python-dotenv
    python stress_test.py

Le script se connecte en localhost (à lancer sur le PC central).
"""

import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# ─── Chargement config ────────────────────────────────────────
ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_PATH)

CENTRAL_IP      = os.getenv("CENTRAL_IP", "127.0.0.1")
KAFKA_BOOTSTRAP = f"{CENTRAL_IP}:9092"
RABBIT_HOST     = CENTRAL_IP
RABBIT_PORT     = 5672
RABBIT_USER     = "admin"
RABBIT_PASS     = "password123"

# ─── Paramètres du test ───────────────────────────────────────
DURATION_SECONDS   = 60      # durée totale du test
KAFKA_THREADS      = 8       # threads producteurs Kafka
RABBIT_THREADS     = 4       # threads producteurs RabbitMQ (limité par BlockingConnection)
REPORT_INTERVAL    = 5       # affichage des stats toutes les N secondes

# ─── Données de test (légères pour maximiser le débit) ────────
PRODUCTS = [
    ("P001", "Laptop Dell XPS",  1200.0),
    ("P002", "iPhone 15 Pro",    1100.0),
    ("P003", "Samsung TV 55",     800.0),
    ("P004", "Nike Air Max",      150.0),
    ("P005", "Livre Python",       35.0),
    ("P006", "Cafe Arabica 1kg",   25.0),
    ("P007", "Casque Sony WH",    350.0),
    ("P008", "Montre Garmin",     450.0),
]
REGIONS  = ["Yaounde", "Douala", "Bafoussam", "Garoua", "Bamenda"]
PAYMENTS = ["carte_bancaire", "mobile_money", "especes", "virement"]


def make_event(pipeline: str) -> bytes:
    pid, name, price = random.choice(PRODUCTS)
    qty   = random.randint(1, 5)
    total = round(price * qty * random.uniform(0.85, 1.15), 2)
    now   = datetime.now(timezone.utc).isoformat()
    return json.dumps({
        "event_id":       str(uuid.uuid4()),
        "pipeline":       pipeline,
        "sent_at":        now,
        "product_id":     pid,
        "product_name":   name,
        "quantity":       qty,
        "total_amount":   total,
        "region":         random.choice(REGIONS),
        "payment_method": random.choice(PAYMENTS),
    }).encode("utf-8")


# ─── Compteurs thread-safe ────────────────────────────────────
class Counter:
    def __init__(self):
        self._lock  = threading.Lock()
        self._count = 0
        self._errors = 0

    def add(self, n=1):
        with self._lock:
            self._count += n

    def error(self):
        with self._lock:
            self._errors += 1

    @property
    def count(self):
        with self._lock:
            return self._count

    @property
    def errors(self):
        with self._lock:
            return self._errors


kafka_counter  = Counter()
rabbit_counter = Counter()
stop_event     = threading.Event()


# ─── Thread Kafka ─────────────────────────────────────────────
def kafka_worker(thread_id: int):
    """
    Producteur Kafka optimisé pour le débit maximal :
    - batch_size=65536  : accumule jusqu'à 64KB avant d'envoyer
    - linger_ms=5       : attend 5ms pour remplir le batch
    - compression=lz4   : compresse les batches
    - acks=1            : confirmation du leader seulement (pas tous les replicas)
    """
    try:
        from kafka import KafkaProducer
        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP,
            batch_size=65536,          # 64KB par batch
            linger_ms=5,               # attend 5ms pour remplir le batch
            compression_type="lz4",    # compression rapide
            acks=1,                    # leader ack seulement
            retries=0,                 # pas de retry pour maximiser le débit
            buffer_memory=67108864,    # 64MB buffer
        )
        print(f"[KAFKA-{thread_id}] ✅ Connecté à {KAFKA_BOOTSTRAP}")
    except Exception as e:
        print(f"[KAFKA-{thread_id}] ❌ Connexion échouée: {e}")
        return

    local_count = 0
    while not stop_event.is_set():
        try:
            msg = make_event("kafka")
            producer.send("stress_test_kafka", value=msg)
            local_count += 1
            # Flush par batch de 1000 pour ne pas bloquer
            if local_count % 1000 == 0:
                producer.flush()
                kafka_counter.add(1000)
        except Exception as e:
            kafka_counter.error()

    # Flush final
    try:
        producer.flush()
        remainder = local_count % 1000
        if remainder:
            kafka_counter.add(remainder)
        producer.close()
    except Exception:
        pass

    print(f"[KAFKA-{thread_id}] Arrêté — {local_count:,} messages envoyés")


# ─── Thread RabbitMQ ──────────────────────────────────────────
def rabbit_worker(thread_id: int):
    """
    Producteur RabbitMQ standard.
    RabbitMQ utilise des connexions bloquantes — chaque thread a sa propre
    connexion. Pas de batching natif, chaque message est envoyé individuellement.
    """
    try:
        import pika
        credentials = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
        params = pika.ConnectionParameters(
            host=RABBIT_HOST,
            port=RABBIT_PORT,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300,
        )
        conn    = pika.BlockingConnection(params)
        channel = conn.channel()
        channel.queue_declare(queue="stress_test_rabbit", durable=False)
        print(f"[RABBIT-{thread_id}] ✅ Connecté à {RABBIT_HOST}:{RABBIT_PORT}")
    except Exception as e:
        print(f"[RABBIT-{thread_id}] ❌ Connexion échouée: {e}")
        return

    local_count = 0
    while not stop_event.is_set():
        try:
            msg = make_event("rabbitmq")
            channel.basic_publish(
                exchange="",
                routing_key="stress_test_rabbit",
                body=msg,
                # delivery_mode=1 = non-persistant (plus rapide, pas de fsync disque)
                properties=pika.BasicProperties(delivery_mode=1),
            )
            local_count += 1
            rabbit_counter.add(1)
        except Exception as e:
            rabbit_counter.error()
            try:
                conn.close()
            except Exception:
                pass
            break

    try:
        conn.close()
    except Exception:
        pass

    print(f"[RABBIT-{thread_id}] Arrêté — {local_count:,} messages envoyés")


# ─── Thread reporter ──────────────────────────────────────────
def reporter():
    start      = time.time()
    prev_kafka  = 0
    prev_rabbit = 0
    prev_time   = start

    while not stop_event.is_set():
        time.sleep(REPORT_INTERVAL)
        now         = time.time()
        elapsed     = now - start
        interval    = now - prev_time

        k_total = kafka_counter.count
        r_total = rabbit_counter.count
        k_rate  = (k_total - prev_kafka)  / interval
        r_rate  = (r_total - prev_rabbit) / interval

        bar_k = "█" * min(int(k_rate / 500), 40)
        bar_r = "█" * min(int(r_rate / 500), 40)

        print(f"\n{'─'*60}")
        print(f"⏱  Temps écoulé : {elapsed:.0f}s / {DURATION_SECONDS}s")
        print(f"🔴 Kafka    : {k_total:>10,} msgs total | {k_rate:>8,.0f} msg/s  {bar_k}")
        print(f"🟡 RabbitMQ : {r_total:>10,} msgs total | {r_rate:>8,.0f} msg/s  {bar_r}")
        if r_rate > 0:
            ratio = k_rate / r_rate
            print(f"📊 Kafka est {ratio:.1f}x {'plus rapide' if ratio >= 1 else 'plus lent'} que RabbitMQ")
        print(f"{'─'*60}")

        prev_kafka  = k_total
        prev_rabbit = r_total
        prev_time   = now


# ─── Main ─────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  STRESS TEST — Kafka vs RabbitMQ")
    print(f"  PC Central   : {CENTRAL_IP}")
    print(f"  Durée        : {DURATION_SECONDS}s")
    print(f"  Threads Kafka: {KAFKA_THREADS}")
    print(f"  Threads Rabbit: {RABBIT_THREADS}")
    print("=" * 60)
    print()

    threads = []

    # Lancer les threads Kafka
    for i in range(KAFKA_THREADS):
        t = threading.Thread(target=kafka_worker, args=(i+1,), daemon=True)
        t.start()
        threads.append(t)

    # Lancer les threads RabbitMQ
    for i in range(RABBIT_THREADS):
        t = threading.Thread(target=rabbit_worker, args=(i+1,), daemon=True)
        t.start()
        threads.append(t)

    # Lancer le reporter
    t_report = threading.Thread(target=reporter, daemon=True)
    t_report.start()

    # Attendre la durée du test
    print(f"\n🚀 Test démarré — durée {DURATION_SECONDS}s...\n")
    time.sleep(DURATION_SECONDS)
    stop_event.set()

    # Attendre que tous les threads finissent (max 10s)
    for t in threads:
        t.join(timeout=10)

    # ─── Résultats finaux ─────────────────────────────────────
    k_total = kafka_counter.count
    r_total = rabbit_counter.count
    k_rate  = k_total / DURATION_SECONDS
    r_rate  = r_total / DURATION_SECONDS
    ratio   = k_rate / r_rate if r_rate > 0 else float("inf")

    print("\n")
    print("═" * 60)
    print("  RÉSULTATS FINAUX")
    print("═" * 60)
    print(f"  Durée du test  : {DURATION_SECONDS} secondes")
    print(f"  Threads Kafka  : {KAFKA_THREADS} | Threads RabbitMQ : {RABBIT_THREADS}")
    print()
    print(f"  🔴 Kafka/Flink")
    print(f"     Total envoyé : {k_total:>12,} messages")
    print(f"     Débit moyen  : {k_rate:>12,.0f} msg/s")
    print(f"     Erreurs      : {kafka_counter.errors:>12,}")
    print()
    print(f"  🟡 RabbitMQ")
    print(f"     Total envoyé : {r_total:>12,} messages")
    print(f"     Débit moyen  : {r_rate:>12,.0f} msg/s")
    print(f"     Erreurs      : {rabbit_counter.errors:>12,}")
    print()
    print("─" * 60)
    if ratio >= 1:
        print(f"  🏆 Kafka est {ratio:.1f}x PLUS RAPIDE que RabbitMQ")
        print(f"     Kafka a traité {k_total - r_total:,} messages de plus")
    else:
        print(f"  🏆 RabbitMQ est {1/ratio:.1f}x plus rapide que Kafka")
    print("═" * 60)

    # Explication pédagogique
    print()
    print("  POURQUOI CET ÉCART ?")
    print("  ─────────────────────────────────────────────────────")
    print("  Kafka utilise le batching : les messages sont accumulés")
    print("  et envoyés en gros paquets compressés (lz4).")
    print("  RabbitMQ envoie chaque message individuellement via")
    print("  AMQP — chaque publish est un aller-retour réseau.")
    print()
    print("  En production, Kafka peut dépasser 1 million msg/s")
    print("  sur un cluster multi-brokers. RabbitMQ plafonne")
    print("  généralement autour de 50 000 msg/s.")
    print("═" * 60)


if __name__ == "__main__":
    main()
