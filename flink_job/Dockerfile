FROM flink:1.18-scala_2.12-java11

# Python est déjà présent dans l'image Flink de base
# On installe uniquement les packages Python nécessaires
RUN pip3 install --no-cache-dir \
    pymongo==4.7.2 \
    kafka-python==2.0.2

# Connecteur Kafka pour Flink (déjà téléchargé dans le cache Docker si disponible)
# Si pas de connexion, ces JARs doivent être présents localement
COPY flink_job.py /opt/flink/usrlib/flink_job.py

ENV PYTHONPATH="/opt/flink/usrlib"
