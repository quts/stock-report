"""Print the response *shape* of an allow-listed feasibility endpoint. Nothing is written to disk.

Opt-in only: `./scripts/runtime.sh contract --allow-network --endpoint <key> [--code 2330]`.

Output is restricted to contract metadata: field names, row count, which fields are
non-empty for the target row, and the character-class shape of each value (digits
become `9`, CJK becomes `字`, latin letters become `a`). No source value is echoed,
so the output is safe to read while every source licence is still `unknown`.
"""
import argparse
import json
import re
from stock_report.collect import DEFAULT_USER_AGENT
from stock_report.providers import HTTPClient

ENDPOINTS = {
    'twse-income-ci': 'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci',
    'twse-balance-ci': 'https://openapi.twse.com.tw/v1/opendata/t187ap07_L_ci',
    'twse-monthly-revenue': 'https://openapi.twse.com.tw/v1/opendata/t187ap05_L',
    'twse-eps-industry': 'https://openapi.twse.com.tw/v1/opendata/t187ap14_L',
    'twse-company-profile': 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L',
    'tpex-income-ci': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci',
    'tpex-balance-ci': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O_ci',
    'tpex-monthly-revenue': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O',
    'tpex-eps-industry': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap14_O',
    'tpex-company-profile': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O',
}


TEXT_ENDPOINTS = {
    'stooq-history-nvda': 'https://stooq.com/q/d/l/?s=nvda.us&i=d',
    'stooq-history-amd': 'https://stooq.com/q/d/l/?s=amd.us&i=d',
    'stooq-history-range-nvda': 'https://stooq.com/q/d/l/?s=nvda.us&d1=20251101&d2=20261114&i=d',
    'stooq-snapshot-nvda': 'https://stooq.com/q/l/?s=nvda.us&f=sd2t2ohlcvn&h&e=csv',
    'stooq-snapshot-amd': 'https://stooq.com/q/l/?s=amd.us&f=sd2t2ohlcvn&h&e=csv',
    'stooq-snapshot-json-nvda': 'https://stooq.com/q/l/?s=nvda.us&f=sd2t2ohlcvn&h&e=json',
    'stooq-snapshot-bogus': 'https://stooq.com/q/l/?s=zzzzqq.us&f=sd2t2ohlcvn&h&e=csv',
    'stooq-history-bogus': 'https://stooq.com/q/d/l/?s=zzzzqq.us&i=d',
}


CANDIDATE_JSON = {
    'nasdaq-historical-nvda': 'https://api.nasdaq.com/api/quote/NVDA/historical?assetclass=stocks&fromdate=2025-11-01&todate=2026-11-14&limit=400',
    'nasdaq-historical-amd': 'https://api.nasdaq.com/api/quote/AMD/historical?assetclass=stocks&fromdate=2025-11-01&todate=2026-11-14&limit=400',
    'nasdaq-info-nvda': 'https://api.nasdaq.com/api/quote/NVDA/info?assetclass=stocks',
    'nasdaq-historical-bogus': 'https://api.nasdaq.com/api/quote/ZZZZQQ/historical?assetclass=stocks&fromdate=2025-11-01&todate=2026-11-14&limit=400',
    'cboe-quote-nvda': 'https://cdn.cboe.com/api/global/delayed_quotes/quotes/NVDA.json',
    'cboe-history-nvda': 'https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/NVDA.json',
    'yahoo-chart-nvda': 'https://query1.finance.yahoo.com/v8/finance/chart/NVDA?range=2y&interval=1d&events=split',
    'yahoo-chart-amd': 'https://query1.finance.yahoo.com/v8/finance/chart/AMD?range=2y&interval=1d&events=split',
    'yahoo-chart-bogus': 'https://query1.finance.yahoo.com/v8/finance/chart/ZZZZQQ?range=2y&interval=1d&events=split',
}
# Identity and contract descriptors (never prices) that may be echoed verbatim to the terminal.
YAHOO_META_IDENTITY = ('currency', 'symbol', 'exchangeName', 'fullExchangeName', 'instrumentType', 'gmtoffset',
                       'timezone', 'exchangeTimezoneName', 'dataGranularity', 'range', 'priceHint',
                       'hasPrePostMarketData')


def yahoo_detail(data):
    from datetime import datetime, timedelta, timezone
    result = data['chart']['result'][0]
    meta, stamps = result['meta'], result.get('timestamp') or []
    tz = timezone(timedelta(seconds=meta['gmtoffset']))
    local = [datetime.fromtimestamp(t, tz) for t in stamps]
    quote = result['indicators']['quote'][0]
    return {'meta_identity': {k: meta.get(k) for k in YAHOO_META_IDENTITY},
            'meta_keys': sorted(meta), 'result_keys': sorted(result),
            'indicator_keys': {k: sorted(v[0]) for k, v in result['indicators'].items()},
            'series_lengths': {k: len(v) for k, v in quote.items()},
            'adjclose_length': len(result['indicators'].get('adjclose', [{}])[0].get('adjclose', [])),
            'null_counts': {k: sum(x is None for x in v) for k, v in quote.items()},
            'timestamps_strictly_increasing': all(a < b for a, b in zip(stamps, stamps[1:])),
            'local_times_of_day': sorted({d.strftime('%H:%M:%S') for d in local}),
            'distinct_local_dates': len({d.date() for d in local}), 'rows': len(stamps),
            'first_local_date': local[0].date().isoformat() if local else None,
            'last_local_date': local[-1].date().isoformat() if local else None,
            'regular_market_time_local': datetime.fromtimestamp(meta['regularMarketTime'], tz).isoformat(),
            'current_trading_period_regular_keys': sorted(meta.get('currentTradingPeriod', {}).get('regular', {})),
            'events': tree(result.get('events')),
            'close_equals_adjclose_everywhere': quote.get('close') == result['indicators'].get('adjclose', [{}])[0].get('adjclose')}



def tree(value, depth=0):
    """Key structure with masked leaves; lists show their length and first element only."""
    if isinstance(value, dict):
        return {str(k)[:60]: tree(v, depth + 1) for k, v in list(value.items())[:60]} if depth < 6 else '{...}'
    if isinstance(value, list):
        return {'list_len': len(value), 'first': tree(value[0], depth + 1) if value else None,
                'last': tree(value[-1], depth + 1) if len(value) > 1 else None}
    return None if value is None else f'{type(value).__name__}:{mask(str(value))[:40]}'


def json_shape(key):
    try:
        data, meta = HTTPClient(DEFAULT_USER_AGENT, retries=0, timeout=25).get_json(CANDIDATE_JSON[key])
    except Exception as exc:
        return {'endpoint': CANDIDATE_JSON[key], 'status': 'failed', 'error_type': type(exc).__name__,
                'http_status': getattr(exc, 'code', None)}
    report = {'endpoint': CANDIDATE_JSON[key], 'status': 'retrieved', 'content_type': meta.get('content_type'),
              'tree': tree(data)}
    if key.startswith('yahoo'):
        report = {'endpoint': report['endpoint'], 'status': 'retrieved', 'detail': yahoo_detail(data)}
    return report


def text_shape(key):
    """Shape of a text/CSV body: header verbatim (field names), everything else masked."""
    try:
        text, meta = HTTPClient(DEFAULT_USER_AGENT, retries=1).get_text(TEXT_ENDPOINTS[key])
    except Exception as exc:
        return {'endpoint': TEXT_ENDPOINTS[key], 'status': 'failed', 'error_type': type(exc).__name__,
                'http_status': getattr(exc, 'code', None)}
    lines = text.splitlines()
    return {'endpoint': TEXT_ENDPOINTS[key], 'status': 'retrieved', 'content_type': meta.get('content_type'),
            'bytes': len(text.encode()), 'line_count': len(lines),
            'first_line_if_header_like': lines[0][:200] if lines and not re.search(r'[0-9]', lines[0]) else None,
            'first_line_shape': shape(lines[0])[:24] if lines else None,
            'row_shapes': {'second': mask(lines[1]) if len(lines) > 1 else None,
                           'last': mask(lines[-1]) if len(lines) > 1 else None},
            'distinct_column_counts': sorted({line.count(',') + 1 for line in lines if line.strip()})[:6]}


def mask(text):
    text = re.sub(r'[0-9]', '9', text)
    return re.sub(r'[A-Za-z]', 'a', text)[:160]


def shape(value):
    text = str(value)
    text = re.sub(r'[0-9]', '9', text)
    text = re.sub(r'[A-Za-z]', 'a', text)
    text = re.sub('[\\u3400-\\u9fff]', '字', text)
    return text[:24]


PERIOD_FIELDS = {'出表日期', '年度', '季別', '資料年月', 'Date', 'Year', 'Season'}


def number(text):
    return float(str(text).replace(',', '').strip())


def cumulative_check(market, code):
    """Is statement revenue cumulative year-to-date, and in the monthly filing's unit?

    The monthly-revenue filing declares cumulative revenue explicitly. If the statement's
    revenue for quarter Q equals that cumulative figure rolled back to the quarter-end month,
    the statement is year-to-date in the same unit. Only ratios are printed.
    """
    if not code:
        raise SystemExit('--code is required')
    print(json.dumps(cumulative_basis(market, code), ensure_ascii=False, indent=1))


def cumulative_basis(market, code, client=None):
    client = client or HTTPClient(DEFAULT_USER_AGENT, retries=1)
    income, _ = client.get_json(ENDPOINTS[f'{market}-income-ci'])
    monthly, _ = client.get_json(ENDPOINTS[f'{market}-monthly-revenue'])
    code_key, year_key, season_key = (('公司代號', '年度', '季別') if market == 'twse'
                                      else ('SecuritiesCompanyCode', 'Year', 'Season'))
    stmt = [r for r in income if str(r.get(code_key, '')).strip() == code]
    month = [r for r in monthly if str(r.get('公司代號', '')).strip() == code]
    if len(stmt) != 1 or len(month) != 1:
        raise SystemExit(f'expected one row each, got {len(stmt)} and {len(month)}')
    stmt, month = stmt[0], month[0]
    ym = str(month['資料年月']).strip()
    month_year, month_no = int(ym[:-2]), int(ym[-2:])
    quarter_end_month = int(stmt[season_key]) * 3
    report = {'check': 'statement revenue vs monthly-filing cumulative revenue rolled back to the quarter end',
              'retained': 'ratios and period relation only; no source value',
              'same_year': int(stmt[year_key]) == month_year, 'months_after_quarter_end': month_no - quarter_end_month}
    revenue = number(stmt['營業收入'])
    cumulative, current, previous = (number(month['累計營業收入-當月累計營收']), number(month['營業收入-當月營收']),
                                     number(month['營業收入-上月營收']))
    rolled = {0: cumulative, 1: cumulative - current, 2: cumulative - current - previous}.get(month_no - quarter_end_month)
    if report['same_year'] and rolled:
        report['statement_revenue_over_monthly_cumulative_to_quarter_end'] = round(revenue / rolled, 3)
        report['single_quarter_would_be_about'] = round(1 / int(stmt[season_key]), 3)
        report['conclusion'] = ('cumulative_ytd_same_scale_as_monthly_filing'
                                if abs(revenue / rolled - 1) < 0.02 else 'not_established')
    else:
        report['conclusion'] = 'not_checkable_for_this_period'
    return report


def main():
    parser = argparse.ArgumentParser(prog='contract_probe')
    parser.add_argument('--allow-network', action='store_true', required=True)
    parser.add_argument('--endpoint', required=True, choices=sorted(ENDPOINTS) + sorted(TEXT_ENDPOINTS) + sorted(CANDIDATE_JSON))
    parser.add_argument('--code', help='Company code whose row shape is reported')
    parser.add_argument('--show-field', action='append', default=[], choices=sorted(PERIOD_FIELDS),
                        help='Echo a period/stamp identifier field verbatim (terminal only, never a financial value)')
    parser.add_argument('--cumulative-check', choices=['twse', 'tpex'],
                        help='Report only the ratio of statement revenue to monthly-filing cumulative revenue')
    args = parser.parse_args()
    if args.cumulative_check:
        return cumulative_check(args.cumulative_check, args.code)
    if args.endpoint in CANDIDATE_JSON:
        return print(json.dumps(json_shape(args.endpoint), ensure_ascii=False, indent=1))
    if args.endpoint in TEXT_ENDPOINTS:
        return print(json.dumps(text_shape(args.endpoint), ensure_ascii=False, indent=1))
    data, meta = HTTPClient(DEFAULT_USER_AGENT, retries=1).get_json(ENDPOINTS[args.endpoint])
    report = {'endpoint': ENDPOINTS[args.endpoint], 'top_level_type': type(data).__name__,
              'attempts': meta.get('attempts')}
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
        report['row_count'] = len(data)
        report['fields'] = list(rows[0]) if rows else []
        report['field_sets'] = len({tuple(sorted(r)) for r in rows})
        if args.code:
            matches = [r for r in rows if args.code in {str(v).strip() for v in r.values()}]
            report['target_rows'] = len(matches)
            report['target_period_fields'] = [{k: r.get(k) for k in args.show_field if k in r} for r in matches[:3]]
            report['target_shapes'] = [{k: shape(v) for k, v in r.items()} for r in matches[:3]]
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
