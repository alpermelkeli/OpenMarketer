# The studio's sandbox: where agent-written Python runs, HTML is rendered to PNG and images are
# read by OCR. Built by `make sandbox`; driven by
# packages/core/src/openmarketer_core/studio/docker_sandbox.py, which starts every container
# with no network, a read-only root file system and no capabilities.
FROM python:3.12.11-slim-bookworm

# Chromium renders HTML; Tesseract reads text back for verification; the fonts are the only
# ones a template or a program can use (all under open licences). libcairo2 is cairosvg's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        tesseract-ocr \
        tesseract-ocr-eng \
        libcairo2 \
        fonts-dejavu-core \
        fonts-inter \
        fonts-noto-core \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/studio/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /opt/studio/requirements.txt

COPY entry.py run_user.py /opt/studio/
RUN chmod 0555 /opt/studio/entry.py /opt/studio/run_user.py \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /tmp --shell /usr/sbin/nologin studio

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp

USER 10001:10001
WORKDIR /out
ENTRYPOINT ["python", "-I", "/opt/studio/entry.py"]
