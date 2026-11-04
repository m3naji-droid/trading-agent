FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 DATA_DIR=/srv/data
COPY requirements.txt ./baked/requirements.txt
RUN pip install --no-cache-dir -r baked/requirements.txt && sha256sum baked/requirements.txt | cut -d' ' -f1 | tr -d '\n' > /srv/.req_hash
COPY app ./baked/app
COPY static ./baked/static
COPY boot.py ./boot.py
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=5 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
# boot.py يحمّل آخر نسخة من GitHub عند كل تشغيل ثم يشغّل الوكيل
CMD ["python", "/srv/boot.py"]
