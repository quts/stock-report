"""Read-only operational readiness; never enables sources or reads login credentials."""
from importlib.metadata import version
import os
from pathlib import Path
from xml.etree import ElementTree
from .licensing import load_registry
from .mock import VERSIONS


def recorded_tests(path):
    path = Path(path)
    if not path.exists():
        return {'status': 'NOT_RUN', 'path': str(path)}
    suites = list(ElementTree.parse(path).getroot().iter('testsuite'))
    counts = {name: sum(int(s.get(name, '0')) for s in suites)
              for name in ('tests', 'failures', 'errors', 'skipped')}
    if counts['failures'] or counts['errors']:
        status = 'FAILED'
    elif not counts['tests']:
        status = 'NOT_RUN'
    elif counts['skipped'] >= counts['tests']:
        status = 'SKIPPED'
    elif counts['skipped']:
        status = 'PARTIAL'
    else:
        status = 'PASSED'
    return {'status': status,
            'path': str(path), **counts,
            'scope': 'Recorded artifact only; not a fresh test run or proof of real research'}


def doctor():
    registry = load_registry()
    enabled = [s.source_id for s in registry.sources if s.permits('internal_use_allowed')]
    blocked = [s.source_id for s in registry.sources if not s.permits('internal_use_allowed')]
    # Which recorded review opens each enabled source; an assistant review is never shown as human.
    bases = {s.source_id: s.review_basis() for s in registry.sources if s.permits('internal_use_allowed')}
    return {'status': 'development_ready', 'versions': VERSIONS,
            'dependencies': {name: version(name) for name in ('pydantic', 'Jinja2', 'WeasyPrint', 'pytest')},
            'distribution': 'personal_use_only', 'license_registry': registry.registry_version,
            'enabled_sources': enabled, 'enabled_source_review_basis': bases, 'closed_sources': blocked,
            'source_review_policy': 'human attestation, or user-authorized assistant review with official '
                                    'evidence of identity, time, terms and free access; unclear sources stay closed',
            'sec_contact_configured': bool(os.environ.get('STOCK_REPORT_SEC_CONTACT', '').strip()),
            'real_research_status': 'blocked' if not enabled else 'requires_per_run_review',
            'real_research_reason': 'No source has a sufficient recorded review; unknown or unclear sources stay closed'
                                    if not enabled else 'Enabled sources alone do not establish complete research coverage',
            'news_sources': {'gdelt-doc-v2': recorded_tests('reports/phase-0/discovery-network-junit.xml'),
                             'nvidia-press-rss': recorded_tests('reports/phase-0/rss-network-junit.xml')},
            'offline_tests': recorded_tests('reports/phase-0/provider-increment-junit.xml'),
            'available_workflows': ['prepare/analyze/finalize', 'event-rank', 'news-discover', 'replay',
                                    'demo', 'qa', 'bundle', 'verify'],
            'network_called': False, 'registry_modified': False}
