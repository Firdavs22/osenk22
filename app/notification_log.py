import logging
import time

from . import db

log = logging.getLogger('uvicorn.error.notifications')


def delivery_result(queue, message_id, state):
    # Never copy message bodies, contacts, credentials or provider responses to logs.
    log.log(logging.WARNING if state == -1 else logging.INFO,
            'Notification queue=%s id=%s state=%s', queue, message_id, state)


def prune_sent(now=None):
    cutoff = (time.time() if now is None else now) - 30 * 86400
    counts = {}
    with db.connect(True) as c:
        for table, timestamp in (('outbox', 'last_attempt'),
                                 ('customer_messages', 'next_try'),
                                 ('max_order_messages', 'next_try')):
            # Keep event keys: deleting them could enqueue old notifications again.
            # Unknown legacy timestamps, pending jobs and failures stay recoverable.
            counts[table] = c.execute(
                f"UPDATE {table} SET text='',error='' WHERE sent=1 AND {timestamp}>0 "
                f"AND {timestamp}<? AND (text<>'' OR error<>'')", (cutoff,)).rowcount
    if any(counts.values()):
        log.info('Cleared delivered notification payloads: %s', counts)
    return counts
