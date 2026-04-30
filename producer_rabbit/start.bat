@echo off
REM ─────────────────────────────────────────────────────────────
REM Script de démarrage — Producteur RabbitMQ distant
REM Compatible Windows 10 / Windows 11
REM ─────────────────────────────────────────────────────────────

echo ==============================================
echo   BENCHMARK Kafka vs RabbitMQ - Producteur RabbitMQ
echo ==============================================

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installe ou pas dans le PATH.
    echo [AIDE]   Telechargez Python 3.8+ depuis https://www.python.org
    echo [AIDE]   Cochez "Add Python to PATH" lors de l'installation
    pause
    exit /b 1
)

python -c "import sys; exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python 3.8+ requis.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo [INFO] %%i detecte

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [INFO] Fichier .env cree depuis .env.example
        echo.
        echo IMPORTANT : Editez le fichier .env et renseignez CENTRAL_IP
        echo    Exemple : CENTRAL_IP=192.168.1.50
        echo.
        notepad .env
        echo Appuyez sur une touche apres avoir sauvegarde .env...
        pause >nul
    ) else (
        echo [ERREUR] Fichier .env.example introuvable.
        pause
        exit /b 1
    )
)

echo [INFO] Installation des dependances...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    pause
    exit /b 1
)
echo [INFO] Dependances installees

echo [INFO] Demarrage du producteur RabbitMQ...
echo.
python producer.py

pause
