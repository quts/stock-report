"""Explicit official-issuer feed alternative. No fallback, article fetching or evidence creation."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request as HTTPRequest, build_opener
from xml.etree import ElementTree
from .discovery import (DiscoveryContractError, LIMITS, MAX_BODY, NoRedirect, aliases, normalized,
                        parse_results, phrase_match, query_url, search_terms)
from .storage import read_json

ENDPOINT = 'https://nvidianews.nvidia.com/cats/press_release.xml'
DOCS = 'https://investor.nvidia.com/investor-resources/rss/default.aspx'
MAX_ITEMS = 200


class FeedClient:
    def get_xml(self):
        req = HTTPRequest(ENDPOINT, headers={'User-Agent': 'StockReport-Phase0/0.1 (personal research feasibility)',
                                           'Accept': 'application/rss+xml, application/xml, text/xml'})
        with build_opener(NoRedirect()).open(req, timeout=20) as response:
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise DiscoveryContractError('RSS response exceeds size limit')
            if not any(t in response.headers.get('Content-Type', '').lower()
                       for t in ('application/rss+xml', 'application/xml', 'text/xml')):
                raise DiscoveryContractError('RSS endpoint did not return XML content')
        return body


def parse_feed(body, request, config, max_results):
    if not isinstance(body, bytes) or len(body) > MAX_BODY:
        raise DiscoveryContractError('Invalid or oversized RSS body')
    # Reject DTDs before parsing, including UTF-16 forms. No external entities or expansion.
    if b'<!doctype' in body.replace(b'\x00', b'').lower() or b'<!entity' in body.replace(b'\x00', b'').lower():
        raise DiscoveryContractError('RSS DTD and entities are disabled')
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise DiscoveryContractError('Malformed RSS XML') from None
    if root.tag != 'rss' or root.find('channel') is None:
        raise DiscoveryContractError('Expected an RSS channel')
    items = root.findall('./channel/item')
    if len(items) > MAX_ITEMS:
        raise DiscoveryContractError('RSS item count exceeds limit')
    terms = search_terms(request, config)
    rows, excluded, unmatched = [], 0, 0
    for item in items:
        title, link, raw_date = (item.findtext(key) for key in ('title', 'link', 'pubDate'))
        if not title or not link or not raw_date or len(title) > 1000 or len(raw_date) > 100:
            excluded += 1
            continue
        try:
            published = parsedate_to_datetime(raw_date)
            if published.tzinfo is None:
                raise ValueError
            if urlsplit(link).hostname != 'nvidianews.nvidia.com':
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            excluded += 1
            continue
        if not all(phrase_match(normalized(title), term) for term in terms):
            unmatched += 1
            continue
        rows.append({'title': title, 'url': link, 'domain': 'nvidianews.nvidia.com',
                     'seendate': published.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')})
    # Normalize/deduplicate/filter all bounded items before truncation; newest retained first.
    leads, counts = parse_results({'articles': rows}, request, config, MAX_ITEMS)
    if 'outside_index_window' in counts['excluded_rows']:
        counts['excluded_rows']['outside_publication_window'] = counts['excluded_rows'].pop('outside_index_window')
    leads.sort(key=lambda row: (row['index_seen_at'], row['url']), reverse=True)
    more = len(leads) > max_results
    leads = leads[:max_results]
    for i, row in enumerate(leads, start=1):
        row['lead_id'] = f'lead-{i:04d}'
        row['source_published_at'] = row.pop('index_seen_at')
        row['publication_basis'] = 'source_declared_timestamp'
    counts.update(feed_items_received=len(items), malformed_feed_items=excluded,
                  title_query_nonmatches=unmatched, leads_after_limit=len(leads),
                  possibly_truncated=more, feed_history_complete=False)
    return leads, counts


def discover_feed(request, *, allow_network=False, fixture=None, max_results=50, client=None, now=None):
    config = aliases()
    query_url(request, config, max_results, request.research_as_of if fixture is not None
              else now or datetime.now(timezone.utc))  # Common query/window validation only; no GDELT call.
    if request.mode == 'stock' and request.ticker != 'NVDA':
        raise ValueError('The NVIDIA press-release feed supports NVDA stock requests only')
    if fixture is None and not allow_network:
        raise ValueError('Real RSS discovery requires --allow-network')
    report = {'artifact': 'news_discovery', 'discovery_version': '1', 'provider': 'nvidia-press-rss',
              'capture_mode': 'fixture' if fixture else 'network', 'endpoint': ENDPOINT, 'documentation': DOCS,
              'data_label': 'DEMO / SYNTHETIC DATA' if fixture else 'REAL FEED CONTRACT ONLY',
              'research_as_of': request.research_as_of.isoformat(), 'license_status': 'unknown',
              'internal_use_allowed': False, 'verified_evidence_created': 0,
              'leads_retained': fixture is not None, 'request_limit': max_results,
              'limits': LIMITS[:3] + ['RSS 來源宣告的發布時間不代表主張已核實；只搜尋有限新聞稿標題，非全文或全市場搜尋。',
                                    '來源有官方 RSS 不等於取得保存新聞原文或研究證據的權限。']}
    try:
        if fixture is not None:
            data = read_json(Path(fixture))
            if not isinstance(data, dict) or data.get('fixture_kind') != 'synthetic-press-rss' or not isinstance(data.get('xml'), str):
                raise ValueError('RSS fixture must explicitly declare synthetic-press-rss')
            body = data['xml'].encode('utf-8')
        else:
            body = (client or FeedClient()).get_xml()
        leads, counts = parse_feed(body, request, config, max_results)
    except (HTTPError, URLError, TimeoutError, DiscoveryContractError) as exc:
        return {**report, 'status': 'failed', 'leads_retained': False,
                'error_type': type(exc).__name__, 'http_status': exc.code if isinstance(exc, HTTPError) else None}
    report.update(status='synthetic_leads' if fixture else 'inspection_only', counts=counts,
                  retained='authored synthetic leads' if fixture else 'contract/count/status metadata only')
    if fixture is not None:
        report['leads'] = leads
    return report
