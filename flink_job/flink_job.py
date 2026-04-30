"""
Job de traitement Flink/Kafka → MongoDB
Consomme les événements Kafka, les traite et les stocke dans MongoDB
Simule le comportement d'un job Flink avec fenêtres temporelles
"""
import json
import time
import threading
from datetime import datetime
from collections import defaultdict
from kafka import KafkaConsumer
from pymongo import MongoClient

KAFKA_BOOTSTRAP_SERVERS = "kafka:29092"
TOPIC_NAME              = "sales_events"
GROUP_ID                = "flink-processor-group"
MONGO_URI               = "mongodb://admin:password123@mongodb:27017/"
MONGO_DB                = "sales_db"

# ─── Connexion MongoDB ───────────────────────────────────────────────────────
def get_mongo_db():
    client = MongoClient(MONGO_URI)
    return client[MONGO_DB]

# ─── Fenêtre temporelle (simule Flink Tumbling Window de 30 secondes) ────────
class TumblingWindow:
    def __init__(self, size_seconds=30):
        self.size      = size_seconds
        self.events    = []
        self.lock      = threading.Lock()
        self.start_at  = time.time()

    def add(self, event):
        with self.lock:
            self.events.append(event)

    def should_flush(self):
        return (time.time() - self.start_at) >= self.size

    def flush(self):
        with self.lock:
            data = list(self.events)
            self.events    = []
            self.start_at  = time.time()
            return data

# ─── Traitement d'un événement individuel ────────────────────────────────────
def process_event(event, db):
    """Enrichit et sauvegarde chaque événement dans MongoDB"""
    # Enrichissement des données
    event['tva_amount']   = round(event['total_amount'] * 0.1925, 2)
    event['amount_ttc']   = round(event['total_amount'] * 1.1925, 2)
    event['processed_at'] = datetime.utcnow().isoformat() + "Z"
    event['processor']    = "kafka-flink-pipeline-v1.0"

    # Sauvegarde dans la collection sales_raw
    db["sales_raw"].insert_one({k: v for k, v in event.items() if k != '_id'})

    # Détection des grosses transactions (alerte)
    if event.get('total_amount', 0) > 500:
        alert = {
            "alert_type":  "HIGH_VALUE_TRANSACTION",
            "event_id":    event.get('event_id'),
            "amount":      event.get('total_amount'),
            "amount_ttc":  event.get('amount_ttc'),
            "product":     event.get('product_name'),
            "category":    event.get('category'),
            "customer":    event.get('customer_name'),
            "region":      event.get('region'),
            "payment":     event.get('payment_method'),
            "timestamp":   event.get('timestamp'),
            "detected_at": datetime.utcnow().isoformat() + "Z",
        }
        db["high_value_alerts"].insert_one(alert)
        print(f"[FLINK] 🚨 ALERTE: {event['total_amount']}€ — {event['product_name']} — {event['region']}")

    print(f"[FLINK] ✅ Traité: {event['product_name']} x{event['quantity']} = {event['total_amount']}€ | {event['region']}")
    return event

# ─── Agrégation par fenêtre temporelle (Tumbling Window 30s) ─────────────────
def aggregate_window(events, db):
    """Agrège les ventes sur une fenêtre de 30 secondes"""
    if not events:
        return

    # Agrégation par catégorie
    by_category = defaultdict(lambda: {"total": 0, "count": 0})
    by_region   = defaultdict(lambda: {"total": 0, "count": 0})
    total_revenue = 0

    for e in events:
        cat = e.get('category', 'Inconnu')
        reg = e.get('region', 'Inconnu')
        amt = e.get('total_amount', 0)

        by_category[cat]["total"] += amt
        by_category[cat]["count"] += 1
        by_region[reg]["total"]   += amt
        by_region[reg]["count"]   += 1
        total_revenue             += amt

    # Sauvegarde de l'agrégat dans MongoDB
    aggregat = {
        "window_start":   datetime.utcfromtimestamp(time.time() - 30).isoformat() + "Z",
        "window_end":     datetime.utcnow().isoformat() + "Z",
        "window_size_s":  30,
        "total_events":   len(events),
        "total_revenue":  round(total_revenue, 2),
        "by_category":    [
            {"category": k, "revenue": round(v["total"], 2), "count": v["count"]}
            for k, v in sorted(by_category.items(), key=lambda x: -x[1]["total"])
        ],
        "by_region": [
            {"region": k, "revenue": round(v["total"], 2), "count": v["count"]}
            for k, v in sorted(by_region.items(), key=lambda x: -x[1]["total"])
        ],
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }
    db["sales_aggregated"].insert_one(aggregat)
    print(f"\n[FLINK] 📊 FENÊTRE FERMÉE: {len(events)} événements | CA={round(total_revenue,2)}€")
    print(f"[FLINK] Top catégorie: {aggregat['by_category'][0]['category'] if aggregat['by_category'] else 'N/A'}")
    print("-" * 60)

# ─── Pipeline principal ───────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("[FLINK-PIPELINE] Démarrage du Job de traitement")
    print(f"[FLINK-PIPELINE] Kafka  : {KAFKA_BOOTSTRAP_SERVERS}")
    print(f"[FLINK-PIPELINE] Topic  : {TOPIC_NAME}")
    print(f"[FLINK-PIPELINE] MongoDB: {MONGO_DB}")
    print("=" * 60)

    # Attendre que Kafka et MongoDB soient prêts
    print("[FLINK-PIPELINE] Attente des services (15s)...")
    time.sleep(15)

    # Connexion MongoDB
    db = get_mongo_db()
    print("[FLINK-PIPELINE] ✅ MongoDB connecté")

    # Connexion Kafka Consumer
    consumer = KafkaConsumer(
        TOPIC_NAME,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=GROUP_ID,
        value_deserializer=lambda m: json.loads(m.decode('utf-8')),
        auto_offset_reset='latest',
        enable_auto_commit=True,
        consumer_timeout_ms=-1,
    )
    print("[FLINK-PIPELINE] ✅ Kafka Consumer connecté")
    print("[FLINK-PIPELINE] 🚀 Pipeline démarré — Traitement en cours...\n")

    # Fenêtre temporelle de 30 secondes
    window = TumblingWindow(size_seconds=30)

    for message in consumer:
        try:
            event = message.value

            # Traitement individuel de l'événement
            process_event(event, db)

            # Ajout à la fenêtre temporelle
            window.add(event)

            # Vérifier si la fenêtre doit être fermée et agrégée
            if window.should_flush():
                events_batch = window.flush()
                aggregate_window(events_batch, db)

        except Exception as e:
            print(f"[FLINK-PIPELINE] ❌ Erreur: {e}")
            continue

if __name__ == "__main__":
    main()