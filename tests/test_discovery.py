"""Synthetic news-index contracts; real content never appears in offline fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import pytest
from stock_report.cli import main
from stock_report.discovery import (DiscoveryContractError, NewsClient, NoRedirect, aliases, canonical_url,
    discover, mentions, parse_results, query_url)
from stock_report.models import Request, ResearchInput
from stock_report.storage import read_json

FIXTURE = Path('examples/news/index-synthetic.json')
NOW = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)


def request(**changes):
    values = dict(mode='event', event_query='chip export policy', research_as_of=NOW)
    return Request(**(values | changes))


def payload():
    return deepcopy(read_json(FIXTURE)['payload'])


def test_fixture_discovers_and_deduplicates_without_verifying():
    result = discover(request(), fixture=FIXTURE)
    assert result['data_label'] == 'DEMO / SYNTHETIC DATA'
    assert result['counts']['rows_received'] == 4 and result['counts']['unique_leads'] == 3
    assert result['counts']['duplicates_removed'] == 1
    assert [r['entity_id'] for r in result['candidate_mentions']] == ['2330.TW', 'AMD', 'NVDA']
    assert all(r['relationship'] == 'unassessed' and r['verification_status'] == 'unverified'
               for r in result['leads'] + result['candidate_mentions'])
    assert result['verified_evidence_created'] == 0
    with pytest.raises(ValueError):
        ResearchInput.model_validate(result)


@pytest.mark.parametrize('text,expected', [
    ('NVIDIA TSMC', ['2330.TW', 'NVDA']), ('台積電與輝達', ['2330.TW', 'NVDA']),
    ('2330 6488 AMD NVDA', []), ('Advanced Micro Devices', ['AMD']),
    ('NOTNVIDIA NVIDIAish', []), ('$NVDA NASDAQ:AMD', ['AMD', 'NVDA']),
    ('＄ＮＶＤＡ', ['NVDA']), ('GlobalWafers', ['6488.TWO'])])
def test_company_mentions_are_explicit_bounded_tokens(text, expected):
    assert mentions(text, aliases())[0] == expected


def test_ambiguous_alias_cannot_select_a_company():
    config = aliases()
    config['entities'][0]['names'].append('NVIDIA')
    assert mentions('NVIDIA', config) == ([], 1)


def test_query_cannot_inject_operators_shell_or_other_endpoints():
    q = query_url(request(event_query='chips domain:evil.example OR $(touch /tmp/x)'), aliases(), 25, NOW)
    assert q.startswith('https://api.gdeltproject.org/api/v2/doc/doc?')
    params = parse_qs(urlsplit(q).query)
    assert params['query'] == ['"chips" "domain" "evil" "example" "OR" "touch" "tmp" "x"']
    assert params['maxrecords'] == ['25'] and params['enddatetime'] == ['20261114090000']
    assert set(params) == {'query', 'mode', 'format', 'maxrecords', 'sort', 'startdatetime', 'enddatetime'}


@pytest.mark.parametrize('changes', [{'research_as_of': NOW + timedelta(seconds=1)},
    {'lookback_days': 90}, {'event_query': '() $$$'}, {'event_query': 'x' * 41},
    {'event_query': ' '.join(['word'] * 13)}])
def test_unsupported_window_or_query_refused(changes):
    with pytest.raises(ValueError):
        query_url(request(**changes), aliases(), 50, NOW)


def test_supported_stock_query_and_unknown_stock_refusal():
    stock = Request(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of=NOW)
    assert parse_qs(urlsplit(query_url(stock, aliases(), 50, NOW)).query)['query'] == ['"NVIDIA"']
    with pytest.raises(ValueError, match='outside'):
        query_url(stock.model_copy(update={'ticker': 'UNKNOWN'}), aliases(), 50, NOW)


@pytest.mark.parametrize('url', ['file:///tmp/a', 'http://localhost/a', 'https://127.0.0.1/a',
    'https://user:secret@example.com/a', 'https://example.com:999/a', 'https://foo.internal/a',
    'https://example.com/\nunsafe', 'javascript:alert(1)'])
def test_article_urls_are_never_executable_or_local(url):
    with pytest.raises(DiscoveryContractError):
        canonical_url(url)


@pytest.mark.parametrize('field,value,reason', [
    ('title', '', 'invalid_title'), ('title', 'bad\ntext', 'invalid_title'),
    ('seendate', '20260230T090000Z', 'invalid_index_time'),
    ('seendate', '20261115T090000Z', 'outside_index_window'),
    ('seendate', '20250101T090000Z', 'outside_index_window'),
    ('domain', 'other.example.invalid', 'domain_mismatch'),
    ('url', 'file:///secret', 'invalid_article_url')])
def test_bad_rows_are_excluded_with_fixed_reasons(field, value, reason):
    body = {'articles': [payload()['articles'][0] | {field: value}]}
    leads, counts = parse_results(body, request(), aliases(), 50)
    assert not leads and counts['excluded_rows'] == {reason: 1}


def test_conflicting_duplicates_are_not_reconciled():
    body = payload()
    body['articles'][1]['title'] = 'Different synthetic title'
    _, counts = parse_results(body, request(), aliases(), 50)
    assert counts['excluded_rows']['conflicting_duplicate'] == 2


@pytest.mark.parametrize('body', [{}, {'articles': 'wrong'}, {'articles': [None] * 51}])
def test_bad_top_level_contract_refused(body):
    with pytest.raises(DiscoveryContractError):
        parse_results(body, request(), aliases(), 50)


def test_truncation_and_empty_are_explicit():
    _, counts = parse_results({'articles': payload()['articles'][:1]}, request(), aliases(), 1)
    assert counts['possibly_truncated']
    assert parse_results({'articles': []}, request(), aliases(), 50)[1]['unique_leads'] == 0


def test_network_requires_opt_in_and_never_falls_back():
    with pytest.raises(ValueError, match='allow-network'):
        discover(request(), now=NOW)


def test_real_content_and_candidate_names_never_leave_module():
    class Client:
        def get_json(self, url):
            return {'articles': [payload()['articles'][0] | {'title': 'NVIDIA REAL_SECRET_TEXT'}]}
    report = discover(request(), allow_network=True, client=Client(), now=NOW)
    text = json.dumps(report)
    assert report['status'] == 'inspection_only' and not report['leads_retained']
    assert report['counts']['unique_leads'] == 1
    for forbidden in ['REAL_SECRET_TEXT', 'NVIDIA', 'NVDA', 'news.example.invalid', 'sha256', 'candidate_mentions']:
        assert forbidden not in text
    assert report['license_status'] == 'unknown' and not report['internal_use_allowed']


@pytest.mark.parametrize('error', [HTTPError('https://secret.invalid', 429, 'SECRET', {}, None),
    URLError('SECRET'), DiscoveryContractError('SECRET')])
def test_network_failure_is_sanitized_and_has_no_fallback(error):
    class Client:
        def get_json(self, url):
            raise error
    report = discover(request(), allow_network=True, client=Client(), now=NOW)
    assert report['status'] == 'failed' and 'SECRET' not in json.dumps(report)
    assert 'leads' not in report


def test_redirects_and_arbitrary_query_endpoints_are_refused():
    with pytest.raises(DiscoveryContractError):
        NoRedirect().redirect_request(None, None, 302, '', {}, 'http://localhost/')
    with pytest.raises(DiscoveryContractError):
        NewsClient().get_json('https://example.invalid/anything')


@pytest.mark.parametrize('body,content_type', [(b'not json', 'application/json'),
    (b'{"articles":[]}', 'text/html'), (b'x' * 2_000_001, 'application/json')])
def test_transport_rejects_error_pages_bad_json_and_large_bodies(monkeypatch, body, content_type):
    class Response:
        headers = {'Content-Type': content_type}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return body[:limit]
    class Opener:
        def open(self, req, timeout):
            assert timeout == 20
            return Response()
    monkeypatch.setattr('stock_report.discovery.build_opener', lambda *args: Opener())
    with pytest.raises(DiscoveryContractError):
        NewsClient().get_json('https://api.gdeltproject.org/api/v2/doc/doc?query=test')


def test_plain_json_fixture_without_synthetic_declaration_is_refused(tmp_path):
    path = tmp_path / 'unlabelled.json'
    path.write_text(json.dumps(payload()))
    with pytest.raises(ValueError, match='explicitly declare'):
        discover(request(), fixture=path)


def test_cli_fixture_and_retention_controls(tmp_path, capsys):
    path = tmp_path / 'discovery.json'
    argv = ['news-discover', '--request', 'examples/event.json', '--fixture', str(FIXTURE), '--output', str(path)]
    assert main(argv) == 0
    report = read_json(path)
    assert report['status'] == 'synthetic_leads' and report['leads_retained']
    assert main(argv) == 1  # Existing results are not silently overwritten.
    assert main(argv + ['--allow-network']) == 1


@pytest.mark.network
def test_live_index_contract_without_source_retention():
    report = discover(request(research_as_of=datetime.now(timezone.utc), lookback_days=1,
                              event_query='NVIDIA semiconductor'), allow_network=True, max_results=5)
    assert report['status'] == 'inspection_only', f"Index contract unavailable: {report.get('error_type')}, HTTP {report.get('http_status')}"
    assert not report['leads_retained'] and 'leads' not in report
