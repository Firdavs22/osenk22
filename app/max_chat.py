"""Native MAX checkout. Local state changes and event receipts commit atomically."""
import hashlib
import re
import secrets
import time

from . import db, config
from .customer_accounts import normalize_phone
from .mini_apps import public_origin
from .shop_policy import DISTRICTS
from .vault import seal, unseal

SIMPLE = {'menu','cart','checkout','cancel','clear','skip','privacy'}
PATTERN = r'(?:menu|cart|product|add|plus|minus|remove|district):[0-9]{1,10}|cat:[0-9]{1,10}:[0-9]{1,10}|method:(?:pickup|delivery)|payment:(?:cash|card)|confirm:[a-f0-9]{32}'


def handles(action):
    return action in SIMPLE or bool(re.fullmatch(PATTERN,action)) or action=='text'


def button(text, payload):
    return {'type':'callback','text':text[:128],'payload':payload}


def body(text, rows):
    return {'text':text[:3900],'attachments':[{'type':'inline_keyboard','payload':{'buttons':rows}}]}


def home():
    return [[button('🍣 Категории','menu'),button('🛒 Корзина','cart')],
            [button('📦 Мои заказы','orders'),button('👤 Личный кабинет','account')]]


def session(c, key, uid, chat):
    c.execute('INSERT OR IGNORE INTO max_chat_sessions(bot_key,user_id,chat_id,data) VALUES (?,?,?,?)',(key,uid,chat,seal({})))
    row=c.execute('SELECT * FROM max_chat_sessions WHERE bot_key=? AND user_id=?',(key,uid)).fetchone()
    if row['chat_id']!=chat:
        raise ValueError('Для заказа откройте личный чат с ботом.')
    return row


def save(c, sid, step, data):
    c.execute('UPDATE max_chat_sessions SET step=?,data=?,expires=? WHERE id=?',(step,seal(data),time.time()+3600,sid))


def catalog(c, cfg, page=0):
    cats=c.execute('SELECT * FROM categories WHERE EXISTS(SELECT 1 FROM products p WHERE p.category_id=categories.id AND p.active=1) ORDER BY id').fetchall()
    page=min(page,max(0,(len(cats)-1)//20))
    rows=[[button(cat['name'],f'cat:{cat["id"]}:0')] for cat in cats[page*20:page*20+20]]
    nav=[]
    if page:nav.append(button('← Назад',f'menu:{page-1}'))
    if (page+1)*20<len(cats):nav.append(button('Ещё категории →',f'menu:{page+1}'))
    if nav:rows.append(nav)
    from .max_bot import navigation
    rows.extend(navigation(cfg)['attachments'][0]['payload']['buttons'])
    return body('🍣 Выберите категорию. Заказ можно полностью оформить здесь, в чате.' if cats else 'В меню пока нет доступных блюд.',rows)


def cart_view(c, user, page=0):
    items=db.cart(user,c)
    if not items:return body('Корзина пуста. Выберите блюда в меню.',home())
    page=min(page,max(0,(len(items)-1)//8))
    shown=items[page*8:page*8+8]
    text='🛒 Корзина\n\n'+'\n'.join(f'{p["name"]} × {p["quantity"]} — {db.money(p["price"]*p["quantity"])}'+(' · Недоступно' if not p['active'] else '') for p in shown)
    text+='\n\nТовары: '+db.money(sum(p['price']*p['quantity'] for p in items))+'\nСкидка и доставка рассчитаются перед подтверждением.'
    rows=[[button('− '+p['name'][:24],f'minus:{p["id"]}'),button('+ '+str(p['quantity']),f'plus:{p["id"]}'),button('Убрать',f'remove:{p["id"]}')] for p in shown]
    nav=[]
    if page:nav.append(button('← Назад',f'cart:{page-1}'))
    if (page+1)*8<len(items):nav.append(button('Ещё →',f'cart:{page+1}'))
    if nav:rows.append(nav)
    return body(text,rows+[[button('Оформить заказ','checkout'),button('Очистить','clear')]]+home())


def change(c, user, pid, delta):
    p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
    if not p or (delta>0 and not p['active']):raise ValueError('Блюдо сейчас недоступно.')
    row=c.execute('SELECT quantity FROM cart WHERE user_id=? AND product_id=?',(user,pid)).fetchone()
    qty=(row['quantity'] if row else 0)+delta
    if qty>99:raise ValueError('Максимум 99 порций одного блюда.')
    if not row and qty>0 and len(db.cart(user,c))>=30:raise ValueError('В корзине может быть максимум 30 разных блюд.')
    if qty<=0:c.execute('DELETE FROM cart WHERE user_id=? AND product_id=?',(user,pid))
    else:c.execute('INSERT INTO cart VALUES (?,?,?) ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=excluded.quantity',(user,pid,qty))


def comment_prompt(c, sid, data):
    save(c,sid,'comment',data)
    return body('Комментарий: количество приборов, пожелания к заказу. Напишите текст или нажмите «Без комментария».',
                [[button('Без комментария','skip')],[button('Отмена','cancel')]])


def payment_prompt(c, sid, data):
    save(c,sid,'payment',data)
    rows=[[button('Наличными при получении','payment:cash')]]
    if db.settings(c).get('card_on_receipt')=='1':rows.append([button('Картой при получении','payment:card')])
    return body('Выберите способ оплаты. Оплата в этом сценарии — при получении заказа.',rows+[[button('Отмена','cancel')]])


def preview(c, sid, user, data, cfg):
    q=db.quote(user,data,c)
    data.update(token=secrets.token_hex(16),fingerprint=q['fingerprint'])
    save(c,sid,'confirm',data)
    # Up to 30 lines: bounded names keep the entire total/contact block visible.
    items=[dict(p,name=p['name'][:38]) for p in q['items']]
    text=db.order_summary(dict(data,id='на подтверждении',status='new',discount=q['discount'],delivery=q['delivery'],total=q['total'],currency=q['currency']),items)
    text+='\n\nПодтверждая заказ, вы соглашаетесь с условиями заказа и передаёте указанные контакты магазину для его выполнения.'
    origin=public_origin(cfg.get('public_url'))
    return body(text,[[button('✅ Подтвердить заказ','confirm:'+data['token'])],
        [{'type':'link','text':'Условия заказа','url':origin+'/legal/offer'},
         {'type':'link','text':'Обработка данных','url':origin+'/legal/privacy'}],
        [button('Корзина','cart'),button('Отмена','cancel')]])


def confirm(c, sess, user, key, token, step, data):
    order_token='max:'+key+':'+str(sess['user_id'])+':'+token
    old=c.execute('SELECT id FROM orders WHERE token=? AND user_id=?',(order_token,user)).fetchone()
    if old:return old['id']
    if step!='confirm' or data.get('token')!=token:raise ValueError('Это старое подтверждение. Откройте корзину и оформите заказ заново.')
    q=db.quote(user,data,c)
    if q['fingerprint']!=data['fingerprint']:raise ValueError('Цены, корзина или условия изменились. Откройте корзину и проверьте новый итог.')
    if data.get('payment_method') not in ('cash','card') or (data['payment_method']=='card' and db.settings(c).get('card_on_receipt')!='1'):
        raise ValueError('Выберите доступный способ оплаты заново.')
    if not all(data.get(k) for k in ('customer','phone','address')):raise ValueError('Не заполнены контактные данные.')
    oid=c.execute('''INSERT INTO orders(token,user_id,customer,phone,method,address,comment,subtotal,delivery,total,currency,
        channel,payment_method,public_token,consent_at,notified,discount,district,legal_snapshot)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,'max',?,?,strftime('%Y-%m-%d %H:%M:%S','now'),0,?,?,?)''',
        (order_token,user,data['customer'],data['phone'],data['method'],data['address'],data.get('comment',''),q['subtotal'],q['delivery'],q['total'],q['currency'],
         data['payment_method'],secrets.token_urlsafe(32),q['discount'],q['district'],q['legal_snapshot'])).lastrowid
    c.executemany('INSERT INTO order_items(order_id,name,price,quantity,product_id,iiko_id,iiko_size) VALUES (?,?,?,?,?,?,?)',
        [(oid,p['name'],p['price'],p['quantity'],p['id'],p['iiko_id'],p['iiko_size']) for p in q['items']])
    c.execute('INSERT INTO max_order_recipients VALUES (?,?,?,?,?)',(oid,key,sess['user_id'],sess['chat_id'],sess['notifications']))
    from .customer_accounts import new_order
    from .store import notify_order
    new_order(c,oid);notify_order(c,oid)
    order=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    queue(c,order,created=True)
    db.auto_accept(c,oid)
    c.execute('DELETE FROM cart WHERE user_id=?',(user,))
    save(c,sess['id'],'',{})
    return oid


def handle(cfg, event):
    """Persist response before network I/O: replay cannot repeat cart/draft mutations."""
    from .max_bot import bot_key
    key=bot_key(cfg)
    uid,chat=event.get('user_id'),event['chat_id']
    if type(uid) is not int or uid<=0:
        with db.connect() as c:return catalog(c,cfg)
    identity=f'{key}:{uid}:{chat}:{event["kind"]}:{event["identity"]}'
    eid=hashlib.sha256(identity.encode()).hexdigest()
    with db.connect(True) as c:
        previous=c.execute('SELECT response FROM max_chat_results WHERE id=?',(eid,)).fetchone()
        if previous:return unseal(previous['response'])
        sess=session(c,key,uid,chat)
        user=-(2**62+sess['id'])  # Disjoint from Telegram (>0) and web ([-2**62,-1]).
        step,data=(sess['step'],unseal(sess['data'])) if sess['expires']>time.time() else ('',{})
        c.execute('SAVEPOINT action')
        try:
            result=dispatch(c,cfg,event,sess,user,key,step,data)
        except (ValueError,IndexError) as exc:
            c.execute('ROLLBACK TO action')
            result=body(str(exc) if isinstance(exc,ValueError) else 'Выберите район кнопкой из списка.',home())
        finally:
            c.execute('RELEASE action')
        c.execute('DELETE FROM max_chat_results WHERE created<?',(time.time()-7*86400,))
        c.execute('INSERT INTO max_chat_results VALUES (?,?,?)',(eid,seal(result),time.time()))
        return result


def dispatch(c,cfg,event,sess,user,key,step,data):
    action=event['action'];sid=sess['id'];s=db.settings(c)
    if action in ('menu','cancel','privacy') or action.startswith('menu:'):
        save(c,sid,'',{})
        if action=='privacy':
            origin=public_origin(cfg.get('public_url'))
            return body('Для заказа магазин сохраняет имя, телефон, адрес, состав заказа и ваш идентификатор MAX. Корзина чата отдельная от мини-приложения.',
                [[{'type':'link','text':'Политика обработки данных','url':origin+'/legal/privacy'},
                  {'type':'link','text':'Условия заказа','url':origin+'/legal/offer'}]]+home())
        return catalog(c,cfg,int(action.split(':')[1]) if ':' in action else 0)
    if action.startswith('cat:'):
        _,cat,page=action.split(':');page=int(page)
        items=c.execute('SELECT * FROM products WHERE category_id=? AND active=1 ORDER BY id',(int(cat),)).fetchall()
        page=min(page,max(0,(len(items)-1)//8))
        rows=[[button(p['name'][:75]+' · '+db.money(p['price']),f'product:{p["id"]}')] for p in items[page*8:page*8+8]]
        nav=[]
        if page:nav.append(button('← Назад',f'cat:{cat}:{page-1}'))
        if (page+1)*8<len(items):nav.append(button('Ещё →',f'cat:{cat}:{page+1}'))
        return body('Выберите блюдо.' if items else 'Доступных блюд пока нет.',rows+([nav] if nav else [])+home())
    if action.startswith('product:'):
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(int(action.split(':')[1]),)).fetchone()
        if not p:raise ValueError('Блюдо сейчас недоступно.')
        text=f'{p["name"]}\n{db.money(p["price"])} · {p["weight"]}\n\nСостав: {p["ingredients"]}\n{p["description"]}'
        result=body(text,[[button('➕ В корзину',f'add:{p["id"]}')]]+home())
        origin=public_origin(cfg.get('public_url'))
        if origin and re.fullmatch(r'[a-f0-9]{32}\.jpg',p['photo']) and (config.MEDIA/p['photo']).is_file():
            result['attachments'].insert(0,{'type':'image','payload':{'url':origin+'/assets/'+p['photo']}})
        return result
    if action=='clear':
        c.execute('DELETE FROM cart WHERE user_id=?',(user,));save(c,sid,'',{})
        return cart_view(c,user)
    if action=='cart' or action.startswith('cart:'):
        save(c,sid,'',{})
        return cart_view(c,user,int(action.split(':')[1]) if ':' in action else 0)
    if action.split(':')[0] in ('add','plus','minus','remove'):
        kind,pid=action.split(':')
        change(c,user,int(pid),{'add':1,'plus':1,'minus':-1,'remove':-99}[kind]);save(c,sid,'',{})
        return body('Добавлено в корзину ✓',home()) if kind=='add' else cart_view(c,user)
    if action=='checkout':
        db.quote(user,{'method':'pickup'},c);save(c,sid,'method',{})
        rows=[[button('Самовывоз','method:pickup')]]
        if s['delivery_enabled']=='1':rows.insert(0,[button('Доставка','method:delivery')])
        return body('Как вам удобно получить заказ?',rows+[[button('Отмена','cancel')]])
    if action.startswith('method:'):
        if step!='method':raise ValueError('Начните оформление через корзину.')
        data['method']=action.split(':')[1]
        if data['method']=='delivery' and s['delivery_enabled']!='1':raise ValueError('Доставка сейчас недоступна.')
        if data['method']=='delivery' and s.get('delivery_districts')=='1':
            save(c,sid,'district',data)
            return body('Выберите район доставки. Другие районы — по согласованию с магазином: '+s['phone'],
                [[button(d,f'district:{i}')] for i,d in enumerate(DISTRICTS)]+[[button('Отмена','cancel')]])
        db.quote(user,data,c);save(c,sid,'customer',data)
        return body('Как к вам обращаться? Напишите имя.',[[button('Отмена','cancel')]])
    if action.startswith('district:'):
        if step!='district':raise ValueError('Начните оформление через корзину.')
        data['district']=DISTRICTS[int(action.split(':')[1])]
        db.quote(user,data,c);save(c,sid,'customer',data)
        return body('Как к вам обращаться? Напишите имя.',[[button('Отмена','cancel')]])
    if action=='skip':
        if step!='comment':raise ValueError('Эта кнопка устарела.')
        data['comment']='';return payment_prompt(c,sid,data)
    if action.startswith('payment:'):
        if step!='payment':raise ValueError('Начните оформление через корзину.')
        data['payment_method']=action.split(':')[1]
        if data['payment_method']=='card' and s.get('card_on_receipt')!='1':raise ValueError('Оплата картой при получении недоступна.')
        return preview(c,sid,user,data,cfg)
    if action.startswith('confirm:'):
        oid=confirm(c,sess,user,key,action.split(':')[1],step,data)
        return body(f'Заказ №{oid} получен! Спасибо, что выбрали нас! Подробности придут отдельным сообщением; история — /orders.',home())
    if action in ('text','contact'):
        text=unseal(event['text']) if event.get('text') else ''
        if step=='customer':
            if not 2<=len(text)<=80:raise ValueError('Введите имя от 2 до 80 символов.')
            data['customer']=text;save(c,sid,'phone',data)
            return body('Введите телефон для связи или отправьте собственный контакт. Это контакт заказа; для входа в личный кабинет используйте /account.',
                [[{'type':'request_contact','text':'Мой телефон'}],[button('Отмена','cancel')]])
        if step=='phone':
            if action=='contact':
                if not event.get('contact'):raise ValueError('Не удалось проверить контакт. Нажмите «Мой телефон» или введите номер вручную для этого заказа.')
                text=unseal(event['contact'])
            phone=normalize_phone(text)
            if not phone:raise ValueError('Введите телефон: от 10 до 15 цифр, можно с + в начале.')
            data['phone']=phone
            if data['method']=='delivery':
                save(c,sid,'address',data)
                return body('Напишите адрес: город, улица, дом, квартира, подъезд, этаж.',[[button('Отмена','cancel')]])
            if not s['address']:raise ValueError('Адрес самовывоза пока не указан. Свяжитесь с магазином.')
            data['address']=s['address'];return comment_prompt(c,sid,data)
        if step=='address':
            if not 10<=len(text)<=250:raise ValueError('Введите полный адрес, от 10 до 250 символов.')
            data['address']=text;return comment_prompt(c,sid,data)
        if step=='comment':
            if not 1<=len(text)<=500:raise ValueError('Комментарий: от 1 до 500 символов, или нажмите «Без комментария».')
            data['comment']=text;return payment_prompt(c,sid,data)
        if step=='confirm':return preview(c,sid,user,data,cfg)
        return body('Используйте кнопки последнего шага или /cart для оформления заново.',home())
    return catalog(c,cfg)


def queue(c, order, created=False):
    recipient=c.execute('SELECT * FROM max_order_recipients WHERE order_id=?',(order['id'],)).fetchone()
    if not recipient or (not created and not recipient['notifications']):return
    text=('Спасибо! Заказ получен, спасибо, что выбрали нас!\n\n'+db.order_summary(order,
        c.execute('SELECT * FROM order_items WHERE order_id=?',(order['id'],)).fetchall())) if created else f'Заказ №{order["id"]}: {db.STATUSES[order["status"]]}.'
    text+='\nИстория: /orders. Отключить статусы: /stopupdates.'
    for part,offset in enumerate(range(0,len(text),3500)):
        event=('created' if created else order['status'])+':'+str(part)
        c.execute('INSERT OR IGNORE INTO max_order_messages(order_id,event,text) VALUES (?,?,?)',(order['id'],event,text[offset:offset+3500]))


def reset(key, uid):
    with db.connect(True) as c:
        c.execute("UPDATE max_chat_sessions SET step='',data=?,expires=0 WHERE bot_key=? AND user_id=?",(seal({}),key,uid))


def notifications(key, uid, enabled):
    with db.connect(True) as c:
        c.execute('UPDATE max_chat_sessions SET notifications=? WHERE bot_key=? AND user_id=?',(int(enabled),key,uid))
        c.execute('UPDATE max_order_recipients SET notifications=? WHERE bot_key=? AND user_id=?',(int(enabled),key,uid))
        if not enabled:c.execute('DELETE FROM max_order_messages WHERE sent<>1 AND order_id IN (SELECT order_id FROM max_order_recipients WHERE bot_key=? AND user_id=?)',(key,uid))


def claim(key):
    with db.connect(True) as c:
        now=time.time()
        c.execute("UPDATE max_order_messages SET sent=-1,error='Исчерпаны попытки' WHERE sent=0 AND attempts>=8 AND next_try<=?",(now,))
        row=c.execute('''SELECT m.*,r.chat_id FROM max_order_messages m JOIN max_order_recipients r ON r.order_id=m.order_id
            WHERE r.bot_key=? AND (r.notifications=1 OR m.event LIKE 'created:%') AND m.sent=0 AND m.attempts<8 AND m.next_try<=?
            AND NOT EXISTS(SELECT 1 FROM max_order_messages older JOIN max_order_recipients ro ON ro.order_id=older.order_id
                WHERE older.sent=0 AND older.id<m.id AND ro.bot_key=r.bot_key AND ro.user_id=r.user_id)
            ORDER BY m.id LIMIT 1''',(key,now)).fetchone()
        if row:c.execute('UPDATE max_order_messages SET next_try=?,attempts=attempts+1 WHERE id=?',(now+60,row['id']))
        return row


def complete(mid,state,error='',delay=30):
    with db.connect(True) as c:c.execute('UPDATE max_order_messages SET sent=?,error=?,next_try=? WHERE id=?',(state,error,time.time()+delay,mid))
