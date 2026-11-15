"""The model exchange must accept the version it actually advertises."""
import pytest
from pydantic import ValidationError
from stock_report.models import Proposal
from stock_report.pipeline import prepare
from stock_report.storage import read_json


def test_exported_prompt_version_is_accepted_by_proposal_schema(request_model, tmp_path):
    from pathlib import Path
    result = prepare(request_model, tmp_path)
    run = Path(result['research_input_path']).parent
    context = read_json(run / 'context.json')
    payload = {'input_sha256': context['input_sha256'], 'model': 'version-contract-test',
               'prompt_version': context['prompt_version'], 'outlooks': []}
    assert Proposal.model_validate(payload).prompt_version == context['prompt_version']
    assert read_json(run / 'proposal.schema.json')['properties']['prompt_version']['const'] == context['prompt_version']
    with pytest.raises(ValidationError):
        Proposal.model_validate(payload | {'prompt_version': '1'})
