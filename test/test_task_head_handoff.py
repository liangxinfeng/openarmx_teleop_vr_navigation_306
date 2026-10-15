"""No ROS nodes or hardware calls: task head ownership regression."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow

@pytest.mark.parametrize('transition', ['initial', 'skip', 'complete', 'retry'])
def test_lock_ack_and_fresh_feedback_precede_navigation(monkeypatch, transition):
    subject, workflow, events = setup(monkeypatch, transition)
    asyncio.run(Bridge._start_current_task_navigation(subject))
    assert events == ['lock', 'locked_feedback', 'navigate', 'pose']
    assert workflow.snapshot()['state'] == 'navigating'
    assert subject._task_transit_pose_error == ''

@pytest.mark.parametrize('failure', ['service', 'timeout', 'stale', 'missing', 'cancel', 'replace', 'exit'])
def test_failed_or_obsolete_handoff_never_starts_motion(monkeypatch, failure):
    subject, workflow, events = setup(monkeypatch, 'skip', failure)
    with pytest.raises(RuntimeError):
        asyncio.run(Bridge._start_current_task_navigation(subject))
    assert 'navigate' not in events and 'pose' not in events
    if failure == 'cancel': assert workflow.snapshot()['state'] == 'cancelled'
    elif failure == 'replace': assert workflow.snapshot()['state'] == 'pending_confirmation'
    else: assert workflow.snapshot()['state'] == 'navigation_failed'

def setup(monkeypatch, transition, failure=None):
    from openarmx_teleop_vr_navigation_306 import navigation_web_bridge as module
    clock=[100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    w=TaskWorkflow({'task_locations': {'one':['pick'], 'two':['place']}})
    tasks=[dict(location='one',task='pick'),dict(location='two',task='place')]
    w.submit('test', tasks);w.confirm(['one','two'])
    if transition != 'initial':
        w.navigation_started()
        if transition == 'skip': w.skip_current()
        elif transition == 'complete':
            w.navigation_arrived('one');w.height_adjusted();w.complete_action()
        else: w.navigation_failed('blocked');w.retry_navigation_failure()
    data={'backend': {'head_follow_enabled':True}}
    events=[]
    sub=NS(_task_workflow=w,_exit_in_progress=False,_task_transit_pose_error='old failure',
        _showroom_single_task_active=False,_navigate_waypoint_client=object(),
        _speak_task=lambda text:None,get_logger=lambda:Mock(),
        _set_task_height=lambda *a,**kw:events.append('pose'))
    async def lock(enabled):
        assert enabled is False
        events.append('lock')
        return failure != 'service', 'mock lock response'
    async def sleep(delay):
        clock[0]+=delay
        if failure == 'cancel': w.cancel()
        elif failure == 'replace':
            w.cancel();w.submit('replacement',tasks)
        elif failure == 'exit': sub._exit_in_progress=True
        if len(events)==1 and failure != 'timeout':
            data['backend']['head_follow_enabled']=False
            events.append('locked_feedback')
    async def navigate(client, request):
        events.append('navigate');return True,'navigation accepted'
    sub._request_head_follow=lock
    sub._call_service=navigate
    sub._teleop_status_snapshot=lambda:(
        {} if failure == 'missing' else data, .01, failure != 'stale')
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    return sub,w,events


def test_retained_head_follow_pauses_pose_without_failing_navigation(monkeypatch):
    from test_task_height_feedback import controller_status, subject_for_task
    data=controller_status(.2)
    data['backend']['head_follow_enabled']=True
    sub, _, _, sent=subject_for_task(monkeypatch,data,20)
    sub._task_transit_pose_error=''
    sub._task_head_publisher=NS(publish=Mock())
    w=TaskWorkflow({'task_locations':{'one':['pick'],'two':['place']}})
    w.submit('test',[dict(location='one',task='pick'),dict(location='two',task='place')])
    w.confirm(['one','two']);w.navigation_started();w.skip_current();w.navigation_started()
    sub._task_workflow=w
    Bridge._set_task_height(sub,20,30)
    assert sub._task_transit_pose_error=='' and w.snapshot()['state']=='navigating'
    assert sub._task_pose_pause is not None and sub._task_height_target_m==.2
    assert all('true' not in payload for payload in sent)
