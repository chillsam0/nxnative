#!/usr/bin/env python3
# Windows replacement for dm/ios/tools/build_ipa.sh: builds the same unsigned IPA on Windows
# without Xcode or a Mac:
#   xcodegen + xcodebuild    -> LLVM clang (-target arm64-apple-ios26.0 -mcpu=apple-m1 -isysroot <iPhoneOS SDK>) + ld64.lld
#   SwiftUI launcher & menu  -> win/ios/Launcher.m (UIKit, implements dm_show_launcher & dm_game_menu)
#   Darwin compiler-rt       -> win/ios/compat_rt.c (__isPlatformVersionAtLeast via _availability_version_check)
#   actool / sips / plutil   -> Pillow PNG icons (CFBundleIcons) + Python plistlib
#   codesign --entitlements  -> win/macho_sign.py (ad-hoc Mach-O signature with XML + DER entitlements)
#   make host_msl + dmhost   -> dm/gfx/tools/host_msl.py + dm/host/Makefile source rules (host, gfx, libvpx, dav1d)
import concurrent.futures as cf, hashlib, json, os, plistlib, re, shutil, subprocess, sys, time
from pathlib import Path

WIN = Path(__file__).resolve().parent
sys.path.insert(0, str(WIN))
import toolchain as tc

REPO = tc.REPO
DM = REPO / 'dm'
IOS = DM / 'ios'
HOST = DM / 'host'
sys.path.insert(0, str(DM / 'tools'))
import romfs as rfs
import shader_lib


def t(msg): print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)
def die(msg): t(f'FAIL: {msg}'); sys.exit(1)


def shim_env(env=None):
    e = dict(os.environ if env is None else env)
    shim = str(WIN / 'pyshim')
    pp = [p for p in e.get('PYTHONPATH', '').split(os.pathsep) if p and p != shim]
    e['PYTHONPATH'] = os.pathsep.join([shim] + pp)
    e.setdefault('PYTHONUTF8', '1')
    return e


def py(*args, env=None, capture=True):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=capture, text=True, env=shim_env(env))
    if r.returncode:
        die(f'{Path(str(args[0])).name} {" ".join(map(str, args[1:]))}: {(r.stderr or r.stdout or "").strip()[-2000:]}')
    return (r.stdout or '').strip() if capture else ''


# ---- host_msl_gen.h (dm/gfx/msl/*.metal compiled for iphoneos 26.0) ----
def build_host_msl(out_h):
    msl_srcs = sorted((DM / 'gfx/msl').glob('*.metal'), key=lambda p: p.name)
    if not msl_srcs: die('no dm/gfx/msl/*.metal sources found')
    out_h = Path(out_h)
    out_h.parent.mkdir(parents=True, exist_ok=True)
    ios_min = tc.setting('ios_deployment', tc.IOS_DEPLOYMENT)
    r = subprocess.run([sys.executable, str(DM / 'gfx/tools/host_msl.py'), '--sdk', 'iphoneos', '--min', ios_min, str(out_h), *map(str, msl_srcs)],
                       capture_output=True, text=True, env=shim_env())
    if r.returncode:
        print(r.stderr[-3000:], file=sys.stderr)
        die('host_msl.py failed')
    if r.stderr.strip(): t(r.stderr.strip().splitlines()[-1])
    return out_h.parent


def _expand_make_list(expr, base_dir):
    """Expand space-separated tokens and $(sort $(wildcard ...)) expressions from dm/host/Makefile"""
    out = []
    pos = 0
    while pos < len(expr):
        m = re.search(r'\$\(sort\s+\$\(wildcard\s+([^)]+)\)\)', expr[pos:])
        if not m:
            out += [base_dir / tok for tok in expr[pos:].split() if tok]
            break
        out += [base_dir / tok for tok in expr[pos:pos + m.start()].split() if tok]
        globs = m.group(1).split()
        matched = []
        for pat in globs:
            if '/' in pat:
                d, g = pat.rsplit('/', 1)
                matched += [base_dir / d / p.name for p in (base_dir / d).glob(g)]
            else:
                matched += list(base_dir.glob(pat))
        out += sorted(matched, key=lambda p: os.path.relpath(p, base_dir).replace('\\', '/'))
        pos += m.end()
    return out


# ---- dm/host/Makefile source rules, reproduced ----
def host_sources(game_h_dir, msl_h_dir):
    """Returns list of (source_path, extra_flags, extra_incs) matching dm/host/Makefile for iOS (host + gfx + libvpx + dav1d)"""
    mk = (HOST / 'Makefile').read_text(encoding='utf-8')
    m_src = re.search(r'^SRC=(.+)$', mk, re.M)
    m_gfx = re.search(r'^GFX=(.+)$', mk, re.M)
    if not m_src or not m_gfx:
        die('dm/host/Makefile missing SRC= or GFX= line')

    src = _expand_make_list(m_src.group(1), HOST)
    gfx = _expand_make_list(m_gfx.group(1), HOST)

    vpx = DM / 'third_party/libvpx'
    vpx_src = sorted(vpx.rglob('*.c'), key=lambda p: p.as_posix())

    dav1d = DM / 'third_party/dav1d'
    dav1d_src = sorted(
        [p for p in (dav1d / 'src').glob('*.c') if not p.name.endswith('_tmpl.c')]
        + [dav1d / 'src/arm/cpu.c']
        + list((dav1d / 'config/tmpl').glob('*.c'))
        + [p for p in (dav1d / 'src/arm/64').glob('*.S') if not p.name.endswith('_tmpl.S') and p.name != 'util.S'],
        key=lambda p: p.as_posix()
    )

    host_incs = [
        DM / 'gfx/nvn/include', Path(msl_h_dir), DM / 'third_party/vulkan-headers/include',
        Path(game_h_dir), vpx, dav1d / 'include', DM / 'third_party/stb_vorbis',
    ]
    host_files = []
    host_dirs = []
    for s in src + gfx:
        s = s.resolve()
        if s.stem.endswith('_mac'): continue   # Mac-only sources (front_mac.m, audio_input_mac.m)
        if s.parent == (IOS / 'Sources').resolve(): continue
        host_files.append(s)
        if s.parent not in host_dirs: host_dirs.append(s.parent)
    host_incs_resolved = [p.resolve() for p in host_incs]
    for d in host_dirs:
        if d not in host_incs_resolved: host_incs_resolved.append(d)

    vpx_incs = [vpx.resolve(), (vpx / 'config').resolve()]
    dav1d_incs = [dav1d.resolve(), (dav1d / 'include').resolve(), (dav1d / 'config').resolve()]
    dav1d_flags = ['-fvisibility=hidden', '-fomit-frame-pointer', '-fno-align-functions']

    items = [(f, [], host_incs_resolved) for f in host_files]
    items += [(f.resolve(), [], vpx_incs) for f in vpx_src]
    items += [(f.resolve(), dav1d_flags, dav1d_incs) for f in dav1d_src]
    return items, host_incs_resolved


FRAMEWORKS_BASE = [
    'WebKit', 'UserNotifications', 'QuartzCore', 'Metal', 'MetalFX', 'GameController',
    'CoreBluetooth', 'CoreHaptics', 'AudioToolbox', 'CoreAudio', 'AVFAudio',
    'CoreFoundation', 'CoreGraphics', 'VideoToolbox', 'CoreMedia', 'CoreVideo',
    'ImageIO', 'UniformTypeIdentifiers', 'UIKit', 'Foundation',
]
IMPORT = re.compile(r'^\s*#\s*(?:import|include)\s*<(\w+)/', re.M)


def frameworks(sdk, files):
    fw_dir = sdk / 'System/Library/Frameworks'
    out = list(FRAMEWORKS_BASE)
    for f in files:
        if f.suffix not in ('.m', '.c', '.h'): continue
        for name in IMPORT.findall(f.read_text(encoding='utf-8', errors='replace')):
            if name not in out and (fw_dir / f'{name}.framework').is_dir(): out.append(name)
    return [n for n in out if (fw_dir / f'{n}.framework').is_dir()]


def dep_newer(o):
    d = o.with_suffix('.d')
    if not d.exists(): return True
    mt = o.stat().st_mtime
    text = d.read_text(errors='replace').replace('\\\n', ' ')
    for dep in re.split(r'(?<!\\)\s+', text.split(':', 1)[-1]):
        dep = dep.replace('\\ ', ' ').strip()
        if dep and os.path.exists(dep) and os.path.getmtime(dep) > mt: return True
    return False


def compile_items(clang, sdk, items, obj_dir, extra_defs=()):
    target = f'arm64-apple-ios{tc.setting("ios_deployment", tc.IOS_DEPLOYMENT)}'
    base = ['-target', target, '-isysroot', str(sdk), '-Os', '-mcpu=apple-m1', '-w', '-fno-common', '-DNS_BLOCK_ASSERTIONS=1', *extra_defs]
    obj_dir.mkdir(parents=True, exist_ok=True)
    jobs, objs = [], []
    main_c = (HOST / 'main.c').resolve()
    for f, extra_flags, incs in items:
        inc_flags = [f'-I{i}' for i in incs]
        if f == main_c: flags = base + extra_flags + ['-std=gnu11', '-Dmain=dm_main'] + inc_flags
        elif f.suffix == '.c': flags = base + extra_flags + ['-std=gnu11'] + inc_flags
        elif f.suffix == '.m': flags = base + extra_flags + ['-std=gnu11', '-fobjc-arc'] + inc_flags
        elif f.suffix == '.S': flags = base + extra_flags + inc_flags
        else: continue
        key = hashlib.sha256((str(f) + '\0' + '\0'.join(flags)).encode()).hexdigest()[:12]
        o = obj_dir / f'{f.stem}-{key}.o'
        objs.append(o)
        if o.exists() and o.stat().st_mtime >= f.stat().st_mtime and not dep_newer(o): continue
        jobs.append((f, o, flags))
    t(f'compile: {len(jobs)} of {len(objs)} files (clang {target})')
    def one(j):
        f, o, flags = j
        d = o.with_suffix('.d')
        r = subprocess.run([str(clang), *flags, '-MMD', '-MF', str(d), '-c', str(f), '-o', str(o) + '.tmp'], capture_output=True, text=True)
        if r.returncode == 0: os.replace(str(o) + '.tmp', o)
        return f, r
    bad = []
    with cf.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        for i, (f, r) in enumerate(ex.map(one, jobs), 1):
            if r.returncode: bad.append((f, r.stderr))
            if i % 50 == 0 or i == len(jobs): t(f'compile: {i}/{len(jobs)}')
    if bad:
        for f, err in bad[:5]:
            print(f'---- {f}\n' + '\n'.join(l for l in err.splitlines() if 'error' in l or 'fatal' in l)[:3000], file=sys.stderr)
        die(f'{len(bad)} file(s) did not compile')
    return objs


def compat_rt_needed(clang):
    r = subprocess.run([str(clang), '-print-resource-dir'], capture_output=True, text=True)
    res = Path(r.stdout.strip()) if r.returncode == 0 else None
    if res and list(res.glob('lib/darwin/libclang_rt.ios.a')): return None
    return WIN / 'build' / 'compat_rt.o'


def link(clang, sdk, objs, fws, embed_flags, out, sign_adhoc=True):
    target = f'arm64-apple-ios{tc.setting("ios_deployment", tc.IOS_DEPLOYMENT)}'
    args = [
        str(clang), '-target', target, '-isysroot', str(sdk), '-fuse-ld=lld', '-fobjc-arc',
        *(['-Wl,-adhoc_codesign'] if sign_adhoc else []),
        '-o', str(out), *map(str, objs),
        '-Wl,-rpath,@executable_path/Frameworks', '-lobjc', '-lz',
        *[x for f in fws for x in ('-framework', f)], *embed_flags,
    ]
    rt = compat_rt_needed(clang)
    if rt: args.append(str(rt))
    t(f'link: {len(objs)} objects, {len(fws)} frameworks (ld64.lld)')
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode: print(r.stderr[-6000:], file=sys.stderr); die('link failed')


# ---- bundle metadata ----
def info_plist(bundle_id, name, version, category, show_fps, lang, sdk_ver, sdk_build, icons):
    p = {
        'CFBundleDevelopmentRegion': 'en',
        'CFBundleExecutable': 'DmIOS',
        'CFBundleIdentifier': bundle_id,
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'DmIOS',
        'CFBundleDisplayName': name,
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        'CFBundleSupportedPlatforms': ['iPhoneOS'],
        'DTPlatformName': 'iphoneos',
        'DTPlatformVersion': sdk_ver,
        'DTSDKName': f'iphoneos{sdk_ver}',
        'DTSDKBuild': sdk_build,
        'MinimumOSVersion': tc.setting('ios_deployment', tc.IOS_DEPLOYMENT),
        'LSRequiresIPhoneOS': True,
        'UIDeviceFamily': [1, 2],
        'UIRequiredDeviceCapabilities': ['arm64'],
        # Matches dm/ios/project.yml info.properties
        'UILaunchScreen': {},
        'UIFileSharingEnabled': True,
        'LSSupportsOpeningDocumentsInPlace': True,
        'UISupportedInterfaceOrientations': [
            'UIInterfaceOrientationLandscapeRight',
            'UIInterfaceOrientationLandscapeLeft',
        ],
        'UISupportedInterfaceOrientations~ipad': [
            'UIInterfaceOrientationPortrait',
            'UIInterfaceOrientationPortraitUpsideDown',
            'UIInterfaceOrientationLandscapeRight',
            'UIInterfaceOrientationLandscapeLeft',
        ],
        'UIStatusBarHidden': True,
        'UIViewControllerBasedStatusBarAppearance': False,
        'LSApplicationCategoryType': category,
        'DMShowFPS': bool(show_fps),
        'DMLanguage': lang,
        'LSSupportsGameMode': True,
        'GCSupportsControllerUserInteraction': True,
        'GCSupportedGameControllers': [{'ProfileName': 'ExtendedGamepad'}],
        'CADisableMinimumFrameDurationOnPhone': True,
        'UIApplicationSceneManifest': {
            'UIApplicationSupportsMultipleScenes': False,
            'UISceneConfigurations': {
                'UIWindowSceneSessionRoleApplication': [{
                    'UISceneConfigurationName': 'Default',
                    'UISceneDelegateClassName': 'SceneDelegate',
                }],
                'UIWindowSceneSessionRoleExternalDisplayNonInteractive': [{
                    'UISceneConfigurationName': 'TV',
                    'UISceneDelegateClassName': 'DmTVSceneDelegate',
                }],
            },
        },
    }
    if icons:
        p['CFBundleIcons'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': ['AppIcon60x60'], 'CFBundleIconName': 'AppIcon'}}
        p['CFBundleIcons~ipad'] = {'CFBundlePrimaryIcon': {'CFBundleIconFiles': ['AppIcon60x60', 'AppIcon76x76', 'AppIcon83.5x83.5'], 'CFBundleIconName': 'AppIcon'}}
    return p


ICONS = [
    ('AppIcon60x60@2x.png', 120),
    ('AppIcon60x60@3x.png', 180),
    ('AppIcon76x76@2x~ipad.png', 152),
    ('AppIcon83.5x83.5@2x~ipad.png', 167),
    ('AppIcon.png', 1024),
]


def make_icons(src, app):
    try: from PIL import Image
    except ImportError: t('no app icon: Pillow is not installed (pip install pillow); the home-screen icon stays blank'); return False
    im = Image.open(src).convert('RGB')
    for name, px in ICONS: im.resize((px, px), Image.LANCZOS).save(app / name, 'PNG')
    t(f'app icon: {src}')
    return True


def git_commit():
    try:
        c = subprocess.run(['git', '-C', str(DM), 'rev-parse', '--short=12', 'HEAD'], capture_output=True, text=True, timeout=10)
        if c.returncode != 0 or not c.stdout.strip(): return ''
        commit = c.stdout.strip()
        st = subprocess.run(['git', '-C', str(DM), 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True, timeout=10)
        if st.returncode == 0 and st.stdout.strip(): commit += '+dirty'
        return commit
    except OSError:
        return ''


def lib_has(lib_dir):
    v = shader_lib.view(lib_dir)
    return v.exists() and any(v.glob('*_b.metallib'))


def check_program(gm, v, game_id, patched, romfs, msl_ios, gfx, capture=False):
    prof = gm.load(game_id)
    gd = prof.dir
    if not (gd / 'game.tsv').exists(): die(f'no game profile {gd}/game.tsv')
    if not (patched / 'main.fixups.tsv').exists(): die(f'no rewritten main in {patched} (run dm/tools/patch_nso.py first)')
    mods = py(DM / 'games/game.py', 'modules', game_id, patched).split()
    for m in mods:
        if not ((patched / f'{m}.patched.bin').exists() and (patched / f'{m}.fixups.tsv').exists()):
            die(f'no rewritten module {patched}/{m} (run dm/tools/patch_nso.py first)')
    marker = prof.get('romfs_marker')
    if not (romfs / marker).exists(): die(f'no RomFS at {romfs} (missing {marker})')
    if not gfx:
        if lib_has(msl_ios / f'b-{v}'): gfx = 'nvn'
        elif (msl_ios / 'vk/vk_shaders.metallib').exists(): gfx = 'vulkan'
        elif (msl_ios / 'gl/gl_shaders.metallib').exists(): gfx = 'gl'
        else: gfx = 'nvn'
    if capture:
        msldir = {'vulkan': 'vk', 'gl': 'gl'}.get(gfx, f'b-{v}')
        return prof, mods, gfx, msldir
    if gfx == 'vulkan':
        if not (msl_ios / 'vk/vk_shaders.metallib').exists(): die(f'no iOS Vulkan shader library {msl_ios}/vk')
        msldir = 'vk'
    elif gfx == 'gl':
        if not (msl_ios / 'gl/gl_shaders.metallib').exists(): die(f'no iOS GL shader library {msl_ios}/gl')
        msldir = 'gl'
    else:
        msldir = f'b-{v}'
        lib_dir = msl_ios / msldir
        if not lib_has(lib_dir): die(f'no iOS shader libraries {lib_dir}')
        inputs = py(DM / 'gfx/tools/dmsh_chain.py', '--inputs', env=dict(os.environ, DM_GAME=game_id))
        stamp = shader_lib.read_stamp(lib_dir)
        if stamp.get('game') != game_id or stamp.get('inputs') != inputs:
            die(f'the iOS shader libraries {lib_dir} are not a full build from this profile\'s current tables (inputs {inputs}; stamp {stamp})')
        c1 = gd / 'c1_full.tsv'
        first_line = (c1.read_text(encoding='utf-8').splitlines()[:1] or [''])[0] if c1.exists() else ''
        if not re.match(rf'^# chain b-{re.escape(v)} inputs {re.escape(inputs)}( |$)', first_line):
            die(f'{c1} is missing or not made from these libraries (chain {v}, inputs {inputs})')
    if gfx == 'nvn' and (gd / 'gl_capture.tsv').is_file() and (gd / 'gl_capture.tsv').stat().st_size > 0:
        if not ((msl_ios / 'gl/gl_shaders.metallib').is_file() and (msl_ios / 'gl/gl_shaders.tsv').is_file()):
            die(f'no supplemental GL shader library and table at {msl_ios}/gl')
    return prof, mods, gfx, msldir


def copy_program_tables(game_id, prof, patched, vnacp, gfx, td, env, capture=False):
    gd = prof.dir
    td.mkdir(parents=True, exist_ok=True)
    for pat in ('*.fixups.tsv', '*.gates.tsv', '*.cuts.tsv', '*.obs.tsv'):
        for f in patched.glob(pat): shutil.copy2(f, td)
    if (patched / 'nro.tsv').exists(): shutil.copy2(patched / 'nro.tsv', td)
    need_tables = ('shader_map.tsv',) if gfx in ('vulkan', 'gl') else ('shader_map.tsv', 'c1_full.tsv')
    for f in need_tables:
        if capture and not (gd / f).exists(): continue
        shutil.copy2(gd / f, td)
    if gfx == 'nvn':
        if (gd / 'cb_extent.tsv').exists(): shutil.copy2(gd / 'cb_extent.tsv', td)
        else: t(f'no {gd}/cb_extent.tsv: the host copies whole uniform bindings')
        for f in ('shader_pairs.tsv', 'pso_pairs.tsv'):
            if (gd / f).exists(): shutil.copy2(gd / f, td)
    if (gd / 'pso_manifest.tsv').exists(): shutil.copy2(gd / 'pso_manifest.tsv', td)
    else: t('no pso_manifest.tsv: no bundled prewarm seed list')
    t('launcher: ' + py(DM / 'games/settings.py', game_id, patched, vnacp, td / 'launcher.json', env=dict(env, GFXAPI=gfx)))
    if vnacp and Path(vnacp).is_file(): shutil.copy2(vnacp, td / 'control.nacp')
    else: t(f'[RED] no control data at {vnacp}: fs::GetCacheStorageMax fails in the app')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--prepare-xcode':
        die('--prepare-xcode makes an Xcode project: it needs a Mac (this Windows build packs the IPA directly)')
    env = os.environ
    game = env.get('GAME') or die('GAME is not set')
    w = Path(env.get('W') or die('W is not set: W=<NSP folder>/<title ID>-work'))
    sys.path.insert(0, str(DM / 'games'))
    import game as gm
    prof0 = gm.load(game)
    pkg = prof0.get('package')
    patched0 = Path(env.get('PATCHED') or w / f'build/patched_{pkg}')
    romfs0 = rfs.Path(env.get('ROMFS') or rfs.source(w / f'data/{pkg}'))
    msl_ios0 = Path(env.get('MSL_IOS') or w / 'build/ios/dmsh_b')
    aoc = Path(env['AOC']) if env.get('AOC') else (None if 'AOC' in env else w / 'data/aoc')
    out_ipa = Path(sys.argv[1]) if len(sys.argv) > 1 else w.parent / prof0.get('ipa')

    nacp = w / 'data/v0/control.nacp'
    if not nacp.exists(): nacp = romfs0.parent / 'control.nacp'
    vnacp0 = Path(env['CONTROL']) if env.get('CONTROL') else (romfs0.parent / 'control.nacp')
    if not vnacp0.exists(): vnacp0 = w / 'data/v0/control.nacp'
    name = env.get('DM_DISPLAY_NAME') or (py(DM / 'tools/nacp.py', nacp) if nacp.exists() else prof0.get('name'))
    r = subprocess.run([sys.executable, str(DM / 'tools/nacp.py'), '--version', str(vnacp0)], capture_output=True, text=True) if vnacp0.exists() else None
    version = env.get('DM_APP_VERSION') or (r.stdout.strip() if r and r.returncode == 0 and r.stdout.strip() else '1.0')
    bundle_id = env.get('DM_BUNDLE_ID') or f'org.nxnative.{game.lower()}'
    prefix = env.get('NXW_BUNDLE_PREFIX')
    if prefix and bundle_id.startswith('org.nxnative.'): bundle_id = prefix.rstrip('.') + bundle_id[len('org.nxnative'):]
    show_fps = env.get('DM_SHOW_FPS', '0') in ('1', 'YES', 'true', 'TRUE')
    lang = env.get('DM_LANG') or 'en-US'

    hdr = (DM / 'gfx/nvn/dmsh_chain.h').read_text(encoding='utf-8')
    v = (re.search(r'^#define DMSH_CHAIN "(.*)"', hdr, re.M) or die('no DMSH_CHAIN in dmsh_chain.h')).group(1)
    capture = env.get('NXW_CAPTURE_IPA') == '1'

    if capture:
        gfx0 = env.get('GFXAPI') or 'nvn'
        msldir0 = {'vulkan': 'vk', 'gl': 'gl'}.get(gfx0, f'b-{v}')
        msl_ios0 = w / 'build/ios/empty_msl'
        (msl_ios0 / msldir0).mkdir(parents=True, exist_ok=True)
        t('capture IPA: no shader libraries; the game saves run-time shaders into Documents')
        prof0, mods0, gfx0, msldir0 = check_program(gm, v, game, patched0, romfs0, msl_ios0, gfx0, capture=True)
    else:
        prof0, mods0, gfx0, msldir0 = check_program(gm, v, game, patched0, romfs0, msl_ios0, env.get('GFXAPI') or '')

    programs = []
    if env.get('PROGRAMS_TSV'):
        ptsv = Path(env['PROGRAMS_TSV'])
        if not ptsv.is_file(): die(f'no PROGRAMS_TSV {ptsv}')
        for ln in ptsv.read_text(encoding='utf-8').splitlines():
            if not ln.strip() or ln.startswith('#'): continue
            pn, gid, p_patched, p_romfs, p_msl, p_gfx = ln.split('\t')
            p_prof, p_mods, p_gfx, p_msldir = check_program(gm, v, gid, Path(p_patched), rfs.Path(p_romfs), Path(p_msl), p_gfx, capture=capture)
            programs.append((pn, gid, p_prof, p_mods, Path(p_patched), rfs.Path(p_romfs), Path(p_msl), p_gfx, p_msldir))
            t(f'program {pn}: {gid} ({p_prof.get("name")}), {p_gfx}')

    sdk = tc.ios_sdk() or die('no iPhoneOS SDK: set NXW_IOS_SDK (win/sdk/extract_xip.py extracts it from Xcode.xip)')
    clang = tc.llvm_tool('clang') or die('no clang: install LLVM for Windows')
    if not tc.llvm_tool('ld64.lld'): die('no ld64.lld in the LLVM folder')
    sdk_ver, sdk_build = tc.sdk_settings(sdk)
    t(f'app: {name} {version} ({bundle_id}), language {lang}{", frame-rate counter on" if show_fps else ""}; SDK {sdk.name} {sdk_ver} {sdk_build}')

    base = w / 'build/ios/win'
    gen, app = base / 'Gen', base / 'Payload/DmIOS.app'
    if gen.exists(): shutil.rmtree(gen)
    if app.exists(): shutil.rmtree(app)
    (gen / 'profile').mkdir(parents=True)
    app.mkdir(parents=True)

    t('generate Gen/ (profile header, host Metal shaders, embedded modules, tables)')
    py(DM / 'games/game.py', 'header', game, gen / 'profile/game_profile.h')
    msl_h_dir = build_host_msl(base / 'host_msl/host_msl_gen.h')

    es = gen / 'embed.S'
    embed_flags = []
    if programs:
        es_parts = []
        for pn, gid, p_prof, p_mods, p_patched, p_romfs, _, _, _ in programs:
            jit_img = p_romfs.parent / 'jit/jit.img'
            jargs = ['--jit', str(jit_img)] if jit_img.is_file() else []
            tmp_s = gen / f'embed_p{pn}.S'
            f_out = py(IOS / 'tools/gen_embed.py', '--tag', f'p{pn}', *jargs, p_patched, tmp_s, *p_mods)
            embed_flags += f_out.split()
            es_parts.append(tmp_s.read_text(encoding='utf-8'))
            tmp_s.unlink()
        es_text = '\n'.join(es_parts)
    else:
        jit_img = romfs0.parent / 'jit/jit.img'
        jargs = ['--jit', str(jit_img)] if jit_img.is_file() else []
        embed_flags = py(IOS / 'tools/gen_embed.py', *jargs, patched0, es, *mods0).split()
        es_text = es.read_text(encoding='utf-8')
    es.write_text(re.sub(r'\.incbin "([^"]*)"', lambda m: '.incbin "' + m.group(1).replace('\\', '/') + '"', es_text), encoding='utf-8')

    dmres = app / 'dm'
    dmres.mkdir()
    if programs:
        (dmres / 'programs.txt').write_text('\n'.join(p[0] for p in programs) + '\n', encoding='utf-8')
        for pn, gid, p_prof, _, p_patched, p_romfs, _, p_gfx, _ in programs:
            p_vnacp = vnacp0 if pn == '0' else (p_romfs.parent / 'control.nacp')
            copy_program_tables(gid, p_prof, p_patched, p_vnacp, p_gfx, dmres / f'p{pn}', env, capture=capture)
        lj = dmres / 'p0/launcher.json'
    else:
        copy_program_tables(game, prof0, patched0, vnacp0, gfx0, dmres, env, capture=capture)
        lj = dmres / 'launcher.json'

    try:
        ld = next((s['default'] for s in json.loads(lj.read_text(encoding='utf-8'))['settings'] if s['env'] == 'DM_LANG'), '')
        if ld and ld != lang:
            t(f'game language: the game has no {lang}; packing {ld}, its first')
            lang = ld
    except (OSError, ValueError, KeyError):
        pass

    # Offline Metal 4 pipeline archive (pso_offline.py with metal-tt)
    pso = None
    p4 = prof0.get('pso_mtl4', 'off')
    pg = prof0.get('pso_gpu', '')
    pgk = 'g18p'
    if p4 == 'on' and pg:
        ws = pg.split()
        if 1 <= len(ws) <= 4 and all(w != 'none' and re.fullmatch(r'(applegpu_)?[A-Za-z0-9]+', w) for w in ws):
            pgk = ','.join(ws)
    gpus = env['DM_PSO_GPUS'] if 'DM_PSO_GPUS' in env else (pgk if p4 == 'on' else 'none')
    if gfx0 == 'nvn' and not capture and gpus and gpus != 'none' and (prof0.dir / 'shader_pairs.tsv').exists():
        pso = w / 'build/ios/pso/bundle'
        t(f'pipeline archive for {gpus} (metal-tt; minutes when its inputs changed)')
        r = subprocess.run(
            [sys.executable, str(DM / 'gfx/tools/pso_offline.py'), '--game', game, '--msl', str(msl_ios0 / msldir0),
             '--out', str(pso), '--work', str(w / 'build/ios/pso/work'), '--gpu', gpus]
            + (['--os-min', env['DM_PSO_IOS_MIN']] if env.get('DM_PSO_IOS_MIN') else []),
            env=shim_env()
        )
        if r.returncode:
            t('[RED] pipeline archive: pso_offline.py failed; building app without archive (device will build pipeline bases on first start)')
            shutil.rmtree(pso, ignore_errors=True)
            pso = None
    elif gfx0 == 'nvn':
        t('no pipeline archive (' + (f'DM_PSO_GPUS={gpus}' if 'DM_PSO_GPUS' in env else 'profile does not set pso_mtl4 on') + ')')

    if env.get('DM_ENV_FILE'): shutil.copy2(env['DM_ENV_FILE'], dmres / 'env.txt')

    # Shared fonts (make_shared_fonts.py: all 6 Switch shared font stand-ins)
    font = tc.std_font() or die('no standard font: set STD_FONT to a TTF with broad Unicode coverage')
    lfont = tc.letter_font() or die('no bold TTF for the button icons: set LETTER_FONT')
    import make_shared_fonts
    make_shared_fonts.build(str(dmres / 'font'), str(font), str(lfont))
    t(f'shared fonts: {font} + {lfont}')

    # Compile and link the iOS executable
    items, host_incs = host_sources(gen / 'profile', msl_h_dir)
    ios_incs = [ (IOS / 'Sources').resolve() ] + host_incs
    ios_srcs = [p.resolve() for p in sorted((IOS / 'Sources').glob('*.m'))]
    ios_srcs += [(WIN / 'ios/Launcher.m').resolve(), es.resolve()]
    items += [(f, [], ios_incs) for f in ios_srcs]

    all_files = [f for f, _, _ in items]
    fws = frameworks(sdk, all_files)

    rt = compat_rt_needed(clang)
    if rt:
        rt.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(clang), '-target', f'arm64-apple-ios{tc.setting("ios_deployment", tc.IOS_DEPLOYMENT)}',
                            '-isysroot', str(sdk), '-Os', '-mcpu=apple-m1', '-c', str(WIN / 'ios/compat_rt.c'), '-o', str(rt)],
                           capture_output=True, text=True)
        if r.returncode: print(r.stderr, file=sys.stderr); die('compat_rt.c failed to compile')

    want_ent = env.get('DM_ENTITLEMENTS', 'default') != '' and env.get('NXW_ENTITLEMENTS', '1') != '0'
    objs = compile_items(clang, sdk, items, base / 'obj')
    link(clang, sdk, objs, fws, embed_flags, app / 'DmIOS', sign_adhoc=want_ent)

    if want_ent:
        import macho_sign
        if env.get('DM_ENTITLEMENTS') and Path(env['DM_ENTITLEMENTS']).is_file():
            ents = plistlib.loads(Path(env['DM_ENTITLEMENTS']).read_bytes())
        else:
            ents = {'com.apple.developer.kernel.increased-memory-limit': True}
        sig_n = macho_sign.sign(str(app / 'DmIOS'), bundle_id, ents)
        t(f'entitlements in ad-hoc signature: {", ".join(ents)} ({sig_n} bytes)')

    icon = Path(env.get('DM_ICON') or romfs0.parent / 'icon.jpg')
    icons = icon.exists() and make_icons(icon, app)
    if not icon.exists(): t(f'no app icon ({icon} not found): the home-screen icon stays blank')

    pl = info_plist(bundle_id, name, version, prof0.get('app_category'), show_fps, lang, sdk_ver, sdk_build, icons)
    commit = git_commit()
    if commit: pl['DMCommit'] = commit; t(f'source: {commit}')
    if msldir0.startswith('b-'): pl['DMChain'] = msldir0[2:]
    if env.get('DM_METAL_CAPTURE') == '1': pl['MetalCaptureEnabled'] = True; t('Metal frame capture: enabled (DM_METAL_CAPTURE=1)')
    with open(app / 'Info.plist', 'wb') as f: plistlib.dump(pl, f, fmt=plistlib.FMT_BINARY)
    (app / 'PkgInfo').write_bytes(b'APPL????')

    # Assemble pack_ipa.py mappings (mirrors build_ipa.sh)
    P = 'Payload/DmIOS.app'
    skip = [f'{P}/_CodeSignature', f'{P}/embedded.mobileprovision', f'{P}/msl', f'{P}/aoc', f'{P}/romfs',
            f'{P}/romfs.img', f'{P}/romfs.id', f'{P}/html', f'{P}/jit', f'{P}/romfs_programs', f'{P}/pso']
    has_aoc = bool(aoc and aoc.is_dir() and any(aoc.iterdir()))
    if has_aoc: t('add-on content: ' + ' '.join(sorted(p.name for p in aoc.iterdir())))

    maps = []
    if programs:
        if env.get('DM_COPY_ROMFS', 'YES') == 'NO':
            die('DM_COPY_ROMFS=NO: a game of several programs keeps its programs\' files in the app')
        maps.append(f'{P}={app}')
        if has_aoc: maps.append(f'{P}/aoc={aoc}')
        for pn, _, p_prof, _, _, p_romfs, p_msl, p_gfx, p_msldir in programs:
            skip.append(f'{P}/p{pn}')
            maps.append(f'{P}/p{pn}/romfs.img={p_romfs}' if rfs.is_image(p_romfs) else f'{P}/p{pn}/romfs={p_romfs}')
            maps.append(f'{P}/msl/p{pn}/{p_msldir}={p_msl / p_msldir}')
            if not capture and p_gfx == 'nvn' and (p_prof.dir / 'gl_capture.tsv').is_file() and (p_prof.dir / 'gl_capture.tsv').stat().st_size > 0:
                maps.append(f'{P}/msl/p{pn}/gl={p_msl / "gl"}')
            for extra in ('html', 'jit', 'romfs_programs'):
                d = p_romfs.parent / extra
                if d.is_dir(): maps.append(f'{P}/p{pn}/{extra}={d}')
        zs = ':'.join(sorted({s for _, _, p_prof, *_ in programs for s in p_prof.get('zip_store').split(':') if s}))
    else:
        if env.get('DM_COPY_ROMFS', 'YES') == 'NO':
            t('no game files in the IPA (DM_COPY_ROMFS=NO): the app takes Documents/romfs.img')
            if rfs.is_image(romfs0):
                id_file = base / 'romfs.id'
                id_txt = py(DM / 'tools/romfs_image.py', '--identity', romfs0, game, version, pkg or '')
                id_file.write_text(id_txt + '\n', encoding='utf-8')
                maps.append(f'{P}/romfs.id={id_file}')
                t(f'game files identity: {id_txt}')
        elif rfs.is_image(romfs0):
            maps.append(f'{P}/romfs.img={romfs0}')
        else:
            maps.append(f'{P}/romfs={romfs0}')
        if has_aoc: maps.append(f'{P}/aoc={aoc}')
        if pso: maps.append(f'{P}/pso={pso}')
        maps.append(f'{P}/msl/{msldir0}={msl_ios0 / msldir0}')
        maps.append(f'{P}={app}')
        if not capture and gfx0 == 'nvn' and (prof0.dir / 'gl_capture.tsv').is_file() and (prof0.dir / 'gl_capture.tsv').stat().st_size > 0:
            maps.append(f'{P}/msl/gl={msl_ios0 / "gl"}')
        for extra in ('html', 'jit', 'romfs_programs'):
            d = romfs0.parent / extra
            if d.is_dir(): maps.append(f'{P}/{extra}={d}')
        zs = ':'.join(prof0.get('zip_store').split(':'))

    t('pack IPA (already-compressed game files stored, everything else deflated)')
    pack_opts = ['--store', zs, '--exec', f'{P}/DmIOS']
    for s in skip: pack_opts += ['--skip', s]
    if not pso and env.get('IOS_PULL', '1') != '1': pack_opts += ['--skip-suffix', '_bp.metallib']
    if env.get('DM_SHADER_FILES', '0') == '1': pack_opts += ['--shader-files']

    r = subprocess.run([sys.executable, str(IOS / 'tools/pack_ipa.py'), str(out_ipa), *pack_opts, *maps], env=shim_env())
    if r.returncode: die('pack_ipa')
    sz = out_ipa.stat().st_size
    t(f'done: {out_ipa} ({sz / 2**30:.1f} GB)' if sz >= 2**30 else f'done: {out_ipa} ({sz / 2**20:.1f} MB)')


if __name__ == '__main__':
    main()
