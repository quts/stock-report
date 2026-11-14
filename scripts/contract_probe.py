"""Print the response *shape* of an allow-listed official endpoint. Nothing is written to disk.

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
    parser.add_argument('--endpoint', required=True, choices=sorted(ENDPOINTS))
    parser.add_argument('--code', help='Company code whose row shape is reported')
    parser.add_argument('--show-field', action='append', default=[], choices=sorted(PERIOD_FIELDS),
                        help='Echo a period/stamp identifier field verbatim (terminal only, never a financial value)')
    parser.add_argument('--cumulative-check', choices=['twse', 'tpex'],
                        help='Report only the ratio of statement revenue to monthly-filing cumulative revenue')
    args = parser.parse_args()
    if args.cumulative_check:
        return cumulative_check(args.cumulative_check, args.code)
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
