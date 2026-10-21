"""Structured checkout addresses with a readable snapshot for existing order views."""

FIELDS = {
    'city': ('Город', 100), 'street': ('Улица', 100), 'house': ('Дом', 10),
    'building': ('Корпус / строение', 10), 'flat': ('Квартира / офис', 10),
    'entrance': ('Подъезд', 10), 'floor': ('Этаж', 10), 'doorphone': ('Домофон', 10),
}
REQUIRED = ('city', 'street', 'house')


def normalize(parts):
    if not isinstance(parts, dict):
        raise ValueError('Заполните адрес доставки')
    result = {}
    for key, (label, limit) in FIELDS.items():
        value = parts.get(key, '')
        if not isinstance(value, str):
            raise ValueError(f'Проверьте поле «{label}»')
        value = ' '.join(value.split())
        if key in REQUIRED and not value:
            raise ValueError(f'Заполните поле «{label}»')
        if len(value) > limit:
            raise ValueError(f'Поле «{label}»: не более {limit} символов')
        result[key] = value
    if len(line1(result)) > 250 or len(full(result)) > 400:
        raise ValueError('Сократите адрес доставки')
    return result


def line1(parts):
    result = [parts['city'], parts['street'], 'д. ' + parts['house']]
    if parts.get('building'):
        result.append('корп. / стр. ' + parts['building'])
    return ', '.join(result)


def full(parts):
    result = [line1(parts)]
    for key, label in (('flat', 'кв. / офис'), ('entrance', 'подъезд'),
                       ('floor', 'этаж'), ('doorphone', 'домофон')):
        if parts.get(key):
            result.append(label + ' ' + parts[key])
    return ', '.join(result)


def for_iiko(parts):
    parts = normalize(parts)
    return {'type': 'city', 'line1': line1(parts),
            **{k: parts[k] for k in ('flat', 'entrance', 'floor', 'doorphone') if parts[k]}}
