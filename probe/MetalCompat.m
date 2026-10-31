#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <stdio.h>

static BOOL depth24Unsupported(id self, SEL selector) {
    // Metal's Depth24Unorm_Stencil8 format is macOS-only. Let the original
    // renderer select its existing Depth32Float_Stencil8 fallback on iPadOS.
    return NO;
}

// The specific MTLDevice instance the renderer queries varies by how it was
// created (MTLCreateSystemDefaultDevice, MTLCopyAllDevices, or lazily by
// -[CAMetalLayer init] internally, which is prebound in the shared cache and
// can't be interposed). Rather than chase every creation path, resolve the
// selector universally at the point of failure: +resolveInstanceMethod: runs
// for any class, on any instance, right before the runtime would otherwise
// give up and throw "unrecognized selector".
static BOOL ResolveInstanceMethod(Class self, SEL _cmd, SEL selector) {
    if (selector == sel_registerName("isDepth24Stencil8PixelFormatSupported")) {
        class_addMethod(self, selector, (IMP)depth24Unsupported, "B@:");
        return YES;
    }
    return NO;
}

__attribute__((constructor))
static void InstallDepth24Resolver(void) {
    Class metaclass = object_getClass(objc_getClass("NSObject"));
    class_replaceMethod(metaclass, sel_registerName("resolveInstanceMethod:"), (IMP)ResolveInstanceMethod, "B@::");
}

void PrepareMetalCompatibility(id<MTLDevice> device) {
    fprintf(stderr,"FACTORIO_PROBE: BC texture compression supported=%d\n",
            device ? [device supportsBCTextureCompression] : -1);
}
