"""VR menu data contracts, independent of ROS and motor commands."""
import hashlib
import json
from .simple_task_catalog import load_catalog, task_settings, lowering_cm, head_pitch_deg


def task_menu_catalog(path):
    catalog = load_catalog(path)
    tasks = []
    for location, entries in catalog.get('task_locations', {}).items():
        for entry in entries:
            entry = {'task': entry} if isinstance(entry, str) else entry
            task = str(entry['task']).strip()
            ident = hashlib.sha256((str(location)+'\0'+task).encode()).hexdigest()[:20]
            tasks.append({'id': ident, 'location': str(location), 'task': task, **task_settings(entry)})
    token = hashlib.sha256(json.dumps(tasks, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'tasks': tasks, 'token': token, 'maximum_tasks': 32}


def compose_menu_tasks(catalog, payload):
    if not isinstance(payload, dict):
        raise ValueError('任务请求格式错误')
    if payload.get('confirmed') is not True:
        raise ValueError('请确认后再启动任务流程')
    if payload.get('token') != catalog['token']:
        raise ValueError('任务库已更新，请重新打开编排菜单核对后启动')
    ids = payload.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= catalog['maximum_tasks']:
        raise ValueError('请选择1–32项任务')
    by_id = {t['id']:t for t in catalog['tasks']}
    if any(not isinstance(i, str) or i not in by_id for i in ids):
        raise ValueError('所选任务已不存在，请重新加载任务库')
    return [{k:v for k,v in by_id[i].items() if k!='id'} for i in ids]


def pose_values(payload):
    # Explicit fields only: missing/invalid values must never imply motion.
    if not isinstance(payload, dict):
        raise ValueError('位置参数格式错误')
    return lowering_cm(payload['body_lowering_cm']), head_pitch_deg(payload['head_pitch_deg'])
