"""Operator-authorized market data imports: prices, splits, consensus and peer multiples.

This path exists for data that no reviewed free source may supply automatically (US closing
prices, price history, analyst consensus, peer valuations). The operator supplies the file and
attests, in their own name, that they may use it for private research. An AI agent cannot
attest. Imported records are secondary data with their stated provenance; they never replace an
official figure silently.
"""
import csv
from datetime import date, datetime, time, timedelta, timezone
import re
from pydantic import AwareDatetime, Field
from .calendars import is_trading_day
from .models import Evidence, Model

SOURCE_ID = 'operator-import'
AI_NAMES = re.compile(r'claude|codex|assistant|gpt|ai\b|bot', re.IGNORECASE)
CLOSE_TIME = {'TWSE': (time(13, 30), 8), 'TPEx': (time(13, 30), 8), 'NYSE': (time(16, 0), -5), 'NASDAQ': (time(16, 0), -5)}
CURRENCY = {'TWSE': 'TWD', 'TPEx': 'TWD', 'NYSE': 'USD', 'NASDAQ': 'USD'}


class Attestation(Model):
    attested_by: str = Field(min_length=2, max_length=120)
    attested_at: AwareDatetime
    rights_statement: str = Field(min_length=20, max_length=600)
    source_name: str = Field(min_length=2, max_length=120)
    source_url: str = Field(pattern=r'^https://[^\s]+$', max_length=500)


class ImportFile(Model):
    schema_version: str = '1'
    kind: str = Field(pattern=r'^(prices|splits|consensus|peers)$')
    ticker: str
    exchange: str = Field(pattern=r'^(TWSE|TPEx|NYSE|NASDAQ)$')
    attestation: Attestation
    rows: list[dict] = Field(min_length=1, max_length=4000)


def _num(text, field):
    try:
        return float(str(text).replace(',', ''))
    except (TypeError, ValueError):
        raise ValueError(f'{field} is not a number: {text!r}') from None


def read_csv(kind, path, ticker, exchange, attestation, now=None):
    """Validate an operator CSV into a normalized import file; nothing is guessed or filled in."""
    att = Attestation.model_validate(attestation)
    if AI_NAMES.search(att.attested_by):
        raise ValueError('An operator attestation must be made by a person, not an AI agent')
    if att.attested_at > (now or datetime.now(timezone.utc)):
        raise ValueError('Attestation cannot be dated in the future')
    with open(path, newline='', encoding='utf-8') as handle:
        raw = list(csv.DictReader(handle))
    rows, seen = [], set()
    for i, row in enumerate(raw, start=2):
        try:
            rows.append(_row(kind, row, exchange))
        except ValueError as error:
            raise ValueError(f'line {i}: {error}') from None
        key = tuple(sorted((k, v) for k, v in rows[-1].items() if k in ('date', 'provider', 'metric', 'period_end', 'peer')))
        if key in seen:
            raise ValueError(f'line {i}: duplicate record {key}')
        seen.add(key)
    return ImportFile(kind=kind, ticker=ticker, exchange=exchange, attestation=att, rows=rows)


def _row(kind, row, exchange):
    if kind == 'prices':
        day = date.fromisoformat(row['date'])
        close = _num(row['close'], 'close')
        if close <= 0:
            raise ValueError('close must be positive; missing prices are omitted, never zero')
        currency = (row.get('currency') or '').upper()
        if currency != CURRENCY[exchange]:
            raise ValueError(f'currency {currency or "missing"} does not match {exchange} ({CURRENCY[exchange]})')
        trading, verified = is_trading_day(exchange, day)
        if verified and not trading:
            raise ValueError(f'{day} is not a {exchange} trading day')
        return {'date': day.isoformat(), 'close': close, 'currency': currency,
                'adjustment': row.get('adjustment') or 'unadjusted'}
    if kind == 'splits':
        ratio = _num(row['ratio'], 'ratio')
        if ratio <= 0:
            raise ValueError('split ratio must be positive')
        return {'date': date.fromisoformat(row['date']).isoformat(), 'ratio': ratio}
    if kind == 'consensus':
        basis = row.get('accounting_basis', '').lower()
        if basis not in ('gaap', 'ifrs', 'non_gaap'):
            raise ValueError('accounting_basis must be gaap, ifrs or non_gaap')
        return {'provider': row['provider'].strip(), 'metric': row['metric'].strip(), 'period_end': date.fromisoformat(row['period_end']).isoformat(),
                'period_label': row.get('period_label') or row['period_end'], 'accounting_basis': basis,
                'value': _num(row['value'], 'value'), 'unit': row['unit'].strip(), 'currency': (row.get('currency') or '').upper() or None,
                'estimate_date': date.fromisoformat(row['estimate_date']).isoformat(),
                'analyst_count': int(row['analyst_count']) if row.get('analyst_count') else None}
    if kind == 'peers':
        metric = row['metric'].strip()
        if metric not in ('pe_trailing', 'ev_ebitda', 'p_fcf', 'p_b'):
            raise ValueError('peer metric must be pe_trailing, ev_ebitda, p_fcf or p_b')
        return {'peer': row['peer'].strip(), 'metric': metric, 'value': _num(row['value'], 'value'),
                'as_of': date.fromisoformat(row['as_of']).isoformat()}
    raise ValueError(f'unknown import kind {kind}')


def evidence(imported, request):
    """Evidence records for one import file, cut at the research cutoff."""
    imp = ImportFile.model_validate(imported)
    if imp.ticker != request.ticker or imp.exchange != request.exchange:
        raise ValueError('Operator import belongs to another instrument')
    att, cutoff = imp.attestation, request.research_as_of
    close_time, offset = CLOSE_TIME[imp.exchange]
    tz = timezone(timedelta(hours=offset))
    base = dict(entity_id=imp.ticker, publisher=f'{att.source_name}（使用者匯入）', source_type='secondary',
                source_url=att.source_url, retrieved_at=att.attested_at, research_as_of=cutoff,
                verification_status='verified', license_status='confirmed', internal_use_allowed=True,
                attribution=f'資料來源：{att.source_name}；由 {att.attested_by} 匯入並聲明可供本人研究使用',
                terms_url=att.source_url, license_checked_at=att.attested_at, license_source_id=SOURCE_ID,
                publication_basis='source_declared_date')
    out = []
    for r in imp.rows:
        if imp.kind == 'prices':
            day = date.fromisoformat(r['date'])
            stamp = datetime.combine(day, close_time, tzinfo=tz)
            if stamp > cutoff:
                continue
            out.append(Evidence(**base, evidence_id=f'import-{imp.ticker}-close-{day}'[:80], kind='price', metric_key='close',
                                claim=f'{imp.ticker} {day} 收盤 {r["close"]} {r["currency"]}（{r["adjustment"]}，使用者匯入）',
                                value=r['close'], unit='per_share', currency=r['currency'], period_start=day, period_end=day,
                                period_basis='trading_day', source_record_id=f'{day}', event_time=stamp, published_at=stamp,
                                source_period_raw=r['adjustment']))
        elif imp.kind == 'splits':
            day = date.fromisoformat(r['date'])
            stamp = datetime.combine(day, time(0), tzinfo=tz)
            if stamp > cutoff:
                continue
            out.append(Evidence(**base, evidence_id=f'import-{imp.ticker}-split-{day}'[:80], kind='event',
                                financial_metric='split_ratio', value=r['ratio'], period_end=day, period_start=day,
                                period_basis='instant', claim=f'{imp.ticker} {day} 股票分割比例 {r["ratio"]}（使用者匯入）',
                                source_record_id=f'split-{day}', event_time=stamp, published_at=stamp))
        elif imp.kind == 'consensus':
            stamp = datetime.combine(date.fromisoformat(r['estimate_date']), time(23, 59), tzinfo=tz)
            if stamp > cutoff:
                continue
            out.append(Evidence(**base, evidence_id=f'import-{imp.ticker}-cons-{r["provider"][:12]}-{r["metric"]}-{r["period_end"]}-{r["estimate_date"]}'[:80].replace(' ', '_'),
                                kind='event', statement_kind='consensus_estimate', institution=r['provider'],
                                financial_metric=r['metric'], value=r['value'], unit=r['unit'], currency=r['currency'],
                                accounting_basis=r['accounting_basis'], target_period=r['period_label'],
                                target_period_end=date.fromisoformat(r['period_end']),
                                claim=f'{r["provider"]} 對 {r["period_label"]} {r["metric"]}（{r["accounting_basis"]}）的共識 {r["value"]}，'
                                      f'估計日 {r["estimate_date"]}' + (f'，{r["analyst_count"]} 位分析師' if r['analyst_count'] else ''),
                                source_record_id=f'{r["provider"]}:{r["estimate_date"]}', event_time=stamp, published_at=stamp))
        elif imp.kind == 'peers':
            day = date.fromisoformat(r['as_of'])
            stamp = datetime.combine(day, time(23, 59), tzinfo=tz)
            if stamp > cutoff:
                continue
            out.append(Evidence(**base, evidence_id=f'import-{imp.ticker}-peer-{r["peer"]}-{r["metric"]}-{day}'[:80].replace(' ', '_'),
                                kind='financial', financial_metric=f'peer_{r["metric"]}', value=r['value'], unit='x',
                                institution=r['peer'], period_start=day, period_end=day, period_basis='instant',
                                claim=f'同業 {r["peer"]} {r["metric"]} {r["value"]} 倍（{day}，使用者匯入）',
                                source_record_id=f'{r["peer"]}:{day}', event_time=stamp, published_at=stamp))
    return out
