#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Script de démarrage — Producteur RabbitMQ distant
# Compatible Linux / macOS — sans droits administrateur
# ─────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=============================================="
echo "  BENCHMARK Kafka vs RabbitMQ - Producteur RabbitMQ"
echo "=============================================="

if ! command -v python3 &>/dev/null; then
    echo "[ERREUR] Python3 n'est pas installé."
    echo "[AIDE]  Installez Python 3.8+ depuis https://www.python.org"
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]; }; then
    echo "[ERREUR] Python $PYTHON_VERSION détecté. Version 3.8+ requise."
    exit 1
fi

echo "[INFO] Python $PYTHON_VERSION détecté ✅"

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "[INFO] Fichier .env créé depuis .env.example"
        echo ""
        echo "⚠️  IMPORTANT : Éditez le fichier .env et renseignez CENTRAL_IP"
        echo "   Exemple : CENTRAL_IP=192.168.1.50"
        echo ""
        read -p "Appuyez sur Entrée après avoir modifié .env..."
    else
        echo "[ERREUR] Fichier .env.example introuvable."
        exit 1
    fi
fi

echo "[INFO] Installation des dépendances..."
python3 -m pip install --quiet --user -r requirements.txt
echo "[INFO] Dépendances installées ✅"

echo "[INFO] Démarrage du producteur RabbitMQ..."
echo ""
python3 producer.py
