-- Sample analytics dataset for QueryMind demos (ClickHouse flavor).
--
-- Auto-loaded by the qm_clickhouse container via the
-- docker-entrypoint-initdb.d mount in docker-compose.dev.yml. To re-seed
-- manually:
--   clickhouse-client --host 127.0.0.1 --port 9000 \
--     --user querymind --password querymind \
--     --multiquery < infra/seed/clickhouse_demo.sql
--
-- ClickHouse has no auto-increment, so customer_id / order_id are written
-- explicitly. MergeTree is the workhorse engine for analytics.

CREATE DATABASE IF NOT EXISTS sales_demo;

DROP TABLE IF EXISTS sales_demo.sales;
DROP TABLE IF EXISTS sales_demo.customers;

CREATE TABLE sales_demo.customers (
    customer_id UInt32,
    name        String,
    segment     String DEFAULT 'standard'
) ENGINE = MergeTree()
ORDER BY (customer_id);

CREATE TABLE sales_demo.sales (
    order_id    UInt32,
    customer_id UInt32,
    ts          DateTime,
    amount      Decimal(10, 2),
    region      String,
    channel     String DEFAULT 'web'
) ENGINE = MergeTree()
ORDER BY (order_id);

INSERT INTO sales_demo.customers (customer_id, name, segment) VALUES
    (1, 'Alice', 'enterprise'),
    (2, 'Bob',   'standard'),
    (3, 'Carol', 'enterprise'),
    (4, 'Dan',   'standard');

INSERT INTO sales_demo.sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (1, 1, now() - INTERVAL 28 DAY, 50.00,  'NA',   'web'),
    (2, 2, now() - INTERVAL 25 DAY, 100.00, 'NA',   'web'),
    (3, 3, now() - INTERVAL 20 DAY, 25.00,  'EU',   'partner'),
    (4, 4, now() - INTERVAL 14 DAY, 200.00, 'EU',   'web'),
    (5, 1, now() - INTERVAL 10 DAY, 75.00,  'APAC', 'web'),
    (6, 2, now() - INTERVAL  7 DAY, 120.00, 'APAC', 'partner'),
    (7, 3, now() - INTERVAL  3 DAY, 60.00,  'EU',   'web'),
    (8, 4, now() - INTERVAL  1 DAY, 300.00, 'NA',   'web');

-- Optional: create a read-only user for QueryMind to connect as.
-- Run via clickhouse-client as the admin user:
--   CREATE USER querymind_ro IDENTIFIED BY 'replace-me';
--   GRANT SELECT ON sales_demo.* TO querymind_ro;
