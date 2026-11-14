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
