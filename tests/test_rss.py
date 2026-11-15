"""Official-feed contract and retention tests use authored XML only."""
from datetime import datetime, timezone
import json
from urllib.error import HTTPError
import pytest
from stock_report.models import Request
from stock_report.cli import main
from stock_report.rss import discover_feed, parse_feed
from stock_report.discovery import DiscoveryContractError, aliases

NOW = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)


def request(**changes):
    return Request(**(dict(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of=NOW) | changes))


def xml(title='NVIDIA synthetic press release', date='Sat, 14 Nov 2026 10:00:00 GMT',
        link='https://nvidianews.nvidia.com/news/synthetic', extra=''):
    return (f'<rss version="2.0"><channel><title>DEMO feed</title><item><title>{title}</title>'
            f'<link>{link}</link><pubDate>{date}</pubDate></item>{extra}</channel></rss>').encode()


def test_rss_declared_time_is_not_index_time():
    leads, counts = parse_feed(xml(), request(), aliases(), 5)
    assert len(leads) == 1 and counts['feed_items_received'] == 1
    assert leads[0]['source_published_at'] == '2026-11-14T10:00:00+00:00'
    assert 'index_seen_at' not in leads[0] and leads[0]['verification_status'] == 'unverified'


@pytest.mark.parametrize('body', [b'<bad>', b'<html/>', b'<rss/>',
    b'<!DOCTYPE rss [<!ENTITY a "bomb">]><rss><channel/></rss>',
    '<!DOCTYPE rss><rss><channel/></rss>'.encode('utf-16'), b'x' * 2_000_001])
def test_bad_or_entity_bearing_xml_is_rejected(body):
    with pytest.raises(DiscoveryContractError):
        parse_feed(body, request(), aliases(), 5)


@pytest.mark.parametrize('changes', [{'date': 'bad'}, {'date': 'Sat, 14 Nov 2026 10:00:00'},
    {'link': 'https://evil.invalid/story'}, {'title': ''}])
def test_malformed_or_non_issuer_items_are_excluded(changes):
    leads, counts = parse_feed(xml(**changes), request(), aliases(), 5)
    assert not leads and counts['malformed_feed_items'] == 1


def test_window_and_title_filtering_are_explicit():
    leads, counts = parse_feed(xml(date='Mon, 16 Nov 2026 10:00:00 GMT'), request(), aliases(), 5)
    assert not leads and counts['excluded_rows']['outside_publication_window'] == 1
    leads, counts = parse_feed(xml(title='Unrelated invented headline'), request(), aliases(), 5)
    assert not leads and counts['title_query_nonmatches'] == 1


def test_feed_size_cap_and_duplicate_removal():
    item = '<item><title>NVIDIA demo</title><link>https://nvidianews.nvidia.com/news/x</link><pubDate>Sat, 14 Nov 2026 10:00:00 GMT</pubDate></item>'
    leads, counts = parse_feed(('<rss><channel>' + item * 2 + '</channel></rss>').encode(), request(), aliases(), 5)
    assert len(leads) == 1 and counts['duplicates_removed'] == 1
    with pytest.raises(DiscoveryContractError):
        parse_feed(('<rss><channel>' + item * 201 + '</channel></rss>').encode(), request(), aliases(), 5)


def test_real_feed_does_not_expose_source_text_or_urls():
    class Client:
        def get_xml(self): return xml(title='NVIDIA SECRET_HEADLINE')
    report = discover_feed(request(), allow_network=True, client=Client(), now=NOW)
    assert report['status'] == 'inspection_only' and report['counts']['unique_leads'] == 1
    assert 'SECRET_HEADLINE' not in json.dumps(report) and 'leads' not in report
    assert not report['internal_use_allowed'] and report['verified_evidence_created'] == 0


def test_no_hidden_provider_switch_or_network_without_opt_in():
    with pytest.raises(ValueError, match='allow-network'):
        discover_feed(request(), now=NOW)
    with pytest.raises(ValueError, match='NVDA stock'):
        discover_feed(request(ticker='2330.TW', exchange='TWSE'), allow_network=True, now=NOW)
    class Client:
        def get_xml(self): raise HTTPError('https://secret.invalid', 429, 'SECRET', {}, None)
    report = discover_feed(request(), allow_network=True, client=Client(), now=NOW)
    assert report['status'] == 'failed' and report['provider'] == 'nvidia-press-rss'
    assert 'SECRET' not in json.dumps(report)


def test_cli_selects_rss_explicitly_and_preserves_synthetic_label(tmp_path, capsys):
    fixture = tmp_path / 'feed.json'
    fixture.write_text(json.dumps({'fixture_kind': 'synthetic-press-rss',
        'xml': xml(date='Fri, 13 Nov 2026 10:00:00 GMT').decode()}))
    output = tmp_path / 'result.json'
    assert main(['news-discover', '--source', 'nvidia-press-rss', '--request', 'examples/stock-nvda.json',
                 '--fixture', str(fixture), '--output', str(output)]) == 0
    report = json.loads(output.read_text())
    assert report['provider'] == 'nvidia-press-rss' and report['data_label'] == 'DEMO / SYNTHETIC DATA'
    assert report['leads'][0]['publication_basis'] == 'source_declared_timestamp'


@pytest.mark.network
def test_live_official_feed_contract():
    report = discover_feed(request(research_as_of=datetime.now(timezone.utc), lookback_days=30), allow_network=True)
    assert report['status'] == 'inspection_only', f"Feed unavailable: {report.get('error_type')}, HTTP {report.get('http_status')}"
    assert report['counts']['feed_items_received'] > 0 and not report['leads_retained']
