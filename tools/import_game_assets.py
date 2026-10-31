#!/usr/bin/env python3
"""Generate the local app icon and mod bundle from your own Factorio install. Neither belongs in version control."""
import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from config import ROOT, DEFAULT_APP, DEFAULT_USER_DATA, game_version

ICON_SOURCE = 'Contents/data/core/graphics/factorio.icon/Assets/factorio.png'
ICON_OUTPUT = ROOT/'Assets.xcassets/AppIcon.appiconset/AppIcon-1024.png'
ICON_SIZE = 1024
ICON_GEAR_FIT = 0.8  # Leaves room for the rounded iPad icon mask.
ICON_BACKGROUND = (28, 28, 30)
ICON_SOLID_ALPHA = 200  # Excludes the art's soft drop shadow when centering the gear.
MODS_OUTPUT = ROOT/'mods'
MANIFEST = 'bundled-mods.json'
MOD_NAME = re.compile(r'^[A-Za-z0-9_\- ]+$')
DEPENDENCY = re.compile(r'^\s*(!|\?|\(\?\)|~)?\s*(.+?)\s*(?:(?:<=|>=|<|>|=)\s*[\d.]+\s*)?$')

def warn(message):
    print(f'warning: {message}', file=sys.stderr)

def generate_icon(app, output=ICON_OUTPUT):
    from PIL import Image  # Only the icon needs it, so the mod listing works without the venv.
    source = app/ICON_SOURCE
    if not source.is_file():
        raise ValueError(f'Icon source not found: {source}')
    art = Image.open(source).convert('RGBA')
    gear = art.getchannel('A').point(lambda alpha: 255 if alpha > ICON_SOLID_ALPHA else 0).getbbox()
    scale = ICON_SIZE*ICON_GEAR_FIT/max(gear[2]-gear[0], gear[3]-gear[1])
    art = art.resize((round(art.width*scale), round(art.height*scale)), Image.LANCZOS)
    # iOS app icons must be opaque.
    canvas = Image.new('RGB', (ICON_SIZE, ICON_SIZE), ICON_BACKGROUND)
    offset = tuple(round(ICON_SIZE/2-(gear[i]+gear[i+2])/2*scale) for i in (0, 1))
    canvas.paste(art, offset, art)
    encoded = io.BytesIO()
    canvas.save(encoded, 'PNG')
    # Xcode may compile the asset catalog while this runs, so leave an up-to-date file alone.
    if not output.is_file() or output.read_bytes() != encoded.getvalue():
        temporary = output.with_suffix('.tmp')
        temporary.write_bytes(encoded.getvalue())
        temporary.replace(output)

def version_key(version):
    return tuple(int(part) for part in version.split('.'))

def read_zip_info(path):
    """Return info.json from a mod zip, or None if it is not a usable mod."""
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [name for name in archive.namelist() if name.count('/') == 1 and name.endswith('/info.json')]
            info = json.loads(archive.read(candidates[0]).decode('utf-8-sig')) if candidates else None
        if not info or not MOD_NAME.match(info['name']):
            return None
        version_key(info['version'])
        return info
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        warn(f'skipping unreadable mod {path.name}: {error}')
        return None

def builtin_mods(app):
    return {path.parent.name for path in (app/'Contents/data').glob('*/info.json')}

def select_mods(app, user_data):
    """Choose the zip for each third-party mod enabled in the desktop game."""
    mods_dir = user_data/'mods'
    list_path = mods_dir/'mod-list.json'
    if not list_path.is_file():
        raise ValueError(f'No mod list at {list_path}. Launch Factorio once on this Mac, or set factorio_user_data '
                         'in .local-config.json if your data lives elsewhere.')
    builtin = builtin_mods(app)
    factorio_version = '.'.join(game_version(app).split('.')[:2])
    installed = {}
    for path in sorted(mods_dir.glob('*.zip')):
        info = read_zip_info(path)
        if info:
            installed.setdefault(info['name'], []).append((info, path))
    selected = {}
    for entry in json.loads(list_path.read_text())['mods']:
        name = entry['name']
        if not entry.get('enabled', True) or name in builtin:
            continue
        # A version in mod-list.json means the player pinned it in the game's mod manager.
        candidates = [(info, path) for info, path in installed.get(name, [])
                      if info.get('factorio_version') == factorio_version
                      and entry.get('version') in (None, info['version'])]
        if not candidates:
            warn(f'{name} is enabled but has no zip in {mods_dir} compatible with Factorio {factorio_version}; skipping')
            continue
        selected[name] = max(candidates, key=lambda candidate: version_key(candidate[0]['version']))
    for name, (info, _) in selected.items():
        for dependency in info.get('dependencies', []):
            match = DEPENDENCY.match(dependency)
            if match and match[1] in (None, '~') and match[2] not in selected and match[2] not in builtin:
                raise ValueError(f'{name} requires {match[2]}, which is not being bundled. Enable it in the game.')
    return selected

def import_mods(app, user_data, output=MODS_OUTPUT):
    selected = select_mods(app, user_data)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest = {'mods': [], 'settings_sha256': None}
    for name, (info, path) in sorted(selected.items()):
        shutil.copy2(path, output/f"{name}_{info['version']}.zip")
        manifest['mods'].append({'name': name, 'version': info['version']})
    settings = user_data/'mods/mod-settings.dat'
    if settings.is_file():
        shutil.copy2(settings, output/settings.name)
        manifest['settings_sha256'] = hashlib.sha256(settings.read_bytes()).hexdigest()
    (output/MANIFEST).write_text(json.dumps(manifest, indent=2)+'\n')
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=DEFAULT_APP, help='Your original macOS factorio.app')
    parser.add_argument('--user-data', type=Path, default=DEFAULT_USER_DATA, help='Folder holding mods/mod-list.json')
    args = parser.parse_args()
    try:
        print(f'Found Factorio {game_version(args.app)} at {args.app}')
        generate_icon(args.app)
        manifest = import_mods(args.app, args.user_data)
    except ValueError as error:
        parser.error(str(error))
    names = ', '.join(f"{mod['name']} {mod['version']}" for mod in manifest['mods'])
    print(f'Bundling {len(manifest["mods"])} enabled mod(s) from {args.user_data}: {names or "none"}')

if __name__ == '__main__':
    main()
