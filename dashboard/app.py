"""
Dashboard Flask — Benchmark Kafka/Flink vs RabbitMQ
API REST + interface web temps réel
"""
import os
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template, request
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

MONGO_URI = os.getenv("MONGO_URI", "mongodb://admin:password123@mongodb:27017/")
MONGO_DB  = "sales_db"

app    = Flask(__name__)
client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

# Créer l'index au démarrage
db["benchmark_metrics"].create_index(
    [("pipeline", ASCENDING), ("timestamp", DESCENDING)],
    background=True
)

VALID_PIPELINES = {"kafka", "rabbitmq"}


# ─── Helpers ─────────────────────────────────────────────────────────────────
def serialize_doc(doc):
    """Convertit un document MongoDB en dict JSON-sérialisable."""
    if doc is None:
        return None
    d = {k: v for k, v in doc.items() if k != "_id"}
    if "timestamp" in d and isinstance(d["timestamp"], datetime):
        d["timestamp"] = d["timestamp"].isoformat()
    return d


def empty_metrics(pipeline):
    return {
        "pipeline":           pipeline,
        "timestamp":          None,
        "throughput_per_sec": 0,
        "avg_latency_ms":     0,
        "total_processed":    0,
        "messages_lost":      0,
        "active_producers":   0,
    }


# ─── Verdict Engine ───────────────────────────────────────────────────────────
def compute_verdict():
    """Compare les 60 derniers points de chaque pipeline et retourne un verdict."""
    try:
        kafka_docs = list(
            db["benchmark_metrics"]
            .find({"pipeline": "kafka"})
            .sort("timestamp", DESCENDING)
            .limit(60)
        )
        rabbit_docs = list(
            db["benchmark_metrics"]
            .find({"pipeline": "rabbitmq"})
            .sort("timestamp", DESCENDING)
            .limit(60)
        )

        kafka_total   = kafka_docs[0].get("total_processed", 0) if kafka_docs else 0
        rabbit_total  = rabbit_docs[0].get("total_processed", 0) if rabbit_docs else 0

        if kafka_total < 100 or rabbit_total < 100:
            return {"sufficient_data": False,
                    "message": f"Données insuffisantes (Kafka: {kafka_total}, RabbitMQ: {rabbit_total} messages). Minimum 100 requis."}

        def avg(docs, field):
            vals = [d.get(field, 0) for d in docs if d.get(field) is not None]
            return sum(vals) / len(vals) if vals else 0

        k_throughput = avg(kafka_docs,  "throughput_per_sec")
        r_throughput = avg(rabbit_docs, "throughput_per_sec")
        k_latency    = avg(kafka_docs,  "avg_latency_ms")
        r_latency    = avg(rabbit_docs, "avg_latency_ms")
        k_lost       = avg(kafka_docs,  "messages_lost")
        r_lost       = avg(rabbit_docs, "messages_lost")

        kafka_score   = 0
        rabbit_score  = 0
        details       = {}

        # Débit
        if k_throughput > r_throughput:
            kafka_score += 1
            ratio = round(k_throughput / r_throughput, 1) if r_throughput > 0 else "∞"
            details["throughput"] = {
                "winner": "kafka",
                "kafka_avg": round(k_throughput, 2),
                "rabbitmq_avg": round(r_throughput, 2),
                "label": f"Kafka {ratio}× plus rapide",
            }
        elif r_throughput > k_throughput:
            rabbit_score += 1
            ratio = round(r_throughput / k_throughput, 1) if k_throughput > 0 else "∞"
            details["throughput"] = {
                "winner": "rabbitmq",
                "kafka_avg": round(k_throughput, 2),
                "rabbitmq_avg": round(r_throughput, 2),
                "label": f"RabbitMQ {ratio}× plus rapide",
            }
        else:
            details["throughput"] = {"winner": None, "kafka_avg": round(k_throughput, 2),
                                     "rabbitmq_avg": round(r_throughput, 2), "label": "Égalité"}

        # Latence (plus bas = mieux)
        if k_latency < r_latency:
            kafka_score += 1
            diff = round(r_latency - k_latency, 1)
            details["latency"] = {
                "winner": "kafka",
                "kafka_avg_ms": round(k_latency, 2),
                "rabbitmq_avg_ms": round(r_latency, 2),
                "label": f"Kafka {diff}ms plus rapide",
            }
        elif r_latency < k_latency:
            rabbit_score += 1
            diff = round(k_latency - r_latency, 1)
            details["latency"] = {
                "winner": "rabbitmq",
                "kafka_avg_ms": round(k_latency, 2),
                "rabbitmq_avg_ms": round(r_latency, 2),
                "label": f"RabbitMQ {diff}ms plus rapide",
            }
        else:
            details["latency"] = {"winner": None, "kafka_avg_ms": round(k_latency, 2),
                                   "rabbitmq_avg_ms": round(r_latency, 2), "label": "Égalité"}

        # Fiabilité (moins de pertes = mieux)
        if k_lost < r_lost:
            kafka_score += 1
            details["reliability"] = {
                "winner": "kafka",
                "kafka_lost": round(k_lost, 2),
                "rabbitmq_lost": round(r_lost, 2),
                "label": f"Kafka {round(r_lost - k_lost, 1)} pertes de moins",
            }
        elif r_lost < k_lost:
            rabbit_score += 1
            details["reliability"] = {
                "winner": "rabbitmq",
                "kafka_lost": round(k_lost, 2),
                "rabbitmq_lost": round(r_lost, 2),
                "label": f"RabbitMQ {round(k_lost - r_lost, 1)} pertes de moins",
            }
        else:
            details["reliability"] = {"winner": None, "kafka_lost": round(k_lost, 2),
                                       "rabbitmq_lost": round(r_lost, 2), "label": "Égalité"}

        # Résumé textuel
        winner_name = "Kafka" if kafka_score > rabbit_score else (
            "RabbitMQ" if rabbit_score > kafka_score else "Égalité"
        )
        parts = []
        if details["throughput"]["winner"] == "kafka":
            parts.append(details["throughput"]["label"])
        if details["latency"]["winner"] == "kafka":
            parts.append(details["latency"]["label"])
        if details["reliability"]["winner"] == "kafka":
            parts.append(details["reliability"]["label"])

        summary = f"{winner_name} remporte {kafka_score}/3 métriques."
        if parts:
            summary += " " + " | ".join(parts)

        return {
            "sufficient_data": True,
            "kafka_score":     kafka_score,
            "rabbitmq_score":  rabbit_score,
            "details":         details,
            "summary":         summary,
        }
    except PyMongoError:
        return {"sufficient_data": False, "message": "Erreur base de données"}


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/metrics")
def api_metrics():
    try:
        result = {}
        for pipeline in VALID_PIPELINES:
            doc = db["benchmark_metrics"].find_one(
                {"pipeline": pipeline},
                sort=[("timestamp", DESCENDING)]
            )
            result[pipeline] = serialize_doc(doc) if doc else empty_metrics(pipeline)
        return jsonify(result)
    except PyMongoError:
        return jsonify({"error": "Database error"}), 500


@app.route("/api/metrics/history")
def api_metrics_history():
    pipeline = request.args.get("pipeline", "")
    limit_str = request.args.get("limit", "60")

    if pipeline not in VALID_PIPELINES:
        return jsonify({"error": f"Paramètre 'pipeline' invalide. Valeurs acceptées: {sorted(VALID_PIPELINES)}"}), 400

    try:
        limit = int(limit_str)
        if limit <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "Paramètre 'limit' doit être un entier positif"}), 400

    limit = min(limit, 1000)

    try:
        docs = list(
            db["benchmark_metrics"]
            .find({"pipeline": pipeline})
            .sort("timestamp", DESCENDING)
            .limit(limit)
        )
        return jsonify([serialize_doc(d) for d in docs])
    except PyMongoError:
        return jsonify({"error": "Database error"}), 500


@app.route("/api/producers")
def api_producers():
    try:
        # Récupérer les derniers heartbeats depuis les métriques
        producers = []
        for pipeline in VALID_PIPELINES:
            doc = db["benchmark_metrics"].find_one(
                {"pipeline": pipeline},
                sort=[("timestamp", DESCENDING)]
            )
            if doc:
                producers.append({
                    "pipeline":       pipeline,
                    "active_count":   doc.get("active_producers", 0),
                    "last_snapshot":  doc.get("timestamp", "").isoformat()
                    if isinstance(doc.get("timestamp"), datetime) else str(doc.get("timestamp", "")),
                })
        return jsonify(producers)
    except PyMongoError:
        return jsonify({"error": "Database error"}), 500


@app.route("/api/verdict")
def api_verdict():
    try:
        return jsonify(compute_verdict())
    except PyMongoError:
        return jsonify({"error": "Database error"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
