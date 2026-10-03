"""Consistent display labels without changing product identities."""
import unicodedata


def clean_label(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())


def label_key(value):
    return clean_label(value).casefold().replace('ё', 'е')


def unique_labels(values):
    result, seen = [], set()
    for value in values:
        label = clean_label(value)
        key = label_key(label)
        if label and key not in seen:
            seen.add(key)
            result.append(label)
    return result
