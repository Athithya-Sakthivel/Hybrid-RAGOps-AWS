# fast single-stage Dockerfile for quick iteration
FROM python:3.12-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
ENV VENV_PATH=/opt/venv
ENV PYTHONUNBUFFERED=1
ENV PATH=${VENV_PATH}/bin:$PATH
ENV PYTHONPATH=/indexing_pipeline
WORKDIR /indexing_pipeline

# 1) minimal build dependencies needed for pip installs / compiling wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc g++ libssl-dev libffi-dev pkg-config \
    libjpeg-dev zlib1g-dev libxml2-dev libxslt-dev libpng-dev libfreetype6-dev \
    libopenjp2-7-dev curl wget ca-certificates git && \
    rm -rf /var/lib/apt/lists/*

# 2) copy only requirements first so changing source doesn't bust Python deps layer
COPY requirements.txt /tmp/requirements.txt

# 3) create venv and install Python dependencies (no --no-cache-dir per request)
RUN python -m venv ${VENV_PATH} \
 && ${VENV_PATH}/bin/pip install --upgrade pip setuptools wheel \
 && ${VENV_PATH}/bin/pip install -r /tmp/requirements.txt

# 4) copy source (cheap to change; won't re-run pip install)
COPY . /indexing_pipeline

# ensure convert script executable
RUN chmod +x /indexing_pipeline/pre_conversions/convert_all.sh || true

# 5) install heavier runtime tools last to keep iteration fast when editing code
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 poppler-utils ffmpeg libgl1-mesa-glx libglib2.0-0 libsm6 libxrender1 libxext6 \
    libjpeg62-turbo jq ca-certificates fonts-dejavu-core unzip default-jre-headless tini \
    libreoffice-core libreoffice-writer libreoffice-calc tesseract-ocr libleptonica-dev libtesseract-dev && \
    rm -rf /var/lib/apt/lists/*

# create non-root user and fix permissions for fast local iteration
RUN useradd --create-home --shell /bin/bash appuser \
 && chown -R appuser:appuser /indexing_pipeline ${VENV_PATH}

USER appuser

EXPOSE 8000
ENV WORKDIR=/indexing_pipeline

ENTRYPOINT ["/opt/venv/bin/python","/indexing_pipeline/indexing_pipeline.py"]
