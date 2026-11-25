#!/usr/bin/env python3
# Compile AND link every source of the iOS app (host, graphics, host_msl Metal shaders,
# libvpx, dav1d, iOS front end, win/ios/Launcher.m, win/ios/compat_rt.c) for a game profile,
# without needing game files. Verifies clang, ld64.lld, Metal compiler, iPhoneOS SDK, and macho_sign.
# Usage: python win/compile_check.py [game id (default 010015100B514000)] [work folder]
import plistlib, subprocess, sys, tempfile
from pathlib import Path

WIN = Path(__file__).resolve().parent
sys.path.insert(0, str(WIN))
import toolchain as tc, build_app as ba, macho_sign

game = sys.argv[1] if len(sys.argv) > 1 else '010015100B514000'
work = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.gettempdir()) / 'nxw_compile_check'
sdk = tc.ios_sdk() or ba.die('no iPhoneOS SDK')
clang = tc.llvm_tool('clang') or ba.die('no clang')

(work / 'profile').mkdir(parents=True, exist_ok=True)
ba.py(ba.DM / 'games/game.py', 'header', game, work / 'profile/game_profile.h')
msl_h_dir = ba.build_host_msl(work / 'host_msl/host_msl_gen.h')

items, host_incs = ba.host_sources(work / 'profile', msl_h_dir)
ios_incs = [(ba.IOS / 'Sources').resolve()] + host_incs
ios_srcs = [p.resolve() for p in sorted((ba.IOS / 'Sources').glob('*.m'))] + [(WIN / 'ios/Launcher.m').resolve()]
items += [(f, [], ios_incs) for f in ios_srcs]

rt = ba.compat_rt_needed(clang)
if rt:
    rt.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(clang), '-target', f'arm64-apple-ios{tc.setting("ios_deployment", tc.IOS_DEPLOYMENT)}',
                        '-isysroot', str(sdk), '-Os', '-mcpu=apple-m1', '-c', str(WIN / 'ios/compat_rt.c'), '-o', str(rt)],
                       capture_output=True, text=True)
    if r.returncode: print(r.stderr, file=sys.stderr); ba.die('compat_rt.c failed')

objs = ba.compile_items(clang, sdk, items, work / 'obj')
fws = ba.frameworks(sdk, [f for f, _, _ in items])
exe = work / 'DmIOS'
ba.link(clang, sdk, objs, fws, [], exe, sign_adhoc=True)
sig_n = macho_sign.sign(str(exe), f'org.nxnative.{game.lower()}', {'com.apple.developer.kernel.increased-memory-limit': True})
print(f'compile_check: {len(objs)} files compiled + linked + signed ({exe.stat().st_size / 2**20:.2f} MB, sig {sig_n} B) for {game} ({sdk.name} {" ".join(tc.sdk_settings(sdk))})')
