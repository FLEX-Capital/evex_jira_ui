FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime dependencies first (better layer caching).
COPY requirements.runtime.txt /app/requirements.runtime.txt
# CKV_DOCKER_3: create an unprivileged user in the same layer as the install
# (DL3059: avoid consecutive RUN instructions). uid/gid 10001 match the Azure Files
# mount options in infra/, so the mounted data/ share is writable by this user.
# data/ is excluded from the build context; create it so local runs work unmounted.
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.runtime.txt \
    && groupadd --gid 10001 appuser \
    && useradd --create-home --uid 10001 --gid 10001 appuser \
    && install -d -o appuser -g appuser /app/data

# Explicit runtime allowlist; tests derive the transitive local import closure.
COPY --chown=appuser:appuser \
    app.py \
    asset_country.py \
    asset_migration.py \
    backfill_country.py \
    backfill_jira.py \
    cache_maintenance.py \
    cache_schema.py \
    category_sync.py \
    chart_data.py \
    country_cache.py \
    dashboard_views.py \
    data_loading.py \
    data_transformation.py \
    desk_sync.py \
    file_io.py \
    http_policy.py \
    interactive.py \
    issue_links.py \
    jira_country_export.py \
    jira_loader.py \
    maintenance_ui.py \
    operation_errors.py \
    plotting.py \
    reporting_policy.py \
    resolution_bands.py \
    service_desks.py \
    settings.py \
    source_sync.py \
    styles.py \
    sync_checkpoints.py \
    sync_jobs.py \
    temporary_history_pull.py \
    ui_auth.py \
    /app/
COPY --chown=appuser:appuser .streamlit/config.toml /app/.streamlit/config.toml
COPY --chown=appuser:appuser assets/evex_logo.png assets/object_id_to_name.json /app/assets/

USER 10001

EXPOSE 8501

# CKV_DOCKER_2: healthcheck against Streamlit's built-in endpoint, on the same
# ${PORT:-8501} the CMD binds to. Azure Container Apps uses its own probes (infra/).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8501') + '/_stcore/health', timeout=4).status == 200 else 1)"]

# Streamlit's default CORS/XSRF protection stays on (pygwalker's endpoint opts out of
# XSRF itself). No file watcher: the code never changes in a running container, and
# data/ is a network share in Azure.
CMD ["sh", "-c", "streamlit run app.py --server.address=0.0.0.0 \"--server.port=${PORT:-8501}\" --server.headless=true --server.fileWatcherType=none --browser.gatherUsageStats=false"]
