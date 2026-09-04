"""Published signing vector and mocked live routing; never calls a broker."""
import json
from datetime import UTC, datetime

import pytest

from honest_alpha_lab.contracts import ContractError
from honest_alpha_lab.trade_control import TradeControl
from honest_alpha_lab.webull_paper import (
    DATA_HOST,
    LIVE_ARMING,
    PLACE,
    SNAPSHOT,
    WebullHKClient,
    signed_headers,
)


def test_official_public_documentation_signature_vector():
    # Public example credentials from Webull's signature documentation, not secrets.
    headers = signed_headers('/trade/place_order',
        {'a1': 'webull', 'a2': '123', 'a3': 'xxx', 'q1': 'yyy'},
        b'{"k1":123,"k2":"this is the api request body","k3":true,"k4":{"foo":[1,2]}}',
        app_key='776da210ab4a452795d74e726ebd74b6',
        app_secret='0f50a2e853334a9aae1a783bee120c1f', host='api.webull.com', token='fixture',
        timestamp='2022-01-04T03:55:31Z', nonce='48ef5afed43d4d91ae514aaeafbc29ba')
    assert headers['x-signature'] == 'kvlS6opdZDhEBo5jq40nHYXaLvM='
    assert 'x-app-secret' not in headers


@pytest.mark.parametrize('quote_age', [0, 120])
def test_live_routing_checks_broker_quote_before_order(tmp_path, monkeypatch, quote_age):
    monkeypatch.setenv('HAL_ENABLE_LIVE_TRADING', LIVE_ARMING)
    for name in ('APP_KEY', 'APP_SECRET', 'ACCESS_TOKEN'):
        monkeypatch.setenv('HAL_WEBULL_LIVE_' + name, 'fixture-not-a-real-credential')
    calls = []

    def transport(method, host, path, query, body, headers):
        calls.append((method, host, path))
        assert host == DATA_HOST
        assert 'x-app-secret' not in headers
        if path == SNAPSHOT:
            return [{'symbol': 'AAPL', 'price': '100', 'last_trade_time': (datetime.now(UTC).timestamp()-quote_age)*1000}]
        assert path == PLACE and method == 'POST'
        assert json.loads(body)['account_id'] == 'fixture-account'
        return {'fixture': 'not a fill confirmation'}

    service = TradeControl(tmp_path / 'control.sqlite')
    service.configure_book('b', {'mode': 'live', 'approval_required': True, 'account_id': 'fixture-account',
        'symbols': ['AAPL'], 'max_order_notional': 200, 'max_daily_notional': 400})
    service.halt(False)
    service.propose({'id': 'test', 'book': 'b', 'symbol': 'AAPL', 'side': 'BUY', 'quantity': 1,
        'limit_price': 100, 'reference_price': 100, 'quote_at': datetime.now(UTC).isoformat(), 'rationale': 'test'})
    service.review('test', approve=True)
    factory = lambda **kwargs: WebullHKClient(transport=transport, **kwargs)
    if quote_age:
        with pytest.raises(ContractError, match='quote'):
            service.dispatch('test', client_factory=factory)
        assert len(calls) == 1
    else:
        assert service.dispatch('test', client_factory=factory)['state'] == 'response_received'
        assert len(calls) == 2
    with pytest.raises(ContractError):
        service.dispatch('test', client_factory=factory)
    assert len(calls) == (1 if quote_age else 2)
