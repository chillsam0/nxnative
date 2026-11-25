#!/usr/bin/env python3
# NSP / XCI / NSZ / XCZ / NUP / dump -> iOS IPA on Windows 10/11 (no Mac or Xcode required).
# Runs upstream scripts/build_ipa_from_nsp.py with macOS-only tools mapped to Windows equivalents:
#   nx_extract / dmsh_xlate  -> native Windows .NET 10 builds (auto-built from in-repo sources if needed)
#   xcrun metal / metallib   -> Apple Metal Developer Tools for Windows (via win/pyshim + win/xcrun.py)
#   spirv-cross / glslang    -> native Windows builds (Vulkan SDK / PATH)
#   dm/ios/tools/build_ipa.sh -> win/build_app.py (LLVM clang + ld64.lld + iPhoneOS SDK + UIKit Launcher.m + macho_sign.py)
# Usage: python win/build_ipa_windows.py <NSP folder or .nup file> [-k <prod.keys>] [options]
import importlib.util, json, os, shutil, subprocess, sys, time
from pathlib import Path

WIN = Path(__file__).resolve().parent
REPO = WIN.parent
SHIM = WIN / 'pyshim'


def _ensure_dotnet_tool(proj_rel, out_rel, exe_name, want_sha, extra_args=()):
    out_dir = WIN / out_rel
    exe = out_dir / (f'{exe_name}.exe' if os.name == 'nt' else exe_name)
    if exe.is_file():
        r = subprocess.run([str(exe), '--source-sha'], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == want_sha:
            return exe
    proj = REPO / proj_rel
    print(f'[nxnative-win] building {exe_name} from {proj_rel}...', flush=True)
    r = subprocess.run(['dotnet', 'build', str(proj), '-c', 'Release', '-p:PublishAot=false', *extra_args, '-o', str(out_dir), '-v', 'quiet', '-nologo'],
                       capture_output=True, text=True)
    if r.returncode != 0 or not exe.is_file():
        sys.exit(f'[RED] failed to build {exe_name}:\n{(r.stderr or r.stdout)[-2000:]}')
    return exe


def main():
    if sys.platform == 'darwin':
        sys.exit('win/build_ipa_windows.py is the Windows/Linux driver: on a Mac run scripts/build_ipa_from_nsp.py')
    if os.name != 'nt':
        os.environ['NXW_LINUX'] = '1'
    for bad in ('--prepare-xcode',):
        if bad in sys.argv:
            sys.exit(f'{bad} needs a Mac with Xcode')
    for i, a in enumerate(sys.argv):
        if (a == '--platform' and i + 1 < len(sys.argv) and sys.argv[i + 1] != 'ios') or (a.startswith('--platform=') and a != '--platform=ios'):
            sys.exit('only --platform ios builds on Windows (the Mac platforms run the game in the Mac host)')

    if '-h' in sys.argv or '--help' in sys.argv:
        print('Windows options:\n'
              '  --entitlements      embed Increased Memory Limit entitlement via ad-hoc Mach-O signature (default)\n'
              '  --no-entitlements   leave executable unsigned without entitlements\n'
              '  (Use --romfs separate for a small IPA + separate Documents/romfs.img)\n')

    rest = []
    for a in sys.argv[1:]:
        if a == '--entitlements': os.environ['NXW_ENTITLEMENTS'] = '1'
        elif a == '--no-entitlements': os.environ['NXW_ENTITLEMENTS'] = '0'
        elif a == '--split-data': rest += ['--romfs', 'separate']
        else: rest.append(a)
    sys.argv[1:] = rest

    # Ensure every child Python loads our subprocess/LF/Windows shim
    os.environ['PYTHONPATH'] = os.pathsep.join([str(SHIM)] + [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p and p != str(SHIM)])
    os.environ.setdefault('PYTHONUTF8', '1')
    os.environ.setdefault('J', str(max(8, 2 * (os.cpu_count() or 4))))
    os.environ.setdefault('DMSH_PROCS', '0')   # Windows has no fork(); run dmsh_build.py's shader workers in threads of this process
    # Metal Developer Tools for Windows ships 32023.850 while upstream locks Xcode 27.2's 32023.922
    os.environ.setdefault('DM_ALLOW_OTHER_METAL', '1')

    if Path(getattr(sys.modules.get('sitecustomize'), '__file__', '') or '').resolve() != (SHIM / 'sitecustomize.py').resolve():
        _spec = importlib.util.spec_from_file_location('nxw_shim', SHIM / 'sitecustomize.py')
        _spec.loader.exec_module(importlib.util.module_from_spec(_spec))

    sys.path.insert(0, str(WIN))
    import toolchain as tc, doctor

    # Put LLVM, VulkanSDK (spirv-cross, glslang), zstd, dotnet and Python Scripts (nsz) on PATH
    extra = [str(p) for p in (tc.llvm_bin(),) if p]
    for tname in ('spirv-cross', 'zstd', 'glslang', 'glslangValidator'):
        tp = tc.tool_on_path(tname)
        if tp and str(tp.parent) not in extra: extra.append(str(tp.parent))
    if tc.WINDOWS:
        py_scripts = Path(sys.executable).parent / 'Scripts'
        user_scripts = Path(os.environ.get('APPDATA', '')) / f'Python/Python{sys.version_info.major}{sys.version_info.minor}/Scripts'
        for sd in (py_scripts, user_scripts, Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'dotnet'):
            if sd.is_dir() and str(sd) not in extra: extra.append(str(sd))
    os.environ['PATH'] = os.pathsep.join(extra + [os.environ.get('PATH', '')])

    sc = tc.tool_on_path('spirv-cross')
    if sc: os.environ.setdefault('SPIRV_CROSS', str(sc))

    # Ensure native Windows dmsh_xlate.exe and nx_extract.exe match in-repo sources
    sys.path.insert(0, str(REPO / 'dm/gfx/tools'))
    import dmsh_chain
    xl_sha = dmsh_chain.xlate_source_sha()
    xl_exe = _ensure_dotnet_tool('dm/gfx/tools/dmsh_xlate/dmsh_xlate.csproj', 'build/tools/dmsh_xlate', 'dmsh_xlate', xl_sha, (f'-p:XlateSourceSha={xl_sha}',))
    os.environ['XLATE'] = str(xl_exe)

    spec = importlib.util.spec_from_file_location('build_ipa_from_nsp', REPO / 'scripts/build_ipa_from_nsp.py')
    up = importlib.util.module_from_spec(spec)
    sys.modules['build_ipa_from_nsp'] = up
    spec.loader.exec_module(up)
    BuildError = up.BuildError

    nx_exe = _ensure_dotnet_tool('dm/tools/nx_extract/nx_extract.csproj', 'build/tools/nx_extract', 'nx_extract', up.nx_extract_source_sha(REPO))
    up.TOOL_PATHS = extra
    up.translator_key = lambda _xlate: ('files', [xl_exe])
    _orig_build_shaders = up.build_shaders
    up.build_shaders = lambda py, repo, sh, xlate, env, logs, g, *a, **kw: _orig_build_shaders(py, repo, sh, xl_exe, env, logs, g, *a, **kw)

    def check_prereqs(env, xlate, mac=False):
        if mac: raise BuildError('the Mac platforms need a Mac')
        bad = [r for r in doctor.checks() if r['required'] and not r['ok']]
        if bad: raise BuildError('missing on this PC: ' + '; '.join(f'{r["name"]} ({r["how"]})' for r in bad) + ' (python win/doctor.py lists them)')
    up.check_prereqs = check_prereqs

    def find_nx_extract(repo, env):
        r = subprocess.run([str(nx_exe), '--source-sha'], capture_output=True, text=True, env=env, timeout=60, errors='replace')
        got, want = r.stdout.strip(), up.nx_extract_source_sha(repo)
        if r.returncode or got != want:
            raise BuildError(f'nx_extract mismatch ({got} / {want}): delete win/build/tools/nx_extract and rerun')
        return nx_exe
    up.find_nx_extract = find_nx_extract

    # Upstream sdk_patches.py + engine_patches.py now apply automatic function-matched SDK/engine cuts (MATCH_CUTS).
    # Keep orig_rewrite untouched unless mod cuts are provided.
    orig_patch_files = up.patch_files

    def patch_files(repo, exe_dir, mods, game_id=None):
        f = orig_patch_files(repo, exe_dir, mods, game_id)
        extra_f = [Path(j) for j in (os.environ.get('NXW_MOD_CUTS'),) if j and Path(j).exists()]
        return f + extra_f
    up.patch_files = patch_files

    up.can_clone = lambda d: False
    up.apple_silicon = lambda: True

    def heap_estimate(repo, d, patched, work, env, logs, g):
        up.PROG.stage('heap estimate (no Mac host)')
        sig = {}
        if int(g.get('sys_resource', '0'), 0) and int(g.get('app_pool', '0'), 0) > 0x80000000:
            sig['app_pool'] = ('0x80000000', 'virtual address memory and no heap check on Windows: 2 GB pool')
        for k, (v, why) in sig.items(): up.say(f'heap estimate: {k} {v}: {why}')
        if sig: g = up.set_keys(g, sig, 'Heap estimate on Windows (no Mac host)')
        p = g.dir / 'game.tsv'
        p.write_text(p.read_text(encoding='utf-8') + f'\n# heap check: {time.strftime("%Y-%m-%d")}, estimated on Windows without booting the game\n', encoding='utf-8')
        up.PROG.end('set ' + ', '.join(sig) if sig else 'guessed keys kept')
        return type(g)(g.dir)
    up.heap_check = heap_estimate

    def region_skip(repo, d, patched, work, env, logs, g):
        up.PROG.stage('address regions (no Mac host)')
        p = g.dir / 'game.tsv'
        p.write_text(p.read_text(encoding='utf-8') + f'\n# address regions: {time.strftime("%Y-%m-%d")}, not checked (no Mac host on this PC)\n', encoding='utf-8')
        up.PROG.end('skipped (needs Mac host)')
        return type(g)(g.dir)
    up.region_check = region_skip

    xc = [sys.executable, str(WIN / 'xcrun.py')]
    up.toolchain = lambda repo=None, env=None: (
        ('cmd', xc + ['-sdk', 'iphoneos', 'metal', '--version']),
        ('cmd', xc + ['-sdk', 'macosx', 'metal', '--version']),
        ('files', [sc]) if sc else ('cmd', [os.environ.get('SPIRV_CROSS', 'spirv-cross'), '--version']),
    )

    CAP = {'glslc': ('glslc_capture', 'DM_GLSLC_CAPTURE', 'nvn'), 'vk': ('vk_capture', 'DM_VK_CAPTURE', 'vulkan'), 'gl': ('gl_capture', 'DM_GL_CAPTURE', 'gl')}

    def device_capture(kind, d, patched, work, env, g, want, done_fn):
        name, var, gfx = CAP[kind]
        up.PROG.stage(f'{name.replace("_", " ")} (from the device)')
        nsp = work.parent
        roots = [nsp] + ([nsp.parent] if nsp.name.endswith('_nxw') else [])
        cands = [r / n for r in roots for n in (name, f'{g.id}_capture/{name}', f'{g.id}_capture')]
        for c in cands:
            if not c.is_dir(): continue
            got = done_fn(c)
            if not got: continue
            if want and not got >= want:
                up.say(f'[RED] {c} holds {len(got & want)} of the {len(want)} shaders the profile names: run the capture IPA longer and copy the folder again')
                continue
            dst = d / name; up.rmtree(dst); shutil.copytree(c, dst)
            up.PROG.end(f'{len(got)} shaders from {c}')
            return dst
        envf = work / 'tmp' / 'capture_env.txt'; envf.parent.mkdir(parents=True, exist_ok=True)
        envf.write_text(f'# shader capture (win/build_ipa_windows.py)\n{var}={name}\n', encoding='utf-8')
        stem = (g.get('ipa') or f'{g.id}.ipa').rsplit('.', 1)[0]
        out = nsp / f'{stem}-capture.ipa'
        nacp = d / 'control.nacp'
        try: gname = subprocess.run([sys.executable, str(REPO / 'dm/tools/nacp.py'), str(nacp)], capture_output=True, text=True).stdout.strip() if nacp.exists() else ''
        except OSError: gname = ''
        cenv = dict(env, GAME=g.id, W=str(work / 'ipa_capture'), PATCHED=str(patched), ROMFS=str(up.rfs.source(d)), AOC='', GFXAPI=gfx, DM_ENV_FILE=str(envf), NXW_CAPTURE_IPA='1',
                    DM_DISPLAY_NAME=f'{gname or stem} (capture)', DM_SHOW_FPS='NO')
        up.PROG.set(step='packing the capture IPA')
        up.run([sys.executable, str(WIN / 'build_app.py'), str(out)], work / 'logs' / 'capture_ipa.log', env=cenv)
        up.PROG.end(ok=False)
        want_n = f'{len(want)} ' if want else ''
        raise BuildError(f'this game compiles {want_n}shaders only at run time; capture them on your iPhone: 1) Sideload {out} and run until the title screen, wait a minute. '
                         f'2) Copy "On My iPhone > {gname or stem} (capture) > {name}" to {nsp}. 3) Run the build again.')

    def capture_glslc(repo, dirs, pkg_ver, patched, work, env, logs, g):
        d = dirs[pkg_ver]
        want = up.glslc_wanted(g)
        if want and up.glslc_done(d / 'glslc_capture') >= want: up.say(f'reusing captured run-time shaders: {d / "glslc_capture"}'); return
        device_capture('glslc', d, patched, work, env, g, want, up.glslc_done)
    up.capture_glslc = capture_glslc

    # Emulated ARM64 Oodle / LZ5 decoder for games that compress shaders with Oodle (Unreal, Trinity, etc.)
    up.build_mac_host = lambda repo, out, env, logs, g: WIN / 'nxw_oodle_host'
    up.UE_DECODE_SECS = up.UE_DECODE_SECS * 20

    import love_shaders
    def capture_shaders(repo, d, patched, work, env, logs, g, api):
        if api != 'gl': return device_capture(api, d, patched, work, env, g, up.capture_wanted(g, api), lambda c: up.capture_done(c, api))
        out, part = d / 'gl_capture', d / 'gl_capture.partial'
        up.PROG.stage('GL shader reconstruction (LÖVE)')
        up.rmtree(part)
        try: table = g.table(up.CAPTURES['gl']['table'])
        except Exception: table = None
        r = love_shaders.generate(d / 'ExeFS/main', up.rfs.source(d), part, table, log=up.say)
        if r is None: return device_capture('gl', d, patched, work, env, g, up.capture_wanted(g, 'gl'), lambda c: up.capture_done(c, 'gl'))
        up.rmtree(out); part.rename(out)
        up.PROG.end(f'{len(r[0])} shaders')
        return out
    up.capture_shaders = capture_shaders

    sys.argv[0] = str(REPO / 'scripts/build_ipa_from_nsp.py')
    return up.cli(sys.argv[1:])


if __name__ == '__main__':
    sys.exit(main())
