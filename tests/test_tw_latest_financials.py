"""Taiwan latest-financials freshness follows the statutory deadline (Securities and Exchange Act Art. 36)."""
from datetime import date, datetime, timedelta, timezone
import pytest
from stock_report.models import (Entity, Evidence, ResearchInput, Request, financial_review_state,
                                 tw_required_period_end)
from stock_report.quant import calculate

TAIPEI = timezone(timedelta(hours=8))
CUTOFF = datetime(2026, 11, 15, 21, 0, tzinfo=TAIPEI)
URL = 'https://www.sec.gov/Archives/edgar/data/1046179/000104617926000797/tsm-boardx202611xx.htm'


@pytest.mark.parametrize('day,expected', [
    (datetime(2026, 11, 14, 23, 0, tzinfo=TAIPEI), date(2026, 6, 30)),   # Q3 due on 11/14: not yet required
    (datetime(2026, 11, 15, 0, 1, tzinfo=TAIPEI), date(2026, 9, 30)),    # deadline day passed
    (datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc), date(2026, 9, 30)),  # 11/15 00:30 in Taipei
    (datetime(2026, 3, 31, 12, 0, tzinfo=TAIPEI), date(2025, 9, 30)),     # annual due 3/31: not yet
    (datetime(2026, 4, 1, 9, 0, tzinfo=TAIPEI), date(2025, 12, 31)),
    (datetime(2026, 5, 16, 9, 0, tzinfo=TAIPEI), date(2026, 3, 31)),
    (datetime(2026, 8, 15, 9, 0, tzinfo=TAIPEI), date(2026, 6, 30)),
])
def test_required_period_follows_statutory_deadlines(day, expected):
    assert tw_required_period_end(day) == expected


def test_non_december_fiscal_year_is_not_assumed():
    assert tw_required_period_end(CUTOFF, fiscal_year_end_month=6) is None


def fact(evidence_id, metric, end, basis='cumulative_ytd', start=None, **changes):
    published = datetime(2026, 11, 10, 8, 0, tzinfo=TAIPEI)
    base = dict(evidence_id=evidence_id, entity_id='2330.TW', claim='test-only', kind='financial',
                financial_metric=metric, value=1.0, unit='TWD', currency='TWD', period_start=start or date(end.year, 1, 1),
                period_end=end, period_basis=basis, source_url=URL, publisher='SEC EDGAR (test)',
                source_type='official', source_record_id='0001046179-26-000797', event_time=published,
                published_at=published, retrieved_at=CUTOFF, research_as_of=CUTOFF,
                verification_status='verified', license_status='confirmed', internal_use_allowed=True,
                attribution='test-only', terms_url='https://www.sec.gov/privacy', license_checked_at=CUTOFF)
    base.update(changes)
    return Evidence(**base)


def identity():
    return fact('tw-identity', None, date(2026, 9, 30), kind='identity', financial_metric=None, value=None,
                unit=None, currency=None, period_start=None, period_end=None, period_basis=None)


def data(*rows, cutoff=CUTOFF):
    request = Request(mode='stock', ticker='2330.TW', exchange='TWSE', research_as_of=cutoff)
    items = [identity(), *rows]
    return ResearchInput(dataset_kind='real', request=request,
                         entities=[Entity(entity_id='2330.TW', name='台積電', exchange='TWSE', currency='TWD')],
                         evidence=items, metrics=calculate(items, cutoff), versions={'dataset': 'test'})


Q3 = date(2026, 9, 30)
Q2 = date(2026, 6, 30)


def test_latest_required_period_with_revenue_and_net_income_is_covered():
    assert financial_review_state(data(fact('r', 'revenue', Q3), fact('n', 'net_income', Q3))) == []


def test_single_quarter_facts_also_cover_the_period():
    rows = [fact('r', 'revenue', Q3, 'single_quarter', date(2026, 7, 1)),
            fact('n', 'net_income', Q3, 'single_quarter', date(2026, 7, 1))]
    assert financial_review_state(data(*rows)) == []


def test_previous_quarter_after_deadline_is_stale():
    assert financial_review_state(data(fact('r', 'revenue', Q2), fact('n', 'net_income', Q2))) == \
        ['LATEST_FINANCIAL_REPORT_STALE']


def test_previous_quarter_is_enough_before_the_deadline():
    early = datetime(2026, 11, 14, 12, 0, tzinfo=TAIPEI)
    rows = [fact(i, m, Q2, research_as_of=early, retrieved_at=early, published_at=early - timedelta(days=60),
                 event_time=early - timedelta(days=60)) for i, m in (('r', 'revenue'), ('n', 'net_income'))]
    assert financial_review_state(data(*rows, cutoff=early)) == []


def test_missing_net_income_for_the_latest_period_is_incomplete():
    assert financial_review_state(data(fact('r', 'revenue', Q3))) == ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']


@pytest.mark.parametrize('changes', [
    {'internal_use_allowed': False}, {'license_status': 'unknown'}, {'source_type': 'secondary'},
    {'statement_kind': 'management_guidance', 'target_period': 'Q4 2026'},
])
def test_unlicensed_secondary_or_forward_figures_do_not_count(changes):
    rows = [fact('r', 'revenue', Q3, **changes), fact('n', 'net_income', Q3, **changes)]
    assert financial_review_state(data(*rows)) == ['LATEST_FINANCIAL_REPORT_MISSING']


def test_figures_published_after_cutoff_do_not_count():
    late = CUTOFF + timedelta(hours=1)
    rows = [fact('r', 'revenue', Q3, published_at=late, event_time=late),
            fact('n', 'net_income', Q3, published_at=late, event_time=late)]
    assert financial_review_state(data(*rows)) == ['LATEST_FINANCIAL_REPORT_MISSING']


def test_no_financial_evidence_is_missing():
    assert financial_review_state(data()) == ['LATEST_FINANCIAL_REPORT_MISSING']


def test_tw_adapter_names_financial_metrics():
    from stock_report.sources import TW_INCOME_ROLES
    assert {spec['metric'] for spec in TW_INCOME_ROLES.values()} == {'revenue', 'net_income', 'basic_eps'}


def test_tsmc_sec_filings_bind_to_the_twse_ticker():
    from stock_report.sources import FILING_PROFILES
    from stock_report.stock_events import filing_url_problem
    profile = FILING_PROFILES['sec-edgar-archives-filing']
    assert filing_url_problem(profile, URL, '2330.TW') is None
    assert filing_url_problem(profile, URL.replace('/1046179/', '/1045810/'), '2330.TW') == 'filing_url_belongs_to_another_issuer'
