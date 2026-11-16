"""Contract checks for bounded, reviewed EDGAR filing collection."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from stock_report.filing_collect import collect_filings, SOURCE
from stock_report.storage import read_json

URL = 'https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/nvda-20261111.htm'
NOW = datetime(2026, 11, 15, tzinfo=timezone.utc)


class Registry:
    def __init__(self, allowed=True):
        self.allowed = allowed

    def snapshot(self, source):
        assert source == SOURCE
        return dict(source_reviewed=self.allowed, internal_use_allowed=self.allowed)

    def get(self, source):
        return SimpleNamespace(covers_endpoint=lambda url: url.startswith('https://www.sec.gov/Archives/'))


class Client:
    def __init__(self, body='<h1>NVIDIA</h1><p>accepted the resignation of the chief executive</p>'):
        self.body, self.calls = body, []

    def fetch(self, url, accept):
        self.calls.append(url)
        return self.body.encode(), {'attempts': 1, 'response_sha256': 'never-retain-this'}


def plan():
    return dict(reviewed_by='test reviewer', authorization='test only', facts=[dict(
        evidence_id='test-resignation', url=URL, claim='Reviewed test fact',
        excerpts=['accepted the resignation of the chief executive'],
        publication_date='2026-11-12', event_date='2026-11-11')])


def collect(p=None, client=None, registry=None, allow=True):
    return collect_filings('NVDA', registry=registry or Registry(), allow_network=allow,
                           plan=p or plan(), client=client or Client(), now=NOW)


def test_live_excerpt_contract_retains_only_normalized_facts():
    c = collect()
    assert c.capture_mode == 'network' and c.observations[0].kind == 'event'
    assert c.observations[0].published_at.isoformat().startswith('2026-11-12T23:59:59')
    assert 'response_sha256' not in c.model_dump_json()
    assert 'excerpts' not in c.model_dump_json()
    assert 'never-retain' not in c.model_dump_json()


@pytest.mark.parametrize('token,expected', [('-', 0), ('( 454 )', -454000000)])
def test_reviewed_statement_dash_and_spaced_parentheses_have_explicit_amounts(token, expected):
    p = plan()
    row = 'Other noncash adjustment ' + token
    p['facts'][0].update(excerpts=[row], kind='financial', financial_metric='cf_other_noncash',
                         numeric_text=token, number_scale=1000000, unit='USD', currency='USD',
                         period_start='2026-01-01', period_end='2026-09-30', period_basis='cumulative_ytd',
                         accounting_basis='gaap')
    c = collect(p, Client('<p>NVIDIA</p><p>' + row + '</p>'))
    assert c.observations[0].value == expected


@pytest.mark.parametrize('url', [URL.replace('/1045810/', '/2488/'), URL.replace('www.sec.gov', 'evil.example')])
def test_wrong_issuer_or_host_blocked_before_network(url):
    p, client = plan(), Client()
    p['facts'][0]['url'] = url
    with pytest.raises(ValueError):
        collect(p, client)
    assert not client.calls


def test_missing_exact_excerpt_fails_without_substitution():
    with pytest.raises(ValueError, match='absent'):
        collect(client=Client('<p>NVIDIA reports unrelated results</p>'))


def test_script_text_does_not_establish_a_fact():
    with pytest.raises(ValueError, match='absent'):
        collect(client=Client('<p>NVIDIA</p><script>accepted the resignation of the chief executive</script>'))


def test_closed_source_or_missing_network_opt_in_never_fetches():
    for registry, allow in [(Registry(False), True), (Registry(), False)]:
        client = Client()
        with pytest.raises(ValueError):
            collect(client=client, registry=registry, allow=allow)
        assert not client.calls


def test_fetch_once_for_multiple_facts_in_one_filing():
    p, client = plan(), Client()
    p['facts'].append({**p['facts'][0], 'evidence_id': 'test-second'})
    c = collect(p, client)
    assert len(c.observations) == 2 and len(client.calls) == 1
