# Build pipeline: CMake, module maps, and xcodebuild -create-xcframework

## The three slices

An XCFramework consumable from both macOS and iOS app targets needs three
separate builds of the same source, because each is a different
target triple:

1. **macOS** — fat binary, `arm64;x86_64`, linked against the macOS SDK.
2. **iOS device** — `arm64` only (no simulator arch), linked against
   `iphoneos` SDK.
3. **iOS Simulator** — fat binary, `arm64;x86_64` (Apple Silicon +
   Intel Macs running the simulator), linked against `iphonesimulator` SDK.

Each gets its own CMake build directory and is configured with
`CMAKE_SYSTEM_NAME=iOS` (only for the two iOS slices) plus
`CMAKE_OSX_SYSROOT` set to `iphoneos` or `iphonesimulator`. CMake's Apple
support handles the actual cross-compilation once these variables are set —
no custom toolchain file needed. `build_xcframework.sh` wipes and rebuilds
all three from scratch every run (`rm -rf` on each build dir) rather than
incrementally rebuilding, trading a slower full rebuild for never shipping
a stale slice.

`xcodebuild -create-xcframework -framework <path> -framework <path> -framework <path> -output X.xcframework`
then wraps the three `.framework` bundles into one `.xcframework`, which is
what actually gets embedded in an Xcode project (or an XcodeGen
`project.yml`).

## Why the library must build as a `.framework`, not a bare `.dylib`

`xcodebuild -create-xcframework` expects `.framework` bundles (or static
`.a` + headers) as input, not a bare `.dylib`. On Apple platforms, CMake
produces a `.framework` bundle automatically when the target has
`FRAMEWORK TRUE` set:

```cmake
set_target_properties(<SDK> PROPERTIES
    FRAMEWORK TRUE
    FRAMEWORK_VERSION A
    MACOSX_FRAMEWORK_IDENTIFIER "<bundle-id>"
    PUBLIC_HEADER include/<SDK>.h
    ...
)
```

`PUBLIC_HEADER` tells CMake which header to copy into
`<SDK>.framework/Headers/`. This step is automatic and needs no extra
plumbing.

## The module map is NOT automatic — this is the #1 gotcha

CMake's `FRAMEWORK TRUE` copies the public header into the bundle, but it
does **not** generate a Clang module map. Without one, Swift's
`import <SDK>` fails to resolve, even though the framework links fine and
the header is right there. There is no CMake variable that fixes this —
you have to inject the module map yourself as a post-build step:

```cmake
add_custom_command(TARGET <SDK> POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory
            "$<TARGET_BUNDLE_DIR:<SDK>>/Modules"
    COMMAND ${CMAKE_COMMAND} -E copy
            "${CMAKE_CURRENT_SOURCE_DIR}/framework/module.modulemap"
            "$<TARGET_BUNDLE_DIR:<SDK>>/Modules/module.modulemap"
)
```

The module map itself is a small, hand-maintained file — it does not need
regenerating unless the umbrella header's *name* changes:

```
framework module <SDK> {
    umbrella header "<SDK>.h"
    export *
    module * { export * }
}
```

If a Swift build ever reports "no such module '<SDK>'" after a clean
xcframework build, check `$<framework>/Modules/module.modulemap` exists
before anything else — 9 times out of 10 the post-build copy step didn't
run (stale build dir, target renamed without updating the custom command)
or the file at `framework/module.modulemap` in source control is stale.

`build_xcframework.sh` asserts this explicitly before calling
`xcodebuild -create-xcframework` — it checks
`$framework/Modules/module.modulemap` exists for all three slices and
fails loudly with a clear message rather than letting `xcodebuild` produce
a working-looking xcframework that Swift can't actually import.

## Export macro: `dllexport`/`dllimport` on Windows, visibility elsewhere

```c
#if defined(_WIN32)
  #ifdef <SDK>_BUILD
    #define <PREFIX>_API __declspec(dllexport)
  #else
    #define <PREFIX>_API __declspec(dllimport)
  #endif
#else
  #define <PREFIX>_API __attribute__((visibility("default")))
#endif
```

`<SDK>_BUILD` is defined with `target_compile_definitions(<SDK> PRIVATE <SDK>_BUILD)`
— **PRIVATE**, so only the library's own translation units see it and get
the export path; anything that merely `#include`s the header from outside
(the smoke test, a consumer app) gets the import/default-visibility path
automatically. This one macro is also the entire reason the header is
already portable to a future Windows `.dll` build without changes.

## Why the smoke test is excluded on iOS

```cmake
if(NOT CMAKE_SYSTEM_NAME STREQUAL "iOS")
    add_executable(<sdk-lower>_smoke_test tools/smoke_test.cpp)
    target_link_libraries(<sdk-lower>_smoke_test PRIVATE <SDK>)
endif()
```

A bare CLI executable can't run on iOS (no entitlements/signing, no app
bundle), and iOS toolchains are picky about signing even build-time
artifacts. The smoke test is only meaningful as a macOS-side sanity check
run from the terminal or an IDE — it's not part of the iOS device/simulator
slice at all, hence the guard.

## Deployment targets live in one place

`DEPLOYMENT_TARGET_MACOS` / `DEPLOYMENT_TARGET_IOS` are set once at the top
of `build_xcframework.sh` and passed as `-DCMAKE_OSX_DEPLOYMENT_TARGET=...`
to each slice. Keep them there rather than hardcoding in CMakeLists.txt —
it's the one file a human actually edits when the minimum OS version
policy changes, and CMakeLists.txt is not it.

## Troubleshooting checklist

- **`xcodebuild -create-xcframework` fails with "framework not found"**:
  a slice's CMake build didn't produce a `.framework` (check `FRAMEWORK TRUE`
  is actually set, and that the platform branch — `if(APPLE)` — is being
  taken; it silently falls to the non-Apple `install()` branch if
  `CMAKE_SYSTEM_NAME`/`CMAKE_OSX_SYSROOT` weren't set as expected).
- **Swift `import <SDK>` fails**: module map missing from the bundle — see
  above.
- **Link errors only on the iOS *device* slice, not simulator or macOS**:
  usually an architecture mismatch (a dependency without an `arm64`
  iOS-device slice) or a use of a macOS-only API that isn't available on iOS.
- **Stale xcframework after a source change**: `build_xcframework.sh`
  deletes all three build dirs and the output xcframework at the top of the
  script specifically to prevent this — if you're invoking `cmake --build`
  slices manually instead of the full script, remember stale `.framework`
  bundles from a previous run can get silently reused.
