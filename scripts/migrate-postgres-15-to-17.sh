#!/usr/bin/env bash
#
# Migrate the OpenWebUI Monitor database from PostgreSQL 15 to 17.
#
# PostgreSQL major versions are not file-compatible: a PG17 container cannot
# read a data volume initialized by PG15. This script performs a dump/restore
# migration:
#
#   1. Dump the database while the old PG15 container is still running
#   2. Stop the stack and remove the old data volume
#   3. Start the new PG17 database container
#   4. Restore the dump BEFORE the app recreates its tables
#   5. Start the full stack again
#
# Usage:
#   ./scripts/migrate-postgres-15-to-17.sh
#
# Prerequisites:
#   - Run from the directory containing your docker-compose.yml and .env
#   - The stack must be up and running the OLD postgres:15 image
#   - docker compose v2 must be available
#
set -euo pipefail

COMPOSE="docker compose"
DB_SERVICE="db"
APP_SERVICE="app"
BACKUP_FILE="backup_pg15_$(date +%F_%H%M%S).sql"

# Load POSTGRES_USER / POSTGRES_DATABASE from .env if present
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

PGUSER="${POSTGRES_USER:-postgres}"
PGDB="${POSTGRES_DATABASE:-openwebui_monitor}"

echo "==> OpenWebUI Monitor: PostgreSQL 15 -> 17 migration"
echo "    Database: ${PGDB} | User: ${PGUSER}"
echo "    Backup file: ${BACKUP_FILE}"
echo

# Safety check: the DB container must be running the old image
if ! ${COMPOSE} ps --status running --services | grep -qx "${DB_SERVICE}"; then
    echo "ERROR: The '${DB_SERVICE}' service is not running."
    echo "Start your existing (PG15) stack first: ${COMPOSE} up -d"
    exit 1
fi

RUNNING_IMAGE=$(${COMPOSE} ps --format json "${DB_SERVICE}" | grep -o '"Image":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
echo "==> Currently running database image: ${RUNNING_IMAGE:-unknown}"
if [[ "${RUNNING_IMAGE}" == *"postgres:17"* ]]; then
    echo "ERROR: The database is already running PostgreSQL 17."
    echo "This script must run while the OLD postgres:15 container is still up."
    exit 1
fi

echo
echo "==> Step 1/5: Dumping database '${PGDB}' from the running PG15 container..."
${COMPOSE} exec "${DB_SERVICE}" pg_dump -U "${PGUSER}" "${PGDB}" > "${BACKUP_FILE}"
if [[ ! -s "${BACKUP_FILE}" ]]; then
    echo "ERROR: Backup file '${BACKUP_FILE}' is empty. Aborting."
    exit 1
fi
echo "    Backup written: ${BACKUP_FILE} ($(du -h "${BACKUP_FILE}" | cut -f1))"

echo
echo "==> Step 2/5: Stopping the stack and removing the old PG15 data volume..."
${COMPOSE} down

VOLUME_NAME=$(docker volume ls --format '{{.Name}}' | grep 'postgres_data' || true)
if [[ -z "${VOLUME_NAME}" ]]; then
    echo "ERROR: Could not find a volume matching 'postgres_data'."
    echo "Run 'docker volume ls' and remove the old volume manually, then re-run."
    exit 1
fi
echo "    Removing volume: ${VOLUME_NAME}"
docker volume rm "${VOLUME_NAME}"

echo
echo "==> Step 3/5: Pulling new images and starting ONLY the PG17 database..."
${COMPOSE} pull
${COMPOSE} up -d "${DB_SERVICE}"

echo "    Waiting for the database to accept connections..."
for i in $(seq 1 30); do
    if ${COMPOSE} exec "${DB_SERVICE}" pg_isready -U "${PGUSER}" > /dev/null 2>&1; then
        break
    fi
    if [[ "${i}" -eq 30 ]]; then
        echo "ERROR: Database did not become ready in time."
        exit 1
    fi
    sleep 2
done

echo
echo "==> Step 4/5: Restoring the dump (before the app creates any tables)..."
${COMPOSE} exec -T "${DB_SERVICE}" psql -U "${PGUSER}" -d "${PGDB}" < "${BACKUP_FILE}"

echo
echo "==> Step 5/5: Starting the full stack..."
${COMPOSE} up -d

echo
echo "==> Migration complete!"
echo
echo "Please verify that your data (users, balances, usage records) is intact"
echo "in the dashboard before deleting the backup file:"
echo "    ${BACKUP_FILE}"
