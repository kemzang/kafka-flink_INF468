"""
Script de monitoring — Affiche les statistiques depuis MongoDB
À exécuter depuis ta machine locale pour voir les résultats en temps réel
"""
import time
from datetime import datetime
from pymongo import MongoClient

MONGO_URI = "mongodb://admin:password123@localhost:27017/"

def print_stats():
    client = MongoClient(MONGO_URI)
    db = client["sales_db"]

    print("\n" + "=" * 65)
    print(f"📊  DASHBOARD VENTES TEMPS RÉEL — {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 65)

    # ── Statistiques générales ──
    total_events = db["sales_raw"].count_documents({})
    total_alerts = db["high_value_alerts"].count_documents({})

    print(f"\n📦  Total événements traités : {total_events}")
    print(f"🚨  Alertes (> 500€)         : {total_alerts}")

    # ── Ventes par catégorie ──
    if total_events > 0:
        pipeline_cat = [
            {"$group": {
                "_id": "$category",
                "total_ventes": {"$sum": "$total_amount"},
                "nombre":       {"$sum": 1}
            }},
            {"$sort": {"total_ventes": -1}},
            {"$limit": 5}
        ]
        print("\n🏆  Top 5 catégories :")
        for doc in db["sales_raw"].aggregate(pipeline_cat):
            print(f"    {doc['_id']:<18} → {doc['total_ventes']:>10.2f}€  ({doc['nombre']} ventes)")

        # ── Ventes par région ──
        pipeline_reg = [
            {"$group": {
                "_id": "$region",
                "total":   {"$sum": "$total_amount"},
                "nombre":  {"$sum": 1}
            }},
            {"$sort": {"total": -1}}
        ]
        print("\n🗺️   Ventes par région :")
        for doc in db["sales_raw"].aggregate(pipeline_reg):
            print(f"    {doc['_id']:<16} → {doc['total']:>10.2f}€  ({doc['nombre']} ventes)")

        # ── 5 dernières alertes ──
        if total_alerts > 0:
            print("\n🚨  Dernières alertes (grosses transactions) :")
            for alert in db["high_value_alerts"].find().sort("detected_at", -1).limit(5):
                print(f"    [{alert['timestamp'][:19]}] {alert['product']:<25} {alert['amount']:>8.2f}€ — {alert['region']}")

    client.close()

def main():
    print("[CONSUMER] Connexion à MongoDB...")
    print("[CONSUMER] Rafraîchissement toutes les 5 secondes. Ctrl+C pour arrêter.\n")

    try:
        while True:
            print_stats()
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[CONSUMER] Arrêt du monitoring.")

if __name__ == "__main__":
    main()