"""Additive, repeatable migrations. Existing catalog and orders are preserved."""

DEFAULTS = {
    'tagline': 'Суши, роллы и маленькие поводы собраться', 'logo': '',
    'hero_mode': 'static', 'delivery_area': '', 'legal_name': '', 'legal_details': '',
    'privacy_text': '', 'offer_text': '',
    'legal_email': '', 'legal_address': '', 'pickup_discount': '0', 'delivery_districts': '0',
    'card_on_receipt': '0', 'delivery_time': '',
}


def migrate(c):
    first_accounts_upgrade = not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_accounts'").fetchone()
    c.execute('''CREATE TABLE IF NOT EXISTS outbox(
        id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,text TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,sent INTEGER NOT NULL DEFAULT 0)''')
    columns = {
        'products': {'tags': "TEXT NOT NULL DEFAULT ''", 'iiko_id': "TEXT NOT NULL DEFAULT ''", 'iiko_size': "TEXT NOT NULL DEFAULT ''", 'iiko_source': "TEXT NOT NULL DEFAULT ''"},
        'orders': {'channel': "TEXT NOT NULL DEFAULT 'telegram'", 'payment_method': "TEXT NOT NULL DEFAULT 'cash'",
                   'payment_status': "TEXT NOT NULL DEFAULT 'unpaid'", 'public_token': 'TEXT',
                   'consent_at': "TEXT NOT NULL DEFAULT ''", 'notified': 'INTEGER NOT NULL DEFAULT 1',
                   'discount': 'INTEGER NOT NULL DEFAULT 0', 'district': "TEXT NOT NULL DEFAULT ''",
                   'legal_snapshot': "TEXT NOT NULL DEFAULT ''", 'phone_key': "TEXT NOT NULL DEFAULT ''"},
        'order_items': {'product_id': 'INTEGER', 'iiko_id': "TEXT NOT NULL DEFAULT ''", 'iiko_size': "TEXT NOT NULL DEFAULT ''"},
        'outbox': {'error': "TEXT NOT NULL DEFAULT ''", 'last_attempt': 'REAL NOT NULL DEFAULT 0',
                   'subscription_order': 'INTEGER', 'subscription_bot': 'INTEGER'},
    }
    columns['products'].update({k: "TEXT NOT NULL DEFAULT ''" for k in ('allergens','nutrition','storage')})
    columns['products'].update({'iiko_available':'INTEGER NOT NULL DEFAULT 1',
        'iiko_resume_active':'INTEGER NOT NULL DEFAULT 0',
        'iiko_photo':"TEXT NOT NULL DEFAULT ''", 'iiko_photo_url':"TEXT NOT NULL DEFAULT ''"})
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
    existing = {row['name'] for row in c.execute('PRAGMA table_info(menu_images)')}
    for name, definition in {'error':"TEXT NOT NULL DEFAULT ''", 'job_token':"TEXT NOT NULL DEFAULT ''",
                             'etag':"TEXT NOT NULL DEFAULT ''", 'last_modified':"TEXT NOT NULL DEFAULT ''",
                             'started':'REAL NOT NULL DEFAULT 0'}.items():
        if name not in existing:
            c.execute(f'ALTER TABLE menu_images ADD COLUMN {name} {definition}')
    c.execute("""CREATE TABLE IF NOT EXISTS menu_sync(
        id INTEGER PRIMARY KEY CHECK(id=1), next_run REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
        last_success TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')""")
    c.execute('INSERT OR IGNORE INTO menu_sync(id) VALUES (1)')
    c.execute('CREATE TABLE IF NOT EXISTS telegram_screens(bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,messages TEXT NOT NULL,PRIMARY KEY(bot_id,chat_id))')
    c.execute("CREATE TABLE IF NOT EXISTS telegram_runtime(id INTEGER PRIMARY KEY CHECK(id=1),bot_id INTEGER NOT NULL,username TEXT NOT NULL,fingerprint TEXT NOT NULL,heartbeat REAL NOT NULL DEFAULT 0)")
    c.execute('CREATE TABLE IF NOT EXISTS order_subscriptions(order_id INTEGER PRIMARY KEY REFERENCES orders(id),chat_id INTEGER NOT NULL,bot_id INTEGER NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS order_subscription_tickets(token_hash TEXT PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),bot_id INTEGER NOT NULL,expires REAL NOT NULL)')
    c.execute("""CREATE TABLE IF NOT EXISTS max_events(
        id TEXT PRIMARY KEY,bot_key TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
        next_try REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')""")
    c.execute('CREATE TABLE IF NOT EXISTS max_screens(bot_key TEXT NOT NULL,chat_id INTEGER NOT NULL,message_id TEXT NOT NULL,PRIMARY KEY(bot_key,chat_id))')
    c.execute('''CREATE TABLE IF NOT EXISTS customer_accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,platform TEXT NOT NULL,bot_key TEXT NOT NULL,
        user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,phone TEXT NOT NULL,verified_at REAL NOT NULL,
        notifications INTEGER NOT NULL DEFAULT 1,UNIQUE(platform,bot_key,user_id),UNIQUE(platform,bot_key,phone))''')
    c.execute('''CREATE TABLE IF NOT EXISTS customer_contact_requests(
        platform TEXT NOT NULL,bot_key TEXT NOT NULL,user_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,
        expires REAL NOT NULL,PRIMARY KEY(platform,bot_key,user_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS customer_orders(
        account_id INTEGER NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
        order_id INTEGER NOT NULL REFERENCES orders(id),PRIMARY KEY(account_id,order_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS customer_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,account_id INTEGER NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
        order_id INTEGER NOT NULL REFERENCES orders(id),event TEXT NOT NULL,text TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',UNIQUE(account_id,order_id,event))''')
    c.execute('CREATE INDEX IF NOT EXISTS orders_phone_key ON orders(phone_key,id)')
    if first_accounts_upgrade:
        # Older versions could track a history view as navigation. Forget ownership
        # once, preserving those existing messages rather than deleting them later.
        c.execute('DELETE FROM telegram_screens')
    if 'phone' in {r['name'] for r in c.execute('PRAGMA table_info(orders)')}:
        from .customer_accounts import normalize_phone
        for row in c.execute("SELECT id,phone FROM orders WHERE phone_key=''").fetchall():
            c.execute('UPDATE orders SET phone_key=? WHERE id=?',(normalize_phone(row['phone']),row['id']))
    c.executemany('INSERT OR IGNORE INTO settings VALUES (?,?)', DEFAULTS.items())
    if not c.execute('SELECT 1 FROM slides LIMIT 1').fetchone():
        c.execute('INSERT INTO slides(title,subtitle) VALUES (?,?)',
                  ('У каждого вечера свой вкус.', 'Любимые роллы, нежный лосось и сеты для тех, кто рядом. Выбирайте — мы приготовим.'))
