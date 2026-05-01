"""
RabbitMQ Worker — Traitement des messages RabbitMQ → MongoDB
Symétrique au Flink Pipeline pour la comparaison benchmark
"""
import json
import os
import time
import threading
from datetime import datetime, timezone
from collections import defaultdict
import pika
from pika.exceptions import AMQPConnectionError
from pymongo import MongoClient, ASCENDING, DESCENDING

RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "rabbitmq")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "admin")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "password123")
MONGO_URI     = os.getenv("MONGO_URI", "mongodb://admin:password123@mongodb:27017/")
MONGO_DB      = "sales_db"
PIPELINE      = "rabbitmq"

METRICS_INTERVAL = 5   # secondes entre chaque snapshot de métriques
MAX_METRICS_DOCS = 1000
HEARTBEAT_TIMEOUT = 15  # secondes avant de considérer un producteur inactif


# ─── État partagé (thread-safe) ──────────────────────────────────────────────
class WorkerState:
    def __init__(self):
        self.lock             = threading.Lock()
        self.total_processed  = 0
        self.messages_lost    = 0
        self.latencies        = []          # latences de la fenêtre courante (5s)
        self.window_count     = 0           # messages dans la fenêtre courante
        self.window_start     = time.time()
        self.active_producers = {}          # producer_id → last_heartbeat timestamp

    def record_event(self, latency_ms):
        with self.lock:
            self.total_processed += 1
            self.window_count    += 1
            if latency_ms is not None:
                self.latencies.append(latency_ms)

    def record_loss(self):
        with self.lock:
            self.messages_lost += 1

    def record_heartbeat(self, producer_id):
        with self.lock:
            self.active_producers[producer_id] = time.time()

    def flush_window(self):
        """Retourne les stats de la fenêtre et remet à zéro."""
        with self.lock:
            elapsed      = max(time.time() - self.window_start, 0.001)
            count        = self.window_count
            latencies    = list(self.latencies)
            total        = self.total_processed
            lost         = self.messages_lost

            # Nettoyage des producteurs inactifs
            now = time.time()
            active = {pid: ts for pid, ts in self.active_producers.items()
                      if now - ts <= HEARTBEAT_TIMEOUT}
            self.active_producers = active

            # Remise à zéro de la fenêtre
            self.window_count  = 0
            self.latencies     = []
            self.window_start  = time.time()

        throughput = round(count / elapsed, 2)
        avg_lat    = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
        return {
            "pipeline":          PIPELINE,
            "timestamp":         datetime.now(timezone.utc),
            "throughput_per_sec": throughput,
            "avg_latency_ms":    avg_lat,
            "total_processed":   total,
            "messages_lost":     lost,
            "active_producers":  len(active),
        }


state = WorkerState()


# ─── Connexion MongoDB ────────────────────────────────────────────────────────
def get_db():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    # Index pour les requêtes rapides du dashboard
    db["benchmark_metrics"].create_index(
        [("pipeline", ASCENDING), ("timestamp", DESCENDING)],
        background=True
    )
    return db


# ─── Enrichissement d'un événement ───────────────────────────────────────────
def enrich_event(event):
    total = event.get("total_amount", 0)
    event["tva_amount"]   = round(total * 0.1925, 2)
    event["amount_ttc"]   = round(total * 1.1925, 2)
    event["processed_at"] = datetime.now(timezone.utc).isoformat()
    event["processor"]    = "rabbitmq-worker-v1.0"
    return event


# ─── Calcul de la latence ─────────────────────────────────────────────────────
def compute_latency(sent_at_str):
    try:
        if not sent_at_str:
            return None
        normalized = sent_at_str.replace("+00:00", "").replace("Z", "").strip()
        sent_at = datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
        now     = datetime.now(timezone.utc)
        return round(max(0.0, (now - sent_at).total_seconds() * 1000), 2)
    except Exception:
        return None


# ─── Callback : messages de ventes ───────────────────────────────────────────
def on_sales_message(ch, method, properties, body, db):
    try:
        event = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # Message malformé → dead letter queue
        db["dead_letter_queue"].insert_one({
            "raw_message": body.decode("utf-8", errors="replace"),
            "source":      PIPELINE,
            "received_at": datetime.now(timezone.utc),
            "error":       str(e),
        })
        state.record_loss()
        ch.basic_ack(delivery_tag=method.delivery_tag)
        print(f"[WORKER] ⚠️  Message malformé → dead_letter_queue")
        return

    # Calcul latence
    latency_ms = compute_latency(event.get("sent_at", ""))

    # Enrichissement
    event = enrich_event(event)
    if latency_ms is not None:
        event["latency_ms"] = round(latency_ms, 2)

    # Stockage dans rabbitmq_raw
    try:
        doc = {k: v for k, v in event.items() if k != "_id"}
        db["rabbitmq_raw"].insert_one(doc)
    except Exception as e:
        print(f"[WORKER] ❌ Erreur MongoDB insert: {e}")

    state.record_event(latency_ms)
    ch.basic_ack(delivery_tag=method.delivery_tag)

    lat_str = f"{latency_ms:.1f}ms" if latency_ms is not None else "N/A"
    print(f"[WORKER] ✅ {event.get('product_name','?')} x{event.get('quantity','?')} "
          f"= {event.get('total_amount','?')}€ | latence: {lat_str}")


# ─── Callback : heartbeats ────────────────────────────────────────────────────
def on_heartbeat(ch, method, properties, body):
    try:
        hb = json.loads(body.decode("utf-8"))
        producer_id = hb.get("producer_id", "unknown")
        state.record_heartbeat(producer_id)
    except Exception:
        pass
    ch.basic_ack(delivery_tag=method.delivery_tag)


# ─── Thread : écriture des métriques toutes les 5s ───────────────────────────
def metrics_writer(db):
    while True:
        time.sleep(METRICS_INTERVAL)
        try:
            snapshot = state.flush_window()
            db["benchmark_metrics"].insert_one(snapshot)

            # Rétention : garder seulement les 1000 derniers par pipeline
            count = db["benchmark_metrics"].count_documents({"pipeline": PIPELINE})
            if count > MAX_METRICS_DOCS:
                oldest = list(
                    db["benchmark_metrics"]
                    .find({"pipeline": PIPELINE}, {"_id": 1})
                    .sort("timestamp", ASCENDING)
                    .limit(count - MAX_METRICS_DOCS)
                )
                ids = [d["_id"] for d in oldest]
                db["benchmark_metrics"].delete_many({"_id": {"$in": ids}})

            print(f"[WORKER] 📊 Métriques: débit={snapshot['throughput_per_sec']} msg/s | "
                  f"latence={snapshot['avg_latency_ms']}ms | "
                  f"total={snapshot['total_processed']} | "
                  f"producteurs={snapshot['active_producers']}")
        except Exception as e:
            print(f"[WORKER] ❌ Erreur écriture métriques: {e}")


# ─── Connexion RabbitMQ avec backoff exponentiel ──────────────────────────────
def connect_rabbitmq():
    delays = [5, 10, 20, 40, 60]
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    params = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300,
    )
    for i, delay in enumerate(delays):
        try:
            conn = pika.BlockingConnection(params)
            print(f"[WORKER] ✅ Connecté à RabbitMQ ({RABBITMQ_HOST}:{RABBITMQ_PORT})")
            return conn
        except AMQPConnectionError as e:
            print(f"[WORKER] ⏳ Tentative {i+1}/{len(delays)} échouée: {e}. Retry dans {delay}s...")
            time.sleep(delay)
    raise RuntimeError(f"[WORKER] ❌ Impossible de se connecter à RabbitMQ après {len(delays)} tentatives.")


# ─── Pipeline principal ───────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("[WORKER] Démarrage du RabbitMQ Worker")
    print(f"[WORKER] RabbitMQ : {RABBITMQ_HOST}:{RABBITMQ_PORT}")
    print(f"[WORKER] MongoDB  : {MONGO_DB}")
    print("=" * 60)

    time.sleep(10)  # Attendre que RabbitMQ et MongoDB soient prêts

    db = get_db()
    print("[WORKER] ✅ MongoDB connecté")

    # Démarrer le thread d'écriture des métriques
    t = threading.Thread(target=metrics_writer, args=(db,), daemon=True)
    t.start()

    while True:
        try:
            connection = connect_rabbitmq()
            channel    = connection.channel()

            # Déclarer les queues durables
            channel.queue_declare(queue="sales_events",       durable=True)
            channel.queue_declare(queue="producer_heartbeats", durable=True)

            # Limiter la charge simultanée
            channel.basic_qos(prefetch_count=10)

            # Consommer les ventes
            channel.basic_consume(
                queue="sales_events",
                on_message_callback=lambda ch, m, p, b: on_sales_message(ch, m, p, b, db),
            )
            # Consommer les heartbeats
            channel.basic_consume(
                queue="producer_heartbeats",
                on_message_callback=on_heartbeat,
            )

            print("[WORKER] 🚀 En attente de messages...")
            channel.start_consuming()

        except (AMQPConnectionError, Exception) as e:
            print(f"[WORKER] ❌ Connexion perdue: {e}. Reconnexion...")
            time.sleep(5)


if __name__ == "__main__":
    main()
