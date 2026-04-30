"""
Producteur Kafka — Simulation de transactions de ventes en temps réel
Ce script génère des données de ventes fictives et les envoie dans Kafka
"""
import json
import time
import random
import os
from datetime import datetime
from kafka import KafkaProducer
from faker import Faker

fake = Faker('fr_FR')

KAFKA_BOOTSTRAP_SERVERS = os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092')
TOPIC_NAME = os.getenv('TOPIC_NAME', 'sales_events')

# Catalogue de produits
PRODUCTS = [
    {"id": "P001", "name": "Laptop Dell XPS", "category": "Informatique", "base_price": 1200.00},
    {"id": "P002", "name": "iPhone 15 Pro",   "category": "Téléphonie",   "base_price": 1100.00},
    {"id": "P003", "name": "Samsung TV 55\"",  "category": "Électronique", "base_price": 800.00},
    {"id": "P004", "name": "Nike Air Max",     "category": "Vêtements",    "base_price": 150.00},
    {"id": "P005", "name": "Livre Python",     "category": "Livres",       "base_price": 35.00},
    {"id": "P006", "name": "Café Arabica 1kg", "category": "Alimentaire",  "base_price": 25.00},
    {"id": "P007", "name": "Casque Sony WH",   "category": "Électronique", "base_price": 350.00},
    {"id": "P008", "name": "Montre Garmin",    "category": "Sport",        "base_price": 450.00},
]

REGIONS = ["Yaoundé", "Douala", "Bafoussam", "Garoua", "Bamenda", "Ngaoundéré"]
PAYMENT_METHODS = ["carte_bancaire", "mobile_money", "espèces", "virement"]

def create_sale_event():
    """Génère un événement de vente aléatoire"""
    product = random.choice(PRODUCTS)
    quantity = random.randint(1, 5)
    # Variation de prix ±15%
    price_variation = random.uniform(0.85, 1.15)
    unit_price = round(product["base_price"] * price_variation, 2)
    total_amount = round(unit_price * quantity, 2)

    return {
        "event_id":       fake.uuid4(),
        "timestamp":      datetime.utcnow().isoformat() + "Z",
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
        "is_premium":     random.random() < 0.2,   # 20% clients premium
    }

def main():
    print(f"[PRODUCTEUR] Connexion à Kafka : {KAFKA_BOOTSTRAP_SERVERS}")
    print(f"[PRODUCTEUR] Topic cible : {TOPIC_NAME}")

    # Attendre que Kafka soit prêt
    time.sleep(10)

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode('utf-8'),
        key_serializer=lambda k: k.encode('utf-8') if k else None,
        retries=5,
        retry_backoff_ms=1000,
    )

    print("[PRODUCTEUR] ✅ Connecté à Kafka. Démarrage de la production...")

    count = 0
    while True:
        try:
            event = create_sale_event()
            # La clé = region (pour la partition Kafka par région)
            producer.send(
                TOPIC_NAME,
                key=event["region"],
                value=event
            )
            count += 1
            print(f"[PRODUCTEUR] Envoi #{count} | {event['product_name']} x{event['quantity']} = {event['total_amount']}€ | {event['region']}")

            # Intervalle aléatoire pour simuler le trafic réel
            time.sleep(random.uniform(0.5, 2.0))

        except Exception as e:
            print(f"[PRODUCTEUR] Erreur : {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()