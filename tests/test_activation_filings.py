"""The filing source must use the archive identity contract, not XBRL role claims."""
import copy
from stock_report.activation import ActivationPackage, package_drift
from stock_report.licensing import LicenseRegistry
from stock_report.sources import FILING_PROFILES
from stock_report.storage import read_json


def setup():
    payload = read_json('tests/fixtures/licenses-closed.json')
    profile = FILING_PROFILES['sec-edgar-archives-filing']
    source = dict(source_id='sec-edgar-archives-filing', publisher='SEC',
                  attribution='Test contract only', endpoint_scope=profile['endpoint'],
                  terms_url='https://www.sec.gov/test-terms', declared_status='unknown')
    payload['sources'].append(source)
    package = read_json('tests/fixtures/activation-package-sec-closed.json')
    package['registry_baseline_version'] = payload['registry_version']
    item = package['items'][0]
    item.update(source_id=source['source_id'], endpoint=profile['endpoint'],
                field_binding=copy.deepcopy(profile['identity_binding']), sec_binding=None)
    config = read_json('config/sec-entities.json')
    return package, LicenseRegistry.model_validate(payload), config


def test_archive_contract_accepts_only_matching_issuer_configuration():
    package, registry, config = setup()
    assert package_drift(ActivationPackage.model_validate(package), registry, config) == []
    config['entities'][0]['cik'] = '0000002488'
    assert package_drift(ActivationPackage.model_validate(package), registry, config)


def test_archive_contract_rejects_fictional_identity_binding():
    package, registry, config = setup()
    package['items'][0]['field_binding'] = {'cik': ['guessed_name']}
    problems = package_drift(ActivationPackage.model_validate(package), registry, config)
    assert any('身份欄位' in p for p in problems)
