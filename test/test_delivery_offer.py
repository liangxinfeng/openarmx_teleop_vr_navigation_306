"""Order-to-VR state tests. No ROS node, HTTP, or hardware is started."""
import asyncio
import json
from threading import Lock
from types import SimpleNamespace as NS
from aiohttp import web
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as B
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow


def fixture():
    snapshot = dict(pickup_task_chain='洗衣房洗衣', last_success_age=0,
                    state='en_route', orders=[dict(order_id='test1', room='101', status_code='PICKUP_STARTED')])
    r = NS(_delivery_handoff=NS(snapshot=lambda: snapshot), _delivery_seen_orders=set(),
           _delivery_offer_lock=Lock(), _delivery_offer=None, _delivery_chain_note=None,
           _drive_lock=Lock(), _drive_sample=None, _navigation_is_active=lambda: False,
           _nav_lock=Lock(), _pose=object(), _relocalization_status={'state':'succeeded'},
           _waypoints=['洗衣机1'],get_logger=lambda:NS(warning=lambda *_:None,info=lambda *_:None),
           _task_workflow=NS(snapshot=lambda: dict(state='idle')))
    r._delivery_chain_tick=lambda: B._delivery_chain_tick(r)
    r._delivery_state_payload=lambda: B._delivery_state_payload(r)
    r._delivery_chain_tick()
    calls=[]
    async def owner(request): return request
    async def start():
        calls.append('start')
        return 'started'
    r._operator_payload=owner
    r._submit_delivery_chain=lambda name,order,confirmed_waypoints=None: calls.append((name,order))
    r._start_current_task_navigation=start
    return r,snapshot,calls


def test_offer_requires_no_automatic_motion_and_dedupes_status_changes():
    r,s,c=fixture()
    assert r._delivery_offer['room_no']=='101' and c==[]
    response=asyncio.run(B._api_delivery_pickup_chain_reject(r,dict(order='test1')))
    assert response.status==200 and c==[]
    s['orders']=[];r._delivery_chain_tick()
    s['orders']=[dict(order_id='test1',status_code='PICKUP_STARTED')];r._delivery_chain_tick()
    assert r._delivery_offer['state']=='rejected'


def test_all_orders_are_checked_and_pending_offer_not_overwritten():
    r,s,c=fixture()
    s['orders'].insert(0,dict(order_id='return',status_code='RETURN_AT_LAUNDRY'))
    s['orders'].append(dict(order_id='test2',status_code='PICKUP_STARTED'))
    r._delivery_chain_tick();assert r._delivery_offer['order']=='test1'
    asyncio.run(B._api_delivery_pickup_chain_reject(r,dict(order='test1')))
    r._delivery_chain_tick();assert r._delivery_offer['order']=='test2'


def test_accept_starts_exact_chain_once():
    r,s,c=fixture()
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==200 and c==[('洗衣房洗衣','test1'),'start']
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==409 and len(c)==2


def test_stale_order_or_offline_or_busy_does_not_start():
    for reason in ('stale','offline','busy','nav','manual','cancelled','owner','pose'):
        r,s,c=fixture();request=dict(order='test1')
        if reason=='stale': request['order']='old'
        if reason=='offline': s['last_success_age']=30
        if reason=='busy': r._task_workflow=NS(snapshot=lambda: dict(state='navigating'))
        if reason=='nav': r._navigation_is_active=lambda: True
        if reason=='manual': r._drive_sample=dict(deadman=True)
        if reason=='cancelled': s['orders'][0]['status_code']='CANCELLED'
        if reason=='pose': r._pose=None
        if reason=='owner':
            async def wrong_owner(_): raise RuntimeError('wrong owner')
            r._operator_payload=wrong_owner
        response=asyncio.run(B._api_delivery_pickup_chain_accept(r,request))
        assert response.status==409 and c==[],reason


def test_failure_is_not_reported_as_started():
    r,s,c=fixture()
    async def rejected():raise RuntimeError('导航请求拒绝')
    r._start_current_task_navigation=rejected
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==409 and not json.loads(response.text)['success']
    assert r._delivery_offer['state']=='accepted'  # workflow owns the retry; never resubmit


def test_invalid_chain_keeps_offer_for_retry():
    r,s,c=fixture()
    def invalid(*_,**kwargs):raise ValueError('任务串不存在')
    r._submit_delivery_chain=invalid
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==409 and r._delivery_offer['state']=='offered' and not c


def real_chain_fixture():
    r,s,c=fixture()
    r._task_workflow=TaskWorkflow({'task_locations':{'洗衣机1':['开门']}})
    r._refresh_idle_task_catalog=lambda:None
    r._task_catalog_error=''
    r._task_templates=[dict(name='洗衣房洗衣',tasks=[dict(location='洗衣机1',task='开门')])]
    r._speak_task=lambda *_:None
    r._submit_delivery_chain=lambda *args,**kwargs:B._submit_delivery_chain(r,*args,**kwargs)
    async def start():
        assert r._task_workflow.snapshot()['state']=='preparing'
        r._task_workflow.navigation_started();c.append('start');return 'started'
    r._start_current_task_navigation=start
    return r,s,c


def test_real_workflow_one_accept_no_second_confirmation():
    r,s,c=real_chain_fixture()
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==200 and c==['start']
    assert json.loads(response.text)['task_dispatch']['state']=='navigating'
    assert r._delivery_offer['state']=='accepted'
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==409 and c==['start']


def test_missing_waypoint_does_not_leave_pending_confirmation():
    r,s,c=real_chain_fixture();r._waypoints=[]
    response=asyncio.run(B._api_delivery_pickup_chain_accept(r,dict(order='test1')))
    assert response.status==409 and c==[]
    assert r._task_workflow.snapshot()['state']=='idle'
    assert r._delivery_offer['state']=='offered'
