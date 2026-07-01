FROM postgres:16
RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
# pgvector preloaded; extension created per-database by migration/init
COPY init-extensions.sql /docker-entrypoint-initdb.d/01-init-extensions.sql
