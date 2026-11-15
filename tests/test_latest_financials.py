"""Latest published financial report is mandatory for completed real stock research."""
from datetime import date, datetime, timedelta, timezone
import pytest
from stock_report.models import HorizonProposal, Proposal, financial_review_state
from stock_report.pipeline import completion
from stock_report.policy import evaluate
from stock_report.reviewed_collect import parse_submissions
from stock_report.storage import digest
from v2_fixtures import (CIK, CUTOFF, DOC, FILED, QUARTER_END, discovery, discovery_record, preview,
                         quarter, real_dell)


def test_latest_quarter_with_required_statements_is_covered():
    assert financial_review_state(real_dell()) == []


def test_missing_review_blocks_completion():
    data = real_dell(review=False)
    assert financial_review_state(data) == ['LATEST_FINANCIAL_REPORT_MISSING']
    result = completion(data)
    assert result['research_complete'] is False
    assert {'LATEST_FINANCIAL_REPORT_MISSING', 'EARNINGS_PREVIEW_SEARCH_MISSING'} <= set(result['completion_blockers'])


def test_annual_figures_cannot_masquerade_as_the_newer_quarter():
    annual = [quarter(m, v, basis='fiscal_year', start=date(2025, 2, 1), end=date(2026, 1, 30))
              for m, v in (('revenue', 95e9), ('net_income', 4e9), ('operating_cash_flow', 6e9))]
    assert financial_review_state(real_dell(rows=annual)) == ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']


def test_quarterly_filing_needs_a_single_quarter_revenue_not_only_ytd():
    rows = [quarter('revenue', 80e9, basis='cumulative_ytd', start=date(2026, 1, 31)),
            quarter('net_income', 1.2e9), quarter('operating_cash_flow', 2e9)]
    assert financial_review_state(real_dell(rows=rows)) == ['LATEST_QUARTER_MISSING']


def test_annual_report_is_valid_when_it_is_the_latest_filing():
    end = date(2026, 1, 30)
    rows = [quarter(m, v, basis='fiscal_year', start=date(2025, 2, 1), end=end)
            for m, v in (('revenue', 95e9), ('net_income', 4e9), ('operating_cash_flow', 6e9))]
    assert financial_review_state(real_dell(rows=rows, disc=discovery(form='10-K', period_end=end))) == []


def test_missing_required_metric_is_incomplete():
    rows = [r for r in [quarter('revenue', 30e9), quarter('net_income', 1e9)]]
    assert financial_review_state(real_dell(rows=rows)) == ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']


@pytest.mark.parametrize('changes', [
    {'published_at': CUTOFF + timedelta(hours=1)},          # filed after the cutoff: lookahead
    {'discovered_at': CUTOFF - timedelta(days=1)},          # discovery run before the cutoff
    {'as_of': CUTOFF - timedelta(days=1)},                  # reviewed a different cutoff
    {'cik': '0001045810'},                                   # another issuer
    {'document_url': DOC.replace('www.sec.gov', 'evil.example')},
])
def test_identity_or_time_mismatch_is_invalid(changes):
    assert financial_review_state(real_dell(disc=discovery(**changes))) == ['LATEST_FINANCIAL_REVIEW_INVALID']


@pytest.mark.parametrize('changes', [
    {'license_source_id': 'sec-xbrl-companyconcept'},
    {'internal_use_allowed': False},
    {'license_status': 'unknown'},
    {'source_record_id': '0001571996-26-000999'},
])
def test_discovery_must_come_from_the_reviewed_submissions_index(changes):
    assert financial_review_state(real_dell(record=discovery_record(**changes))) == ['LATEST_FINANCIAL_DISCOVERY_UNVERIFIED']


def test_statement_published_after_cutoff_is_not_counted():
    late = FILED.replace(year=2027)
    rows = [quarter('revenue', 30e9, published_at=late, event_time=late),
            quarter('net_income', 1.2e9), quarter('operating_cash_flow', 2e9)]
    assert financial_review_state(real_dell(rows=rows)) == ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']


def test_forward_guidance_is_never_counted_as_reported_statements():
    rows = [quarter('revenue', 30e9, statement_kind='management_guidance', target_period='Q3 FY2027'),
            quarter('net_income', 1.2e9), quarter('operating_cash_flow', 2e9)]
    assert financial_review_state(real_dell(rows=rows)) == ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']


def test_gate_downgrades_direction_without_latest_filing_or_preview_search():
    data = real_dell(review=False)
    proposal = Proposal(input_sha256=digest(data), model='manual-test', outlooks=[
        HorizonProposal(entity_id='DELL', horizon=h, outlook='bullish',
                        supporting_evidence_ids=['q-revenue-single_quarter', 'q-net_income-single_quarter'],
                        interpretation='測試用解讀。', invalidation_conditions=['測試條件'])
        for h in ('3-6m', '6-12m')])
    decision = evaluate(data, proposal)
    assert [p.outlook for p in decision.outlooks] == ['insufficient'] * 2
    codes = {f.rule_id for f in decision.findings}
    assert {'LATEST_FINANCIAL_REPORT_MISSING', 'EARNINGS_PREVIEW_SEARCH_MISSING'} <= codes


def test_completion_records_covered_status_and_preview():
    result = completion(real_dell(earnings=preview()))
    assert result['latest_financial_report_status'] == ['covered']
    # Only the (absent) recent-event review still blocks; filing coverage and preview search do not.
    assert result['completion_blockers'] and not any(
        c.startswith(('LATEST_', 'EARNINGS_')) for c in result['completion_blockers'])
    assert result['earnings_preview']['status'] == 'unavailable'


# --- SEC submissions discovery -------------------------------------------------------------

ROW = {'ticker': 'DELL', 'cik': CIK, 'expected_entity_name_contains': 'DELL'}


def submissions(rows):
    keys = ('form', 'filingDate', 'reportDate', 'accessionNumber', 'primaryDocument', 'acceptanceDateTime')
    return {'cik': CIK, 'name': 'Dell Technologies Inc.',
            'filings': {'recent': {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}


Q2 = ('10-Q', '2026-09-05', '2026-07-31', '0001571996-26-000100', 'dell-q2.htm', '2026-09-05T16:05:00.000Z')
K = ('10-K', '2026-03-20', '2026-01-30', '0001571996-26-000050', 'dell-10k.htm', '2026-03-20T16:05:00.000Z')
K_AMEND = ('10-K/A', '2026-10-01', '2026-01-30', '0001571996-26-000110', 'dell-10ka.htm', '2026-10-01T16:05:00.000Z')
Q3_FUTURE = ('10-Q', '2026-12-05', '2026-10-30', '0001571996-26-000140', 'dell-q3.htm', '2026-12-05T16:05:00.000Z')
OBSERVED = CUTOFF + timedelta(minutes=1)


def test_discovery_picks_latest_period_and_excludes_future_filings():
    found = parse_submissions(submissions([Q3_FUTURE, Q2, K]), ROW, CUTOFF, OBSERVED)
    assert (found.form, found.accession, found.period_end) == ('10-Q', Q2[3], date(2026, 7, 31))
    assert found.document_url.endswith('/000157199626000100/dell-q2.htm')


def test_later_amendment_of_annual_report_does_not_displace_newer_quarter():
    found = parse_submissions(submissions([K_AMEND, Q2, K]), ROW, CUTOFF, OBSERVED)
    assert found.accession == Q2[3]


def test_original_preferred_over_amendment_for_the_same_period():
    found = parse_submissions(submissions([K_AMEND, K]), ROW, CUTOFF, OBSERVED)
    assert found.form == '10-K'


def test_cutoff_before_every_filing_reports_no_coverage():
    with pytest.raises(ValueError, match='No eligible'):
        parse_submissions(submissions([Q2]), ROW, datetime(2026, 1, 1, tzinfo=timezone.utc), OBSERVED)


def test_submissions_from_another_issuer_rejected():
    payload = submissions([Q2]); payload['cik'] = '0001045810'
    with pytest.raises(ValueError, match='another issuer'):
        parse_submissions(payload, ROW, CUTOFF, OBSERVED)


def test_inconsistent_columns_rejected():
    payload = submissions([Q2, K]); payload['filings']['recent']['form'].pop()
    with pytest.raises(ValueError):
        parse_submissions(payload, ROW, CUTOFF, OBSERVED)
