"""Machine-local settings; only the example belongs in version control."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / '.local-config.json'
SETTINGS = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
STEAM_DIR = Path.home() / 'Library/Application Support/Steam'
FACTORIO_RELATIVE = Path('steamapps/common/Factorio/factorio.app')

def steam_libraries():
    """Steam library roots: the default one, then every path in libraryfolders.vdf."""
    roots = [STEAM_DIR]
    vdf = STEAM_DIR / 'steamapps/libraryfolders.vdf'
    try:
        text = vdf.read_text(errors='replace')
    except OSError:
        return roots
    for raw in re.findall(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
        root = Path(re.sub(r'\\(.)', r'\1', raw))
        if root not in roots:
            roots.append(root)
    return roots

def find_factorio_app():
    """The first Steam library containing Factorio, else the default library's path."""
    for root in steam_libraries():
        if (root / FACTORIO_RELATIVE).is_dir():
            return root / FACTORIO_RELATIVE
    return STEAM_DIR / FACTORIO_RELATIVE

DEFAULT_APP = Path(os.environ.get('FACTORIO_APP') or SETTINGS.get('factorio_app') or
                   find_factorio_app()).expanduser()
DEFAULT_USER_DATA = Path(os.environ.get('FACTORIO_USER_DATA') or SETTINGS.get('factorio_user_data') or
                         Path.home() / 'Library/Application Support/factorio').expanduser()
CMAKE = os.environ.get('FACTORIO_CMAKE') or SETTINGS.get('cmake') or 'cmake'
DEFAULT_BUNDLE_ID = 'local.factorio.loaderprobe'
# Optional signing and device overrides for tools/deploy.py.
TEAM_ID = os.environ.get('FACTORIO_TEAM_ID') or SETTINGS.get('team_id')
BUNDLE_ID = os.environ.get('FACTORIO_BUNDLE_ID') or SETTINGS.get('bundle_id')
DEVICE_ID = os.environ.get('FACTORIO_DEVICE_ID') or SETTINGS.get('device_id')
SUPPORTED_SHA256 = {
    '991c1182742f60cde97be2a412106670f73ba483fd1715a95e687b73c8b5250d',  # 2.1.24, macOS Steam universal binary (static SDL 3.4.14)
}

def save_setting(key, value):
    SETTINGS[key] = value
    CONFIG_PATH.write_text(json.dumps(SETTINGS, indent=2) + '\n')

def game_version(app):
    return json.loads((app / 'Contents/data/base/info.json').read_text())['version']

def validate_source(app):
    import hashlib
    binary = app / 'Contents/MacOS/factorio'
    if not binary.is_file():
        raise ValueError(f'Factorio executable not found: {binary}. Run tools/setup.py --app /path/to/factorio.app')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest not in SUPPORTED_SHA256:
        raise ValueError(f'Unsupported Factorio executable SHA-256: {digest}\n'
                         f'Required one of: {sorted(SUPPORTED_SHA256)} (2.1.24, macOS Steam universal binary).\n'
                         'Do not bypass this check: the patches are specific to these executables.')
    for relative in ('Contents/Info.plist', 'Contents/data/base/info.json', 'Contents/data/core/info.json'):
        if not (app / relative).is_file():
            raise ValueError(f'Incomplete Factorio app: missing {relative}')
    return digest
