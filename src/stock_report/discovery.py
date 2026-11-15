"""Bounded GDELT news-index discovery. Mentions are unverified leads, never evidence.

Real results are processed only in memory. The unreviewed provider has no retention rights;
only contract/count/status metadata can leave this module. Authored synthetic fixtures can
expose their lead list for offline workflow testing. No article URL is ever fetched.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request as HTTPRequest, build_opener
from .events import load_universe
from .storage import read_json

ENDPOINT = 'https://api.gdeltproject.org/api/v2/doc/doc'
DOCS = 'https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/'
VERSION = '1'
MAX_BODY = 2_000_000
LIMITS = [
    '新聞索引與標題提及僅為待查線索，不代表事件成立、公司身份或直接／間接關聯。',
    '只比對支援清單的明確名稱及帶市場／符號的代號；不以裸數字或 AMD 等歧義縮寫判定公司。',
    '未命中不代表無關；標題、語言、別名及新聞索引覆蓋均可能遺漏。',
    'seendate 是索引時間，不冒充原文發布時間；不得直接匯入研究或自動簽認。',
    '單次有界查詢、最多 100 筆，沒有完整分頁；達上限視為可能截斷。',
]


class DiscoveryContractError(ValueError):
    """Fixed error messages only; never include an untrusted body or URL."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DiscoveryContractError('News-index redirects are disabled')


class NewsClient:
    """One request, 20s timeout, 2MB cap, no cache, redirects, keys or automatic retries."""
    def get_json(self, url):
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc, parts.path) != ('https', 'api.gdeltproject.org', '/api/v2/doc/doc'):
            raise DiscoveryContractError('Only the recorded news-index endpoint is allowed')
        request = HTTPRequest(url, headers={'User-Agent': 'StockReport-Phase0/0.1 (personal research feasibility)',
                                           'Accept': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise DiscoveryContractError('News-index response exceeds size limit')
            if 'application/json' not in response.headers.get('Content-Type', '').lower():
                raise DiscoveryContractError('News-index did not return JSON content')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise DiscoveryContractError('News-index returned invalid JSON') from None


def normalized(text):
    return unicodedata.normalize('NFKC', text).casefold()


def aliases(path=None):
    config = read_json(path or 'config/discovery-aliases.json')
    supported = {e.entity_id for e in load_universe().entities}
    rows = config.get('entities', [])
    ids = [row['entity_id'] for row in rows]
    if len(ids) != len(set(ids)) or set(ids) != supported:
        raise ValueError('Discovery aliases must match the supported universe exactly')
    for row in rows:
        for field in ('names', 'ticker_tokens'):
            if not isinstance(row.get(field), list) or not row[field] or any(
                    not isinstance(s, str) or not 2 <= len(s) <= 80 for s in row[field]):
                raise ValueError('Invalid discovery alias configuration')
    return config


def phrase_match(text, phrase):
    phrase = normalized(phrase)
    # CJK company names need substring matching; Latin tokens require alphanumeric boundaries.
    if any('\u3400' <= c <= '\u9fff' for c in phrase):
        return phrase in text
    return re.search(r'(?<![\w])' + re.escape(phrase) + r'(?![\w])', text) is not None


def mentions(title, config):
    text = normalized(title)
    found = defaultdict(set)
    for row in config['entities']:
        for alias in row['names'] + row['ticker_tokens']:
            if phrase_match(text, alias):
                found[normalized(alias)].add(row['entity_id'])
    ambiguous = {alias for alias, entities in found.items() if len(entities) > 1}
    matched = sorted({entity for alias, entities in found.items() if alias not in ambiguous for entity in entities})
    return matched, len(ambiguous)


def query_url(request, config, max_results, now):
    if not 1 <= max_results <= 100:
        raise ValueError('max_results must be between 1 and 100')
    start = request.research_as_of - timedelta(days=request.lookback_days)
    if request.research_as_of > now or start < now - timedelta(days=60):
        raise ValueError('Discovery supports only a non-future window within the last 60 days; cutoff is never rewritten')
    if request.mode == 'stock':
        row = next((r for r in config['entities'] if r['entity_id'] == request.ticker), None)
        if row is None:
            raise ValueError('Stock is outside the supported discovery universe')
        raw = row['names'][0]
    else:
        raw = request.event_query
    # Tokenize operator syntax away, then quote each token. No user-controlled search operators.
    tokens = re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', raw), re.UNICODE)
    if not tokens or len(tokens) > 12 or any(len(token) > 40 for token in tokens):
        raise ValueError('Search needs 1 to 12 plain terms, each at most 40 characters')
    query = ' '.join('"' + token + '"' for token in tokens)
    utc = lambda stamp: stamp.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
    return ENDPOINT + '?' + urlencode({'query': query, 'mode': 'ArtList', 'format': 'json',
        'maxrecords': max_results, 'sort': 'DateDesc', 'startdatetime': utc(start),
        'enddatetime': utc(request.research_as_of)})


def canonical_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or any(ord(c) < 33 for c in value):
        raise DiscoveryContractError('invalid_article_url')
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in ('http', 'https') or not host or parts.username or parts.password
                or parts.port not in (None, 443 if parts.scheme == 'https' else 80)
                or not re.fullmatch(r'[a-zA-Z0-9.-]+', host)):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
            raise ValueError
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.casefold().startswith('utm_') and k.casefold() not in ('fbclid', 'gclid')]
        return urlunsplit((parts.scheme.lower(), host.lower(), parts.path or '/', urlencode(sorted(query)), ''))
    except ValueError:
        raise DiscoveryContractError('invalid_article_url') from None


def parse_results(payload, request, config, max_results):
    if not isinstance(payload, dict) or not isinstance(payload.get('articles'), list):
        raise DiscoveryContractError('Expected a news-index articles list')
    articles = payload['articles']
    if len(articles) > max_results:
        raise DiscoveryContractError('News-index returned more rows than requested')
    rejected, groups = Counter(), defaultdict(list)
    start = request.research_as_of - timedelta(days=request.lookback_days)
    for article in articles:
        if not isinstance(article, dict):
            rejected['invalid_row'] += 1
            continue
        try:
            title = article.get('title')
            if not isinstance(title, str) or not 1 <= len(title) <= 1000 or any(ord(c) < 32 for c in title):
                raise DiscoveryContractError('invalid_title')
            url = canonical_url(article.get('url'))
            stamp = article.get('seendate')
            if not isinstance(stamp, str) or not re.fullmatch(r'\d{8}T\d{6}Z', stamp):
                raise DiscoveryContractError('invalid_index_time')
            try:
                seen = datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            except ValueError:
                raise DiscoveryContractError('invalid_index_time') from None
            if not start <= seen <= request.research_as_of:
                raise DiscoveryContractError('outside_index_window')
            host = urlsplit(url).hostname
            domain = article.get('domain')
            if not isinstance(domain, str) or host.removeprefix('www.') != domain.casefold().removeprefix('www.'):
                raise DiscoveryContractError('domain_mismatch')
            matched, ambiguous = mentions(title, config)
            groups[url].append({'title': title, 'url': url, 'index_seen_at': seen.isoformat(),
                                'mentions': matched, 'ambiguous_aliases': ambiguous})
        except DiscoveryContractError as exc:
            rejected[str(exc)] += 1
    leads, duplicates = [], 0
    for url in sorted(groups):
        rows = groups[url]
        if len({(normalized(r['title']), r['index_seen_at']) for r in rows}) != 1:
            rejected['conflicting_duplicate'] += len(rows)
            continue
        duplicates += len(rows) - 1
        leads.append({'lead_id': f'lead-{len(leads) + 1:04d}', **rows[0],
                      'verification_status': 'unverified', 'relationship': 'unassessed'})
    return leads, {'rows_received': len(articles), 'unique_leads': len(leads), 'duplicates_removed': duplicates,
                   'excluded_rows': dict(sorted(rejected.items())),
                   'ambiguous_alias_matches': sum(r['ambiguous_aliases'] for r in leads),
                   'possibly_truncated': len(articles) == max_results}


def discover(request, *, allow_network=False, fixture=None, max_results=50, client=None, now=None,
             aliases_path=None):
    """Return a persistence-safe report. A real capture never returns its lead list."""
    now = now or datetime.now(timezone.utc)
    config = aliases(aliases_path)
    # Fixture windows are deterministic and never require current wall-clock data.
    url = query_url(request, config, max_results, request.research_as_of if fixture is not None else now)
    capture = 'fixture' if fixture is not None else 'network'
    base = {'artifact': 'news_discovery', 'discovery_version': VERSION, 'capture_mode': capture,
            'data_label': 'DEMO / SYNTHETIC DATA' if fixture is not None else 'REAL INDEX CONTRACT ONLY',
            'provider': 'gdelt-doc-v2', 'endpoint': ENDPOINT, 'documentation': DOCS,
            'alias_version': config['config_version'], 'research_as_of': request.research_as_of.isoformat(),
            'license_status': 'unknown', 'internal_use_allowed': False, 'verified_evidence_created': 0,
            'request_limit': max_results, 'limits': LIMITS}
    if fixture is None and not allow_network:
        raise ValueError('Real discovery requires --allow-network; no synthetic fallback')
    try:
        if fixture is not None:
            wrapper = read_json(Path(fixture))
            if not isinstance(wrapper, dict) or wrapper.get('fixture_kind') != 'synthetic-news-index':
                raise ValueError('Fixture must explicitly declare synthetic-news-index')
            payload = wrapper.get('payload')
        else:
            payload = (client or NewsClient()).get_json(url)
        leads, counts = parse_results(payload, request, config, max_results)
    except (HTTPError, URLError, TimeoutError, DiscoveryContractError, json.JSONDecodeError, UnicodeError) as exc:
        # Neither exception strings nor queried URLs may expose source content.
        return {**base, 'status': 'failed', 'error_type': type(exc).__name__,
                'http_status': exc.code if isinstance(exc, HTTPError) else None,
                'retained': 'contract/status metadata only', 'leads_retained': False}
    report = {**base, 'status': 'synthetic_leads' if fixture is not None else 'inspection_only',
              'counts': counts, 'leads_retained': fixture is not None,
              'retained': 'authored synthetic leads' if fixture is not None else 'contract/count/status metadata only'}
    if fixture is not None:
        report['leads'] = leads
        report['candidate_mentions'] = [
            {'entity_id': entity, 'lead_ids': [r['lead_id'] for r in leads if entity in r['mentions']],
             'relationship': 'unassessed', 'verification_status': 'unverified'}
            for entity in sorted({e for r in leads for e in r['mentions']})]
    return report
