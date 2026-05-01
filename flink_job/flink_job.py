"""
Job PyFlink réel — Kafka → MongoDB
Utilise l'API PyFlink DataStream avec KafkaSource officiel.
Les métriques sont calculées directement depuis MongoDB (compatible multi-process PyFlink).
"""
import json
import os
import time
import threading
from collections import defaultdict
from datetime import datetime, timezone

from pyflink.common import WatermarkStrategy, Duration, Types
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment, RuntimeExecutionMode
from pyflink.datastream.connectors.kafka import KafkaSource, KafkaOffsetsInitializer
from pyflink.datastream.functions import MapFunction, ProcessWindowFunction
from pyflink.datastream.window import TumblingProcessingTimeWindows, Time

from kafka import KafkaConsumer
from pymongo import MongoClient, ASCENDING, DESCENDING

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092")
TOPIC_SALES       = "sales_events"
TOPIC_HEARTBEATS  = "producer_heartbeats"
MONGO_URI         = os.getenv("MONGO_URI", "mongodb://admin:password123@mongodb:27017/")
MONGO_DB          = "sales_db"
PIPELINE          = "kafka"
METRICS_INTERVAL  = 5
MAX_METRICS_DOCS  = 1000
HEARTBEAT_TIMEOUT = 15


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
        if not sent_at_str:
            return None
        # Normalise le format ISO : retire +00:00 ou Z pour compatibilité Python 3.9/3.10
        normalized = sent_at_str.replace("+00:00", "").replace("Z", "").strip()
        sent_at = datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
        return round(max(0.0, (datetime.now(timezone.utc) - sent_at).total_seconds() * 1000), 2)
    except Exception:
        return None


# ─── PyFlink MapFunction ──────────────────────────────────────
# Tourne dans le TaskManager (processus séparé du main).
# Écrit chaque événement dans MongoDB et insère un doc de métriques par événement.
class EnrichEvent(MapFunction):
    def __init__(self, mongo_uri, mongo_db):
        self.mongo_uri = mongo_uri
        self.mongo_db  = mongo_db
        self._db       = None

    def open(self, runtime_context):
        self._db = MongoClient(self.mongo_uri)[self.mongo_db]

    def map(self, value):
        try:
            event = json.loads(value)
        except Exception as e:
            try:
                self._db["dead_letter_queue"].insert_one({
                    "raw_message": str(value),
                    "source":      "kafka",
                    "received_at": datetime.now(timezone.utc),
                    "error":       str(e),
                })
            except Exception:
                pass
            return None

        total      = event.get("total_amount", 0)
        latency_ms = compute_latency(event.get("sent_at", ""))

        event["tva_amount"]   = round(total * 0.1925, 2)
        event["amount_ttc"]   = round(total * 1.1925, 2)
        event["processed_at"] = datetime.now(timezone.utc).isoformat()
        event["processor"]    = "pyflink-pipeline-v1.0"
        if latency_ms is not None:
            event["latency_ms"] = latency_ms

        try:
            self._db["sales_raw"].insert_one({k: v for k, v in event.items() if k != "_id"})
        except Exception as ex:
            print(f"[PYFLINK] ❌ MongoDB insert: {ex}")

        if total > 500:
            try:
                self._db["high_value_alerts"].insert_one({
                    "alert_type":  "HIGH_VALUE_TRANSACTION",
                    "amount":      total,
                    "product":     event.get("product_name"),
                    "region":      event.get("region"),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass
            print(f"[PYFLINK] 🚨 ALERTE: {total}€ — {event.get('product_name')}")

        lat_str = f"{latency_ms:.1f}ms" if latency_ms is not None else "N/A"
        print(f"[PYFLINK] ✅ {event.get('product_name','?')} x{event.get('quantity','?')} "
              f"= {total}€ | latence: {lat_str}")
        return json.dumps(event)


# ─── PyFlink ProcessWindowFunction ───────────────────────────
class AggregateWindow(ProcessWindowFunction):
    def __init__(self, mongo_uri, mongo_db):
        self.mongo_uri = mongo_uri
        self.mongo_db  = mongo_db
        self._db       = None

    def open(self, runtime_context):
        self._db = MongoClient(self.mongo_uri)[self.mongo_db]

    def process(self, key, context, elements):
        by_cat  = defaultdict(lambda: {"total": 0, "count": 0})
        by_reg  = defaultdict(lambda: {"total": 0, "count": 0})
        revenue = 0
        count   = 0
        for elem in elements:
            if not elem:
                continue
            try:
                e   = json.loads(elem) if isinstance(elem, str) else elem
                cat = e.get("category", "Inconnu")
                reg = e.get("region",   "Inconnu")
                amt = e.get("total_amount", 0)
                by_cat[cat]["total"] += amt
                by_cat[cat]["count"] += 1
                by_reg[reg]["total"] += amt
                by_reg[reg]["count"] += 1
                revenue              += amt
                count                += 1
            except Exception:
                continue

        if count == 0:
            return

        try:
            self._db["sales_aggregated"].insert_one({
                "window_end":    datetime.utcnow().isoformat() + "Z",
                "window_size_s": 30,
                "total_events":  count,
                "total_revenue": round(revenue, 2),
                "by_category":   [{"category": k, "revenue": round(v["total"], 2), "count": v["count"]}
                                   for k, v in sorted(by_cat.items(), key=lambda x: -x[1]["total"])],
                "by_region":     [{"region": k, "revenue": round(v["total"], 2), "count": v["count"]}
                                   for k, v in sorted(by_reg.items(), key=lambda x: -x[1]["total"])],
            })
        except Exception as e:
            print(f"[PYFLINK] ❌ Agrégation: {e}")

        print(f"\n[PYFLINK] 📊 FENÊTRE 30s: {count} événements | CA={round(revenue,2)}€")
        yield json.dumps({"window_closed": True, "events": count})


# ─── Thread heartbeats (Kafka consumer natif) ─────────────────
# Stocke les heartbeats dans MongoDB pour que le metrics_writer puisse les lire.
def heartbeat_consumer(db):
    time.sleep(20)
    while True:
        try:
            c = KafkaConsumer(
                TOPIC_HEARTBEATS,
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="pyflink-heartbeat-group",
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                auto_offset_reset="latest",
                enable_auto_commit=True,
                consumer_timeout_ms=-1,
            )
            print("[PYFLINK] ✅ Heartbeat consumer connecté")
            for msg in c:
                try:
                    producer_id = msg.value.get("producer_id", "unknown")
                    # Upsert du heartbeat dans MongoDB (source de vérité partagée)
                    db["producer_heartbeats"].update_one(
                        {"producer_id": producer_id, "pipeline": "kafka"},
                        {"$set": {
                            "producer_id": producer_id,
                            "pipeline":    "kafka",
                            "last_seen":   datetime.now(timezone.utc),
                        }},
                        upsert=True,
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[PYFLINK] ❌ Heartbeat: {e}. Retry 10s...")
            time.sleep(10)


# ─── Thread métriques ─────────────────────────────────────────
# Calcule les métriques depuis MongoDB directement — fonctionne même
# si la MapFunction tourne dans un processus TaskManager séparé.
def metrics_writer(db):
    # Fenêtre glissante : on garde le timestamp du dernier snapshot
    last_snapshot_time = datetime.now(timezone.utc)
    last_total         = db["sales_raw"].count_documents({"pipeline": "kafka"}) if "sales_raw" in db.list_collection_names() else 0

    while True:
        time.sleep(METRICS_INTERVAL)
        try:
            now = datetime.now(timezone.utc)

            # Total traités = nombre de docs dans sales_raw avec pipeline=kafka
            total_processed = db["sales_raw"].count_documents({"processor": "pyflink-pipeline-v1.0"})

            # Débit = nouveaux docs depuis le dernier snapshot
            new_in_window   = total_processed - last_total
            elapsed_s       = max((now - last_snapshot_time).total_seconds(), 0.001)
            throughput      = round(new_in_window / elapsed_s, 2)

            # Latence moyenne sur les derniers docs traités
            recent_docs = list(
                db["sales_raw"]
                .find(
                    {"processor": "pyflink-pipeline-v1.0", "latency_ms": {"$exists": True}},
                    {"latency_ms": 1}
                )
                .sort("processed_at", DESCENDING)
                .limit(50)
            )
            latencies   = [d["latency_ms"] for d in recent_docs if d.get("latency_ms") is not None]
            avg_latency = round(sum(latencies) / len(latencies), 2) if latencies else 0.0

            # Producteurs actifs via MongoDB heartbeats
            cutoff = datetime.now(timezone.utc).timestamp() - HEARTBEAT_TIMEOUT
            from datetime import timezone as tz
            active_producers = db["producer_heartbeats"].count_documents({
                "pipeline": "kafka",
                "last_seen": {"$gte": datetime.fromtimestamp(cutoff, tz=timezone.utc)},
            })

            snap = {
                "pipeline":           PIPELINE,
                "timestamp":          now,
                "throughput_per_sec": throughput,
                "avg_latency_ms":     avg_latency,
                "total_processed":    total_processed,
                "messages_lost":      0,
                "active_producers":   active_producers,
            }
            db["benchmark_metrics"].insert_one(snap)

            # Rétention
            count = db["benchmark_metrics"].count_documents({"pipeline": PIPELINE})
            if count > MAX_METRICS_DOCS:
                oldest = list(db["benchmark_metrics"]
                              .find({"pipeline": PIPELINE}, {"_id": 1})
                              .sort("timestamp", ASCENDING)
                              .limit(count - MAX_METRICS_DOCS))
                db["benchmark_metrics"].delete_many({"_id": {"$in": [d["_id"] for d in oldest]}})

            print(f"[PYFLINK] 📈 débit={throughput} msg/s | "
                  f"latence={avg_latency}ms | "
                  f"total={total_processed} | "
                  f"producteurs={active_producers}")

            last_total         = total_processed
            last_snapshot_time = now

        except Exception as e:
            print(f"[PYFLINK] ❌ Métriques: {e}")


# ─── Main ─────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("[PYFLINK] Démarrage du Job PyFlink réel")
    print(f"[PYFLINK] Kafka  : {KAFKA_BOOTSTRAP}")
    print(f"[PYFLINK] MongoDB: {MONGO_DB}")
    print("=" * 60)

    time.sleep(20)
    db = get_db()
    print("[PYFLINK] ✅ MongoDB connecté")

    threading.Thread(target=metrics_writer,          args=(db,), daemon=True).start()
    threading.Thread(target=heartbeat_consumer,      args=(db,), daemon=True).start()

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_runtime_mode(RuntimeExecutionMode.STREAMING)
    env.set_parallelism(2)

    kafka_source = (
        KafkaSource.builder()
        .set_bootstrap_servers(KAFKA_BOOTSTRAP)
        .set_topics(TOPIC_SALES)
        .set_group_id("pyflink-sales-group")
        .set_starting_offsets(KafkaOffsetsInitializer.earliest())
        .set_value_only_deserializer(SimpleStringSchema())
        .set_property("metadata.max.age.ms", "30000")
        .set_property("request.timeout.ms", "30000")
        .set_property("socket.connection.setup.timeout.ms", "10000")
        .set_property("client.dns.lookup", "use_all_dns_ips")
        .set_property("security.protocol", "PLAINTEXT")
        .build()
    )

    stream = env.from_source(
        kafka_source,
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(5)),
        "Kafka Sales Source",
    )

    enriched = stream.map(
        EnrichEvent(MONGO_URI, MONGO_DB),
        output_type=Types.STRING(),
    ).filter(lambda x: x is not None)

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
