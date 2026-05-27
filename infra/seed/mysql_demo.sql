-- Sample analytics dataset for QueryMind demos (MySQL flavor).
--
-- Auto-loaded by the qm_mysql container via the docker-entrypoint-initdb.d
-- mount in docker-compose.dev.yml. To re-seed manually:
--   mysql -h 127.0.0.1 -P 3306 -u querymind -pquerymind sales_demo \
--     < infra/seed/mysql_demo.sql
--
-- Then add a workspace in QueryMind pointing at this database.

CREATE DATABASE IF NOT EXISTS sales_demo
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE sales_demo;

DROP TABLE IF EXISTS sales;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255) NOT NULL,
    segment     VARCHAR(64)  NOT NULL DEFAULT 'standard'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sales (
    order_id    INT AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    ts          DATETIME NOT NULL,
    amount      DECIMAL(10,2) NOT NULL,
    region      VARCHAR(32) NOT NULL,
    channel     VARCHAR(32) NOT NULL DEFAULT 'web',
    CONSTRAINT fk_sales_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO customers (name, segment) VALUES
    ('Alice', 'enterprise'),
    ('Bob',   'standard'),
    ('Carol', 'enterprise'),
    ('Dan',   'standard');

INSERT INTO sales (customer_id, ts, amount, region, channel) VALUES
    (1, NOW() - INTERVAL 28 DAY, 50.00,  'NA',   'web'),
    (2, NOW() - INTERVAL 25 DAY, 100.00, 'NA',   'web'),
    (3, NOW() - INTERVAL 20 DAY, 25.00,  'EU',   'partner'),
    (4, NOW() - INTERVAL 14 DAY, 200.00, 'EU',   'web'),
    (1, NOW() - INTERVAL 10 DAY, 75.00,  'APAC', 'web'),
    (2, NOW() - INTERVAL  7 DAY, 120.00, 'APAC', 'partner'),
    (3, NOW() - INTERVAL  3 DAY, 60.00,  'EU',   'web'),
    (4, NOW() - INTERVAL  1 DAY, 300.00, 'NA',   'web');

-- Optional: create a read-only user for QueryMind to connect as.
-- Run as root:
--   CREATE USER 'querymind_ro'@'%' IDENTIFIED BY 'replace-me';
--   GRANT SELECT ON sales_demo.* TO 'querymind_ro'@'%';
--   FLUSH PRIVILEGES;
