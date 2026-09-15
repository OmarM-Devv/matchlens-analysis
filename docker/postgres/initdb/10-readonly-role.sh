#!/usr/bin/env bash
# Runs once, when the postgres data volume is first initialised.
# Creates the read-only login used by the API and locks down default privileges.
# Tables are created later by the loader (as POSTGRES_USER); ALTER DEFAULT
# PRIVILEGES makes them readable by the read-only role automatically.
#
# To re-run after changing credentials: docker compose down -v (drops all data).

: "${MATCHLENS_RO_USER:?MATCHLENS_RO_USER must be set}"
: "${MATCHLENS_RO_PASSWORD:?MATCHLENS_RO_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --set=ro_user="$MATCHLENS_RO_USER" \
     --set=ro_password="$MATCHLENS_RO_PASSWORD" \
     --set=owner="$POSTGRES_USER" \
     --set=dbname="$POSTGRES_DB" <<'EOSQL'
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE :"ro_user" WITH LOGIN PASSWORD :'ro_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT 20;
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;
ALTER ROLE :"ro_user" SET statement_timeout = '5s';
ALTER ROLE :"ro_user" SET idle_in_transaction_session_timeout = '30s';

GRANT CONNECT ON DATABASE :"dbname" TO :"ro_user";
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT SELECT ON TABLES TO :"ro_user";
EOSQL
