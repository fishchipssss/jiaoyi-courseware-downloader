"""Read private per-user configuration without embedding it in source code."""
import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / 'credentials.local.json'


def local_setting(name, default=''):
    if not SETTINGS_PATH.exists():
        return default
    try:
        values = json.loads(SETTINGS_PATH.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise RuntimeError('credentials.local.json 无法读取，请检查 JSON 格式。') from exc
    if not isinstance(values, dict):
        raise RuntimeError('credentials.local.json 必须是 JSON 对象。')
    value = values.get(name, default)
    if not isinstance(value, str):
        raise RuntimeError(f'本地配置字段 {name} 必须填写文字。')
    return value or default
