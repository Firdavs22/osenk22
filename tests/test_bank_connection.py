import asyncio
import ssl

import httpx
import pytest

from app import bank_connection as connection, config, integrations
from test_integrations import create_online


def test_extra_ca_keeps_hostname_and_chain_verification(shop, monkeypatch):
    original = ssl.create_default_context
    class Context:
        def load_verify_locations(self, **kwargs):
            self.loaded = kwargs
    context = Context()
    monkeypatch.setattr(connection.ssl, 'create_default_context', lambda: context)
    monkeypatch.setattr(config, 'TBANK_CA_BUNDLE', '/app/data/tbank-ca.pem')
    assert connection.tls_context() is context
    assert context.loaded == {'cafile': '/app/data/tbank-ca.pem'}
    monkeypatch.setattr(connection.ssl, 'create_default_context', original)
    monkeypatch.setattr(config, 'TBANK_CA_BUNDLE', '')
    real = connection.tls_context()
    assert real.check_hostname and real.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize('response', [
    {'Success': False, 'ErrorCode': '999', 'Message': 'SECRET', 'Details': 'SECRET'},
    {'Success': False, 'ErrorCode': 'SECRET'},
    ['SECRET'],
])
def test_bank_responses_never_expose_provider_payloads(shop, monkeypatch, response):
    original = httpx.AsyncClient
    def client(**kwargs):
        assert isinstance(kwargs['verify'], ssl.SSLContext)
        assert kwargs['verify'].check_hostname
        assert kwargs['trust_env'] is False
        return original(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response)))
    monkeypatch.setattr(integrations.httpx, 'AsyncClient', client)
    with pytest.raises(connection.BankError) as error:
        asyncio.run(integrations.bank_call({'terminal': 'SECRET', 'password': 'SECRET'}, 'Init', {}))
    assert 'SECRET' not in str(error.value)


def test_tls_error_is_actionable_and_preserved_after_reconciliation(client, monkeypatch):
    oid = create_online(client)
    calls = []
    async def bank(cfg, method, data):
        calls.append(method)
        if method == 'Init':
            assert data['DATA'] == {'OperationInitiatorType': '0'}
            raise httpx.ConnectError('[SSL: CERTIFICATE_VERIFY_FAILED] SECRET')
        raise httpx.ReadTimeout('SECRET')
    monkeypatch.setattr(integrations, 'bank_call', bank)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(integrations.process_payment(oid))
    first = integrations.payment_row(oid)['init_error']
    assert 'TBANK_CA_BUNDLE' in first and 'SECRET' not in first
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(integrations.process_payment(oid))
    assert integrations.payment_row(oid)['init_error'] == first
    assert integrations.payment_row(oid)['payment_status'] == 'pending'
    assert calls == ['Init', 'CheckOrder']
