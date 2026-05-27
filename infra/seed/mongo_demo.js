// Sample analytics dataset for QueryMind demos (MongoDB flavor).
//
// Auto-loaded by the qm_mongodb container via the
// /docker-entrypoint-initdb.d mount in docker-compose.dev.yml. The mongo
// image runs *.js files with mongosh against the admin DB after the root
// user is created, so we explicitly switch to sales_demo with
// getSiblingDB() and the numeric ids match the relational seed for parity.
//
// To re-seed manually:
//   mongosh "mongodb://querymind:querymind@127.0.0.1:27017/?authSource=admin" \
//     --file infra/seed/mongo_demo.js

const db = db.getSiblingDB("sales_demo");

db.customers.drop();
db.sales.drop();

const now = new Date();
function daysAgo(n) {
    return new Date(now.getTime() - n * 24 * 60 * 60 * 1000);
}

db.customers.insertMany([
    { customer_id: 1, name: "Alice", segment: "enterprise" },
    { customer_id: 2, name: "Bob",   segment: "standard" },
    { customer_id: 3, name: "Carol", segment: "enterprise" },
    { customer_id: 4, name: "Dan",   segment: "standard" }
]);

db.sales.insertMany([
    { order_id: 1, customer_id: 1, ts: daysAgo(28), amount: 50.00,  region: "NA",   channel: "web" },
    { order_id: 2, customer_id: 2, ts: daysAgo(25), amount: 100.00, region: "NA",   channel: "web" },
    { order_id: 3, customer_id: 3, ts: daysAgo(20), amount: 25.00,  region: "EU",   channel: "partner" },
    { order_id: 4, customer_id: 4, ts: daysAgo(14), amount: 200.00, region: "EU",   channel: "web" },
    { order_id: 5, customer_id: 1, ts: daysAgo(10), amount: 75.00,  region: "APAC", channel: "web" },
    { order_id: 6, customer_id: 2, ts: daysAgo(7),  amount: 120.00, region: "APAC", channel: "partner" },
    { order_id: 7, customer_id: 3, ts: daysAgo(3),  amount: 60.00,  region: "EU",   channel: "web" },
    { order_id: 8, customer_id: 4, ts: daysAgo(1),  amount: 300.00, region: "NA",   channel: "web" }
]);

// Indexes that match the relational schema's primary/foreign keys.
db.customers.createIndex({ customer_id: 1 }, { unique: true });
db.sales.createIndex({ order_id: 1 }, { unique: true });
db.sales.createIndex({ customer_id: 1 });

print("Seeded sales_demo: customers=" + db.customers.countDocuments() +
      " sales=" + db.sales.countDocuments());
