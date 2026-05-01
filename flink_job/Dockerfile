FROM flink:1.19-scala_2.12-java11

USER root

# ── Outils système ────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y python3 python3-pip wget curl && \
    ln -sf /usr/bin/python3 /usr/bin/python && \
    rm -rf /var/lib/apt/lists/*

# ── PyFlink + dépendances Python ──────────────────────────────
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=1000
ENV PIP_RETRIES=20
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PIP_REQUIRE_HASH=false
RUN pip3 install --no-cache-dir setuptools wheel && \
    pip3 install --no-cache-dir --timeout 1000 --retries 20 \
        --trusted-host files.pythonhosted.org \
        --trusted-host pypi.org \
        pyarrow==11.0.0 && \
    pip3 install --no-cache-dir --timeout 1000 --retries 20 \
        --trusted-host files.pythonhosted.org \
        --trusted-host pypi.org \
        ruamel.yaml==0.18.6 \
        ruamel.yaml.clib==0.2.8 \
        apache-flink==1.19.0 \
        pymongo==4.7.2 \
        kafka-python==2.0.2

# ── Connecteur Kafka pour Flink (JAR) ─────────────────────────
RUN wget -q -P /opt/flink/lib \
    https://repo.maven.apache.org/maven2/org/apache/flink/flink-connector-kafka/3.2.0-1.19/flink-connector-kafka-3.2.0-1.19.jar

# ── Client Kafka (JAR) ────────────────────────────────────────
RUN wget -q -P /opt/flink/lib \
    https://repo.maven.apache.org/maven2/org/apache/kafka/kafka-clients/3.4.0/kafka-clients-3.4.0.jar

# ── Job PyFlink ───────────────────────────────────────────────
COPY flink_job.py /opt/flink/usrlib/flink_job.py

ENV PYTHONPATH="/opt/flink/usrlib"
