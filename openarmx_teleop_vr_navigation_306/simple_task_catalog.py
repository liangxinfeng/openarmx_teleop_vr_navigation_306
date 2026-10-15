"""One row per task; optional head pitch and reverse-squat remark."""
from pathlib import Path
import hashlib
import json
import math
import re


def lowering_cm(value):
    if isinstance(value, bool):
        raise ValueError('身体下降量需要填写厘米数')
    value = float(re.sub(r'\s*(?:cm|厘米)\s*$', '', str(value), flags=re.I))
    if not math.isfinite(value) or not 0 <= value <= 56:
        raise ValueError('身体下降量必须在0–56厘米内，0为最高站姿')
    return value


def automatic_flag(value):
    if isinstance(value, bool): return value
    value = str(value).strip().lower()
    if value in ('是', '自动', 'yes', 'true', '1'): return True
    if value in ('否', '手动', 'no', 'false', '0'): return False
    raise ValueError('是否自动化请填是或否')


def head_pitch_deg(value):
    if isinstance(value, bool):
        raise ValueError('头部俯视角度需要填写度数')
    value = float(re.sub(r'\s*(?:°|度|deg)\s*$', '', str(value), flags=re.I))
    if not math.isfinite(value) or not 0 <= value <= 45:
        raise ValueError('头部俯视角度必须在0–45°内，0为正前方')
    return value


def task_settings(item):
    """Read old plans only at the compatibility boundary, not in UI/config."""
    if 'body_lowering_cm' in item:
        height = lowering_cm(item['body_lowering_cm'])
    else:
        height = 14.0 * (5 - max(1, min(5, int(item.get('body_height_level', 5)))))
    reverse = item.get('reverse_squat', False)
    if not isinstance(reverse, bool):
        raise ValueError('反方向下蹲参数必须为布尔值')
    if reverse and height > 53:
        raise ValueError('反方向下蹲最多53厘米')
    result = {'body_lowering_cm': height, 'automatic': automatic_flag(item.get('automatic', False)),
              'head_pitch_deg': head_pitch_deg(item.get('head_pitch_deg', 30))}
    if reverse: result['reverse_squat'] = True
    return result


def parse_catalog(text):
    catalog = {'version': 2, 'task_locations': {}, 'task_templates': []}
    pairs = {}
    section = None
    names = set()
    for number, raw in enumerate(text.lstrip('\ufeff').splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line: continue
        if line in ('任务库', '任务库:', '任务库：'):
            section = None
            continue
        heading = re.match(r'^任务串[\s:：]+(.+)$', line)
        if heading:
            name = heading[1].strip()
            if name in names: raise ValueError(f'第{number}行：任务串名称重复：{name}')
            names.add(name)
            section = {'id': 'chain_' + hashlib.sha256(name.encode()).hexdigest()[:12],
                       'name': name, 'tasks': []}
            catalog['task_templates'].append(section)
            continue
        try:
            columns = ([x.strip() for x in re.split(r'[|｜\t,，;；]', line) if x.strip()]
                       if re.search(r'[|｜\t,，;；]', line) else line.split())
            if len(columns) not in (4, 5, 6):
                raise ValueError('每行填：地点 | 任务 | 身体下降厘米数 | 是否自动化 | 头部俯视角度 | 反方向下蹲（后两项可省略）')
            if len(columns) == 6 and columns[5] not in ('反方向下蹲', '正常下蹲'):
                raise ValueError('第六项请填反方向下蹲或正常下蹲')
            location, task, height, automatic = columns[:4]
            item = {'location': location, 'task': task,
                    'body_lowering_cm': lowering_cm(height), 'automatic': automatic_flag(automatic),
                    'head_pitch_deg': head_pitch_deg(columns[4] if len(columns) >= 5 else 30)}
            if len(columns) == 6 and columns[5] == '反方向下蹲':
                item['reverse_squat'] = True
            item.update(task_settings(item))
        except (TypeError, ValueError) as error:
            raise ValueError(f'第{number}行：{error}') from error
        pair = (location, task)
        if pair not in pairs:
            entry = {k:v for k,v in item.items() if k != 'location'}
            pairs[pair] = entry
            catalog['task_locations'].setdefault(location, []).append(entry)
        elif section is None and pairs[pair] != {k:v for k,v in item.items() if k != 'location'}:
            raise ValueError(f'第{number}行：任务库同名任务设置重复且不一致')
        if section is not None: section['tasks'].append(item)
    if not pairs: raise ValueError('任务库为空，请至少填写一项任务')
    for template in catalog['task_templates']:
        if not template['tasks']: raise ValueError(f'任务串“{template["name"]}”没有任务')
    return catalog


def load_catalog(path):
    path = Path(path)
    text = path.read_text(encoding='utf-8-sig')
    # Only explicit legacy paths are parsed as JSON. Active defaults use txt.
    return json.loads(text) if path.suffix.lower()=='.json' else parse_catalog(text)
