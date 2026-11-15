from urllib.error import HTTPError
from stock_report.providers import HTTPClient, probes
from stock_report.storage import read_json


def test_offline_transport_retry_and_cache(tmp_path,monkeypatch):
    calls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):return b'{"value":1}'
    def transport(request,timeout):
        calls.append(request.full_url)
        if len(calls)==1:raise HTTPError(request.full_url,429,'rate limited',{},None)
        return Response()
    monkeypatch.setattr('stock_report.providers.urlopen',transport)
    monkeypatch.setattr('stock_report.providers.time.sleep',lambda _:None)
    client=HTTPClient('test',interval=0,cache_dir=tmp_path)
    data,meta=client.get_json('https://example.invalid/test')
    assert data=={'value':1} and meta['attempts']==2
    assert client.get_json('https://example.invalid/test')[1]['cached']
    assert len(calls)==2


def test_network_failure_is_recorded_no_synthetic_fallback(tmp_path,monkeypatch):
    def fail(*a,**k):raise TimeoutError('simulated network outage')
    monkeypatch.setattr(HTTPClient,'get_json',fail)
    result=probes(tmp_path,'Phase0 test contact@example.invalid')
    assert result['retrieved']==0
    report=read_json(tmp_path/'provider-probes.json')
    assert report['dataset_kind']=='real'
    assert all(r['status']=='failed' and not r['shareable_in_pdf'] for r in report['results'])


def test_unknown_probe_never_persists_source_values_or_hashes(tmp_path, monkeypatch):
    def response(self, url):
        data = ({'entityName': 'PRIVATE_ISSUER', 'cik': 'PRIVATE_CIK',
                 'facts': {'us-gaap': {'PRIVATE_CONCEPT': {'value': 918273.45}}}}
                if 'sec.gov' in url else
                [{'Code': '2330', 'Name': 'PRIVATE_ISSUER', 'ClosingPrice': 918273.45},
                 {'Code': '6488', 'Name': 'PRIVATE_ISSUER', 'ClosingPrice': 918273.45}])
        return data, {'cached': False, 'attempts': 1, 'response_sha256': 'PRIVATE_HASH'}
    monkeypatch.setattr(HTTPClient, 'get_json', response)
    result = probes(tmp_path, 'test contact@example.invalid')
    assert result['retrieved'] == 4
    text = (tmp_path / 'provider-probes.json').read_text()
    for forbidden in ('PRIVATE_', '918273.45', 'response_sha256', 'contact@example.invalid'):
        assert forbidden not in text
    assert all(row['license_status'] == 'unknown' for row in read_json(tmp_path / 'provider-probes.json')['results'])


def test_probe_error_does_not_persist_response_or_contact(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError('PRIVATE_RESPONSE contact@example.invalid')
    monkeypatch.setattr(HTTPClient, 'get_json', fail)
    probes(tmp_path, 'test contact@example.invalid')
    text = (tmp_path / 'provider-probes.json').read_text()
    assert 'PRIVATE_RESPONSE' not in text and 'contact@example.invalid' not in text
    assert all(row['error_type'] == 'ValueError' for row in read_json(tmp_path / 'provider-probes.json')['results'])
