import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np
import pytest
from openarmx_teleop_vr_306_v4.controller_node import IndependentArmController as Controller
from openarmx_teleop_vr_306_v4.body_height import body_height_joint_targets
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow


def controller_status(measured=.298, feedback_fresh=True):
    # Actual mapper envelope: controller body_height lives under backend.
    return {'state':'HOLDING','backend':{'hardware_enabled':True,
        'feedback_age_sec':.005,'body_height':{'lowering_m':.30,
        'feedback_time_monotonic':time.monotonic(),
        'measured_lowering_m':measured,'feedback_fresh':feedback_fresh}}}


@pytest.mark.parametrize('offset',[0.,8.])
def test_controller_height_uses_measured_joints_excluding_reach_assist(offset):
    joints=np.array([*body_height_joint_targets(.30),0.]);joints[2]+=offset
    subject=NS(_feedback=NS(as_dict=lambda:{'leg_waist':joints}),
        _waist_follow_enabled=True,_waist_follow_profile='forward_pitch_only',
        _waist_follow_neutral_pitch_deg=joints[2]-offset,
        _waist_follow_pitch_target_deg=joints[2])
    assert Controller._measured_body_lowering_m(subject)==pytest.approx(.30)
    assert joints[2]==pytest.approx(body_height_joint_targets(.30)[2]+offset)


@pytest.mark.parametrize('measured',[.298,0.,.56])
def test_nested_controller_feedback_is_read(measured):
    assert Bridge._task_measured_lowering(controller_status(measured),True)==measured


@pytest.mark.parametrize('data,fresh',[
    (controller_status(),False),(controller_status(feedback_fresh=False),True),
    ({'backend':{'body_height':{'lowering_m':.3}}},True),
    ({'backend':None},True),(controller_status(float('nan')),True),
    (controller_status(.6),True),
])
def test_stale_missing_or_command_only_values_cannot_confirm_height(data,fresh):
    assert Bridge._task_measured_lowering(data,fresh) is None


def subject_for_task(monkeypatch, data, height_cm=30):
    from openarmx_teleop_vr_navigation_306 import navigation_web_bridge as module
    monkeypatch.setattr(module.rclpy,'ok',lambda:True)
    clock=[100.]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    wf=TaskWorkflow({'task_locations':{'前台':[{'task':'拿水','body_lowering_cm':height_cm}]}})
    wf.submit('酒店公区',[{'location':'前台','task':'拿水'}]);wf.confirm(['前台']);wf.navigation_started();wf.navigation_arrived('前台')
    sent=[]
    subject=NS(_exit_in_progress=False,_task_workflow=wf,_task_height_lock=threading.Lock(),
        _task_height_target_m=None,_task_height_timeout_seconds=1.25,
        _task_height_stable_seconds=.125,_teleop_status_snapshot=lambda:(copy.deepcopy(data),.01,True),
        _body_height_publisher=NS(publish=lambda m:sent.append(m.data)))
    subject._task_height_tick=lambda:Bridge._task_height_tick(subject)
    data['backend']['body_height']['feedback_time_monotonic']=clock[0]
    return subject,wf,clock,sent


def test_front_desk_reaches_action_prompt_with_real_status_envelope(monkeypatch):
    subject,wf,clock,sent=subject_for_task(monkeypatch,controller_status())
    Bridge._set_task_height(subject,30)
    assert wf.snapshot()['state']=='adjusting_height'
    clock[0]+=.15;Bridge._task_height_tick(subject)
    assert wf.snapshot()['state']=='awaiting_action'
    frontend=(Path(__file__).parents[1]/'web/nav_app.js').read_text()
    assert "awaiting ? '长按A完成动作 · 长按X退出任务'" in frontend
    assert subject._task_height_target_m is None


def test_command_reached_but_body_still_moving_does_not_confirm(monkeypatch):
    subject,wf,clock,sent=subject_for_task(monkeypatch,controller_status(.25))
    Bridge._set_task_height(subject,30)
    clock[0]+=.2;Bridge._task_height_tick(subject)
    assert wf.snapshot()['state']=='adjusting_height'
    assert subject._task_height_target_m==.3


def test_fresh_mapper_with_old_controller_feedback_is_rejected():
    data=controller_status()
    data['backend']['body_height']['feedback_time_monotonic']-=2
    assert Bridge._task_measured_lowering(data,True) is None


def test_merged_version_keeps_new_controls_and_matching_cache_version():
    root=Path(__file__).parents[1]
    bridge=(root/'openarmx_teleop_vr_navigation_306/navigation_web_bridge.py').read_text()
    web=(root/'web/nav_app.js').read_text()
    index=(root/'web/index.html').read_text()
    monitor=(root/'web/monitor.js').read_text()
    import re
    version=re.search(r"'ui_version': '([^']+)'", bridge)[1]
    assert f"'ui_version': '{version}'" in bridge
    assert f"NAVIGATION_ASSET_VERSION = '{version}'" in web
    assert f'nav_app.js?v={version}' in index
    assert '/api/gripper/width' in bridge and '/api/gripper/width' in web
    assert 'holdGripperResetChord' in web
    assert 'task_actions.txt' in bridge
    assert 'current.body_height_level' not in web+monitor
    end=web.split("addEventListener('sessionend', () => {",1)[1].split("addEventListener('sessionstart'",1)[0]
    for name in ('deliveryUiVisible','deliveryCardActive','deliveryCardArmed','deliveryNoticeKey'):
        assert f'navUi.{name} =' not in end


@pytest.mark.parametrize('start,target',[(0,.2),(0,.3),(.3,0),(.2,.3),(0,.56)])
def test_timeout_covers_measured_robot_rate_without_relaxing_arrival(monkeypatch,start,target):
    data=controller_status(start)
    height_cm=round(target*100,6)
    subject,wf,clock,_=subject_for_task(monkeypatch,data,height_cm=height_cm)
    Bridge._set_task_height(subject,height_cm)
    assert subject._task_height_deadline-clock[0]<=31.001
    for step in range(1,321):
        elapsed=step*.1;clock[0]=100+elapsed
        travel=min(abs(target-start),max(0,elapsed-.5)*.025)
        actual=start+(travel if target>start else -travel)
        data['backend']['body_height'].update(measured_lowering_m=actual,feedback_time_monotonic=clock[0])
        Bridge._task_height_tick(subject)
        if wf.snapshot()['state']!='adjusting_height':break
    assert wf.snapshot()['state']=='awaiting_action'
    assert abs(actual-target)<=.008
    if start==0 and target==.2:assert elapsed>7  # old budget stopped a healthy motion


def test_blocked_body_still_times_out_instead_of_falsely_confirming(monkeypatch):
    data=controller_status(0)
    subject,wf,clock,sent=subject_for_task(monkeypatch,data,height_cm=20)
    Bridge._set_task_height(subject,20)
    for step in range(1,142):
        clock[0]=100+step*.1
        data['backend']['body_height']['feedback_time_monotonic']=clock[0]
        Bridge._task_height_tick(subject)
        if wf.snapshot()['state']=='failed':break
    assert wf.snapshot()['state']=='failed'
    assert '目标下降20cm，实测下降0.0cm' in wf.snapshot()['message']
    assert subject._task_height_target_m is None
