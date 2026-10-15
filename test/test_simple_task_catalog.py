import json
import threading
from types import SimpleNamespace as NS
import pytest
from openarmx_teleop_vr_navigation_306.simple_task_catalog import parse_catalog,load_catalog,lowering_cm
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow,task_chain_templates
from openarmx_teleop_vr_navigation_306.task_voice_web import TaskVoiceServer
from openarmx_teleop_vr_navigation_306.navigation_web_bridge import NavigationVrWebBridge as Bridge

TEXT='''任务库
前台 | 拿水 | 20 | 否
洗衣机 收毛巾 32cm 否
任务串：演示
前台 | 拿水 | 15 | 否
洗衣机 | 收毛巾 | 32 | 是
'''


def test_rows_templates_and_per_step_overrides():
    catalog=parse_catalog(TEXT)
    templates=task_chain_templates(catalog)
    wf=TaskWorkflow(catalog)
    wf.submit('demo',templates[0]['tasks'])
    assert wf.current()['body_lowering_cm']==15
    wf.confirm(['前台','洗衣机']);wf.navigation_started();wf.navigation_arrived('前台');wf.height_adjusted()
    assert wf.complete_action()[0]=='next'
    wf.navigation_started();wf.navigation_arrived('洗衣机');wf.height_adjusted()
    assert wf.current()['automatic'] is True
    assert wf.snapshot()['state']=='awaiting_action'
    assert '尚未部署' in wf.snapshot()['message']


@pytest.mark.parametrize('separator',[' | ','｜','\t','，',';','  '])
def test_forgiving_separators(separator):
    row=separator.join(['前台','拿水','10','否'])
    assert parse_catalog(row)['task_locations']['前台'][0]['body_lowering_cm']==10


@pytest.mark.parametrize('value',['-1','57','NaN','inf'])
def test_invalid_height_is_not_silently_clamped(value):
    with pytest.raises(ValueError,match='第1行'):parse_catalog(f'前台 | 拿水 | {value} | 否')


def test_incomplete_row_reports_line():
    with pytest.raises(ValueError,match='第2行'):parse_catalog('任务库\n前台 拿水 10')


def test_voice_and_card_keep_height_and_hot_reload(tmp_path):
    path=tmp_path/'tasks.txt';path.write_text(TEXT)
    server=TaskVoiceServer(path,'https://localhost/api/tasks/submit')
    pair={'location':'前台','task':'拿水'}
    assert server._normalise_catalog_tasks([pair])[0]['body_lowering_cm']==20
    assert server._normalise_voice_tasks([pair])[0]['body_lowering_cm']==20
    path.write_text(TEXT.replace('20 | 否','25 | 否'))
    assert server._normalise_catalog_tasks([pair])[0]['body_lowering_cm']==25
    path.write_text('错误的一行')
    server.catalog_snapshot()
    assert '第1行' in server.catalog_error
    with pytest.raises(ValueError):server._normalise_catalog_tasks([pair])
    with pytest.raises(ValueError):server._normalise_voice_tasks([pair])
    path.write_text(TEXT)
    assert server._normalise_catalog_tasks([pair])[0]['body_lowering_cm']==20


def test_arrival_height_sends_immediately_and_uses_centimetres(monkeypatch):
    from openarmx_teleop_vr_navigation_306 import navigation_web_bridge as module
    monkeypatch.setattr(module.rclpy,'ok',lambda:True)
    wf=TaskWorkflow(parse_catalog(TEXT));wf.submit('test',[{'location':'前台','task':'拿水'}]);wf.confirm(['前台']);wf.navigation_started();wf.navigation_arrived('前台')
    sent=[]
    subject=NS(_task_height_lock=threading.Lock(),_task_height_target_m=None,
        _task_height_timeout_seconds=1.25,_task_height_stable_seconds=.1,_task_workflow=wf,
        _exit_in_progress=False,_teleop_status_snapshot=lambda:({'body_height':{'lowering_m':0}},0,True),
        _body_height_publisher=NS(publish=lambda m:sent.append(json.loads(m.data))))
    subject._task_height_tick=lambda:Bridge._task_height_tick(subject)
    Bridge._set_task_height(subject,20)
    assert sent==[{'enabled':True,'direction':0.0,'target_lowering_m':.2}]
    assert wf.snapshot()['state']=='adjusting_height'
    wf.cancel();Bridge._task_height_tick(subject)
    assert sent[-1]['enabled'] is False


def test_idle_chassis_packets_do_not_cancel_task_height_but_xy_does(monkeypatch):
    from openarmx_teleop_vr_navigation_306 import navigation_web_bridge as module
    monkeypatch.setattr(module.rclpy,'ok',lambda:True)
    sent=[]
    wf=TaskWorkflow(parse_catalog(TEXT));wf.submit('test',[{'location':'前台','task':'拿水'}]);wf.confirm(['前台']);wf.navigation_started();wf.navigation_arrived('前台')
    subject=NS(_exit_in_progress=False,_drive_lock=threading.Lock(),_drive_sample=None,
        _task_height_lock=threading.Lock(),_task_height_target_m=.2,
        _task_workflow=wf,_navigation_is_active=lambda:False,
        _body_height_publisher=NS(publish=lambda m:sent.append(json.loads(m.data))),
        get_logger=lambda:NS(info=lambda _:None))
    subject._release_task_height_for_manual_control=lambda:Bridge._release_task_height_for_manual_control(subject)
    Bridge._set_drive_sample(subject,{'deadman':False})
    assert sent==[] and subject._task_height_target_m==.2
    Bridge._set_drive_sample(subject,{'deadman':True,'body_height_enabled':True,'body_height_direction':-1})
    assert subject._task_height_target_m is None
    assert sent==[{'enabled':True,'direction':-1}]
    assert wf.snapshot()['state']=='awaiting_action'


def test_height_timeout_never_claims_ready(monkeypatch):
    from openarmx_teleop_vr_navigation_306 import navigation_web_bridge as module
    monkeypatch.setattr(module.rclpy,'ok',lambda:True)
    wf=TaskWorkflow(parse_catalog(TEXT));wf.submit('test',[{'location':'前台','task':'拿水'}]);wf.confirm(['前台']);wf.navigation_started();wf.navigation_arrived('前台')
    sent=[]
    subject=NS(_exit_in_progress=False,_task_height_lock=threading.Lock(),
        _task_height_target_m=.2,_task_height_deadline=0,_task_workflow=wf,
        _teleop_status_snapshot=lambda:({'backend':{'body_height':{
            'measured_lowering_m':.16,'feedback_fresh':True,
            'feedback_time_monotonic':module.time.monotonic()}}},0,True),
        _body_height_publisher=NS(publish=lambda m:sent.append(json.loads(m.data))))
    Bridge._task_height_tick(subject)
    assert wf.snapshot()['state']=='failed'
    assert '目标下降20cm，实测下降16.0cm' in wf.snapshot()['message']
    assert sent==[{'enabled':False,'direction':0}]
