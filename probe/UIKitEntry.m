#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int (*MainFunction)(int, char **);
typedef int (*UIKitRunApp)(int, char **, MainFunction, void *);
extern void PrepareMetalCompatibility(id<MTLDevice> device);
extern void InstallTouchControls(void);

// dyld image 0 is only the main executable when nothing was inserted ahead of it;
// Xcode debug launches prepend helper dylibs (e.g. libLogRedirect), so search by
// filetype instead of assuming an index.
static const struct mach_header *main_executable_header(void) {
    uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; i++) {
        const struct mach_header *header = _dyld_get_image_header(i);
        if (header->filetype == MH_EXECUTE) return header;
    }
    fprintf(stderr, "FACTORIO_PROBE: no MH_EXECUTE image found\n");
    abort();
}

static void RemoveModZips(NSString *mods,NSString *name) {
    NSString *prefix=[name stringByAppendingString:@"_"];
    NSCharacterSet *notVersion=[[NSCharacterSet characterSetWithCharactersInString:@"0123456789."] invertedSet];
    for (NSString *existing in [[NSFileManager defaultManager] contentsOfDirectoryAtPath:mods error:NULL]) {
        if (![existing hasPrefix:prefix] || ![existing hasSuffix:@".zip"]) continue;
        // Only "<name>_<version>.zip": "foo_" must not match another mod called "foo_bar".
        NSString *version=[[existing substringFromIndex:prefix.length] stringByDeletingPathExtension];
        if ([version rangeOfCharacterFromSet:notVersion].location!=NSNotFound) continue;
        [[NSFileManager defaultManager] removeItemAtPath:[mods stringByAppendingPathComponent:existing] error:NULL];
    }
}

static NSArray *ManifestMods(NSData *manifest) {
    id parsed=manifest?[NSJSONSerialization JSONObjectWithData:manifest options:0 error:NULL]:nil;
    NSArray *list=[parsed isKindOfClass:[NSDictionary class]]?parsed[@"mods"]:nil;
    return [list isKindOfClass:[NSArray class]]?list:nil;
}

// Applies the mods chosen at build time (tools/import_game_assets.py) once per
// change to that selection, so in-game choices stick between launches.
static BOOL SyncBundledMods(NSString *bundledMods,NSString *mods,NSString *root,NSError **error) {
    NSFileManager *files=[NSFileManager defaultManager];
    NSString *applied=[root stringByAppendingPathComponent:@"bundled-mods.json"];
    NSData *manifest=[NSData dataWithContentsOfFile:[bundledMods stringByAppendingPathComponent:@"bundled-mods.json"]];
    NSData *appliedManifest=[NSData dataWithContentsOfFile:applied];
    NSArray *bundled=ManifestMods(manifest);
    if (!bundled || [manifest isEqualToData:appliedManifest]) return YES;
    NSArray *bundledNames=[bundled valueForKey:@"name"];
    NSMutableArray *removed=[[ManifestMods(appliedManifest) valueForKey:@"name"] mutableCopy];
    [removed removeObjectsInArray:bundledNames];
    for (NSString *name in removed) RemoveModZips(mods,name);
    for (NSDictionary *mod in bundled) {
        NSString *zip=[NSString stringWithFormat:@"%@_%@.zip",mod[@"name"],mod[@"version"]];
        // A different bundled version replaces the old copy; otherwise Factorio would see two versions.
        RemoveModZips(mods,mod[@"name"]);
        if (![files copyItemAtPath:[bundledMods stringByAppendingPathComponent:zip] toPath:[mods stringByAppendingPathComponent:zip] error:error]) return NO;
    }
    NSString *bundledSettings=[bundledMods stringByAppendingPathComponent:@"mod-settings.dat"];
    NSString *settings=[mods stringByAppendingPathComponent:@"mod-settings.dat"];
    if ([files fileExistsAtPath:bundledSettings]) {
        [files removeItemAtPath:settings error:NULL];
        if (![files copyItemAtPath:bundledSettings toPath:settings error:error]) return NO;
    }
    NSString *modList=[mods stringByAppendingPathComponent:@"mod-list.json"];
    NSData *existingList=[NSData dataWithContentsOfFile:modList];
    id parsedList=existingList?[NSJSONSerialization JSONObjectWithData:existingList options:NSJSONReadingMutableContainers error:NULL]:nil;
    NSMutableDictionary *listRoot=[parsedList isKindOfClass:[NSMutableDictionary class]]?parsedList:[@{@"mods":@[]} mutableCopy];
    NSMutableArray *entries=[listRoot[@"mods"] isKindOfClass:[NSMutableArray class]]?listRoot[@"mods"]:[NSMutableArray array];
    listRoot[@"mods"]=entries;
    NSIndexSet *stale=[entries indexesOfObjectsPassingTest:^BOOL(id entry,NSUInteger idx,BOOL *stop) {
        return [entry isKindOfClass:[NSDictionary class]] && ([removed containsObject:entry[@"name"]] || [bundledNames containsObject:entry[@"name"]]);
    }];
    [entries removeObjectsAtIndexes:stale];
    for (NSString *name in bundledNames) [entries addObject:@{@"name":name,@"enabled":@YES}];
    NSData *newList=[NSJSONSerialization dataWithJSONObject:listRoot options:NSJSONWritingPrettyPrinted error:error];
    if (!newList || ![newList writeToFile:modList options:NSDataWritingAtomic error:error]) return NO;
    // Recorded last so an interrupted sync runs again on the next launch.
    return [manifest writeToFile:applied options:NSDataWritingAtomic error:error];
}

int ProbeMain(int argc, char **argv) {
    MainFunction original = (MainFunction)((char *)main_executable_header() + ORIGINAL_MAIN_OFFSET);
    bool coreTest=argc==2 && !strcmp(argv[1],"--probe-core");
    bool benchmarkTest=argc==2 && !strcmp(argv[1],"--probe-benchmark");
    if (argc==1 || coreTest || benchmarkTest) {
        @autoreleasepool {
            NSString *root=[NSHomeDirectory() stringByAppendingPathComponent:@"Documents/Factorio"];
            NSString *user=[root stringByAppendingPathComponent:@"user"];
            NSString *data=[[[NSBundle mainBundle] bundlePath] stringByAppendingPathComponent:@"data"];
            NSError *error=nil;
            if (![[NSFileManager defaultManager] createDirectoryAtPath:user withIntermediateDirectories:YES attributes:nil error:&error]) {
                fprintf(stderr,"FACTORIO_PROBE: cannot create data directory: %s\n",error.description.UTF8String); return 1;
            }
            NSString *mods=[user stringByAppendingPathComponent:@"mods"];
            if (![[NSFileManager defaultManager] createDirectoryAtPath:mods withIntermediateDirectories:YES attributes:nil error:&error]) {
                fprintf(stderr,"FACTORIO_PROBE: cannot create mods directory: %s\n",error.description.UTF8String); return 1;
            }
            NSString *bundledMods=[[[NSBundle mainBundle] bundlePath] stringByAppendingPathComponent:@"mods"];
            if (!SyncBundledMods(bundledMods,mods,root,&error)) {
                fprintf(stderr,"FACTORIO_PROBE: cannot sync bundled mods: %s\n",error.description.UTF8String); return 1;
            }
            NSString *config=[root stringByAppendingPathComponent:@"config.ini"];
            NSString *text=[NSString stringWithFormat:@"[path]\nread-data=%@\nwrite-data=%@\n[general]\ncheck-updates=false\n[graphics]\ntexture-compression-level=none\ncompress-virtual-atlas=false\ngpu-accelerated-compression=false\ngpu-accelerated-mipmap-compression=false\nvideo-memory-usage=low\nmax-sprite-loading-threads=2\n",data,user];
            if ([[NSFileManager defaultManager] fileExistsAtPath:config]) {
                NSString *existing=[NSString stringWithContentsOfFile:config encoding:NSUTF8StringEncoding error:&error];
                if (!existing) { fprintf(stderr,"FACTORIO_PROBE: cannot read config: %s\n",error.description.UTF8String); return 1; }
                NSMutableArray<NSString *> *lines=[[existing componentsSeparatedByString:@"\n"] mutableCopy];
                BOOL inPaths=NO;
                for (NSUInteger i=0;i<lines.count;i++) {
                    NSString *line=[lines[i] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
                    if ([line hasPrefix:@"["]) inPaths=[line isEqualToString:@"[path]"];
                    if (inPaths && [line hasPrefix:@"read-data="]) lines[i]=[@"read-data=" stringByAppendingString:data];
                    if (inPaths && [line hasPrefix:@"write-data="]) lines[i]=[@"write-data=" stringByAppendingString:user];
                }
                text=[lines componentsJoinedByString:@"\n"];
            }
            // Steam is unavailable (PlatformFallbacks.m), so the game's non-Steam
            // local achievement tracking is the only path; it does not reliably
            // survive relaunches here, which replays "unlocked" popups every
            // session. Suppress the popups via the game's own [other] setting
            // rather than the notification actually reflecting new progress.
            NSDictionary<NSString *,NSString *> *otherSettings=@{@"enable-crash-log-uploading":@"false",@"hide-achievement-popups":@"true"};
            NSMutableArray<NSString *> *missingSettings=[NSMutableArray array];
            for (NSString *key in otherSettings) {
                NSString *value=otherSettings[key];
                NSRegularExpression *setting=[NSRegularExpression regularExpressionWithPattern:[NSString stringWithFormat:@"(?m)^;?\\s*%@=.*$",key] options:0 error:NULL];
                if ([setting numberOfMatchesInString:text options:0 range:NSMakeRange(0,text.length)]) {
                    text=[setting stringByReplacingMatchesInString:text options:0 range:NSMakeRange(0,text.length) withTemplate:[NSString stringWithFormat:@"%@=%@",key,value]];
                } else {
                    [missingSettings addObject:[NSString stringWithFormat:@"%@=%@",key,value]];
                }
            }
            if (missingSettings.count) {
                text=[text stringByAppendingFormat:@"\n[other]\n%@\n",[missingSettings componentsJoinedByString:@"\n"]];
            }
            if (![text writeToFile:config atomically:YES encoding:NSUTF8StringEncoding error:&error]) {
                fprintf(stderr,"FACTORIO_PROBE: cannot write config: %s\n",error.description.UTF8String); return 1;
            }
            NSMutableArray<NSString *> *arguments=[@[[NSString stringWithUTF8String:argv[0]],@"--config",config,@"--nogamepad"] mutableCopy];
            if (coreTest || benchmarkTest) [arguments addObject:@"--disable-audio"];
            if (coreTest) {
                NSString *settings=[root stringByAppendingPathComponent:@"map-gen-settings.json"];
                [@"{\"width\":64,\"height\":64,\"seed\":12345}" writeToFile:settings atomically:YES encoding:NSUTF8StringEncoding error:NULL];
                [arguments addObjectsFromArray:@[@"--create",[user stringByAppendingPathComponent:@"probe-map.zip"],@"--map-gen-settings",settings]];
            } else if (benchmarkTest) {
                [arguments addObjectsFromArray:@[@"--benchmark",[user stringByAppendingPathComponent:@"probe-map.zip"],@"--benchmark-ticks",@"120"]];
            } else {
                [arguments addObjectsFromArray:@[@"--force-metal",@"--graphics-quality",@"medium"]];
            }
            argc=(int)arguments.count;
            char **owned=calloc(argc+1,sizeof(char *));
            for (int i=0;i<argc;i++) owned[i]=strdup(arguments[i].UTF8String);
            argv=owned; // Process-lifetime storage; UIKit and the game retain argv.
        }
    }
    for (int i=1;i<argc;i++) {
        if (!strcmp(argv[i],"--version") || !strcmp(argv[i],"--help") ||
            !strcmp(argv[i],"--create") || !strcmp(argv[i],"--benchmark") ||
            !strcmp(argv[i],"--generate-map-preview")) return original(argc,argv);
    }
    @autoreleasepool {
        NSString *frameworks = [[NSBundle mainBundle] bundlePath];
        NSString *sdl = [frameworks stringByAppendingPathComponent:@"Frameworks/libSDL3.dylib"];
        void *handle = dlopen(sdl.fileSystemRepresentation, RTLD_NOW|RTLD_LOCAL);
        UIKitRunApp run = handle ? (UIKitRunApp)dlsym(handle,"SDL_RunApp") : NULL;
        if (!run) { fprintf(stderr,"FACTORIO_PROBE: SDL UIKit load failed: %s\n",dlerror()); return 1; }
        id<MTLDevice> device=MTLCreateSystemDefaultDevice();
        PrepareMetalCompatibility(device);
        fprintf(stderr,"FACTORIO_PROBE: Metal device=%s mac1=%d apple7=%d apple3=%d\n",
                device.name.UTF8String ?: "none", [device supportsFamily:MTLGPUFamilyMac1],
                [device supportsFamily:MTLGPUFamilyApple7],[device supportsFamily:MTLGPUFamilyApple3]);
        fprintf(stderr,"FACTORIO_PROBE: entering UIKit with original game main\n");
        InstallTouchControls();
        return run(argc,argv,original,NULL);
    }
}
