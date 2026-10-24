#import <AppKit/AppKit.h>
#import <objc/runtime.h>
#include <node_api.h>
#include <cstring>

// Retained by the host view, released with the window. No global NSWindow pointers.
static char materialKey;
@interface LTMaterialHost : NSView
@end
@implementation LTMaterialHost
- (NSView *)hitTest:(NSPoint)point { return nil; }
@end

static napi_value SetMaterial(napi_env env, napi_callback_info info) {
  size_t count = 3;
  napi_value args[3], result;
  napi_get_cb_info(env, info, &count, args, nullptr, nullptr);
  bool isBuffer = false, enabled = false, dark = false;
  void *bytes = nullptr;
  size_t size = 0;
  if (count != 3 || napi_is_buffer(env, args[0], &isBuffer) != napi_ok || !isBuffer ||
      napi_get_buffer_info(env, args[0], &bytes, &size) != napi_ok || size != sizeof(void *) ||
      napi_get_value_bool(env, args[1], &enabled) != napi_ok ||
      napi_get_value_bool(env, args[2], &dark) != napi_ok || ![NSThread isMainThread]) {
    napi_throw_type_error(env, nullptr, "Expected native window handle and two booleans on the main thread");
    return nullptr;
  }
  NSView * __unsafe_unretained host = nil;
  std::memcpy(&host, bytes, sizeof(host));
  bool supported = false;
  @autoreleasepool {
    if (host && host.window) {
      // Scope the resolved preference to this window. Changing Electron's global
      // themeSource would break the renderer's ability to follow the OS theme.
      host.window.appearance = [NSAppearance appearanceNamed:dark ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
    }
    if (@available(macOS 26.0, *)) {
      if (host && host.window) {
        supported = true;
        LTMaterialHost *container = objc_getAssociatedObject(host, &materialKey);
        if (!enabled || NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceTransparency ||
            NSWorkspace.sharedWorkspace.accessibilityDisplayShouldIncreaseContrast) {
          [container removeFromSuperview];
          objc_setAssociatedObject(host, &materialKey, nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
        } else {
          if (!container) {
            container = [[LTMaterialHost alloc] initWithFrame:host.bounds];
            container.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
            // Glass samples within the window. Supply the actual desktop backdrop
            // through AppKit's public behind-window material instead of a flat fill.
            NSVisualEffectView *backdrop = [[NSVisualEffectView alloc] initWithFrame:container.bounds];
            backdrop.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
            backdrop.material = NSVisualEffectMaterialUnderWindowBackground;
            backdrop.blendingMode = NSVisualEffectBlendingModeBehindWindow;
            backdrop.state = NSVisualEffectStateActive;
            [container addSubview:backdrop];
            NSGlassEffectView *glass = [[NSGlassEffectView alloc] initWithFrame:container.bounds];
            glass.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
            glass.style = NSGlassEffectViewStyleClear;
            glass.cornerRadius = 16;
            glass.contentView = [[NSView alloc] initWithFrame:glass.bounds];
            [container addSubview:glass];
            [host addSubview:container positioned:NSWindowBelow relativeTo:nil];
            objc_setAssociatedObject(host, &materialKey, container, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
          }
          container.appearance = [NSAppearance appearanceNamed:dark ? NSAppearanceNameDarkAqua : NSAppearanceNameAqua];
          host.window.opaque = NO;
          host.window.backgroundColor = NSColor.clearColor;
        }
      }
    }
  }
  napi_get_boolean(env, supported, &result);
  return result;
}
static napi_value Init(napi_env env, napi_value exports) {
  napi_value fn;
  napi_create_function(env, "setMaterial", NAPI_AUTO_LENGTH, SetMaterial, nullptr, &fn);
  napi_set_named_property(env, exports, "setMaterial", fn);
  return exports;
}
NAPI_MODULE(NODE_GYP_MODULE_NAME, Init)
