from pathlib import Path
import pytest
from stock_report.models import Request
from stock_report.mock import fixture
from stock_report.analysis import synthetic_proposal
from stock_report.storage import read_json

@pytest.fixture
def request_model():
    return Request(mode='stock', ticker='NVDA', exchange='NASDAQ', research_as_of='2026-11-14T17:00:00+08:00')

@pytest.fixture
def data(request_model):
    return fixture(request_model)

@pytest.fixture
def proposal(data):
    return synthetic_proposal(data)

@pytest.fixture
def mapping():
    return read_json(Path('config/ratings.json'))
