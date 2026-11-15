"""Offline, test-only builders for real-labelled DELL inputs. Values are invented for unit tests and
are never research facts; the 'real' label only exercises the real-data code paths."""
from datetime import date, datetime, timedelta, timezone
from stock_report.models import (Entity, Evidence, EarningsPreviewReview, FilingDiscovery,
                                 LatestFinancialReview, Request, ResearchInput)
from stock_report.quant import calculate

CUTOFF = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
CIK = '0001571996'
ACCESSION = '0001571996-26-000123'
DOC = f'https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{ACCESSION.replace("-", "")}/dell-20261030.htm'
SUBMISSIONS = f'https://data.sec.gov/submissions/CIK{CIK}.json'
QUARTER_END = date(2026, 10, 30)
FILED = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)


def request(**changes):
    return Request(mode='stock', ticker='DELL', exchange='NYSE', research_as_of=CUTOFF, **changes)


def evidence(evidence_id, kind='financial', **changes):
    base = dict(evidence_id=evidence_id, entity_id='DELL', claim=f'test-only {evidence_id}', kind=kind,
                source_url=DOC, publisher='SEC EDGAR (test)', source_type='official',
                source_record_id=ACCESSION, event_time=FILED, published_at=FILED,
                retrieved_at=CUTOFF + timedelta(minutes=1), research_as_of=CUTOFF,
                verification_status='verified', license_status='confirmed', internal_use_allowed=True,
                attribution='test-only attribution', terms_url='https://www.sec.gov/privacy',
                license_checked_at=CUTOFF, license_source_id='sec-edgar-archives-filing')
    base.update(changes)
    return Evidence(**base)


def quarter(metric, value, basis='single_quarter', start=date(2026, 8, 1), end=QUARTER_END, **changes):
    return evidence(f'q-{metric}-{basis}', financial_metric=metric, value=value, unit='USD', currency='USD',
                    period_start=start, period_end=end, period_basis=basis, period='Q3 FY2027', **changes)


def identity():
    return evidence('dell-identity', kind='identity', claim='Dell Technologies Inc. (test identity)')


def discovery_record(**changes):
    base = dict(source_url=SUBMISSIONS, source_record_id=ACCESSION, license_source_id='sec-submissions-history')
    base.update(changes)
    return evidence('sec-DELL-latest-filing', kind='identity', **base)


def discovery(**changes):
    base = dict(entity_id='DELL', cik=CIK, as_of=CUTOFF, discovered_at=CUTOFF + timedelta(minutes=1),
                form='10-Q', accession=ACCESSION, period_end=QUARTER_END, published_at=FILED,
                document_url=DOC, discovery_evidence_id='sec-DELL-latest-filing')
    base.update(changes)
    return FilingDiscovery(**base)


def complete_rows():
    return [quarter('revenue', 30e9), quarter('net_income', 1.2e9),
            quarter('operating_cash_flow', 2.0e9, basis='cumulative_ytd', start=date(2026, 1, 31))]


def preview(**changes):
    base = dict(status='unavailable', as_of=CUTOFF, searched_at=CUTOFF + timedelta(minutes=5),
                search_queries=['Dell next earnings date'], sources_checked=['https://investors.delltechnologies.com/'],
                limitations=['No reliable named external estimate was found (test).'])
    base.update(changes)
    return base


def real_dell(rows=None, review=True, disc=None, record=None, extra=(), earnings=None, req=None):
    rows = complete_rows() if rows is None else rows
    items = [identity(), record or discovery_record(), *rows, *extra]
    latest = LatestFinancialReview(discovery=disc or discovery(), evidence_ids=[e.evidence_id for e in rows]) if review else None
    req = req or request()
    return ResearchInput(dataset_kind='real', request=req,
                         entities=[Entity(entity_id='DELL', name='Dell Technologies', exchange='NYSE', currency='USD')],
                         evidence=items, metrics=calculate(items, req.research_as_of), latest_financials=latest,
                         earnings_preview=EarningsPreviewReview.model_validate(earnings) if earnings else None,
                         versions={'dataset': 'test-real-label'})
