"""Opt-in official-source feasibility probes, not licensed research adapters."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .storage import write_json, read_json

SOURCES = {
    'sec-nvda': ('https://data.sec.gov/api/xbrl/companyfacts/CIK0001045810.json', 'https://www.sec.gov/about/developer-resources'),
    'sec-amd': ('https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json', 'https://www.sec.gov/about/developer-resources'),
    'twse': ('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL', 'https://www.twse.com.tw/zh/terms/terms.html'),
    'tpex': ('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes', 'https://www.tpex.org.tw/zh-tw/terms.html'),
}

class HTTPClient:
    """Bounded retries/rate limits; raw response cache requires explicit permission."""
    def __init__(self, user_agent, retries=2, interval=1.0, timeout=20, cache_dir=None, cache_ttl=3600):
        if not 0 <= retries <= 5 or interval < 0 or timeout <= 0:
            raise ValueError('Invalid HTTP limits')
        self.user_agent, self.retries, self.interval, self.timeout = user_agent, retries, interval, timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl, self.last = cache_ttl, 0.0

    def get_json(self, url):
        cache = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + '.json') if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < self.cache_ttl:
            return read_json(cache), {'cached': True, 'attempts': 0}
        raw, meta = self.fetch(url, 'application/json')
        data = json.loads(raw)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            write_json(cache, data)
        return data, meta

    def get_text(self, url):
        """Strict UTF-8 text body (CSV sources). Never cached: raw retention needs its own permission."""
        raw, meta = self.fetch(url, 'text/csv, text/plain;q=0.9, */*;q=0.1')
        return raw.decode('utf-8'), meta

    def fetch(self, url, accept):
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                request = Request(url, headers={'User-Agent': self.user_agent, 'Accept': accept})
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read(12_000_001)
                    if len(raw) > 12_000_000:
                        raise ValueError('Provider response exceeds size limit')
                    content_type = getattr(response, 'headers', {}).get('Content-Type')
                return raw, {'cached': False, 'attempts': attempt + 1, 'content_type': content_type,
                             'response_sha256': hashlib.sha256(raw).hexdigest()}
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise
            except (URLError, TimeoutError):
                if attempt == self.retries:
                    raise
            time.sleep(min(2 ** attempt, 8))


def probes(output, user_agent, retries=1, interval=1.0):
    if not user_agent or '\n' in user_agent or '\r' in user_agent:
        raise ValueError('A valid SEC-compliant User-Agent/contact is required')
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    client = HTTPClient(user_agent, retries=retries, interval=interval)
    results = []
    for name, (url, terms) in SOURCES.items():
        started = time.monotonic()
        row = {'provider': name, 'endpoint': url, 'terms_url': terms,
               'tested_at': datetime.now(timezone.utc).isoformat(), 'license_status': 'unknown',
               'shareable_in_pdf': False, 'terms_review': 'PENDING human review; access does not establish redistribution rights'}
        try:
            if name.startswith('sec') and '@' not in user_agent:
                raise ValueError('SEC probe blocked: configure a declared User-Agent with contact email; no personal login data is reused')
            data, transport = client.get_json(url)
            row.update(status='retrieved', transport=transport)
            if name.startswith('sec'):
                facts = data.get('facts', {}).get('us-gaap', {})
                row['sample'] = {'entityName': data.get('entityName'), 'cik': data.get('cik'),
                                 'us_gaap_concepts': len(facts)}
                if not facts or not data.get('entityName'):
                    raise ValueError('SEC response missing identity or financial facts')
            else:
                ticker = '2330' if name == 'twse' else '6488'
                if not isinstance(data, list):
                    raise ValueError('Expected a list of exchange records')
                selected = [r for r in data if isinstance(r, dict) and ticker in {str(v).strip() for v in r.values()}]
                row['sample'] = {'target_ticker': ticker, 'matched_records': selected[:1], 'record_count': len(data)}
                if not selected:
                    raise ValueError('Target ticker not found in endpoint response')
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:250])
        row['elapsed_seconds'] = round(time.monotonic() - started, 3)
        results.append(row)
    write_json(directory / 'provider-probes.json', {'dataset_kind': 'real', 'results': results,
        'warning': 'Feasibility metadata only. No real research PDF or sharing authorization established.'})
    return {'status': 'partial', 'probe_path': str((directory / 'provider-probes.json').resolve()),
            'retrieved': sum(r['status'] == 'retrieved' for r in results), 'total': len(results),
            'warnings': ['All redistribution permissions remain unknown; no synthetic fallback used']}
