"""Readiness must report closed sources and actual recorded network outcomes without secrets."""
import json
import pytest
from stock_report.cli import main
from stock_report.diagnostics import doctor, recorded_tests


def test_doctor_keeps_sources_closed_and_never_prints_contact(monkeypatch, capsys):
    monkeypatch.setenv('STOCK_REPORT_SEC_CONTACT', 'SECRET_CONTACT')
    report = doctor()
    assert report['status'] == 'development_ready' and report['real_research_status'] == 'blocked'
    assert report['enabled_sources'] == [] and not report['registry_modified'] and not report['network_called']
    assert report['sec_contact_configured'] and 'SECRET_CONTACT' not in json.dumps(report)
    assert main(['doctor']) == 0
    assert 'SECRET_CONTACT' not in capsys.readouterr().out


def test_recorded_test_results_do_not_treat_failures_as_passed(tmp_path):
    path = tmp_path / 'test.xml'
    assert recorded_tests(path)['status'] == 'NOT_RUN'
    path.write_text('<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0"/></testsuites>')
    assert recorded_tests(path)['status'] == 'FAILED'
    path.write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>')
    assert recorded_tests(path)['status'] == 'PASSED'


@pytest.mark.parametrize('tests,skipped,expected', [
    (0, 0, 'NOT_RUN'), (2, 2, 'SKIPPED'), (2, 1, 'PARTIAL'),
])
def test_unexecuted_contracts_are_not_passed(tmp_path, tests, skipped, expected):
    path = tmp_path / 'test.xml'
    path.write_text(f'<testsuites><testsuite tests="{tests}" skipped="{skipped}"/></testsuites>')
    assert recorded_tests(path)['status'] == expected
