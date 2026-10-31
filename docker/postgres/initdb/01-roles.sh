#!/bin/sh
# Runs once, on first start of an empty data volume, as the container superuser.
# Roles are created here rather than in a migration because creating roles needs
# superuser or CREATEROLE, and the migration role must not hold either.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE ROLE chi_owner LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE PASSWORD '${CHI_OWNER_PASSWORD}';
CREATE ROLE chi_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE PASSWORD '${CHI_APP_PASSWORD}';
ALTER DATABASE "${POSTGRES_DB}" OWNER TO chi_owner;
GRANT CONNECT ON DATABASE "${POSTGRES_DB}" TO chi_app;
EOSQL
