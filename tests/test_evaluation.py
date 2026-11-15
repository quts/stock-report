import hashlib
from pathlib import Path
import pytest
from stock_report.evaluation import load_evaluation, CASES, ARTIFACTS
from stock_report.storage import write_json


def test_missing_evaluation_does_not_claim_success(tmp_path):
    assert load_evaluation(tmp_path/'missing',tmp_path/'ledger',tmp_path)['status']=='NOT_RUN'


def test_ledger_is_historical_without_claiming_artifacts_present(tmp_path):
    rows=[{'case':c,'schema_valid':True,'matched_expected':True} for c in CASES]
    write_json(tmp_path/'ledger.json',{'status':'PASSED','cases':rows,'implementation_commit':'historical'})
    result=load_evaluation(tmp_path/'missing',tmp_path/'ledger.json',tmp_path)
    assert result['implementation_commit']=='historical'
    assert not result['artifact_integrity_verified']
    assert not result['reexecuted_during_bundle_creation']


def test_local_evaluation_requires_intact_artifacts(tmp_path):
    rows=[]
    for case in CASES:
        run=tmp_path/case;run.mkdir()
        for name in ARTIFACTS:
            write_json(run/name,{'outlooks':[{'outlook':'insufficient'}]})
        rows.append({'case':case,'run':str(run),'schema_valid':True,'matched_expected':True,
                     'actual_outlooks':['insufficient'],'expected_outlooks':['insufficient'],
                     'artifact_hashes':{n:hashlib.sha256((run/n).read_bytes()).hexdigest() for n in ARTIFACTS}})
    local=tmp_path/'evaluation.json';write_json(local,{'status':'PASSED','cases':rows})
    assert load_evaluation(local,tmp_path/'missing',tmp_path)['artifact_integrity_verified']
    (Path(rows[0]['run'])/'report.pdf').write_bytes(b'changed')
    with pytest.raises(ValueError,match='integrity'):load_evaluation(local,tmp_path/'missing',tmp_path)
