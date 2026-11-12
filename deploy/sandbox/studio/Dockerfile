# The studio's sandbox: where agent-written Python runs, HTML is rendered to PNG and images are
# read by OCR. Built by `make sandbox`; driven by
# packages/core/src/openmarketer_core/studio/docker_sandbox.py, which starts every container
# with no network, a read-only root file system and no capabilities.
FROM python:3.12.11-slim-bookworm

# Chromium renders HTML; Tesseract reads text back for verification; the fonts are the only
# ones a template or a program can use (all under open licences, each covering the Turkish
# letters). The fonts after fontconfig are the studio's catalogue, which
# packages/core/src/openmarketer_core/studio/typefaces.py lists (a test holds this list to
# it); DejaVu and Noto's emoji are fallbacks. libcairo2 is cairosvg's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-tur \
        tesseract-ocr-deu \
        tesseract-ocr-fra \
        tesseract-ocr-spa \
        tesseract-ocr-ita \
        tesseract-ocr-por \
        libcairo2 \
        fontconfig \
        fonts-bebas-neue \
        fonts-comfortaa \
        fonts-ebgaramond \
        fonts-firacode \
        fonts-inter \
        fonts-jetbrains-mono \
        fonts-lato \
        fonts-league-spartan \
        fonts-manrope \
        fonts-noto-core \
        fonts-open-sans \
        fonts-roboto-slab \
        fonts-roboto-unhinted \
        fonts-sora \
        fonts-vollkorn \
        fonts-dejavu-core \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/studio/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /opt/studio/requirements.txt

COPY entry.py run_user.py read_text.py check_fonts.py /opt/studio/
# The typography helper agent-written code imports (run_user.py puts lib on its path).
COPY studio_type.py typefaces.json /opt/studio/lib/
RUN chmod 0555 /opt/studio/entry.py /opt/studio/run_user.py /opt/studio/read_text.py \
        /opt/studio/check_fonts.py \
    && chmod 0444 /opt/studio/lib/studio_type.py /opt/studio/lib/typefaces.json \
    && python -I /opt/studio/check_fonts.py /opt/studio/lib/typefaces.json \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /tmp --shell /usr/sbin/nologin studio

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp

USER 10001:10001
WORKDIR /out
ENTRYPOINT ["python", "-I", "/opt/studio/entry.py"]
