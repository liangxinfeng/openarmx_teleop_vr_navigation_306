import json
import threading
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock
import numpy as np
import pytest
from test_task_height_feedback import controller_status, subject_for_task
from openarmx_teleop_vr_navigation_306.simple_task_catalog import parse_catalog, head_pitch_deg
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow, task_chain_templates
from openarmx_teleop_vr_navigation_306.task_voice_web import TaskVoiceServer
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
from openarmx_teleop_vr_306_v4.controller_node import IndependentArmController as Controller

@pytest.mark.parametrize('value,expected', [('0°',0),('30度',30),('45deg',45),('12.5',12.5)])
def test_pitch_units(value,expected):
    assert head_pitch_deg(value)==expected

@pytest.mark.parametrize('value', ['-1','46','nan','inf',True])
def test_invalid_pitch(value):
    with pytest.raises(ValueError):head_pitch_deg(value)

def test_legacy_rows_and_template_override():
    cat=parse_catalog('任务库\n前台 | 拿水 | 20 | 否\n任务串：演示\n前台 | 拿水 | 20 | 否 | 10°')
    wf=TaskWorkflow(cat)
    wf.submit('demo',task_chain_templates(cat)[0]['tasks'])
    assert wf.current()['head_pitch_deg']==10
    assert cat['task_locations']['前台'][0]['head_pitch_deg']==30

def test_voice_card_and_signature_reload(tmp_path):
    path=tmp_path/'tasks.txt';path.write_text('前台 | 拿水 | 20 | 否 | 30')
    srv=TaskVoiceServer(path,'https://localhost/api/tasks/submit')
    pair={'location':'前台','task':'拿水'}
    old=srv._normalise_voice_tasks([pair])
    path.write_text('前台 | 拿水 | 20 | 否 | 0')
    new=srv._normalise_catalog_tasks([pair])
    assert new[0]['head_pitch_deg']==0
    assert srv._task_signature(new)!=srv._task_signature(old)

def test_height_ready_waits_for_measured_head_then_finishes(monkeypatch):
    data=controller_status(.2);data['backend']['measured_neck_deg']=[0,-10,0]
    sub,wf,clock,sent=subject_for_task(monkeypatch,data,20)
    head=[];sub._task_head_publisher=NS(publish=lambda m:head.append(json.loads(m.data)))
    Bridge._set_task_height(sub,20,30)
    data['backend']['task_head_goal']={'request_id':sub._task_head_request_id,
        'requested_deg':30.,'effective_deg':30.,'safe_max_deg':43.}
    clock[0]+=.2;data['backend']['body_height']['feedback_time_monotonic']=clock[0]
    Bridge._task_height_tick(sub)
    assert wf.snapshot()['state']=='adjusting_height'
    data['backend']['measured_neck_deg'][1]=-29.5
    Bridge._task_height_tick(sub)
    clock[0]+=.15;data['backend']['body_height']['feedback_time_monotonic']=clock[0]
    Bridge._task_height_tick(sub)
    assert wf.snapshot()['state']=='awaiting_action'
    assert head[0]['head_pitch_deg']==30 and head[0]['enabled']
    assert not head[-1]['enabled'] and not json.loads(sent[-1])['enabled']

@pytest.mark.parametrize('cancel', [False, True])
def test_cancel_or_manual_head_takes_ownership(monkeypatch,cancel):
    data=controller_status(.2);data['backend']['measured_neck_deg']=[0,0,0]
    sub,wf,clock,sent=subject_for_task(monkeypatch,data,20)
    head=[];sub._task_head_publisher=NS(publish=lambda m:head.append(json.loads(m.data)))
    Bridge._set_task_height(sub,20,30)
    if cancel:wf.cancel()
    else:data['backend']['head_follow_enabled']=True
    Bridge._task_height_tick(sub)
    assert sub._task_head_pitch_deg is None
    assert sub._task_height_target_m == (None if cancel else .2)
    assert bool(sub._task_pose_pause) is not cancel
    assert not head[-1]['enabled']
    assert wf.snapshot()['state']!='awaiting_action'

def controller():
    c=NS(_lock=threading.Lock(),_task_head_request_id='',_task_head_cancelled_id='',
         _task_head_target=None,_task_head_target_time=0,_head_follow_enabled=False,
         _reset_active=False,_enable_pending=False,_estop_latched=False,_hardware_enabled=True,
         _feedback=NS(neck=[3,-10,4]),_feedback_time=time.monotonic(),
         get_logger=lambda:Mock(),
         get_parameter=lambda name:NS(value=.5),
         _kinematics=NS(joint_q={'Joint_Neck_Pitch':0},lower=np.deg2rad([-45]),upper=np.deg2rad([30])))
    c._follow_authority_allowed=lambda:Controller._follow_authority_allowed(c)
    c._head_follow_active=lambda:Controller._head_follow_active(c)
    c._cancel_task_head_locked=lambda:Controller._cancel_task_head_locked(c)
    return c

def command(c,angle=30,request_id='one',enabled=True):
    Controller._on_task_head_target(c,NS(data=json.dumps(dict(
        request_id=request_id,head_pitch_deg=angle,enabled=enabled))))

def test_controller_pitch_sign_limits_and_cancel_latch():
    c=controller();command(c)
    assert list(c._task_head_target)==[3,-30,4]
    command(c,enabled=False);command(c)
    assert c._task_head_target is None  # heartbeat cannot resurrect cancelled goal
    command(c,45,'two');assert c._task_head_target[1]==-45
    command(c,enabled=False,request_id='one')
    assert c._task_head_target[1]==-45  # stale stop does not cancel a newer preset

@pytest.mark.parametrize('flag', ['_head_follow_enabled','_reset_active','_enable_pending','_estop_latched'])
def test_controller_does_not_compete_or_enable(flag):
    c=controller();setattr(c,flag,True);command(c)
    assert c._task_head_target is None
    setattr(c,flag,False);command(c)
    assert c._task_head_target is None

def test_stale_feedback_stops_and_narrower_model_limit_saturates_safely():
    c=controller();c._feedback_time-=1;command(c)
    assert c._task_head_target is None
    c._feedback_time=time.monotonic();c._kinematics.lower=np.deg2rad([-20]);command(c)
    assert c._task_head_target[1]==pytest.approx(-20)
    assert c._task_head_goal['effective_deg']==pytest.approx(20)

def test_task_six_with_real_283_model_margin(monkeypatch):
    from ament_index_python.packages import get_package_share_directory
    from openarmx_teleop_vr_306_v4.kinematics import IndependentArmKinematics
    share=get_package_share_directory('openarmx_teleop_vr_306_v4')
    c=controller()
    c._kinematics=IndependentArmKinematics(share+'/urdf/robot_v2_2_simplified.urdf',
        collision_check=False,limit_margin_rad=np.deg2rad(2.0))
    data=controller_status(.15);data['backend']['measured_neck_deg']=[0,-40,0]
    sub,wf,clock,sent=subject_for_task(monkeypatch,data,15)
    c._feedback_time=clock[0]
    heads=[]
    sub._task_head_publisher=NS(publish=lambda m:heads.append(json.loads(m.data)))
    Bridge._set_task_height(sub,15,45)
    command(c,45,sub._task_head_request_id)
    effective=c._task_head_goal['effective_deg']
    assert effective==pytest.approx(43,abs=.001)
    assert c._task_head_cancelled_id==''
    data['backend']['task_head_goal']=dict(c._task_head_goal)
    data['backend']['measured_neck_deg'][1]=-effective
    Bridge._task_height_tick(sub)
    clock[0]+=.15;data['backend']['body_height']['feedback_time_monotonic']=clock[0]
    Bridge._task_height_tick(sub)
    assert wf.snapshot()['state']=='awaiting_action'
    assert heads[-1]['enabled'] is False

@pytest.mark.parametrize('bad', ['old_id','missing','wrong_request','nan','above_limit'])
def test_unacknowledged_or_invalid_effective_goal_cannot_finish(bad):
    data=controller_status(.15);data['backend']['measured_neck_deg']=[0,-43,0]
    goal={'request_id':'new','requested_deg':45.,'effective_deg':43.,'safe_max_deg':43.}
    if bad=='old_id':goal['request_id']='old'
    if bad=='wrong_request':goal['requested_deg']=40
    if bad=='nan':goal['effective_deg']=float('nan')
    if bad=='above_limit':goal['effective_deg']=45
    if bad!='missing':data['backend']['task_head_goal']=goal
    assert not Bridge._task_head_ready(data,True,45,'new')

def test_effective_target_still_requires_actual_neck_arrival():
    data=controller_status(.15);data['backend']['measured_neck_deg']=[0,-40,0]
    data['backend']['task_head_goal']={'request_id':'new','requested_deg':45.,'effective_deg':43.,'safe_max_deg':43.}
    assert not Bridge._task_head_ready(data,True,45,'new')
