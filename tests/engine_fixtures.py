"""Regression fixtures shaped like the DELL and 2330.TW research inputs.

Values are test-only (rounded from the 2026-11-15 reports) and exist to exercise US and Taiwan
conventions through one engine; they are not research facts.
"""
from datetime import date, datetime, timedelta, timezone
from stock_report.models import Entity, Evidence, Request, ResearchInput
from stock_report.quant import calculate

UTC = timezone.utc
TAIPEI = timezone(timedelta(hours=8))
NEW_YORK = timezone(timedelta(hours=-5))

# DELL fiscal quarters (FY ends late January): windows end on these dates.
DELL_QUARTERS = [(date(2023, 8, 5), date(2023, 11, 3)), (date(2023, 11, 4), date(2024, 2, 2)),
                 (date(2024, 2, 3), date(2024, 5, 3)), (date(2024, 5, 4), date(2024, 8, 2)), (date(2024, 8, 3), date(2024, 11, 1)),
                 (date(2024, 11, 2), date(2025, 1, 31)), (date(2025, 2, 1), date(2025, 5, 2)),
                 (date(2025, 5, 3), date(2025, 8, 1)), (date(2025, 8, 2), date(2025, 10, 31)),
                 (date(2025, 11, 1), date(2026, 1, 30)), (date(2026, 1, 31), date(2026, 5, 1)),
                 (date(2026, 5, 2), date(2026, 7, 31))]
TSMC_QUARTERS = [(date(2024, 7, 1), date(2024, 9, 30)), (date(2024, 10, 1), date(2024, 12, 31)),
                 (date(2025, 1, 1), date(2025, 3, 31)), (date(2025, 4, 1), date(2025, 6, 30)),
                 (date(2025, 7, 1), date(2025, 9, 30)), (date(2025, 10, 1), date(2025, 12, 31)),
                 (date(2026, 1, 1), date(2026, 3, 31)), (date(2026, 4, 1), date(2026, 6, 30)),
                 (date(2026, 7, 1), date(2026, 9, 30))]


def ev(eid, ticker, cutoff, **kw):
    published = kw.pop('published_at', cutoff - timedelta(days=30))
    base = dict(evidence_id=eid, entity_id=ticker, claim=kw.pop('claim', f'test-only {eid}'), kind='financial',
                source_url='https://example.test/filing', publisher='Test official filer', source_type='official',
                source_record_id=eid, event_time=kw.pop('event_time', published), published_at=published,
                retrieved_at=cutoff, research_as_of=cutoff, verification_status='verified', license_status='confirmed',
                internal_use_allowed=True, attribution='test-only', terms_url='https://example.test/terms',
                license_checked_at=cutoff)
    base.update(kw)
    return Evidence(**base)


def fin(ticker, cutoff, metric, value, start, end, basis, unit, currency, **kw):
    return ev(f'{ticker.lower().replace(".", "")}-{metric}-{end}-{basis}', ticker, cutoff, financial_metric=metric,
              value=value, unit=unit, currency=currency, period_start=start if basis != 'instant' else end,
              period_end=end, period_basis=basis, published_at=kw.pop('published_at', datetime.combine(
                  end + timedelta(days=40), datetime.min.time(), tzinfo=UTC)), **kw)


def dell(cutoff=datetime(2026, 11, 15, 9, 0, tzinfo=UTC), price=150.0, price_day=date(2026, 11, 13),
         revenue_scale=1.0, net_income=None, extra=(), drop=()):
    t, items = 'DELL', []
    revenues = [22.3, 22.3, 22.2, 24.4, 24.4, 23.9, 23.4, 29.8, 27.0, 33.0, 37.0, 46.97]
    incomes = net_income or [1.0, 1.16, 0.96, 0.84, 1.13, 1.53, 0.97, 1.16, 1.5, 1.7, 2.6, 4.13]
    eps = [round(i / 0.70, 2) for i in incomes]
    for (start, end), r, n, e in zip(DELL_QUARTERS, revenues, incomes, eps):
        items += [fin(t, cutoff, 'revenue', r * 1e9 * revenue_scale, start, end, 'single_quarter', 'USD', 'USD', accounting_basis='gaap'),
                  fin(t, cutoff, 'operating_income', r * 1e9 * 0.075, start, end, 'single_quarter', 'USD', 'USD'),
                  fin(t, cutoff, 'gross_profit', r * 1e9 * 0.21, start, end, 'single_quarter', 'USD', 'USD'),
                  fin(t, cutoff, 'net_income', n * 1e9, start, end, 'single_quarter', 'USD', 'USD'),
                  fin(t, cutoff, 'income_before_tax', n * 1e9 / 0.82, start, end, 'single_quarter', 'USD', 'USD'),
                  fin(t, cutoff, 'diluted_eps', e, start, end, 'single_quarter', 'per_share', 'USD'),
                  fin(t, cutoff, 'depreciation_amortization', 0.8e9, start, end, 'single_quarter', 'USD', 'USD'),
                  fin(t, cutoff, 'diluted_shares', 700e6, start, end, 'single_quarter', 'shares', None)]
    # Cash flow only as year-to-date windows of fiscal 2026 and 2027, as a 10-Q reports it.
    for start, end, ocf, capex in [(date(2025, 2, 1), date(2025, 5, 2), 2.8, 0.7), (date(2025, 2, 1), date(2025, 8, 1), 5.3, 1.4),
                                   (date(2025, 2, 1), date(2025, 10, 31), 7.0, 2.1), (date(2025, 2, 1), date(2026, 1, 30), 9.0, 2.8),
                                   (date(2026, 1, 31), date(2026, 5, 1), 4.1, 0.6), (date(2026, 1, 31), date(2026, 7, 31), 6.3, 1.8)]:
        basis = 'single_quarter' if (end - start).days < 100 else ('fiscal_year' if (end - start).days > 350 else 'cumulative_ytd')
        items += [fin(t, cutoff, 'operating_cash_flow', ocf * 1e9, start, end, basis, 'USD', 'USD'),
                  fin(t, cutoff, 'capital_expenditure', capex * 1e9, start, end, basis, 'USD', 'USD')]
    for end, cash, debt, ar, inv, ap, eq in [(date(2025, 8, 1), 9.0, 30.0, 14.0, 8.0, 25.0, -2.0),
                                             (date(2026, 1, 30), 11.5, 31.5, 17.6, 10.4, 27.0, -1.5),
                                             (date(2026, 7, 31), 11.6, 34.5, 22.9, 21.3, 33.0, -1.0)]:
        items += [fin(t, cutoff, 'cash_equivalents', cash * 1e9, None, end, 'instant', 'USD', 'USD'),
                  fin(t, cutoff, 'long_term_debt', debt * 1e9, None, end, 'instant', 'USD', 'USD'),
                  fin(t, cutoff, 'accounts_receivable_net', ar * 1e9, None, end, 'instant', 'USD', 'USD'),
                  fin(t, cutoff, 'inventory', inv * 1e9, None, end, 'instant', 'USD', 'USD'),
                  fin(t, cutoff, 'accounts_payable', ap * 1e9, None, end, 'instant', 'USD', 'USD'),
                  fin(t, cutoff, 'total_equity', eq * 1e9, None, end, 'instant', 'USD', 'USD')]
    items.append(ev('dell-guidance-eps-gaap', t, cutoff, kind='event', statement_kind='management_guidance',
                    financial_metric='diluted_eps', value=6.10, unit='per_share', currency='USD', accounting_basis='gaap',
                    target_period='FY2027 Q3', target_period_end=date(2026, 10, 30), published_at=datetime(2026, 9, 1, 22, tzinfo=UTC)))
    items.append(ev('dell-guidance-rev', t, cutoff, kind='event', statement_kind='management_guidance',
                    financial_metric='revenue', value=49e9, unit='USD', currency='USD', target_period='FY2027 Q3',
                    target_period_end=date(2026, 10, 30), published_at=datetime(2026, 9, 1, 22, tzinfo=UTC)))
    if price is not None:
        close = datetime.combine(price_day, datetime.min.time(), tzinfo=NEW_YORK) + timedelta(hours=16)
        items.append(ev('dell-close', t, cutoff, kind='price', metric_key='close', value=price, unit='per_share',
                        currency='USD', period_start=price_day, period_end=price_day, period_basis='trading_day',
                        event_time=close, published_at=close + timedelta(hours=1)))
    items.append(ev('dell-identity', t, cutoff, kind='identity'))
    items = [e for e in items if e.evidence_id not in drop] + list(extra)
    request = Request(mode='stock', ticker=t, exchange='NYSE', research_as_of=cutoff, report_profile='investment',
                      max_pdf_pages=8)
    return ResearchInput(dataset_kind='real', request=request,
                         entities=[Entity(entity_id=t, name='Dell Technologies (test)', exchange='NYSE', currency='USD')],
                         evidence=items, metrics=calculate(items, cutoff), versions={'dataset': 'test'})


def tsmc(cutoff=datetime(2026, 11, 15, 21, 20, tzinfo=TAIPEI), price=2350.0, price_day=date(2026, 11, 13)):
    t, items = '2330.TW', []
    revenues = [759.7, 868.5, 839.3, 933.8, 989.9, 1046.1, 1134.1, 1270.4, 1494.2]
    incomes = [325.3, 374.7, 361.6, 398.3, 452.3, 505.7, 572.5, 706.6, 763.1]
    for (start, end), r, n in zip(TSMC_QUARTERS, revenues, incomes):
        items += [fin(t, cutoff, 'revenue', r * 1e9, start, end, 'single_quarter', 'TWD', 'TWD', accounting_basis='ifrs'),
                  fin(t, cutoff, 'net_income', n * 1e9, start, end, 'single_quarter', 'TWD', 'TWD', accounting_basis='ifrs'),
                  fin(t, cutoff, 'income_before_tax', n * 1e9 / 0.84, start, end, 'single_quarter', 'TWD', 'TWD', accounting_basis='ifrs'),
                  fin(t, cutoff, 'gross_profit', r * 1e9 * 0.62, start, end, 'single_quarter', 'TWD', 'TWD'),
                  fin(t, cutoff, 'operating_income', r * 1e9 * 0.52, start, end, 'single_quarter', 'TWD', 'TWD'),
                  fin(t, cutoff, 'diluted_eps', round(n / 25.93, 2), start, end, 'single_quarter', 'per_share', 'TWD'),
                  fin(t, cutoff, 'diluted_shares', 25.93e9, start, end, 'single_quarter', 'shares', None)]
    items += [fin(t, cutoff, 'operating_cash_flow', 2402.1e9, date(2026, 1, 1), date(2026, 9, 30), 'cumulative_ytd', 'TWD', 'TWD'),
              fin(t, cutoff, 'net_income', 2042.2e9, date(2026, 1, 1), date(2026, 9, 30), 'cumulative_ytd', 'TWD', 'TWD'),
              fin(t, cutoff, 'cash_equivalents', 3621.9e9, None, date(2026, 9, 30), 'instant', 'TWD', 'TWD'),
              fin(t, cutoff, 'bonds_payable', 770.3e9, None, date(2026, 9, 30), 'instant', 'TWD', 'TWD')]
    items.append(ev('tsmc-guidance-rev-low', t, cutoff, kind='event', statement_kind='management_guidance',
                    financial_metric='revenue', value=52.3e9, unit='USD', currency='USD', target_period='Q4 2026',
                    target_period_end=date(2026, 12, 31)))
    if price is not None:
        close = datetime.combine(price_day, datetime.min.time(), tzinfo=TAIPEI) + timedelta(hours=14)
        items.append(ev('tsmc-close', t, cutoff, kind='price', metric_key='close', value=price, unit='per_share',
                        currency='TWD', period_start=price_day, period_end=price_day, period_basis='trading_day',
                        event_time=close, published_at=close + timedelta(hours=3)))
    items.append(ev('tsmc-identity', t, cutoff, kind='identity'))
    request = Request(mode='stock', ticker=t, exchange='TWSE', research_as_of=cutoff, report_profile='investment',
                      max_pdf_pages=8)
    return ResearchInput(dataset_kind='real', request=request,
                         entities=[Entity(entity_id=t, name='台積電（測試）', exchange='TWSE', currency='TWD')],
                         evidence=items, metrics=calculate(items, cutoff), versions={'dataset': 'test'})
