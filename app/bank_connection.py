"""Verified bank transport and diagnostics without credentials or provider payloads."""
import re
import ssl

import httpx

from . import config


class BankError(ValueError):
    pass


def tls_context():
    context = ssl.create_default_context()
    if config.TBANK_CA_BUNDLE:
        context.load_verify_locations(cafile=config.TBANK_CA_BUNDLE)
    return context


def diagnostic(exc):
    if isinstance(exc,BankError):
        return str(exc)
    cause,seen = exc,set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause,ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(cause):
            return 'Т-Банк: сервер не доверяет TLS-сертификату банка. Настройте TBANK_CA_BUNDLE: sudo bash deploy/setup-tbank-ca.sh.'
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc,(FileNotFoundError,PermissionError,ssl.SSLError)):
        return 'Т-Банк: проверьте файл TBANK_CA_BUNDLE, формат PEM и доступ пользователя sushi к файлу.'
    if isinstance(exc,httpx.TimeoutException):
        return 'Т-Банк: истекло время ожидания ответа. Исход платежа пока неизвестен; повторное создание не выполняется.'
    if isinstance(exc,httpx.HTTPStatusError):
        return f'Т-Банк: HTTP {exc.response.status_code}. Проверьте доступ VPS к API банка и настройки терминала.'
    if isinstance(exc,httpx.ConnectError):
        return 'Т-Банк: соединение не установлено. Проверьте DNS и исходящий HTTPS-доступ VPS к securepay.tinkoff.ru:443.'
    if type(exc) is ValueError:
        # Locally generated validation messages only; provider JSON is never passed here.
        return str(exc)
    return 'Т-Банк: нет подтверждения от сервиса. Проверка будет повторена автоматически.'


def provider_error(method,result):
    code = str(result.get('ErrorCode',''))
    code = code if re.fullmatch(r'[0-9]{1,8}',code) else 'не указан'
    return BankError(f'Т-Банк: {method} — запрос отклонён, код {code}. Проверьте терминал, пароль и параметры кассы в кабинете эквайринга.')
