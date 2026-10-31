#!/usr/bin/env python3
"""Fetch pinned upstream SDL and compile a separate ARM64 iOS platform library."""
import argparse
import hashlib
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from config import ROOT, CMAKE

SHA256='9d57b178fb297e121ef2605275937b7afaa7cd24d99ce1f95953e69e7a2535d6'
URL='https://github.com/libsdl-org/SDL/archive/refs/tags/release-3.4.14.tar.gz'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--sdk',choices=['iphoneos','iphonesimulator'],default='iphonesimulator')
    args=parser.parse_args()
    (ROOT/'reports').mkdir(exist_ok=True)
    vendor=ROOT/'build/vendor'
    vendor.mkdir(parents=True,exist_ok=True)
    archive=vendor/'SDL3-3.4.14.tar.gz'
    if not archive.exists(): urllib.request.urlretrieve(URL,archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=SHA256:
        raise ValueError('SDL archive checksum mismatch')
    source=vendor/'SDL-release-3.4.14'
    if not source.exists():
        with tarfile.open(archive) as tar: tar.extractall(vendor,filter='data')
    # Keep the upstream code intact; add only our small input adapter to its
    # existing target, which can then call SDL's private event/state functions.
    with tarfile.open(archive) as tar:
        upstream=tar.extractfile('SDL-release-3.4.14/CMakeLists.txt').read().decode()
    cmake=upstream+'\n# Local iPad input adapter\n'+f'target_sources(SDL3-shared PRIVATE "{ROOT}/probe/SDLInputBridge.c")\n'+f'target_include_directories(SDL3-shared PRIVATE "{source}/src")\n'
    cmake_file=source/'CMakeLists.txt'
    if cmake_file.read_text()!=cmake: cmake_file.write_text(cmake)
    # iOS forbids submitting GPU work while backgrounded, and stock SDL keeps
    # pumping/rendering anyway, spamming IOGPUMetalError and burning battery.
    # Block the game's per-frame event pump while backgrounded so its main
    # loop (which calls this before it ticks and renders) stalls until the
    # app returns to the foreground.
    events_file=source/'src/video/uikit/SDL_uikitevents.m'
    events=events_file.read_text()
    if 'UIKit_AppInBackground' not in events:
        patched=events.replace(
            'static BOOL UIKit_EventPumpEnabled = YES;',
            'static BOOL UIKit_EventPumpEnabled = YES;\nstatic BOOL UIKit_AppInBackground = NO;',1)
        patched=patched.replace(
            '- (void)applicationDidEnterBackground\n{\n    SDL_OnApplicationDidEnterBackground();\n}',
            '- (void)applicationDidEnterBackground\n{\n    UIKit_AppInBackground = YES;\n    SDL_OnApplicationDidEnterBackground();\n}',1)
        patched=patched.replace(
            '- (void)applicationWillEnterForeground\n{\n    SDL_OnApplicationWillEnterForeground();\n}',
            '- (void)applicationWillEnterForeground\n{\n    UIKit_AppInBackground = NO;\n    SDL_OnApplicationWillEnterForeground();\n}',1)
        patched=patched.replace(
            '    if (!UIKit_EventPumpEnabled) {\n        return;\n    }\n\n    /* Let the run loop run for a short amount of time: long enough for',
            '    if (!UIKit_EventPumpEnabled) {\n        return;\n    }\n\n'
            '    while (UIKit_AppInBackground) {\n'
            '        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, TRUE);\n'
            '    }\n\n'
            '    /* Let the run loop run for a short amount of time: long enough for',1)
        if patched.count('UIKit_AppInBackground')!=4:
            raise ValueError('SDL UIKit event pump no longer matches the background patch; re-audit prepare_sdl.py')
        events_file.write_text(patched)
    build=ROOT/'build'/('sdl3-simulator' if args.sdk=='iphonesimulator' else 'sdl3-iphoneos')
    with (ROOT/f'reports/sdl-configure-{args.sdk}.log').open('w') as log:
        subprocess.run([CMAKE,'-S',str(source),'-B',str(build),'-DCMAKE_SYSTEM_NAME=iOS',
            '-DCMAKE_OSX_SYSROOT='+args.sdk,'-DCMAKE_OSX_ARCHITECTURES=arm64',
            '-DCMAKE_OSX_DEPLOYMENT_TARGET=26.0','-DSDL_SHARED=ON','-DSDL_STATIC=OFF',
            '-DSDL_TEST_LIBRARY=OFF','-DSDL_TESTS=OFF','-DSDL_INSTALL=OFF','-DCMAKE_BUILD_TYPE=Release'],
            check=True,stdout=log,stderr=subprocess.STDOUT)
    with (ROOT/f'reports/sdl-build-{args.sdk}.log').open('w') as log:
        subprocess.run([CMAKE,'--build',str(build),'-j','8'],check=True,stdout=log,stderr=subprocess.STDOUT)
    print(build/'libSDL3.0.dylib')

if __name__=='__main__': main()
