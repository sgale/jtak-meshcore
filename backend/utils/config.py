import yaml
from pathlib import Path

_config = None
_config_mtime = None
_config_path = "/opt/jtak/config/jtak.yaml"

def load_config(path: str = _config_path) -> dict:
    global _config, _config_mtime
    mtime = Path(path).stat().st_mtime
    if _config is None or mtime != _config_mtime:
        with open(path) as f:
            _config = yaml.safe_load(f)
        _config_mtime = mtime
    return _config

def get(key: str, default=None):
    cfg = load_config()
    keys = key.split(".")
    val = cfg
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k, default)
    return val
