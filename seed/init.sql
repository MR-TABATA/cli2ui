-- Sample data so `docker compose up` shows a real table list immediately.
CREATE TABLE customers (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    email       text UNIQUE NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id      serial PRIMARY KEY,
    name    text NOT NULL,
    price   numeric(10, 2) NOT NULL
);

CREATE TABLE orders (
    id           serial PRIMARY KEY,
    customer_id  int NOT NULL REFERENCES customers(id),
    total        numeric(10, 2) NOT NULL,
    placed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    id          serial PRIMARY KEY,
    order_id    int NOT NULL REFERENCES orders(id),
    product_id  int NOT NULL REFERENCES products(id),
    quantity    int NOT NULL DEFAULT 1
);

INSERT INTO customers (name, email)
SELECT 'Customer ' || g, 'customer' || g || '@example.com'
FROM generate_series(1, 50) AS g;

INSERT INTO products (name, price)
SELECT 'Product ' || g, (random() * 100 + 1)::numeric(10, 2)
FROM generate_series(1, 20) AS g;

INSERT INTO orders (customer_id, total)
SELECT (random() * 49 + 1)::int, (random() * 500 + 10)::numeric(10, 2)
FROM generate_series(1, 200) AS g;

INSERT INTO order_items (order_id, product_id, quantity)
SELECT (random() * 199 + 1)::int, (random() * 19 + 1)::int, (random() * 5 + 1)::int
FROM generate_series(1, 600) AS g;

-- Refresh planner stats so row estimates show up right away in the UI.
-- Note: the row counts shown are pg_stat estimates (what `\dt+`-style tooling
-- reports), not exact COUNT(*) — faithful to the DB, and cheap on big tables.
ANALYZE;
