"""Navigation/head ownership transitions, using real methods and fake publishers."""
import json
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from test_task_height_feedback import controller_status, subject_for_task
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow


def setup(monkeypatch, phase='navigating'):
    data=controller_status(.2)
    data['backend'].update(head_follow_enabled=False,hardware_ready=True,measured_neck_deg=[0,0,0])
    r,_,clock,body=subject_for_task(monkeypatch,data,20)
    w=TaskWorkflow({'task_locations':{'one':[{'task':'pick','body_lowering_cm':20}],
                                    'two':[{'task':'place','body_lowering_cm':20}]}})
    w.submit('chain',[dict(location='one',task='pick'),dict(location='two',task='place')])
    w.confirm(['one','two']);w.navigation_started()
    if phase=='adjusting_height':w.navigation_arrived('one')
    r._task_workflow=w;r._task_transit_pose_error=''
    r._nav_lock=threading.Lock();r._status_payload=Bridge._status_payload
    r._speak_task=lambda _:None;r._showroom_single_task_active=False
    r._set_task_height=lambda *a,**kw:Bridge._set_task_height(r,*a,**kw)
    r._navigation_is_active=lambda:False
    head=[];r._task_head_publisher=NS(publish=lambda m:head.append(json.loads(m.data)))
    Bridge._set_task_height(r,20,30)
    return r,w,data,clock,body,head


def tick(r,data,clock,elapsed=.1):
    clock[0]+=elapsed;data['backend']['body_height']['feedback_time_monotonic']=clock[0]
    Bridge._task_height_tick(r)


def arrive(r):
    Bridge._on_navigation_status(r,NS(data=json.dumps({'state':'succeeded','active_waypoint':'one'})))


@pytest.mark.parametrize('arrival_before_lock',[True,False])
def test_navigation_continues_and_arrival_waits_until_explicit_head_lock(monkeypatch,arrival_before_lock):
    r,w,data,clock,body,head=setup(monkeypatch)
    token=w.snapshot()['task_token'];old_id=r._task_head_request_id
    data['backend']['head_follow_enabled']=True;tick(r,data,clock)
    assert w.snapshot()['state']=='navigating' and r._task_transit_pose_error==''
    assert r._task_pose_pause and not head[-1]['enabled'] and not json.loads(body[-1])['enabled']
    count=len(head);tick(r,data,clock,90)
    assert len(head)==count and w.snapshot()['state']=='navigating'
    if arrival_before_lock:arrive(r);assert w.snapshot()['state']=='adjusting_height'
    assert r._task_pose_pause and r._task_head_pitch_deg is None
    data['backend']['head_follow_enabled']=False
    data['backend'].update(task_head_cancelled_id=old_id,task_head_cancel_reason='head_mode_changed')
    tick(r,data,clock)
    assert r._task_pose_pause is None and r._task_head_request_id!=old_id
    assert head[-1]['enabled'] and head[-1]['request_id']==r._task_head_request_id
    assert r._task_height_deadline-clock[0]>=15
    if not arrival_before_lock:arrive(r)
    data['backend']['task_head_goal']={'request_id':r._task_head_request_id,'requested_deg':30.,
                                     'effective_deg':30.,'safe_max_deg':43.}
    # Fresh commanded goal alone must not mark real pose ready.
    tick(r,data,clock);assert w.snapshot()['state']=='adjusting_height'
    data['backend']['measured_neck_deg'][1]=-30
    tick(r,data,clock);tick(r,data,clock,.2)
    assert w.snapshot()['state']=='awaiting_action' and w.snapshot()['task_token']==token
    assert w.complete_action()[2]['location']=='two'


def test_head_takeover_after_transit_pose_finished_waits_at_arrival(monkeypatch):
    r,w,data,clock,body,head=setup(monkeypatch)
    data['backend']['measured_neck_deg'][1]=-30
    data['backend']['task_head_goal']={'request_id':r._task_head_request_id,'requested_deg':30.,
                                     'effective_deg':30.,'safe_max_deg':43.}
    tick(r,data,clock);tick(r,data,clock,.2)
    assert r._task_height_target_m is None
    data['backend']['head_follow_enabled']=True;arrive(r)
    assert w.snapshot()['state']=='adjusting_height' and r._task_pose_pause
    assert r._task_transit_pose_error==''


@pytest.mark.parametrize('reason',['reset','enabling','estop','hardware_disabled','invalid_neck_limits'])
def test_real_safety_cancellation_is_not_hidden_by_head_takeover(monkeypatch,reason):
    r,w,data,clock,body,head=setup(monkeypatch,'adjusting_height')
    data['backend']['head_follow_enabled']=True;tick(r,data,clock)
    data['backend'].update(task_head_cancelled_id=r._task_head_request_id,task_head_cancel_reason=reason)
    tick(r,data,clock)
    assert w.snapshot()['state']=='failed' and r._task_height_target_m is None
    assert r._task_pose_pause is None and not head[-1]['enabled']


@pytest.mark.parametrize('flag',['emergency_stop_latched','quick_reset_active','hardware_enable_pending'])
def test_hardware_fault_has_priority_even_with_head_following(monkeypatch,flag):
    r,w,data,clock,body,head=setup(monkeypatch,'adjusting_height')
    data['backend'].update(head_follow_enabled=True,**{flag:True});tick(r,data,clock)
    assert w.snapshot()['state']=='failed' and not r._task_pose_pause


@pytest.mark.parametrize('change',['cancel','next','manual_height'])
def test_paused_pose_cannot_resume_after_ownership_or_task_changes(monkeypatch,change):
    r,w,data,clock,body,head=setup(monkeypatch)
    data['backend']['head_follow_enabled']=True;tick(r,data,clock)
    if change=='cancel':w.cancel()
    elif change=='next':w.skip_current();w.navigation_started()
    else:Bridge._release_task_height_for_manual_control(r)
    data['backend']['head_follow_enabled']=False;count=len(head);tick(r,data,clock)
    assert r._task_pose_pause is None and r._task_height_target_m is None
    assert not any(p['enabled'] for p in head[count:])


def test_locked_head_with_stale_encoder_feedback_stays_paused(monkeypatch):
    r,w,data,clock,body,head=setup(monkeypatch)
    data['backend']['head_follow_enabled']=True;tick(r,data,clock)
    data['backend']['head_follow_enabled']=False;clock[0]+=10;count=len(head)
    Bridge._task_height_tick(r)
    assert r._task_pose_pause and len(head)==count


def test_manual_pose_still_yields_without_auto_resuming(monkeypatch):
    r,w,data,clock,body,head=setup(monkeypatch,'adjusting_height')
    Bridge._set_task_height(r,20,30,manual=True)
    data['backend']['head_follow_enabled']=True;tick(r,data,clock)
    assert r._manual_pose_status['state']=='failed' and not r._task_pose_pause


def test_repeated_takeover_resume_is_accepted_by_real_controller_goal_handler(monkeypatch):
    from test_task_head_pose import controller
    from openarmx_teleop_vr_306_v4.controller_node import IndependentArmController as Controller
    r,w,data,clock,body,head=setup(monkeypatch)
    c=controller()
    def publish(message):
        head.append(json.loads(message.data))
        Controller._on_task_head_target(c,message)
    r._task_head_publisher=NS(publish=publish)
    Controller._on_task_head_target(c,NS(data=json.dumps(head[-1])))
    ids={r._task_head_request_id}
    for _ in range(3):
        c._head_follow_enabled=True
        Controller._cancel_task_head_locked(c,'head_mode_changed')
        data['backend'].update(head_follow_enabled=True,task_head_cancelled_id=c._task_head_cancelled_id,
                               task_head_cancel_reason=c._task_head_cancel_reason)
        tick(r,data,clock)
        assert c._task_head_target is None and r._task_pose_pause
        c._head_follow_enabled=False;c._feedback_time=clock[0]+.1
        data['backend']['head_follow_enabled']=False;tick(r,data,clock)
        assert c._task_head_target is not None and c._task_head_goal['request_id']==r._task_head_request_id
        assert r._task_head_request_id not in ids;ids.add(r._task_head_request_id)
        assert w.snapshot()['state']=='navigating' and r._task_transit_pose_error==''


def test_estop_cannot_be_misreported_as_pose_ready_even_at_target(monkeypatch):
    r,w,data,clock,body,head=setup(monkeypatch,'adjusting_height')
    data['backend']['task_head_goal']={'request_id':r._task_head_request_id,'requested_deg':30.,
                                     'effective_deg':30.,'safe_max_deg':43.}
    data['backend'].update(measured_neck_deg=[0,-30,0],emergency_stop_latched=True)
    r._task_height_stable_since=clock[0]-1
    tick(r,data,clock)
    assert w.snapshot()['state']=='failed'
