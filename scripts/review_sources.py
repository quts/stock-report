"""Opt-in official CSV/JSON binding checks; retain no source values or response hashes."""
import argparse
import csv
import io
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from stock_report.providers import HTTPClient
from stock_report.storage import write_json

parser = argparse.ArgumentParser()
parser.add_argument('--allow-network', action='store_true', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
contracts = [
    ('2330', 'https://mopsfin.twse.com.tw/opendata/t187ap06_L_ci.csv',
     'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci', 'https://data.gov.tw/dataset/91998'),
    ('6488', 'https://mopsfin.twse.com.tw/opendata/t187ap06_O_ci.csv',
     'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci', 'https://data.gov.tw/dataset/94853')]
fields = ['公司名稱', '年度', '季別', '營業收入', '淨利（淨損）歸屬於母公司業主', '基本每股盈餘（元）']
def norm(value):
    return unicodedata.normalize('NFKC', str(value)).strip().replace('\ufeff', '')
def row(value):
    aliases = {'SecuritiesCompanyCode': '公司代號', 'CompanyName': '公司名稱', 'Year': '年度', 'Season': '季別'}
    return {norm(aliases.get(k, k)): norm(v) for k, v in value.items() if k is not None}
def equal(a, b):
    try: return Decimal(a.replace(',', '')) == Decimal(b.replace(',', ''))
    except InvalidOperation: return a == b
client = HTTPClient('StockReport-Phase0/0.1 source binding review', retries=0, interval=1, timeout=25)
results = []
for code, csv_url, json_url, dataset in contracts:
    record = dict(ticker=code, csv_url=csv_url, json_url=json_url, dataset=dataset,
                  checked_at=datetime.now(timezone.utc).isoformat(), retained_source_values=False,
                  retained_response_hash=False, checked_by='assistant')
    try:
        body, _ = client.fetch(csv_url, 'text/csv')
        text = body.decode('utf-8-sig')
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if '公司代號' in line and '年度' in line)
        csv_rows = [row(r) for r in csv.DictReader(io.StringIO('\n'.join(lines[start:])))]
        data, _ = client.get_json(json_url)
        if not isinstance(data, list): raise ValueError('Unexpected JSON contract')
        json_rows = [row(r) for r in data]
        left = [r for r in csv_rows if r.get('公司代號') == code]
        right = [r for r in json_rows if r.get('公司代號') == code]
        record.update(csv_target_matches=len(left), json_target_matches=len(right))
        if len(left) != 1 or len(right) != 1: raise ValueError('Target not unique')
        checks = {field: norm(field) in left[0] and norm(field) in right[0]
                  and equal(left[0][norm(field)], right[0][norm(field)]) for field in fields}
        record.update(field_matches=checks, status='matched_target_fields' if all(checks.values()) else 'mismatch',
                      csv_header_declares_monetary_scale=any(k in lines[start] for k in ('仟元', '千元', '百萬元')))
    except Exception as exc:
        record.update(status='failed', error_type=type(exc).__name__, http_status=getattr(exc, 'code', None))
    results.append(record)
write_json(args.output, {'results': results, 'permissions_changed': False,
                        'limit': 'Target fields at one capture only; no proof of units, publication or complete equivalence'})
print(args.output)
raise SystemExit(0 if all(r['status'] == 'matched_target_fields' for r in results) else 1)
