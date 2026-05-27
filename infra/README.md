# Infra cheatsheet

## Start the dev stack (Postgres + Redis)
```bash
docker compose -f infra/docker-compose.dev.yml up -d
```

## Run migrations against the dev Postgres
```bash
cd backend && alembic upgrade head
```

## Start vLLM on the host (recommended — needs your GPU)
```bash
vllm serve google/gemma-3-4b-it \
  --guided-decoding-backend xgrammar \
  --max-model-len 8192 \
  --port 8000
```
The OpenAI-compatible endpoint will then be reachable at `http://localhost:8000/v1`.

## Run the ephemeral test Postgres (port 55432)
```bash
docker compose -f infra/docker-compose.test.yml up -d
```

## Wipe everything (containers + named volume)
```bash
docker compose -f infra/docker-compose.dev.yml down -v
docker compose -f infra/docker-compose.test.yml down -v
```

## Per-dialect dev databases

The dev compose file ships **commented-out** service blocks for MySQL,
ClickHouse, MongoDB, and Oracle XE so the default `up -d` only starts
the metadata Postgres + Redis. To smoke-test QueryMind against a real
instance of a non-Postgres dialect:

1. Open `infra/docker-compose.dev.yml`.
2. Uncomment the service block you want.
3. Uncomment the matching named volume at the bottom of the file
   (`mysql_data`, `clickhouse_data`, `mongodb_data`, `oracle_data`).
4. `docker compose -f infra/docker-compose.dev.yml up -d <service>`.

Each container auto-loads its seed file from `infra/seed/` on first
start (empty volume), giving you the same `customers` + `sales` dataset
across dialects. To re-seed, `docker compose down -v` then `up -d`.

### MySQL 8.4
```bash
docker compose -f infra/docker-compose.dev.yml up -d mysql
# Re-seed manually (optional):
docker exec -i qm_mysql mysql -u querymind -pquerymind sales_demo \
  < infra/seed/mysql_demo.sql
```
Add-Connection form:
```
Dialect:  mysql
Host:     localhost
Port:     3306
Database: sales_demo
User:     querymind
Password: querymind
```

### ClickHouse 24.8
```bash
docker compose -f infra/docker-compose.dev.yml up -d clickhouse
# Re-seed manually (optional):
docker exec -i qm_clickhouse clickhouse-client \
  --user querymind --password querymind --multiquery \
  < infra/seed/clickhouse_demo.sql
```
Add-Connection form (HTTP interface):
```
Dialect:  clickhouse
Host:     localhost
Port:     8123
Database: sales_demo
User:     querymind
Password: querymind
```
Port 9000 is exposed as well for the native protocol if you prefer it.

### MongoDB 7
```bash
docker compose -f infra/docker-compose.dev.yml up -d mongodb
# Re-seed manually (optional):
docker exec -i qm_mongodb mongosh \
  "mongodb://querymind:querymind@localhost:27017/?authSource=admin" \
  < infra/seed/mongo_demo.js
```
Add-Connection form:
```
Dialect:  mongodb
Host:     localhost
Port:     27017
Database: sales_demo
User:     querymind
Password: querymind
Auth DB:  admin
```

### Oracle XE 21c
First start takes **~2 minutes** while Oracle initializes the database.
Tail the logs until you see `DATABASE IS READY TO USE!`:
```bash
docker compose -f infra/docker-compose.dev.yml up -d oracle
docker logs -f qm_oracle
# Re-seed manually (optional, once ready):
docker exec -i qm_oracle sqlplus -S querymind/querymind@//localhost:1521/sales_demo \
  < infra/seed/oracle_demo.sql
```
Add-Connection form:
```
Dialect:  oracle
Host:     localhost
Port:     1521
Service:  sales_demo
User:     querymind
Password: querymind
```
