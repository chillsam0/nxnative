// UIKit launcher and in-game menu for the Windows build: replaces dm/ios/Sources/Launcher.swift,
// LauncherRow.swift, LauncherSettings.swift, GameMenu.swift, DmHostingController.swift and
// SaveSyncReport.swift so the iOS app compiles with LLVM clang alone (no Swift toolchain needed).
// Implements the exact C/Obj-C contracts called by front_ios.m and DmGamepad.m:
//   - bool dm_show_launcher(void *view_controller, void (*done)(void))
//   - void *dm_game_menu(void)   (DmGameMenu.h)
#import <UIKit/UIKit.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#include <math.h>
#include <os/proc.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#import "CloudSaves.h"
#import "DmGameMenu.h"
#import "DmRotation.h"
#include "save_sync.h"

extern int front_screen_max_fps(void);   // front_ios.m: 40 and 120 fps offered only on 120 Hz screens

@interface DmSetting : NSObject
@property (copy) NSString *env, *kind, *label, *note, *def, *section, *on, *off;
@property (copy) NSArray<NSArray<NSString *> *> *options;   // choice: [value, label]
@property double min, max, step;
@property BOOL hasMin, hasMax, hasStep;
@property BOOL disabled;
@end

@implementation DmSetting
- (NSString *)sectionTitle { return self.section.length ? self.section : @"Game"; }
- (NSString *)onValue { return self.on ?: @"1"; }
- (NSString *)offValue { return self.off ?: @"0"; }
- (BOOL)isDefault:(NSString *)v {
    if ([self.kind isEqualToString:@"range"]) return fabs(v.doubleValue - self.def.doubleValue) < 1e-6;
    return [v isEqualToString:self.def];
}
- (NSArray<NSArray<NSString *> *> *)offeredOptionsForCurrent:(NSString *)cur {
    if (!self.options) return @[];
    if (![self.env isEqualToString:@"DM_FPS_TARGET"] || front_screen_max_fps() >= 120) return self.options;
    NSMutableArray *out = [NSMutableArray array];
    for (NSArray<NSString *> *o in self.options) {
        if (!([o[0] isEqualToString:@"40"] || [o[0] isEqualToString:@"120"]) || [o[0] isEqualToString:cur]) [out addObject:o];
    }
    return out;
}
- (BOOL)accepts:(NSString *)v {
    if ([self.kind isEqualToString:@"choice"]) {
        for (NSArray<NSString *> *o in self.options) if ([o[0] isEqualToString:v]) return YES;
        return NO;
    }
    if ([self.kind isEqualToString:@"range"]) {
        NSScanner *sc = [NSScanner scannerWithString:v]; double d;
        return [sc scanDouble:&d] && sc.isAtEnd && isfinite(d) && self.hasMin && self.hasMax && d >= self.min && d <= self.max;
    }
    if ([self.kind isEqualToString:@"toggle"]) return [v isEqualToString:self.onValue] || [v isEqualToString:self.offValue];
    return YES;
}
@end

static NSString *json_str(NSDictionary *d, NSString *k) { id v = d[k]; return [v isKindOfClass:NSString.class] ? v : nil; }

static NSArray<DmSetting *> *load_settings(NSData *data) {
    id root = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![root isKindOfClass:NSDictionary.class] || ![root[@"settings"] isKindOfClass:NSArray.class]) return nil;
    NSMutableArray *out = [NSMutableArray array];
    for (NSDictionary *d in root[@"settings"]) {
        if (![d isKindOfClass:NSDictionary.class]) return nil;
        DmSetting *s = [DmSetting new];
        s.env = json_str(d, @"env"); s.kind = json_str(d, @"kind"); s.label = json_str(d, @"label");
        s.note = json_str(d, @"note"); s.def = json_str(d, @"default");
        s.section = json_str(d, @"section"); s.on = json_str(d, @"on"); s.off = json_str(d, @"off");
        if (!s.env || !s.kind || !s.label || !s.note || !s.def) return nil;
        if ([d[@"options"] isKindOfClass:NSArray.class]) {
            NSMutableArray *opts = [NSMutableArray array];
            for (id o in d[@"options"]) {
                if (![o isKindOfClass:NSArray.class] || [o count] < 2 || ![o[0] isKindOfClass:NSString.class] || ![o[1] isKindOfClass:NSString.class]) return nil;
                [opts addObject:@[o[0], o[1]]];
            }
            s.options = opts;
        }
        if ([d[@"min"] isKindOfClass:NSNumber.class]) { s.min = [d[@"min"] doubleValue]; s.hasMin = YES; }
        if ([d[@"max"] isKindOfClass:NSNumber.class]) { s.max = [d[@"max"] doubleValue]; s.hasMax = YES; }
        if ([d[@"step"] isKindOfClass:NSNumber.class]) { s.step = [d[@"step"] doubleValue]; s.hasStep = YES; }
        s.disabled = [d[@"disabled"] isKindOfClass:NSNumber.class] && [d[@"disabled"] boolValue];
        [out addObject:s];
    }
    return out;
}

static int increased_memory_limit(void) {
    static int v = -1;
    if (v >= 0) return v;
    NSData *d = [NSData dataWithContentsOfFile:NSBundle.mainBundle.executablePath options:NSDataReadingMappedIfSafe error:nil];
    NSData *key = [@"com.apple.developer.kernel.increased-memory-limit</key>" dataUsingEncoding:NSUTF8StringEncoding];
    v = 0;
    if (d.length) {
        NSRange r = [d rangeOfData:key options:NSDataSearchBackwards range:NSMakeRange(0, d.length)];
        if (r.location != NSNotFound) {
            NSUInteger at = NSMaxRange(r), n = MIN((NSUInteger)64, d.length - at);
            NSString *after = [[NSString alloc] initWithData:[d subdataWithRange:NSMakeRange(at, n)] encoding:NSASCIIStringEncoding];
            v = [[after stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet] hasPrefix:@"<true/>"];
        }
    }
    fprintf(stderr, "launcher: Increased Memory Limit %s (the app's signature)\n", v ? "on" : "off");
    return v;
}

// ---- LauncherModel ----
@interface DmLauncherModel : NSObject
@property (strong) NSArray<DmSetting *> *settings;
@property (strong) NSArray<NSString *> *sections;
@property (strong) NSSet<NSString *> *fromFile;
@property (strong) NSMutableDictionary<NSString *, NSString *> *values;
@end

static DmLauncherModel *g_menu_model = nil;

@implementation DmLauncherModel
- (instancetype)initWith:(NSArray<DmSetting *> *)settings {
    if (!(self = [super init])) return nil;
    _settings = settings;
    _values = [NSMutableDictionary dictionary];
    NSMutableArray *secs = [NSMutableArray array];
    for (DmSetting *s in settings) if (![secs containsObject:s.sectionTitle]) [secs addObject:s.sectionTitle];
    _sections = secs;
    NSString *keys = NSProcessInfo.processInfo.environment[@"DM_ENV_FILE_KEYS"] ?: @"";
    NSMutableSet *ff = [NSMutableSet set];
    for (NSString *k in [keys componentsSeparatedByString:@" "]) if (k.length) [ff addObject:k];
    _fromFile = ff;
    NSUserDefaults *ud = NSUserDefaults.standardUserDefaults;
    for (DmSetting *s in settings) {
        NSString *envv = NSProcessInfo.processInfo.environment[s.env];
        if ([ff containsObject:s.env] && envv) { _values[s.env] = envv; continue; }
        NSString *saved = [ud stringForKey:[@"launcher." stringByAppendingString:s.env]];
        _values[s.env] = (saved && [s accepts:saved]) ? saved : s.def;
    }
    return self;
}
- (NSArray<DmSetting *> *)inSection:(NSString *)sec {
    return [self.settings filteredArrayUsingPredicate:[NSPredicate predicateWithBlock:^BOOL(DmSetting *s, NSDictionary *b) { return [s.sectionTitle isEqualToString:sec]; }]];
}
- (NSString *)value:(DmSetting *)s { return self.values[s.env] ?: s.def; }
- (BOOL)locked:(DmSetting *)s { return s.disabled || [self.fromFile containsObject:s.env]; }
- (NSString *)note:(DmSetting *)s {
    if (s.disabled || ![self.fromFile containsObject:s.env]) return s.note;
    return [NSString stringWithFormat:@"%@Set by an env file", s.note.length ? [s.note stringByAppendingString:@" · "] : @""];
}
- (double)rangeValue:(DmSetting *)s {
    NSString *v = [self value:s]; NSScanner *sc = [NSScanner scannerWithString:v]; double d;
    return ([sc scanDouble:&d] && sc.isAtEnd) ? d : (s.hasMax ? s.max : 1);
}
- (NSString *)percentage:(DmSetting *)s { return [NSString stringWithFormat:@"%ld %%", lround([self rangeValue:s] * 100)]; }
- (NSString *)choiceLabel:(DmSetting *)s {
    NSString *cur = [self value:s];
    for (NSArray *o in [s offeredOptionsForCurrent:cur]) if ([o[0] isEqualToString:cur]) return o[1];
    return cur;
}
- (BOOL)changed {
    for (DmSetting *s in self.settings) if (!s.disabled && ![self.fromFile containsObject:s.env] && ![s isDefault:[self value:s]]) return YES;
    return NO;
}
- (void)choose:(DmSetting *)s value:(NSString *)v {
    if ([self locked:s]) return;
    self.values[s.env] = v;
    NSString *key = [@"launcher." stringByAppendingString:s.env];
    if ([s isDefault:v]) [NSUserDefaults.standardUserDefaults removeObjectForKey:key];
    else [NSUserDefaults.standardUserDefaults setObject:v forKey:key];
    setenv(s.env.UTF8String, v.UTF8String, 1);
}
- (void)reset {
    NSUserDefaults *ud = NSUserDefaults.standardUserDefaults;
    for (DmSetting *s in self.settings) {
        [ud removeObjectForKey:[@"launcher." stringByAppendingString:s.env]];
        if (![self.fromFile containsObject:s.env]) self.values[s.env] = s.def;
    }
    if (self.values[@"DM_ROTATION"]) dm_rotation_set(self.values[@"DM_ROTATION"].UTF8String);
    fprintf(stderr, "launcher: settings reset to the game's defaults\n");
}
- (void)apply {
    NSUserDefaults *ud = NSUserDefaults.standardUserDefaults;
    for (DmSetting *s in self.settings) {
        NSString *v = [self value:s], *key = [@"launcher." stringByAppendingString:s.env];
        if (s.disabled) continue;
        if ([self.fromFile containsObject:s.env]) { fprintf(stderr, "launcher: %s kept at %s (env file)\n", s.env.UTF8String, v.UTF8String); continue; }
        BOOL def = [s isDefault:v];
        if (def) [ud removeObjectForKey:key]; else [ud setObject:v forKey:key];
        setenv(s.env.UTF8String, v.UTF8String, 1);
        fprintf(stderr, "launcher: %s=%s%s\n", s.env.UTF8String, v.UTF8String, def ? "" : " (changed)");
    }
}
- (void)applySaved {
    for (DmSetting *s in self.settings) {
        if (s.disabled || [self.fromFile containsObject:s.env]) continue;
        NSString *v = [self value:s];
        setenv(s.env.UTF8String, v.UTF8String, 1);
    }
    if (self.values[@"DM_ROTATION"]) dm_rotation_set(self.values[@"DM_ROTATION"].UTF8String);
}
@end

// ---- In-game menu (DmGameMenu.h, replaces GameMenu.swift) ----
// Front ends supply DM_GAME_MENU_TEXT in this order: 0 moreSettings, 1 controllerLayout, 2 tap, 3 fixedSetting.
static NSString *game_menu_text(NSInteger idx, NSString *english) {
    const char *raw = getenv("DM_GAME_MENU_TEXT");
    if (!raw || !*raw) return english;
    NSString *payload = [[NSString stringWithUTF8String:raw] stringByReplacingOccurrencesOfString:@"\\n" withString:@"\n"];
    NSArray<NSString *> *lines = [payload componentsSeparatedByString:@"\n"];
    if (idx < 0 || idx >= (NSInteger)lines.count) return english;
    NSString *text = [lines[idx] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    return text.length ? text : english;
}

static UIMenuElement *game_menu_row(DmLauncherModel *m, DmSetting *s) {
    NSString *now = [m value:s];
    BOOL fixed = [m.fromFile containsObject:s.env];
    UIMenuElementAttributes attrs = fixed ? UIMenuElementAttributesDisabled : 0;
    NSString *fixedText = game_menu_text(3, @"Set in env.txt");
    if ([s.kind isEqualToString:@"toggle"]) {
        BOOL isOn = [now isEqualToString:s.onValue];
        UIAction *a = [UIAction actionWithTitle:s.label image:nil identifier:nil handler:^(UIAction *act) {
            NSString *next = isOn ? s.offValue : s.onValue;
            [m choose:s value:next];
            dm_setting_live(s.env.UTF8String, next.UTF8String);
        }];
        a.attributes = attrs;
        a.state = isOn ? UIMenuElementStateOn : UIMenuElementStateOff;
        if (fixed) a.subtitle = fixedText;
        return a;
    }
    NSArray<NSArray<NSString *> *> *opts = [s offeredOptionsForCurrent:now];
    NSString *curLabel = now;
    for (NSArray<NSString *> *o in opts) if ([o[0] isEqualToString:now]) { curLabel = o[1]; break; }
    NSMutableArray<UIAction *> *actions = [NSMutableArray array];
    for (NSArray<NSString *> *o in opts) {
        UIAction *a = [UIAction actionWithTitle:o[1] image:nil identifier:nil handler:^(UIAction *act) {
            [m choose:s value:o[0]];
            dm_setting_live(s.env.UTF8String, o[0].UTF8String);
        }];
        a.attributes = attrs;
        a.state = [o[0] isEqualToString:now] ? UIMenuElementStateOn : UIMenuElementStateOff;
        [actions addObject:a];
    }
    UIMenu *sub = [UIMenu menuWithTitle:s.label image:nil identifier:nil options:UIMenuOptionsSingleSelection children:actions];
    if (fixed) sub.subtitle = [NSString stringWithFormat:@"%@ · %@", curLabel, fixedText];
    return sub;
}

static NSArray<UIMenuElement *> *game_menu_items(void) {
    NSArray<NSString *> *names = @[@"2D", @"3D", game_menu_text(2, @"Tap")];
    NSMutableArray<UIAction *> *layouts = [NSMutableArray array];
    int curLayout = dm_pad_layout();
    for (int l = 0; l < 3; l++) {
        if (!dm_pad_layout_offered(l)) continue;
        UIAction *a = [UIAction actionWithTitle:names[l] image:nil identifier:nil handler:^(UIAction *act) { dm_pad_layout_set(l); }];
        a.state = (curLayout == l) ? UIMenuElementStateOn : UIMenuElementStateOff;
        [layouts addObject:a];
    }
    UIMenu *layoutMenu = [UIMenu menuWithTitle:game_menu_text(1, @"Controller layout") image:nil identifier:nil options:UIMenuOptionsSingleSelection children:layouts];
    NSMutableArray<UIMenuElement *> *items = [NSMutableArray array];
    BOOL placed = NO;
    DmLauncherModel *m = g_menu_model;
    if (m) {
        for (NSString *sec in m.sections) {
            NSMutableArray<UIMenuElement *> *rows = [NSMutableArray array];
            if ([sec isEqualToString:@"Controls"]) { [rows addObject:layoutMenu]; placed = YES; }
            for (DmSetting *s in [m inSection:sec]) {
                if (s.disabled) continue;
                if (([s.kind isEqualToString:@"choice"] || [s.kind isEqualToString:@"toggle"]) && dm_setting_live(s.env.UTF8String, NULL)) {
                    [rows addObject:game_menu_row(m, s)];
                }
            }
            if (rows.count) [items addObject:[UIMenu menuWithTitle:@"" image:nil identifier:nil options:UIMenuOptionsDisplayInline children:rows]];
        }
    }
    if (!placed) [items insertObject:[UIMenu menuWithTitle:@"" image:nil identifier:nil options:UIMenuOptionsDisplayInline children:@[layoutMenu]] atIndex:0];
    return items;
}

void *dm_game_menu(void) {
    NSString *title = g_menu_model ? game_menu_text(0, @"More settings on the start screen") : @"";
    UIDeferredMenuElement *deferred = [UIDeferredMenuElement elementWithUncachedProvider:^(void (^completion)(NSArray<UIMenuElement *> *)) {
        completion(game_menu_items());
    }];
    UIMenu *menu = [UIMenu menuWithTitle:title children:@[deferred]];
    return (__bridge_retained void *)menu;
}

// ---- UI helpers ----
static UILabel *body_label(NSString *text) {
    UILabel *l = [UILabel new]; l.text = text; l.font = [UIFont preferredFontForTextStyle:UIFontTextStyleBody];
    l.textColor = UIColor.labelColor; l.numberOfLines = 0; l.translatesAutoresizingMaskIntoConstraints = NO;
    return l;
}

static UILabel *value_label(NSString *text) {
    UILabel *l = [UILabel new]; l.text = text; l.textColor = UIColor.secondaryLabelColor; l.textAlignment = NSTextAlignmentRight;
    l.font = [UIFont monospacedDigitSystemFontOfSize:[UIFont preferredFontForTextStyle:UIFontTextStyleBody].pointSize weight:UIFontWeightRegular];
    l.translatesAutoresizingMaskIntoConstraints = NO;
    [l setContentHuggingPriority:UILayoutPriorityRequired forAxis:UILayoutConstraintAxisHorizontal];
    [l setContentCompressionResistancePriority:UILayoutPriorityRequired forAxis:UILayoutConstraintAxisHorizontal];
    return l;
}

static UIStackView *label_stack(NSString *title, NSString *note, BOOL locked) {
    UIStackView *st = [UIStackView new]; st.axis = UILayoutConstraintAxisVertical; st.spacing = 4; st.translatesAutoresizingMaskIntoConstraints = NO;
    UILabel *l = body_label(title); if (locked) l.textColor = UIColor.tertiaryLabelColor; [st addArrangedSubview:l];
    if (note.length) {
        UILabel *n = body_label(note); n.font = [UIFont preferredFontForTextStyle:UIFontTextStyleFootnote];
        n.textColor = UIColor.secondaryLabelColor; [st addArrangedSubview:n];
    }
    return st;
}

// ---- Settings table view controller ----
@interface DmSettingsVC : UITableViewController <UIDocumentPickerDelegate>
@property (strong) DmLauncherModel *m;
@property (strong) NSTimer *timer;
@property (strong) UILabel *memory;
@property (copy) NSString *cloudFolder;
@property BOOL cloudStale;
@end

@implementation DmSettingsVC
- (void)refreshCloud {
    BOOL stale = NO;
    self.cloudFolder = dm_cloud_saves_path(&stale) ?: @"";
    self.cloudStale = stale;
}
- (void)viewDidLoad {
    [super viewDidLoad];
    [self refreshCloud];
    self.tableView.backgroundColor = UIColor.clearColor;
    self.tableView.rowHeight = UITableViewAutomaticDimension; self.tableView.estimatedRowHeight = 56;
    __weak DmSettingsVC *w = self;
    self.timer = [NSTimer scheduledTimerWithTimeInterval:2 repeats:YES block:^(NSTimer *t) {
        w.memory.text = [NSByteCountFormatter stringFromByteCount:(long long)os_proc_available_memory() countStyle:NSByteCountFormatterCountStyleMemory];
    }];
}
- (void)dealloc { [self.timer invalidate]; }
- (NSInteger)cloudSection { return self.m.sections.count; }
- (NSInteger)resetSection { return self.m.sections.count + 1; }
- (NSInteger)memSection { return self.m.sections.count + 2; }
- (NSInteger)numberOfSectionsInTableView:(UITableView *)tv { return self.m.sections.count + 3; }
- (NSInteger)tableView:(UITableView *)tv numberOfRowsInSection:(NSInteger)sec {
    if (sec < (NSInteger)self.m.sections.count) return (NSInteger)[self.m inSection:self.m.sections[sec]].count;
    if (sec == self.cloudSection) return self.cloudFolder.length ? 2 : 1;
    if (sec == self.resetSection) return 1;
    return 2;
}
- (NSString *)tableView:(UITableView *)tv titleForHeaderInSection:(NSInteger)sec {
    if (sec < (NSInteger)self.m.sections.count) return self.m.sections[sec];
    if (sec == self.cloudSection) return @"Cloud saves";
    return nil;
}
- (NSString *)tableView:(UITableView *)tv titleForFooterInSection:(NSInteger)sec {
    if (sec == self.cloudSection) {
        if (self.cloudStale) return @"The saved folder bookmark expired (e.g. after re-signing). Pick your iCloud Drive folder again to resume syncing.";
        return @"Pick a folder in iCloud Drive to sync saves across devices.";
    }
    if (sec == self.resetSection) return @"Brings back all settings this game was tested with.";
    if (sec == self.memSection && !increased_memory_limit())
        return @"Increased Memory Limit is off: iOS gives this app less memory, and a big game can close when an area loads.";
    return nil;
}

- (UITableViewCell *)tableView:(UITableView *)tv cellForRowAtIndexPath:(NSIndexPath *)ip {
    UITableViewCell *c = [[UITableViewCell alloc] initWithStyle:UITableViewCellStyleDefault reuseIdentifier:nil];
    c.selectionStyle = UITableViewCellSelectionStyleNone;
    UIView *v = c.contentView; UILayoutGuide *g = v.layoutMarginsGuide;

    if (ip.section == self.cloudSection) {
        c.selectionStyle = UITableViewCellSelectionStyleDefault;
        if (ip.row == 0) {
            UIListContentConfiguration *cfg = [UIListContentConfiguration valueCellConfiguration];
            cfg.text = self.cloudFolder.length ? @"Folder" : @"Choose iCloud folder…";
            cfg.secondaryText = self.cloudFolder.length ? self.cloudFolder.lastPathComponent : (self.cloudStale ? @"Pick again" : @"Off");
            if (!self.cloudFolder.length) cfg.textProperties.color = self.view.tintColor;
            c.contentConfiguration = cfg;
        } else {
            UIListContentConfiguration *cfg = [UIListContentConfiguration cellConfiguration];
            cfg.text = @"Turn off cloud saves";
            cfg.textProperties.color = UIColor.systemRedColor;
            c.contentConfiguration = cfg;
        }
        return c;
    }
    if (ip.section == self.resetSection) {
        UIButtonConfiguration *cfg = [UIButtonConfiguration plainButtonConfiguration];
        cfg.title = @"Reset to tested defaults"; cfg.contentInsets = NSDirectionalEdgeInsetsZero;
        __weak DmSettingsVC *w = self;
        UIButton *b = [UIButton buttonWithConfiguration:cfg primaryAction:[UIAction actionWithHandler:^(UIAction *a) { [w.m reset]; [w.tableView reloadData]; }]];
        b.enabled = self.m.changed; b.contentHorizontalAlignment = UIControlContentHorizontalAlignmentLeading;
        b.translatesAutoresizingMaskIntoConstraints = NO; [v addSubview:b];
        [NSLayoutConstraint activateConstraints:@[
            [b.leadingAnchor constraintEqualToAnchor:g.leadingAnchor], [b.trailingAnchor constraintEqualToAnchor:g.trailingAnchor],
            [b.centerYAnchor constraintEqualToAnchor:v.centerYAnchor], [v.heightAnchor constraintGreaterThanOrEqualToConstant:48]]];
        return c;
    }
    if (ip.section == self.memSection && ip.row == 1) {
        UILabel *l = body_label(@"Increased Memory Limit");
        int on = increased_memory_limit();
        UILabel *st = value_label(on ? @"● On" : @"● Off"); st.textColor = on ? UIColor.systemGreenColor : UIColor.systemOrangeColor;
        [v addSubview:l]; [v addSubview:st];
        [NSLayoutConstraint activateConstraints:@[
            [l.leadingAnchor constraintEqualToAnchor:g.leadingAnchor], [l.centerYAnchor constraintEqualToAnchor:v.centerYAnchor],
            [st.trailingAnchor constraintEqualToAnchor:g.trailingAnchor], [st.centerYAnchor constraintEqualToAnchor:v.centerYAnchor],
            [v.heightAnchor constraintGreaterThanOrEqualToConstant:48]]];
        return c;
    }
    if (ip.section == self.memSection) {
        UILabel *l = body_label(@"Memory available");
        self.memory = value_label([NSByteCountFormatter stringFromByteCount:(long long)os_proc_available_memory() countStyle:NSByteCountFormatterCountStyleMemory]);
        [v addSubview:l]; [v addSubview:self.memory];
        [NSLayoutConstraint activateConstraints:@[
            [l.leadingAnchor constraintEqualToAnchor:g.leadingAnchor], [l.centerYAnchor constraintEqualToAnchor:v.centerYAnchor],
            [self.memory.trailingAnchor constraintEqualToAnchor:g.trailingAnchor], [self.memory.centerYAnchor constraintEqualToAnchor:v.centerYAnchor],
            [v.heightAnchor constraintGreaterThanOrEqualToConstant:48]]];
        return c;
    }

    DmSetting *s = [self.m inSection:self.m.sections[ip.section]][ip.row];
    BOOL locked = [self.m locked:s];
    NSInteger tag = [self.m.settings indexOfObject:s];
    UIStackView *lab = label_stack(s.label, [self.m note:s], locked);
    NSMutableArray *cs = [NSMutableArray array];
    if ([s.kind isEqualToString:@"range"]) {
        UILabel *pct = value_label([self.m percentage:s]); pct.tag = 1000 + tag;
        UISlider *sl = [UISlider new]; sl.translatesAutoresizingMaskIntoConstraints = NO;
        sl.minimumValue = s.hasMin ? s.min : 0; sl.maximumValue = s.hasMax ? s.max : 1; sl.value = [self.m rangeValue:s];
        sl.enabled = !locked; sl.accessibilityLabel = s.label; sl.accessibilityValue = pct.text; sl.tag = tag;
        [sl addTarget:self action:@selector(slid:) forControlEvents:UIControlEventValueChanged];
        [sl addTarget:self action:@selector(slidEnd:) forControlEvents:UIControlEventTouchUpInside | UIControlEventTouchUpOutside];
        [v addSubview:lab]; [v addSubview:pct]; [v addSubview:sl];
        [cs addObjectsFromArray:@[
            [lab.leadingAnchor constraintEqualToAnchor:g.leadingAnchor], [lab.topAnchor constraintEqualToAnchor:g.topAnchor],
            [pct.leadingAnchor constraintGreaterThanOrEqualToAnchor:lab.trailingAnchor constant:12],
            [pct.trailingAnchor constraintEqualToAnchor:g.trailingAnchor], [pct.firstBaselineAnchor constraintEqualToAnchor:lab.firstBaselineAnchor],
            [sl.topAnchor constraintEqualToAnchor:lab.bottomAnchor constant:8], [sl.leadingAnchor constraintEqualToAnchor:g.leadingAnchor],
            [sl.trailingAnchor constraintEqualToAnchor:g.trailingAnchor], [sl.bottomAnchor constraintEqualToAnchor:g.bottomAnchor]]];
    } else {
        UIView *ctl;
        if ([s.kind isEqualToString:@"choice"]) {
            UIButtonConfiguration *cfg = [UIButtonConfiguration plainButtonConfiguration];
            cfg.title = [self.m choiceLabel:s]; cfg.baseForegroundColor = UIColor.secondaryLabelColor;
            cfg.image = [UIImage systemImageNamed:@"chevron.up.chevron.down" withConfiguration:[UIImageSymbolConfiguration configurationWithPointSize:12 weight:UIImageSymbolWeightSemibold]];
            cfg.imagePlacement = NSDirectionalRectEdgeTrailing; cfg.imagePadding = 5; cfg.contentInsets = NSDirectionalEdgeInsetsMake(6, 6, 6, 0);
            cfg.titleTextAttributesTransformer = ^NSDictionary *(NSDictionary *a) {
                NSMutableDictionary *mm = [a mutableCopy]; mm[NSFontAttributeName] = [UIFont preferredFontForTextStyle:UIFontTextStyleBody]; return mm;
            };
            UIButton *b = [UIButton buttonWithConfiguration:cfg primaryAction:nil];
            b.showsMenuAsPrimaryAction = YES; b.changesSelectionAsPrimaryAction = NO; b.enabled = !locked;
            b.menu = [self menuFor:s button:b];
            ctl = b;
        } else {
            UISwitch *sw = [UISwitch new]; sw.on = [[self.m value:s] isEqualToString:s.onValue]; sw.enabled = !locked; sw.tag = tag;
            [sw addTarget:self action:@selector(toggled:) forControlEvents:UIControlEventValueChanged];
            ctl = sw;
        }
        ctl.translatesAutoresizingMaskIntoConstraints = NO;
        [ctl setContentCompressionResistancePriority:UILayoutPriorityRequired forAxis:UILayoutConstraintAxisHorizontal];
        [v addSubview:lab]; [v addSubview:ctl];
        [cs addObjectsFromArray:@[
            [lab.leadingAnchor constraintEqualToAnchor:g.leadingAnchor], [lab.topAnchor constraintEqualToAnchor:g.topAnchor],
            [lab.bottomAnchor constraintEqualToAnchor:g.bottomAnchor],
            [ctl.leadingAnchor constraintGreaterThanOrEqualToAnchor:lab.trailingAnchor constant:12],
            [ctl.trailingAnchor constraintEqualToAnchor:g.trailingAnchor], [ctl.centerYAnchor constraintEqualToAnchor:v.centerYAnchor]]];
    }
    [cs addObject:[v.heightAnchor constraintGreaterThanOrEqualToConstant:48]];
    [NSLayoutConstraint activateConstraints:cs];
    return c;
}

- (void)tableView:(UITableView *)tv didSelectRowAtIndexPath:(NSIndexPath *)ip {
    [tv deselectRowAtIndexPath:ip animated:YES];
    if (ip.section != self.cloudSection) return;
    if (ip.row == 0) {
        UIDocumentPickerViewController *pk = [[UIDocumentPickerViewController alloc] initForOpeningContentTypes:@[UTTypeFolder]];
        pk.delegate = self; pk.allowsMultipleSelection = NO;
        [self presentViewController:pk animated:YES completion:nil];
    } else {
        dm_clear_cloud_saves();
        [self refreshCloud];
        [self.tableView reloadSections:[NSIndexSet indexSetWithIndex:self.cloudSection] withRowAnimation:UITableViewRowAnimationNone];
    }
}

- (void)documentPicker:(UIDocumentPickerViewController *)controller didPickDocumentsAtURLs:(NSArray<NSURL *> *)urls {
    NSURL *url = urls.firstObject;
    if (url) dm_store_cloud_saves(url);
    [self refreshCloud];
    [self.tableView reloadSections:[NSIndexSet indexSetWithIndex:self.cloudSection] withRowAnimation:UITableViewRowAnimationNone];
}

- (UIMenu *)menuFor:(DmSetting *)s button:(UIButton *)b {
    NSMutableArray *items = [NSMutableArray array];
    NSString *cur = [self.m value:s];
    __weak DmSettingsVC *w = self; __weak UIButton *wb = b;
    for (NSArray *o in [s offeredOptionsForCurrent:cur]) {
        UIAction *a = [UIAction actionWithTitle:o[1] image:nil identifier:nil handler:^(UIAction *act) {
            DmSettingsVC *me = w; if (!me) return;
            me.m.values[s.env] = o[0];
            if ([s.env isEqualToString:@"DM_ROTATION"]) dm_rotation_set(((NSString *)o[0]).UTF8String);
            UIButtonConfiguration *cfg = wb.configuration; cfg.title = o[1]; wb.configuration = cfg;
            wb.menu = [me menuFor:s button:wb];
            [me refreshReset];
        }];
        a.state = [o[0] isEqualToString:cur] ? UIMenuElementStateOn : UIMenuElementStateOff;
        [items addObject:a];
    }
    return [UIMenu menuWithTitle:@"" image:nil identifier:nil options:UIMenuOptionsSingleSelection children:items];
}
- (void)refreshReset { [self.tableView reloadRowsAtIndexPaths:@[[NSIndexPath indexPathForRow:0 inSection:self.resetSection]] withRowAnimation:UITableViewRowAnimationNone]; }
- (void)slid:(UISlider *)sl {
    DmSetting *s = self.m.settings[sl.tag];
    double step = s.hasStep ? s.step : 0.05, lo = s.hasMin ? s.min : 0;
    double v = step > 0 ? lo + round((sl.value - lo) / step) * step : sl.value;
    self.m.values[s.env] = [NSString stringWithFormat:@"%.2f", v];
    UILabel *pct = [sl.superview viewWithTag:1000 + sl.tag];
    pct.text = [self.m percentage:s]; sl.accessibilityValue = pct.text;
}
- (void)slidEnd:(UISlider *)sl { [self refreshReset]; }
- (void)toggled:(UISwitch *)sw { DmSetting *s = self.m.settings[sw.tag]; self.m.values[s.env] = sw.on ? s.onValue : s.offValue; [self refreshReset]; }
@end

// ---- Main launcher view controller ----
@interface DmLauncherVC : UIViewController
@property (strong) DmLauncherModel *m;
@property (copy) void (^play)(void);
@property (strong) UIButton *playButton;
@property BOOL checkingSync;
@end

@implementation DmLauncherVC
static id bundle_info(NSString *k) { return [NSBundle.mainBundle objectForInfoDictionaryKey:k]; }
static UIImage *app_icon(void) {
    UIImage *i = [UIImage imageNamed:@"AppIcon"]; if (i) return i;
    NSDictionary *icons = bundle_info(@"CFBundleIcons");
    NSArray *files = [icons isKindOfClass:NSDictionary.class] ? icons[@"CFBundlePrimaryIcon"][@"CFBundleIconFiles"] : nil;
    for (NSString *f in [files reverseObjectEnumerator]) { UIImage *im = [f isKindOfClass:NSString.class] ? [UIImage imageNamed:f] : nil; if (im) return im; }
    return [UIImage imageNamed:@"AppIcon60x60"];
}
- (void)viewDidAppear:(BOOL)animated {
    [super viewDidAppear:animated];
    NSString *rot = self.m.values[@"DM_ROTATION"];
    if (rot) dm_rotation_set(rot.UTF8String);
}
- (void)startWithChoice:(NSString *)choice {
    if (choice.length) {
        setenv("DM_SAVE_SYNC_CHOICE", choice.UTF8String, 1);
        fprintf(stderr, "launcher: DM_SAVE_SYNC_CHOICE=%s\n", choice.UTF8String);
    }
    if (self.play) self.play();
}
- (void)checkCloudAndPlay {
    if (self.checkingSync) return;
    dm_cloud_saves_env();
    const char *save = getenv("DM_SAVE"), *syncDir = getenv("DM_SAVE_SYNC"), *title = dm_save_title();
    if (!save || !*save || !syncDir || !*syncDir || !title || !*title) { [self startWithChoice:nil]; return; }
    self.checkingSync = YES;
    self.playButton.enabled = NO;
    NSString *saveStr = @(save), *titleStr = @(title);
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        char buf[1024] = "";
        int rc = save_sync_report(saveStr.UTF8String, titleStr.UTF8String, NULL, 60000, buf, sizeof buf);
        NSString *line = rc == 0 ? @(buf) : nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            self.checkingSync = NO;
            self.playButton.enabled = YES;
            if (!line) { [self startWithChoice:nil]; return; }
            NSMutableDictionary<NSString *, NSString *> *f = [NSMutableDictionary dictionary];
            for (NSString *pair in [line componentsSeparatedByCharactersInSet:NSCharacterSet.whitespaceCharacterSet]) {
                NSRange eq = [pair rangeOfString:@"="];
                if (eq.location != NSNotFound) f[[pair substringToIndex:eq.location]] = [pair substringFromIndex:eq.location + 1];
            }
            if (![f[@"sync"] isEqualToString:@"1"]) { [self startWithChoice:nil]; return; }
            int cloud = [f[@"cloud"] intValue];
            BOOL conflict = [f[@"conflict"] isEqualToString:@"1"];
            NSString *chash = ([f[@"chash"] isEqualToString:@"-"] || !f[@"chash"].length) ? @"" : f[@"chash"];
            NSString *here = UIDevice.currentDevice.userInterfaceIdiom == UIUserInterfaceIdiomPad ? @"iPad" : @"iPhone";
            if (cloud < 0) {
                UIAlertController *ac = [UIAlertController alertControllerWithTitle:@"The cloud save didn't come"
                    message:[NSString stringWithFormat:@"iCloud didn't deliver this game's save in time. Play this %@'s save without syncing?", here]
                    preferredStyle:UIAlertControllerStyleAlert];
                [ac addAction:[UIAlertAction actionWithTitle:[NSString stringWithFormat:@"Play This %@'s Save", here] style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) { [self startWithChoice:@"offline"]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Try Again" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) { [self checkCloudAndPlay]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
                [self presentViewController:ac animated:YES completion:nil];
                return;
            }
            if (conflict && cloud == 0) {
                UIAlertController *ac = [UIAlertController alertControllerWithTitle:@"No cloud save found"
                    message:[NSString stringWithFormat:@"This %@ synced a save here before, but iCloud shows none now. Sending replaces any save iCloud has not shown yet.", here]
                    preferredStyle:UIAlertControllerStyleAlert];
                [ac addAction:[UIAlertAction actionWithTitle:[NSString stringWithFormat:@"Send This %@'s Save", here] style:UIAlertActionStyleDestructive handler:^(UIAlertAction *a) { [self startWithChoice:@"local:none"]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Play Without Sending" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) { [self startWithChoice:@"offline"]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Try Again" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) { [self checkCloudAndPlay]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
                [self presentViewController:ac animated:YES completion:nil];
                return;
            }
            if (conflict) {
                UIAlertController *ac = [UIAlertController alertControllerWithTitle:@"Which save do you want to play?"
                    message:[NSString stringWithFormat:@"Both this %@'s save and the one in iCloud changed since they were last the same.", here]
                    preferredStyle:UIAlertControllerStyleAlert];
                [ac addAction:[UIAlertAction actionWithTitle:[NSString stringWithFormat:@"Use This %@'s Save", here] style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) { [self startWithChoice:[@"local:" stringByAppendingString:chash.length ? chash : @"none"]]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Use iCloud Save" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) { [self startWithChoice:[@"cloud:" stringByAppendingString:chash]]; }]];
                [ac addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:nil]];
                [self presentViewController:ac animated:YES completion:nil];
                return;
            }
            [self startWithChoice:@"ask"];
        });
    });
}
- (void)viewDidLoad {
    [super viewDidLoad];
    self.overrideUserInterfaceStyle = UIUserInterfaceStyleDark;
    self.view.backgroundColor = UIColor.systemGroupedBackgroundColor;
    UIStackView *card = [UIStackView new]; card.axis = UILayoutConstraintAxisVertical; card.alignment = UIStackViewAlignmentCenter; card.spacing = 16;
    UIImage *icon = app_icon();
    if (icon) {
        UIImageView *iv = [[UIImageView alloc] initWithImage:icon]; iv.layer.cornerRadius = 22; iv.layer.cornerCurve = kCACornerCurveContinuous; iv.clipsToBounds = YES;
        iv.isAccessibilityElement = NO; [iv.widthAnchor constraintEqualToConstant:96].active = YES; [iv.heightAnchor constraintEqualToConstant:96].active = YES;
        [card addArrangedSubview:iv];
    }
    UILabel *name = [UILabel new]; name.text = [bundle_info(@"CFBundleDisplayName") isKindOfClass:NSString.class] ? bundle_info(@"CFBundleDisplayName") : @"Game";
    UIFontDescriptor *fd = [[UIFont preferredFontForTextStyle:UIFontTextStyleTitle1].fontDescriptor fontDescriptorWithSymbolicTraits:UIFontDescriptorTraitBold];
    name.font = [UIFont fontWithDescriptor:fd size:0]; name.numberOfLines = 0; name.textAlignment = NSTextAlignmentCenter; [card addArrangedSubview:name];
    UILabel *ver = [UILabel new]; ver.text = [NSString stringWithFormat:@"Version %@", bundle_info(@"CFBundleShortVersionString") ?: @""];
    ver.font = [UIFont preferredFontForTextStyle:UIFontTextStyleCallout]; ver.textColor = UIColor.secondaryLabelColor; [card addArrangedSubview:ver];
    UIButtonConfiguration *cfg = nil;
    SEL glass = NSSelectorFromString(@"prominentGlassButtonConfiguration");
    if ([UIButtonConfiguration respondsToSelector:glass]) cfg = ((UIButtonConfiguration *(*)(id, SEL))[UIButtonConfiguration methodForSelector:glass])(UIButtonConfiguration.class, glass);
    if (!cfg) cfg = [UIButtonConfiguration filledButtonConfiguration];
    cfg.buttonSize = UIButtonConfigurationSizeLarge; cfg.cornerStyle = UIButtonConfigurationCornerStyleCapsule;
    UIFontDescriptor *pd = [[UIFont preferredFontForTextStyle:UIFontTextStyleTitle3].fontDescriptor fontDescriptorWithSymbolicTraits:UIFontDescriptorTraitBold];
    cfg.attributedTitle = [[NSAttributedString alloc] initWithString:@"Play" attributes:@{NSFontAttributeName: [UIFont fontWithDescriptor:pd size:0]}];
    __weak DmLauncherVC *w = self;
    UIButton *b = [UIButton buttonWithConfiguration:cfg primaryAction:[UIAction actionWithHandler:^(UIAction *a) { [w checkCloudAndPlay]; }]];
    self.playButton = b;
    [b.widthAnchor constraintEqualToConstant:220].active = YES; [card addArrangedSubview:b];
    card.translatesAutoresizingMaskIntoConstraints = NO; [self.view addSubview:card];

    DmSettingsVC *list = [[DmSettingsVC alloc] initWithStyle:UITableViewStyleInsetGrouped]; list.m = self.m;
    [self addChildViewController:list]; list.view.translatesAutoresizingMaskIntoConstraints = NO; [self.view addSubview:list.view]; [list didMoveToParentViewController:self];
    UIView *v = self.view; UILayoutGuide *safe = v.safeAreaLayoutGuide;
    UILayoutGuide *left = [UILayoutGuide new]; [v addLayoutGuide:left];
    [NSLayoutConstraint activateConstraints:@[
        [list.view.topAnchor constraintEqualToAnchor:v.topAnchor], [list.view.bottomAnchor constraintEqualToAnchor:v.bottomAnchor],
        [list.view.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor], [list.view.widthAnchor constraintEqualToAnchor:safe.widthAnchor multiplier:2.0 / 3.0],
        [left.leadingAnchor constraintEqualToAnchor:safe.leadingAnchor], [left.trailingAnchor constraintEqualToAnchor:list.view.leadingAnchor],
        [card.centerXAnchor constraintEqualToAnchor:left.centerXAnchor], [card.centerYAnchor constraintEqualToAnchor:v.centerYAnchor],
        [card.widthAnchor constraintLessThanOrEqualToAnchor:left.widthAnchor constant:-32]]];
}
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return dm_rotation_vc_mask(); }
- (BOOL)prefersStatusBarHidden { return YES; }
- (BOOL)prefersHomeIndicatorAutoHidden { return YES; }
@end

static void make_capture_folders(void) {
    static const char *const keys[] = {"DM_GLSLC_CAPTURE", "DM_VK_CAPTURE", "DM_GL_CAPTURE"};
    for (int i = 0; i < 3; i++) {
        const char *k = keys[i], *d = getenv(k);
        if (d && *d) {
            [NSFileManager.defaultManager createDirectoryAtPath:@(d) withIntermediateDirectories:YES attributes:nil error:nil];
            fprintf(stderr, "capture: %s -> Documents/%s\n", k, d);
        }
    }
}

bool dm_show_launcher(void *view_controller, void (*done)(void)) {
    make_capture_folders();
    const char *appDm = getenv("DM_APP_DM");
    NSString *dir = (appDm && *appDm) ? @(appDm) : [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"dm"];
    NSString *path = [dir stringByAppendingPathComponent:@"launcher.json"];
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data) return false;
    NSArray<DmSetting *> *settings = load_settings(data);
    if (!settings.count) { fprintf(stderr, "[RED] launcher: dm/launcher.json cannot be read; starting the game\n"); return false; }
    DmLauncherModel *model = [[DmLauncherModel alloc] initWith:settings];
    g_menu_model = model;
    const char *off = getenv("DM_LAUNCHER");
    if (off && !strcmp(off, "0")) { fprintf(stderr, "launcher: off (DM_LAUNCHER=0)\n"); [model applySaved]; return false; }
    UIViewController *parent = (__bridge UIViewController *)view_controller;
    DmLauncherVC *vc = [DmLauncherVC new]; vc.m = model; vc.modalPresentationStyle = UIModalPresentationFullScreen;
    __weak DmLauncherVC *wvc = vc; __block BOOL started = NO;
    vc.play = ^{
        if (started || !wvc) return;
        started = YES;
        [model apply];
        [wvc dismissViewControllerAnimated:NO completion:^{ done(); }];
    };
    [parent presentViewController:vc animated:NO completion:nil];
    fprintf(stderr, "launcher: %lu settings shown\n", (unsigned long)settings.count);
    return true;
}
