FROM node:24.21.0-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.txt requirements-bootstrap.lock.txt requirements.lock.txt /app/
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r requirements-bootstrap.lock.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock.txt
RUN groupadd -g 1000 grocyste && useradd -u 1000 -g 1000 -M -s /usr/sbin/nologin grocyste
COPY grocyste /app/grocyste
COPY web /app/web
COPY scripts /app/scripts
COPY trust /app/trust
COPY catalog.json /app/catalog.json
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8788 \
    STATE_DIR=/state PACKAGES_DIR=/packages WEB_DIR=/app/web \
    SECRET_FILE=/state/secrets/grocy.json MANAGER_SOCKET=/run/grocyste/manager.sock \
    CATALOG_PUBLIC_KEY=/app/trust/catalog.pub
USER 1000:1000
EXPOSE 8788
CMD ["gunicorn", "--workers", "2", "--threads", "4", "--timeout", "90", "--bind", "0.0.0.0:8788", "grocyste.runtime:create_app()"]

FROM runtime AS tests
USER root
COPY requirements-dev.txt requirements-dev.lock.txt /app/
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements-dev.lock.txt
COPY tests /app/tests
COPY docs /app/docs
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]
