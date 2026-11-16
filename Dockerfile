FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    CLIPMAKER_DATA_DIR=/tmp/clipmaker

# ffmpeg : téléchargement, découpe, sous-titres. fontconfig + Liberation :
# libass doit trouver une police pour le style « Arial Black » du fichier ASS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-liberation libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Alias Arial Black → Liberation Sans (métriquement compatible avec Arial).
RUN mkdir -p /etc/fonts/conf.d \
    && printf '%s\n' \
    '<?xml version="1.0"?>' \
    '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">' \
    '<fontconfig>' \
    '  <match target="pattern">' \
    '    <test name="family"><string>Arial Black</string></test>' \
    '    <edit name="family" mode="assign" binding="strong">' \
    '      <string>Liberation Sans</string>' \
    '    </edit>' \
    '  </match>' \
    '</fontconfig>' \
    > /etc/fonts/conf.d/99-clipmaker.conf \
    && fc-cache -f

RUN useradd --create-home --uid 1000 user
WORKDIR /app

COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cloud.txt

COPY . .
RUN chmod +x start_cloud.sh \
    && mkdir -p /tmp/clipmaker \
    && chown -R user:user /app /tmp/clipmaker

USER user

EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=8).status == 200 else 1)"

CMD ["./start_cloud.sh"]
