"""
Job PyFlink réel — Kafka → MongoDB
Utilise l'API PyFlink DataStream avec KafkaSource officiel.

Architecture :
  KafkaSource (sales_events)
      → deserialize JSON
      → enrich (TVA, TTC, latence)
      → detect high-value alerts
      → sink MongoDB (sales_raw)
      → TumblingWindow 30s → sink MongoDB (sales_aggregated)
      → metrics snapshot toutes les 5s → sink MongoDB (benchmark_metrics)

  KafkaSource (producer_heartbeats) — thread séparé (kafka-python)
      → maintenir active_producers
"""

import json
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

# ── PyFlink imports ───────────────────────────────────────────
from pyflink.common import WatermarkStrategy, Duration, Types
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment, RuntimeExecutionMode
from pyflink.datastream.connectors.kafka import (
    KafkaSource,
    KafkaOffsetsInitializer,
)
from pyflink.datastream.functions import MapFunction, ProcessWindowFunction
from pyflink.datastream.window import TumblingProcessingTimeWindows, Time

# ── kafka-python pour les heartbeats (thread séparé) ─────────
from kafka import KafkaConsumer

# ── MongoDB ───────────────────────────────────────────────────
from pymongo import MongoClient, ASCENDING, DESCENDING

# ── Configuration ─────────────────────────────────────────────
KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092")
TOPIC_SALES       = "sales_events"
TOPIC_HEARTBEATS  = "producer_heartbeats"
MONGO_URI         = os.getenv("MONGO_URI", "mongodb://admin:password123@mongodb:27017/")
MONGO_DB          = "sales_db"
PIPELINE          = "kafka"
METRICS_INTERVAL  = 5
MAX_METRICS_DOCS  = 1000
HEARTBEAT_TIMEOUT = 15


# ─── État global partagé (métriques + heartbeats) ─────────────
class PipelineState:
    def __init__(self):
        self.lock             = threading.Lock()
        self.total_processed  = 0
        self.messages_lost    = 0
        self.latencies        = []
        self.window_count     = 0
        self.window_start     = time.time()
        self.active_producers = {}

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
        with self.lock:
            elapsed   = max(time.time() - self.window_start, 0.001)
            count     = self.window_count
            latencies = list(self.latencies)
            total     = self.total_processed
            lost      = self.messages_lost
            now       = time.time()
            active    = {pid: ts for pid, ts in self.active_producers.items()
                         if now - ts <= HEARTBEAT_TIMEOUT}
            self.active_producers = active
            self.window_count     = 0
            self.latencies        = []
            self.window_start     = time.time()

        return {
            "pipeline":           PIPELINE,
            "timestamp":          datetime.now(timezone.utc),
            "throughput_per_sec": round(count / elapsed, 2),
            "avg_latency_ms":     round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
            "total_processed":    total,
            "messages_lost":      lost,
            "active_producers":   len(active),
        }


state = PipelineState()


# ─── MongoDB helpers ──────────────────────────────────────────
def get_db():
    client = MongoClient(MONGO_URI)
    db = client[MONGO_DB]
    db["benchmark_metrics"].create_index(
        [("pipeline", ASCENDING), ("timestamp", DESCENDING)],
        background=True
    )
    return db


def compute_latency(sent_at_str):
    try:
        sent_at = datetime.fromisoformat(sent_at_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - sent_at).total_seconds() * 1000)
    except Exception:
        return None


# ─── PyFlink : MapFunction — enrichissement ───────────────────
class EnrichEvent(MapFunction):
    """
    Reçoit un message JSON string depuis Kafka,
    enrichit l'événement (TVA, TTC, latence, processor),
    retourne un JSON string enrichi.
    """

    def __init__(self, mongo_uri, mongo_db):
        self.mongo_uri = mongo_uri
        self.mongo_db  = mongo_db
        self._db       = None

    def open(self, runtime_context):
        # Connexion MongoDB ouverte une fois par worker Flink
        client   = MongoClient(self.mongo_uri)
        self._db = client[self.mongo_db]

    def map(self, value):
        # Désérialisation
        try:
            event = json.loads(value)
        except (json.JSONDecodeError, TypeError) as e:
            # Message malformé → dead letter queue
            try:
                self._db["dead_letter_queue"].insert_one({
                    "raw_message": str(value),
                    "source":      "kafka",
                    "received_at": datetime.now(timezone.utc),
                    "error":       str(e),
                })
            except Exception:
                pass
            state.record_loss()
            return None  # filtré en aval

        total      = event.get("total_amount", 0)
        latency_ms = compute_latency(event.get("sent_at", ""))

        # Enrichissement
        event["tva_amount"]   = round(total * 0.1925, 2)
        event["amount_ttc"]   = round(total * 1.1925, 2)
        event["processed_at"] = datetime.now(timezone.utc).isoformat()
        event["processor"]    = "pyflink-pipeline-v1.0"
        if latency_ms is not None:
            event["latency_ms"] = round(latency_ms, 2)

        # Stockage MongoDB
        try:
            self._db["sales_raw"].insert_one({k: v for k, v in event.items() if k != "_id"})
        except Exception as ex:
            print(f"[PYFLINK] ❌ MongoDB insert error: {ex}")

        # Alerte grosse transaction
        if total > 500:
            try:
                self._db["high_value_alerts"].insert_one({
                    "alert_type":  "HIGH_VALUE_TRANSACTION",
                    "event_id":    event.get("event_id"),
                    "amount":      total,
                    "amount_ttc":  event.get("amount_ttc"),
                    "product":     event.get("product_name"),
                    "category":    event.get("category"),
                    "customer":    event.get("customer_name"),
                    "region":      event.get("region"),
                    "payment":     event.get("payment_method"),
                    "timestamp":   event.get("timestamp"),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass
            print(f"[PYFLINK] 🚨 ALERTE: {total}€ — {event.get('product_name')} — {event.get('region')}")

        state.record_event(latency_ms)
        lat_str = f"{latency_ms:.1f}ms" if latency_ms is not None else "N/A"
        print(f"[PYFLINK] ✅ {event.get('product_name','?')} x{event.get('quantity','?')} "
              f"= {total}€ | latence: {lat_str}")

        return json.dumps(event)


# ─── PyFlink : ProcessWindowFunction — agrégation 30s ─────────
class AggregateWindow(ProcessWindowFunction):
    """
    Agrège les événements d'une fenêtre Tumbling de 30s.
    Stocke le résultat dans MongoDB (sales_aggregated).
    """

    def __init__(self, mongo_uri, mongo_db):
        self.mongo_uri = mongo_uri
        self.mongo_db  = mongo_db
        self._db       = None

    def open(self, runtime_context):
        client   = MongoClient(self.mongo_uri)
        self._db = client[self.mongo_db]

    def process(self, key, context, elements):
        by_category   = defaultdict(lambda: {"total": 0, "count": 0})
        by_region     = defaultdict(lambda: {"total": 0, "count": 0})
        total_revenue = 0
        count         = 0

        for elem in elements:
            if elem is None:
                continue
            try:
                e   = json.loads(elem) if isinstance(elem, str) else elem
                cat = e.get("category", "Inconnu")
                reg = e.get("region",   "Inconnu")
                amt = e.get("total_amount", 0)
                by_category[cat]["total"] += amt
                by_category[cat]["count"] += 1
                by_region[reg]["total"]   += amt
                by_region[reg]["count"]   += 1
                total_revenue             += amt
                count                     += 1
            except Exception:
                continue

        if count == 0:
            return

        aggregat = {
            "window_start":  datetime.utcnow().isoformat() + "Z",
            "window_end":    datetime.utcnow().isoformat() + "Z",
            "window_size_s": 30,
            "total_events":  count,
            "total_revenue": round(total_revenue, 2),
            "by_category": [
                {"category": k, "revenue": round(v["total"], 2), "count": v["count"]}
                for k, v in sorted(by_category.items(), key=lambda x: -x[1]["total"])
            ],
            "by_region": [
                {"region": k, "revenue": round(v["total"], 2), "count": v["count"]}
                for k, v in sorted(by_region.items(), key=lambda x: -x[1]["total"])
            ],
            "computed_at": datetime.utcnow().isoformat() + "Z",
        }

        try:
            self._db["sales_aggregated"].insert_one(aggregat)
        except Exception as e:
            print(f"[PYFLINK] ❌ Erreur agrégation: {e}")

        print(f"\n[PYFLINK] 📊 FENÊTRE 30s: {count} événements | CA={round(total_revenue,2)}€")
        yield json.dumps({"window_closed": True, "events": count})


# ─── Thread : métriques toutes les 5s ─────────────────────────
def metrics_writer(db):
    while True:
        time.sleep(METRICS_INTERVAL)
        try:
            snapshot = state.flush_window()
            db["benchmark_metrics"].insert_one(snapshot)

            count = db["benchmark_metrics"].count_documents({"pipeline": PIPELINE})
            if count > MAX_METRICS_DOCS:
                oldest = list(
                    db["benchmark_metrics"]
                    .find({"pipeline": PIPELINE}, {"_id": 1})
                    .sort("timestamp", ASCENDING)
                    .limit(count - MAX_METRICS_DOCS)
                )
                db["benchmark_metrics"].delete_many(
                    {"_id": {"$in": [d["_id"] for d in oldest]}}
                )

            print(f"[PYFLINK] 📈 Métriques: débit={snapshot['throughput_per_sec']} msg/s | "
                  f"latence={snapshot['avg_latency_ms']}ms | "
                  f"total={snapshot['total_processed']} | "
                  f"producteurs={snapshot['active_producers']}")
        except Exception as e:
            print(f"[PYFLINK] ❌ Erreur métriques: {e}")


# ─── Thread : heartbeat consumer (kafka-python) ───────────────
def heartbeat_consumer():
    time.sleep(20)
    while True:
        try:
            consumer = KafkaConsumer(
                TOPIC_HEARTBEATS,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="pyflink-heartbeat-group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                auto_offset_reset="latest",
                enable_auto_commit=True,
                consumer_timeout_ms=-1,
            )
            print("[PYFLINK] ✅ Heartbeat consumer connecté")
            for msg in consumer:
                try:
                    state.record_heartbeat(msg.value.get("producer_id", "unknown"))
                except Exception:
                    pass
        except Exception as e:
            print(f"[PYFLINK] ❌ Heartbeat erreur: {e}. Retry dans 10s...")
            time.sleep(10)


# ─── Point d'entrée PyFlink ───────────────────────────────────
def main():
    print("=" * 60)
    print("[PYFLINK] Démarrage du Job PyFlink réel")
    print(f"[PYFLINK] Kafka  : {KAFKA_BOOTSTRAP}")
    print(f"[PYFLINK] Topic  : {TOPIC_SALES}")
    print(f"[PYFLINK] MongoDB: {MONGO_DB}")
    print("=" * 60)

    time.sleep(20)  # Attendre Kafka + MongoDB

    db = get_db()
    print("[PYFLINK] ✅ MongoDB connecté")

    # Threads auxiliaires
    threading.Thread(target=metrics_writer,    args=(db,), daemon=True).start()
    threading.Thread(target=heartbeat_consumer,             daemon=True).start()

    # ── Environnement Flink ───────────────────────────────────
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_runtime_mode(RuntimeExecutionMode.STREAMING)
    env.set_parallelism(2)

    # ── Source Kafka ──────────────────────────────────────────
    kafka_source = (
        KafkaSource.builder()
        .set_bootstrap_servers(KAFKA_BOOTSTRAP)
        .set_topics(TOPIC_SALES)
        .set_group_id("pyflink-sales-group")
        .set_starting_offsets(KafkaOffsetsInitializer.latest())
        .set_value_only_deserializer(SimpleStringSchema())
        .build()
    )

    # ── DataStream pipeline ───────────────────────────────────
    stream = env.from_source(
        kafka_source,
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(5)),
        "Kafka Sales Source",
    )

    # Enrichissement + stockage MongoDB (MapFunction)
    enriched = stream.map(
        EnrichEvent(MONGO_URI, MONGO_DB),
        output_type=Types.STRING(),
    ).filter(lambda x: x is not None)

    # Agrégation Tumbling Window 30s
    enriched \
        .key_by(lambda x: "all") \
        .window(TumblingProcessingTimeWindows.of(Time.seconds(30))) \
        .process(
            AggregateWindow(MONGO_URI, MONGO_DB),
            output_type=Types.STRING(),
        )

    print("[PYFLINK] 🚀 Soumission du job au cluster Flink...")
    env.execute("Kafka-Flink Sales Pipeline — Benchmark")


if __name__ == "__main__":
    main()
