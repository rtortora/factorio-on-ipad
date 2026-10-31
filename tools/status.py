#!/usr/bin/env python3
"""Print what the build window shows as JSON: the Factorio install, its mods, signing teams or devices.

Standard library only, so it runs before setup.py has made the virtualenv.
"""
import json
import os
import sys
from config import DEFAULT_APP, DEFAULT_USER_DATA, TEAM_ID, DEVICE_ID, game_version, validate_source
import import_game_assets
import deploy


def game():
    app = DEFAULT_APP
    result = {'app': str(app), 'version': None, 'supported': False, 'problem': None,
              'user_data': str(DEFAULT_USER_DATA), 'mods': [], 'mods_problem': None}
    try:
        result['version'] = game_version(app)
        validate_source(app)
        result['supported'] = True
    except (OSError, ValueError, KeyError) as error:
        result['problem'] = str(error)
    if result['version'] is None:
        return result
    try:
        selected = import_game_assets.select_mods(app, DEFAULT_USER_DATA)
    except ValueError as error:
        selected, result['mods_problem'] = {}, str(error)
    builtin = import_game_assets.builtin_mods(app)
    try:
        listed = json.loads((DEFAULT_USER_DATA / 'mods/mod-list.json').read_text())['mods']
    except (OSError, ValueError, KeyError):
        listed = []
    for entry in listed:
        name = entry['name']
        if name in builtin:
            continue
        enabled = entry.get('enabled', True)
        info = selected.get(name, (None, None))[0]
        state = 'bundled' if info else 'missing' if enabled else 'disabled'
        result['mods'].append({'name': name, 'state': state, 'version': info['version'] if info else None})
    return result


def teams():
    return {'selected': TEAM_ID,
            'teams': [{'id': i, 'name': n, 'kind': k, 'free': f} for i, n, k, f in deploy.signing_teams()]}


def devices():
    deploy.LOG = open(os.devnull, 'wb')
    found = []
    for device in deploy.devicectl_json('list', 'devices')['devices']:
        hardware, props = device['hardwareProperties'], device['deviceProperties']
        connection = device['connectionProperties']
        if hardware.get('reality') != 'physical' or hardware.get('deviceType') != 'iPad':
            continue
        reachable = connection.get('pairingState') == 'paired' and bool(connection.get('transportType'))
        found.append({'id': hardware['udid'], 'name': props['name'],
                      'model': hardware.get('marketingName', hardware['deviceType']),
                      'reachable': reachable,
                      'running': deploy.game_running(device) if reachable else None})
    return {'selected': DEVICE_ID, 'devices': found}


if __name__ == '__main__':
    commands = {'game': game, 'teams': teams, 'devices': devices}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f'Usage: status.py {{{"|".join(commands)}}}')
    print(json.dumps(commands[sys.argv[1]]()))
