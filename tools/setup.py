#!/usr/bin/env python3
"""Validate local prerequisites and prepare an isolated Python environment."""
import argparse
import json
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from config import ROOT, CONFIG_PATH, DEFAULT_APP, SETTINGS, validate_source

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=DEFAULT_APP, help='Your original macOS factorio.app')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12 or newer is required.')
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        parser.error('Run natively on an Apple Silicon Mac (not under Rosetta).')
    app = args.app.expanduser().resolve()
    try:
        validate_source(app)
    except ValueError as error:
        parser.error(str(error))
    cmake = shutil.which('cmake')
    if not cmake:
        parser.error('CMake is missing. Install it and ensure cmake is in PATH, then retry.')
    subprocess.run(['xcodebuild', '-version'], check=True)
    sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'], text=True).strip()
    if int(sdk.split('.')[0]) < 26:
        parser.error(f'iOS SDK 26 or newer is required; found {sdk}. Select the full Xcode installation.')
    for directory in ('build', 'reports'):
        (ROOT / directory).mkdir(exist_ok=True)
    python = ROOT / '.venv/bin/python'
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    install = subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')],
                             capture_output=True, text=True)
    if install.returncode:
        print(install.stdout + install.stderr, file=sys.stderr)
        parser.error('Installing the Python requirements failed; see the pip output above.')
    CONFIG_PATH.write_text(json.dumps({**SETTINGS, 'factorio_app': str(app), 'cmake': cmake}, indent=2) + '\n')
    subprocess.run([str(python), str(ROOT / 'tools/import_game_assets.py'), '--app', str(app)], check=True)
    print('\nSetup complete. Local paths are saved in ignored .local-config.json.')
    print('Build with tools/deploy.py, or open FactorioProbe.xcodeproj, select your team and iPad, then Run.')

if __name__ == '__main__':
    main()
