"""Additive, repeatable migrations. Existing catalog and orders are preserved."""

DEFAULTS = {
    'tagline': 'Суши, роллы и маленькие поводы собраться', 'logo': '',
    'hero_mode': 'static', 'delivery_area': '', 'legal_name': '', 'legal_details': '',
    'privacy_text': '', 'offer_text': '',
    'legal_email': '', 'legal_address': '', 'pickup_discount': '0', 'delivery_districts': '0',
    'card_on_receipt': '0', 'delivery_time': '',
}


def migrate(c):
    columns = {
        'products': {'tags': "TEXT NOT NULL DEFAULT ''", 'iiko_id': "TEXT NOT NULL DEFAULT ''", 'iiko_size': "TEXT NOT NULL DEFAULT ''", 'iiko_source': "TEXT NOT NULL DEFAULT ''"},
        'orders': {'channel': "TEXT NOT NULL DEFAULT 'telegram'", 'payment_method': "TEXT NOT NULL DEFAULT 'cash'",
                   'payment_status': "TEXT NOT NULL DEFAULT 'unpaid'", 'public_token': 'TEXT',
                   'consent_at': "TEXT NOT NULL DEFAULT ''", 'notified': 'INTEGER NOT NULL DEFAULT 1',
                   'discount': 'INTEGER NOT NULL DEFAULT 0', 'district': "TEXT NOT NULL DEFAULT ''",
                   'legal_snapshot': "TEXT NOT NULL DEFAULT ''"},
        'order_items': {'product_id': 'INTEGER', 'iiko_id': "TEXT NOT NULL DEFAULT ''", 'iiko_size': "TEXT NOT NULL DEFAULT ''"},
    }
    columns['products'].update({k: "TEXT NOT NULL DEFAULT ''" for k in ('allergens','nutrition','storage')})
    for table, fields in columns.items():
        existing = {row['name'] for row in c.execute(f'PRAGMA table_info({table})')}
        for name, definition in fields.items():
            if name not in existing:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    # execute statements individually: executescript implicitly commits a migration transaction.
    for sql in [
        'CREATE TABLE IF NOT EXISTS menu_imports(token TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL, created REAL NOT NULL, result TEXT)',
        "CREATE TABLE IF NOT EXISTS menu_images(product_id INTEGER PRIMARY KEY REFERENCES products(id), url TEXT NOT NULL, source TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending')",
        'CREATE UNIQUE INDEX IF NOT EXISTS orders_public ON orders(public_token)',
        "CREATE TABLE IF NOT EXISTS slides(id INTEGER PRIMARY KEY, title TEXT NOT NULL, subtitle TEXT NOT NULL DEFAULT '', button TEXT NOT NULL DEFAULT 'Выбрать роллы', target TEXT NOT NULL DEFAULT '#catalog', photo TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)",
        'CREATE TABLE IF NOT EXISTS integrations(name TEXT PRIMARY KEY, config TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS web_limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL)',
        "CREATE TABLE IF NOT EXISTS payments(order_id INTEGER PRIMARY KEY REFERENCES orders(id), bank_order TEXT UNIQUE NOT NULL, payment_id TEXT UNIQUE, url TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'new', attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', credentials TEXT NOT NULL, receipt TEXT NOT NULL, expires REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS iiko_jobs(order_id INTEGER PRIMARY KEY REFERENCES orders(id), external_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '', credentials TEXT NOT NULL DEFAULT '')",
    ]:
        c.execute(sql)
    c.executemany('INSERT OR IGNORE INTO settings VALUES (?,?)', DEFAULTS.items())
    if not c.execute('SELECT 1 FROM slides LIMIT 1').fetchone():
        c.execute('INSERT INTO slides(title,subtitle) VALUES (?,?)',
                  ('У каждого вечера свой вкус.', 'Любимые роллы, нежный лосось и сеты для тех, кто рядом. Выбирайте — мы приготовим.'))
