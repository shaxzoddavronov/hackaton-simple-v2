-- Sample analytics dataset for QueryMind demos (Oracle XE flavor).
--
-- Auto-loaded by the qm_oracle container via the
-- /container-entrypoint-initdb.d mount in docker-compose.dev.yml. The
-- gvenzl/oracle-xe image runs init scripts as APP_USER against the
-- APP_USER's schema, so the tables below are owned by `querymind`.
--
-- To re-seed manually (after the container is up):
--   sqlplus querymind/querymind@//localhost:1521/sales_demo \
--     @infra/seed/oracle_demo.sql
--
-- Oracle has no IF EXISTS for DROP, so we swallow ORA-00942 (table
-- doesn't exist) via PL/SQL.

BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE sales';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN RAISE; END IF;
END;
/

BEGIN
    EXECUTE IMMEDIATE 'DROP TABLE customers';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLCODE != -942 THEN RAISE; END IF;
END;
/

CREATE TABLE customers (
    customer_id NUMBER(10)     PRIMARY KEY,
    name        VARCHAR2(255)  NOT NULL,
    segment     VARCHAR2(64)   DEFAULT 'standard' NOT NULL
);

CREATE TABLE sales (
    order_id    NUMBER(10)     PRIMARY KEY,
    customer_id NUMBER(10)     NOT NULL,
    ts          TIMESTAMP      NOT NULL,
    amount      NUMBER(10, 2)  NOT NULL,
    region      VARCHAR2(32)   NOT NULL,
    channel     VARCHAR2(32)   DEFAULT 'web' NOT NULL,
    CONSTRAINT fk_sales_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
);

INSERT INTO customers (customer_id, name, segment) VALUES (1, 'Alice', 'enterprise');
INSERT INTO customers (customer_id, name, segment) VALUES (2, 'Bob',   'standard');
INSERT INTO customers (customer_id, name, segment) VALUES (3, 'Carol', 'enterprise');
INSERT INTO customers (customer_id, name, segment) VALUES (4, 'Dan',   'standard');

INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (1, 1, SYSTIMESTAMP - INTERVAL '28' DAY, 50.00,  'NA',   'web');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (2, 2, SYSTIMESTAMP - INTERVAL '25' DAY, 100.00, 'NA',   'web');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (3, 3, SYSTIMESTAMP - INTERVAL '20' DAY, 25.00,  'EU',   'partner');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (4, 4, SYSTIMESTAMP - INTERVAL '14' DAY, 200.00, 'EU',   'web');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (5, 1, SYSTIMESTAMP - INTERVAL '10' DAY, 75.00,  'APAC', 'web');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (6, 2, SYSTIMESTAMP - INTERVAL '7'  DAY, 120.00, 'APAC', 'partner');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (7, 3, SYSTIMESTAMP - INTERVAL '3'  DAY, 60.00,  'EU',   'web');
INSERT INTO sales (order_id, customer_id, ts, amount, region, channel) VALUES
    (8, 4, SYSTIMESTAMP - INTERVAL '1'  DAY, 300.00, 'NA',   'web');

COMMIT;

-- Optional: create a read-only user for QueryMind to connect as.
-- Run as SYS or SYSTEM:
--   CREATE USER querymind_ro IDENTIFIED BY "replace-me";
--   GRANT CREATE SESSION TO querymind_ro;
--   GRANT SELECT ON querymind.customers TO querymind_ro;
--   GRANT SELECT ON querymind.sales     TO querymind_ro;
