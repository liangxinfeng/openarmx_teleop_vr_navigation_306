"""No node construction / no motion: state and controller boundary regressions."""
import ast, asyncio, json, math, time
from pathlib import Path
from types import SimpleNamespace as NS
from threading import Lock
from aiohttp import web
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow, navigation_terminal_outcome

def workflow():
    w=TaskWorkflow({'task_locations':{'取衣处':['拿衣服'],'等候处':['等候']}})
    w.submit('test',[dict(location='取衣处',task='拿衣服'),dict(location='等候处',task='等候')])
    w.confirm(['取衣处','等候处']);w.navigation_started()
    return w

def test_operator_arrival_keeps_current_task():
    w=workflow();token=w.snapshot()['task_token'];w.navigation_failed('blocked')
    assert w.navigation_arrived('取衣处',operator=True)
    assert w.snapshot()['task_token']==token and w.snapshot()['current_index']==0
    assert w.snapshot()['current']['operator_arrived']
    assert w.navigation_arrived('取衣处',operator=True) is None
    w.height_adjusted();assert w.snapshot()['state']=='awaiting_action'
    assert w.complete_action()[0]=='next' and w.snapshot()['current_index']==1
    assert w.snapshot()['task_token']!=token
    assert navigation_terminal_outcome('operator_arrived')=='succeeded'

def test_skip_separate_and_resume_same_target():
    w=workflow();w.navigation_failed('blocked')
    assert not w.navigation_resumed('等候处')
    assert w.navigation_resumed('取衣处') and w.snapshot()['current_index']==0
    assert w.skip_current()[0]=='next' and w.snapshot()['current_index']==1
    w.navigation_started();assert w.skip_current()[0]=='done'
    old=w.snapshot()['task_token'];w.submit('again',[dict(location='取衣处',task='拿衣服')])
    assert old!=w.snapshot()['task_token']

def methods(path,names,scope):
    module=ast.parse(Path(path).read_text())
    nodes=[n for n in ast.walk(module) if isinstance(n,ast.FunctionDef) or isinstance(n,ast.AsyncFunctionDef)]
    exec(compile(ast.Module(body=[n for n in nodes if n.name in names],type_ignores=[]),str(path),'exec'),scope)
    return scope

def test_stop_boundary_and_operator_status():
    # Filled by installer with the staged manager, not the live ROS node.
    import os
    src=os.environ['OPERATOR_MANAGER_SOURCE']
    class Clock:
        def __sub__(self,other):return NS(nanoseconds=0)
    scope=methods(src,{'_navigation_stop_ready','_handle_operator_arrival','_complete_confirmed_arrival'},
                  dict(time=time,math=math))
    r=NS(_active_goal_name=None,_manual_control_active=False,_short_servo_active=False,
         _pause_stop_futures=[],_arrival_odom=(time.monotonic(),0,0),_arrival_odom_stamp=Clock(),
         _operator_stopped_since=time.monotonic()-2,get_clock=lambda:NS(now=lambda:Clock()))
    assert scope['_navigation_stop_ready'](r)[0]
    r._manual_control_active=True;assert not scope['_navigation_stop_ready'](r)[0];r._manual_control_active=False
    r._pause_stop_futures=[NS(done=lambda:False)];assert not scope['_navigation_stop_ready'](r)[0]
    r._pause_stop_futures=[NS(done=lambda:True,result=lambda:NS(status=5))]
    assert scope['_navigation_stop_ready'](r)[0]
    r._arrival_odom=(time.monotonic()-1,0,0);assert not scope['_navigation_stop_ready'](r)[0]
    emitted=[]
    r=NS(_paused_goal_name='取衣处',_failed_goal_name=None,_waypoints={'取衣处':{}},_active_goal_id=7,
         _navigation_stop_ready=lambda:(True,''),_clear_paused_navigation=lambda:None,
         _complete_confirmed_arrival=lambda *a,**kw:emitted.append((a,kw)))
    assert not scope['_handle_operator_arrival'](r,NS(name='等候处'),NS()).success
    assert scope['_handle_operator_arrival'](r,NS(name='取衣处'),NS()).success
    assert emitted==[(('取衣处',7,None),{'operator':True})]

def test_drive_timeout_has_no_implicit_resume():
    import inspect
    from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
    source=inspect.getsource(Bridge._drive_tick)
    assert '_resume_after_drive' not in source and '_resume_navigation_client' not in source
    r=NS(_exit_in_progress=False,_drive_session_generation=2,_drive_link_alive=lambda:False)
    try:asyncio.run(Bridge._wait_navigation_stop(r,2))
    except RuntimeError:pass
    else:assert False,'Disconnected transport was allowed to resume'

def test_legacy_skip_endpoint_is_disabled():
    from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
    response=asyncio.run(Bridge._api_tasks_skip_navigation_failure(NS(),None))
    assert response.status==409

def test_explicit_release_resumes_only_after_verified_stop():
    from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
    events=[];w=workflow();w.navigation_failed('blocked')
    async def owner(_):return {'client_id':'valid'}
    async def stopped(generation):events.append(('stopped',generation))
    async def service(*args):events.append('resume');return True,'ok'
    r=NS(_operator_payload=owner,_task_intervention=False,_resume_requested_for_drive=False,
        _drive_session_generation=3,_set_drive_sample=lambda p:events.append(('sample',p)),
        _publish_stop=lambda:events.append('zero'),_publish_manual_override=lambda a:events.append(('manual',a)),
        _wait_navigation_stop=stopped,_navigation_paused_for_drive=True,_nav_lock=Lock(),
        _navigation_status={'active_waypoint':'取衣处'},_resume_navigation_client=None,
        _call_service=service,_task_workflow=w)
    response=asyncio.run(Bridge._api_release_drive(r,None))
    assert response.status==200 and ('stopped',3) in events
    assert events.index(('stopped',3))<events.index('resume')
    assert w.snapshot()['state']=='navigating' and w.snapshot()['current_index']==0
    async def disconnected(_):raise RuntimeError('link lost')
    r._wait_navigation_stop=disconnected;r._navigation_paused_for_drive=True;events.clear()
    response=asyncio.run(Bridge._api_release_drive(r,None))
    assert response.status==409 and 'resume' not in events

def test_operator_api_requires_current_token_and_stops_without_skipping():
    from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
    w=workflow();w.navigation_failed('blocked');events=[]
    async def owner(_):return dict(task_token=w.snapshot()['task_token'])
    async def stopped(_):events.append('stopped')
    async def service(client,request):
        if client=='arrival':
            events.append('arrival');w.navigation_arrived(request.name,operator=True)
        else:events.append('pause')
        return True,'ok'
    r=NS(_operator_payload=owner,_task_workflow=w,_task_intervention=False,
         _resume_requested_for_drive=False,_drive_session_generation=0,
         _set_drive_sample=lambda _:None,_publish_stop=lambda:events.append('zero'),
         _publish_manual_override=lambda _:None,_pause_navigation_client='pause',
         _operator_arrival_client='arrival',_call_service=service,_wait_navigation_stop=stopped,
         _navigation_is_active=lambda:False)
    response=asyncio.run(Bridge._api_task_intervention(r,None))
    assert response.status==200 and events==['zero','pause','stopped','arrival']
    assert w.snapshot()['current_index']==0 and w.snapshot()['state']=='adjusting_height'
    async def stale(_):return dict(task_token='old-token')
    r._operator_payload=stale;events.clear()
    assert asyncio.run(Bridge._api_task_intervention(r,None)).status==409 and not events

def test_confirmation_and_authority_guard():
    from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge
    class Request:
        remote='other-headset'
        async def json(self):return {'client_id':'old'}
    r=NS(_client_lock=Lock(),_exit_in_progress=False,_control_peer='owner',_control_client_id='valid')
    try:asyncio.run(Bridge._operator_payload(r,Request()))
    except RuntimeError:pass
    else:assert False,'Wrong headset accepted'
    w=workflow()
    async def payload(_):return {'task_token':w.snapshot()['task_token']}
    r=NS(_operator_payload=payload,_task_workflow=w)
    assert asyncio.run(Bridge._api_task_intervention(r,None,skip=True)).status==409
