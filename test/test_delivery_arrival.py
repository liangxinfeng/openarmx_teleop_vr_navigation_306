"""Door identity/authority regression tests. HTTP and hardware are mocked."""
import asyncio
import time
from types import SimpleNamespace as NS
from openarmx_teleop_vr_navigation_306.delivery_handoff import DeliveryHandoffMonitor
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as B


def fixture(tmp_path):
    m=DeliveryHandoffMonitor(tmp_path/'unused.json', NS(info=lambda *_:None,warning=lambda *_:None))
    m._apply_get_payload({'handoffs':[dict(order_id='test',task_id='test-pickup',door_id='1',
                                          arrived=True,status='PICKUP_AT_LAUNDRY')]})
    m._last_success_monotonic=time.monotonic()
    m._url='https://unused.invalid/mock'
    calls=[]
    m._request_json=lambda *a: calls.append(a) or {'door_id':'1'}
    return m,calls,dict(order_id='test',task_id='test-pickup',door_id='1')


def test_card_opens_matching_door_once(tmp_path):
    m,c,e=fixture(tmp_path)
    assert m.request_open(expected=e)[0] and len(c)==1
    assert m.snapshot()['state']=='opened'
    assert m.request_open(expected=e)[0] and len(c)==1


def test_stale_order_task_door_or_connection_cannot_open(tmp_path):
    for key in ('order_id','task_id','door_id','age','unknown'):
        m,c,e=fixture(tmp_path)
        if key=='age': m._last_success_monotonic-=30
        elif key=='unknown':m._state='unknown'
        else:e[key]='stale'
        assert not m.request_open(expected=e)[0] and not c


def test_not_arrived_cannot_open(tmp_path):
    m,c,e=fixture(tmp_path);m._arrived=False
    assert not m.request_open(expected=e)[0] and not c


def test_api_requires_current_headset_and_identity(tmp_path):
    m,c,e=fixture(tmp_path)
    async def operator(request):return request
    r=NS(_operator_payload=operator,_delivery_handoff=m)
    response=asyncio.run(B._api_delivery_handoff_open(r,{}))
    assert response.status==409 and not c
    async def bad(_):raise RuntimeError('not current headset')
    r._operator_payload=bad
    response=asyncio.run(B._api_delivery_handoff_open(r,e))
    assert response.status==409 and not c
    r._operator_payload=operator
    response=asyncio.run(B._api_delivery_handoff_open(r,e))
    assert response.status==200 and len(c)==1
