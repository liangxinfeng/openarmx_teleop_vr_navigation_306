"""Offline state/endpoint regressions; no ROS node or hardware connection."""
import asyncio
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from test_task_height_feedback import controller_status
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow


def subject(monkeypatch, failure=None):
    from openarmx_teleop_vr_navigation_306 import navigation_web_bridge as module
    clock=[100.]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.rclpy,'ok',lambda:True)
    w=TaskWorkflow({'task_locations':{name:[{'task':'act','body_lowering_cm':20}]
                                     for name in ['one','two','three']}})
    tasks=[dict(location=name,task='act') for name in ['one','two','three']]
    w.submit('chain',tasks);w.confirm(['one','two','three']);w.navigation_started()
    w.navigation_arrived('one');w.height_adjusted();w.complete_action();w.navigation_started()
    w.navigation_arrived('two');w.height_failed('head tracking conflict')
    before=w.snapshot();data=controller_status(.2)
    data['backend'].update(hardware_ready=True,head_follow_enabled=False,measured_neck_deg=[0,-30,0])
    r=NS(_task_workflow=w,_task_intervention=False,_resume_requested_for_drive=False,
         _drive_session_generation=1,_exit_in_progress=False,_drive_lock=threading.Lock(),
         _drive_sample=None,_task_height_lock=threading.Lock(),_task_height_target_m=None,
         _task_height_timeout_seconds=15.,_task_height_stable_seconds=.125,
         _task_transit_pose_error='old error',get_logger=lambda:Mock(),
         _body_height_publisher=NS(publish=Mock()),_task_head_publisher=NS(publish=Mock()))
    calls=[]
    async def owner(request):
        calls.append('auth')
        if failure=='owner' or (failure=='owner_changed' and calls.count('auth')>1):
            raise RuntimeError('wrong headset')
        return {'task_token':'old' if failure=='token' else before['task_token']}
    async def stopped(generation):
        calls.append('stop')
        if failure=='stop': raise RuntimeError('not stopped')
        if failure=='cancel': w.cancel()
        if failure=='replace': w.cancel();w.submit('new',tasks)
        if failure=='exit': r._exit_in_progress=True
        if failure=='generation': r._drive_session_generation+=1
    r._operator_payload=owner;r._wait_navigation_stop=stopped
    r._navigation_is_active=lambda:failure=='navigation'
    r._teleop_status_snapshot=lambda:(data,.01,failure!='stale')
    r._task_measured_lowering=Bridge._task_measured_lowering
    r._task_height_tick=lambda:Bridge._task_height_tick(r)
    def pose(*args,**kwargs):
        calls.append('pose');Bridge._set_task_height(r,*args,**kwargs)
    r._set_task_height=pose
    if failure=='head':data['backend']['head_follow_enabled']=True
    if failure=='missing_head':del data['backend']['head_follow_enabled']
    if failure=='hardware':data['backend']['hardware_ready']=False
    if failure=='feedback':data['backend']['body_height']['feedback_time_monotonic']=0
    if failure=='arms':data['active_arms']=['left']
    if failure=='drive':r._drive_sample={'deadman':True}
    if failure=='pose_busy':r._task_height_target_m=.1
    if failure=='busy':r._task_intervention=True
    return r,w,data,before,calls,clock


def test_retry_preserves_current_and_remaining_tasks_until_measured_pose_ready(monkeypatch):
    r,w,data,before,calls,clock=subject(monkeypatch)
    response=asyncio.run(Bridge._api_tasks_retry_pose(r,None))
    assert response.status==200 and json.loads(response.body)['success']
    assert calls==['auth','stop','auth','pose']
    after=w.snapshot()
    for key in ['tasks','current_index','task_token','source_text','total']:
        assert after[key]==before[key]
    assert after['current_index']==1 and after['state']=='adjusting_height'
    assert r._task_transit_pose_error=='' and not r._task_intervention
    request_id=r._task_head_request_id
    # A repeated request cannot create another pose request.
    assert asyncio.run(Bridge._api_tasks_retry_pose(r,None)).status==409
    assert r._task_head_request_id==request_id
    data['backend']['task_head_goal']={'request_id':request_id,'requested_deg':30.,
                                     'effective_deg':30.,'safe_max_deg':43.}
    Bridge._task_height_tick(r)
    clock[0]+=.2;data['backend']['body_height']['feedback_time_monotonic']=clock[0]
    Bridge._task_height_tick(r)
    assert w.snapshot()['state']=='awaiting_action'
    result,_,next_task=w.complete_action()
    assert result=='next' and next_task['location']=='three'


@pytest.mark.parametrize('failure',['owner','owner_changed','token','stop','cancel','replace',
    'exit','generation','head','missing_head','hardware','feedback','stale','arms','drive',
    'navigation','pose_busy','busy'])
def test_rejected_retry_never_publishes_pose(monkeypatch,failure):
    r,w,data,before,calls,_=subject(monkeypatch,failure)
    response=asyncio.run(Bridge._api_tasks_retry_pose(r,None))
    assert response.status==409 and 'pose' not in calls
    r._body_height_publisher.publish.assert_not_called()
    r._task_head_publisher.publish.assert_not_called()
    assert r._task_intervention==(failure=='busy')
    if failure not in ['cancel','replace']:assert w.snapshot()['state']=='failed'


def test_concurrent_retry_does_not_release_first_request_lock(monkeypatch):
    r,w,_,_,calls,_=subject(monkeypatch)
    async def scenario():
        started=asyncio.Event();release=asyncio.Event()
        async def wait(generation):started.set();await release.wait()
        r._wait_navigation_stop=wait
        first=asyncio.create_task(Bridge._api_tasks_retry_pose(r,None))
        await started.wait()
        assert (await Bridge._api_tasks_retry_pose(r,None)).status==409
        assert r._task_intervention
        release.set();assert (await first).status==200
    asyncio.run(scenario())
    assert calls.count('pose')==1 and not r._task_intervention
