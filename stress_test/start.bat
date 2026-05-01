@echo off
echo ============================================================
echo   STRESS TEST - Kafka vs RabbitMQ
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/2] Installation des dependances...
pip install -r requirements.txt -q

echo [2/2] Lancement du stress test...
echo.
python stress_test.py

pause
