import logging

from app import db, notification_log
from test_admin import login
from test_analytics_banners_sync import place


def test_cleanup_preserves_pending_failures_history_and_deduplication(client, caplog):
    place(client)
    now = 1800000000
    old = now - 31 * 86400
    with db.connect(True) as c:
        initial_outbox = [r[0] for r in c.execute('SELECT text FROM outbox ORDER BY id')]
        aid = c.execute("INSERT INTO customer_accounts(platform,bot_key,user_id,chat_id,phone,verified_at) VALUES ('telegram','123',1,1,'+79991112233',?)", (now,)).lastrowid
        c.execute('INSERT INTO customer_orders VALUES (?,1)', (aid,))
        for sent, stamp, event in [(1,old,'old'), (1,now,'recent'), (0,old,'pending'), (-1,old,'failed'), (1,0,'legacy')]:
            c.execute('INSERT INTO outbox(chat_id,text,sent,last_attempt) VALUES (1,?,?,?)', (event,sent,stamp))
            c.execute('INSERT INTO customer_messages(account_id,order_id,event,text,sent,next_try) VALUES (?,1,?,?,?,?)', (aid,event,event,sent,stamp))
            c.execute('INSERT INTO max_order_messages(order_id,event,text,sent,next_try) VALUES (1,?,?,?,?)', (event,event,sent,stamp))
    caplog.set_level(logging.INFO)
    assert notification_log.prune_sent(now)=={'outbox':1,'customer_messages':1,'max_order_messages':1}
    assert notification_log.prune_sent(now)=={'outbox':0,'customer_messages':0,'max_order_messages':0}
    with db.connect(True) as c:
        for table in ('outbox','customer_messages','max_order_messages'):
            expected = (initial_outbox if table=='outbox' else []) + ['','recent','pending','failed','legacy']
            assert [r[0] for r in c.execute(f'SELECT text FROM {table} ORDER BY id')]==expected
        assert c.execute('SELECT count(*) FROM orders').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM customer_orders').fetchone()[0]==1
        c.execute("INSERT OR IGNORE INTO customer_messages(account_id,order_id,event,text) VALUES (?,1,'old','duplicate')", (aid,))
        c.execute("INSERT OR IGNORE INTO max_order_messages(order_id,event,text) VALUES (1,'old','duplicate')")
        assert c.execute('SELECT count(*) FROM customer_messages').fetchone()[0]==5
        assert c.execute('SELECT count(*) FROM max_order_messages').fetchone()[0]==5
    notification_log.delivery_result('telegram', 1, -1)
    assert 'state=-1' in caplog.text and '+79991112233' not in caplog.text


def test_diagnostics_is_under_settings_and_shows_only_actionable_messages(client):
    assert client.get('/admin/settings/diagnostics',follow_redirects=False).status_code==303
    login(client)
    with db.connect(True) as c:
        c.execute("INSERT INTO outbox(chat_id,text,sent) VALUES (1,'sent-secret-body',1),(1,'failed-message',-1)")
    page=client.get('/admin').text
    assert 'delivery-note' not in page and 'Посмотреть очередь' not in page
    assert '<span>Уведомления</span>' not in page and 'id="browser-notifications"' in page
    assert 'Диагностика' in client.get('/admin/settings').text
    page=client.get('/admin/settings/diagnostics').text
    assert 'failed-message' in page and 'sent-secret-body' not in page
    assert 'Повторить' in page and '30 дней' in page
