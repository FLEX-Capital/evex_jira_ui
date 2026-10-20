FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime dependencies first (better layer caching on Render).
COPY requirements.runtime.txt /app/requirements.runtime.txt
# CKV_DOCKER_3: create an unprivileged user in the same layer as the install
# (DL3059: avoid consecutive RUN instructions).
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.runtime.txt \
    && useradd --create-home --uid 10001 appuser

# Copy the app (owned by the unprivileged user so Streamlit can write its cache)
COPY --chown=appuser:appuser . /app

USER 10001

EXPOSE 8501

# CKV_DOCKER_2: healthcheck against Streamlit's built-in endpoint. Uses the same
# ${PORT:-8501} the CMD binds to, so it follows Render's injected port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8501') + '/_stcore/health', timeout=4).status == 200 else 1)"]

# Render provides $PORT. Default to 8501 for local runs.
CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 \"--server.port=${PORT:-8501}\" --server.headless=true"]


