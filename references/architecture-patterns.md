# Architecture patterns, and why each one exists

This is the reasoning behind the shape `scripts/scaffold.py` generates.
Use it to make good judgment calls when the user's domain doesn't fit the
skeleton exactly — the goal is the underlying property (a stable C-ABI
boundary with comfortable C++ underneath it), not literal conformance to
the file layout.

## Two shapes: `device` vs `library`

Not every SDK talks to hardware. `scripts/scaffold.py --kind device` gives
you `Manager → Session → I{Channel}` for anything with a connection
lifecycle and asynchronous, background-thread-driven events. `--kind
library` gives you a single Pimpl compute object (`{Entity}`) with a
synchronous `Create → Process → Destroy` C API for anything that's
input-in, output-out, with no external device involved — a codec, an
image/signal-processing pipeline, a parser, on-device inference.

Both share the same universal core (pure-C boundary, Pimpl, an abstract
interface + factory for the swappable implementation, a CMake build that
produces a real `.framework` with an injected module map). What differs is
what sits behind the interface and how the C API is shaped: `device`'s
`I{Channel}` models a communication channel (open/close/start-streaming)
with borrowed, callback-scoped pointers; `library`'s `I{Channel}` models a
computation (`process(input) -> output`) with an owned, explicitly-freed
output buffer. Don't force a `device`-shaped Manager/Session onto a
compute-only SDK just because it's the more familiar template — it adds a
discovery/connection lifecycle that has no referent in the domain, and the
user (or a future maintainer) will have to reason about state transitions
that can never actually occur.

## The one hard constraint: pure C at the boundary

C++ does not have a stable Application Binary Interface. Name mangling,
exception unwinding tables, `std::string`/`std::vector` layout, and vtable
layout all vary across compiler vendors and even compiler versions. If a
public header exposes any of that, every consumer of the compiled library
is locked to the exact toolchain that built it — intolerable for a
`.dylib`/`.framework`/`.dll` meant to be linked from Swift, Kotlin, or C#.

The fix is boring and absolute: the public header is `extern "C"`, uses
only fixed-width integer types (`stdint.h`), POD structs, opaque pointer
handles, and C function pointers for callbacks. No exceptions cross the
boundary (catch everything internally). No C++ references as parameters.
This is *why* every other pattern below exists — they're how you keep
writing normal C++ on the inside while presenting C on the outside.

### POD structs that can grow

A public struct handed back by value (`{API}DeviceInfo`, `{API}Sample`)
should lead with a `uint32_t struct_size` field once it's likely to grow.
Old consumers keep comparing against their own `sizeof(...)`, so adding
fields at the end later doesn't break already-compiled callers. Small,
closed, unlikely-to-grow structs (like `{API}Version`) can skip this.

### Opaque handles, not exposed structs

`{API}{Entity}ManagerRef` is `struct {API}{Entity}Manager*` where the
struct is *never defined* in the public header — callers only ever hold
and pass the pointer. Internally, the C-ABI facade does
`reinterpret_cast<{ns}::{Entity}Manager*>(ref)`. This means the real C++
class can change its private layout freely (add members, change the STL
container it uses) without ever touching the ABI, because the pointer's
pointee type is opaque to consumers by construction.

## Pimpl idiom

Every concrete class with real state (`{Entity}Manager`, `{Entity}Session`)
hides its implementation behind a private `class Impl` and a
`std::unique_ptr<Impl> impl_`. The pattern:

```cpp
// Header
class {Entity}Manager {
public:
    explicit {Entity}Manager(Callbacks callbacks);
    ~{Entity}Manager();   // declared, NOT defaulted here
    ...
private:
    class Impl;            // incomplete type in the header
    std::unique_ptr<Impl> impl_;
};

// .cpp
class {Entity}Manager::Impl { /* real fields, real logic */ };
{Entity}Manager::{Entity}Manager(Callbacks cb) : impl_(std::make_unique<Impl>(std::move(cb))) {}
{Entity}Manager::~{Entity}Manager() = default;   // defined here, where Impl IS complete
```

The destructor must be declared in the header but *defined* in the .cpp,
even if it's just `= default`. `unique_ptr<Impl>`'s deleter needs
`sizeof(Impl)` at the point the destructor body is instantiated; if you
default it inline in the header, `Impl` is still incomplete there and the
build fails with "invalid application of sizeof to incomplete type" (this
is a real error you will hit if you get this wrong — the scaffold hit it
during development in exactly this shape, just one level up: a `.cpp` that
called a method on an interface pointer without including that interface's
full header).

Why bother: consumers of `{Entity}Manager.h` never see `Impl`'s fields, so
changing them (swap a `std::vector` for a `std::deque`, add a mutex) is a
binary-compatible internal change, and the header doesn't need to
`#include` whatever heavy internal dependencies `Impl` pulls in — keeping
compile times and coupling down even on the pure-C++ side of the boundary.

## Pure abstract interfaces

`I{Channel}` is a pure abstract class — every method is `= 0` except ones
with a sensible universal default (marked non-pure so backends only
override what they actually support). Code above the interface holds
`std::unique_ptr<I{Channel}>` and never knows or cares which concrete
implementation is behind it. In the `device` kind that's `{Entity}Session`
over a transport — simulator, real hardware, replay-from-file, or a test
mock, all interchangeable. In the `library` kind that's the `{Entity}`
compute object over `Reference{Channel}` — with a faster SIMD/GPU/vendor
implementation swappable in later, and the reference kept around as the
correctness oracle the fast path is tested against. Either way, nothing
above the interface (including the C-ABI facade) changes when an
implementation is added.

## Factory as the single creation point

`{Channel}Factory::Create({Channel}Mode)` is the *only* place that calls
`new Simulator{Channel}()` / `new Reference{Channel}()` (or, later,
`new Real{Channel}()` / `new Optimized{Channel}()`). Nothing else in the
codebase is allowed to instantiate a concrete backend directly. This means
adding a second implementation later is: write the class, add one `case`
to the factory's switch, done — every call site that already goes through
the factory picks it up for free, and there is exactly one place to look
when deciding what implementation a given mode maps to.

One caution, mainly for the `library` kind: the interface+factory pair
earns its keep when a second implementation is *plausible* (the `device`
kind always has at least two — simulator and real hardware; a compute SDK
has two only if a faster/accelerated path is actually on the roadmap). An
interface with exactly one permanent implementation is needless
indirection — if it becomes clear no second backend is ever coming, it's
fine to collapse the interface into the concrete class rather than
maintain ceremony for a future that won't arrive.

## `std::function` callbacks below the boundary, C function pointers above it

Below the C-ABI boundary, callbacks are `std::function<void(const T&)>`
member fields set via `setXCallback()`. The threading contract differs by
kind, and both sides of the boundary must agree on it:

- **`device` kind** — delivery is asynchronous: worker threads
  (`std::thread worker_`) push samples/state/errors while streaming.
  Workers use `std::atomic<bool>` for the running/streaming flag and
  `join()` on stop or in the destructor — never detach a worker that
  touches the object's members, or you get a use-after-free the moment the
  object destructs while the thread is still running.
- **`library` kind** — there is exactly one callback (`ProgressCallback`)
  and it fires *synchronously on the calling thread* during `process()`.
  No worker thread, no atomics needed. If a backend internally uses threads
  (a GPU queue, a thread pool), it still blocks `process()` until done and
  invokes progress from the calling thread — keeping the public contract
  synchronous unless a caller genuinely needs otherwise.

At the C-ABI boundary, `std::function` cannot cross (it's not POD), so the
facade converts each registration into a matching C function pointer +
`void* user_data` pair, and wraps the C callback in a lambda that captures
just the C function pointer and user_data by value:

```cpp
// capi/*.cpp
asSession(session)->setSampleCallback([callback, user_data](const ns::Sample& s) {
    {API}Sample c{ s.data.data(), (int32_t)s.data.size(), s.timestamp };
    callback(&c, user_data);   // callback: a plain C function pointer
});
```

This lambda has no captures that need destructors run in a specific order
and no `this` capture — it's copyable, POD-safe to store inside a
`std::function`, and correctly outlives the `capi.cpp` call that created it.

## Ownership across the boundary: borrowed vs owned pointers

The `device` and `library` C APIs hand out pointers under two different,
incompatible contracts — mixing them up is a use-after-free or a leak, so
be explicit in the header comment for every pointer-bearing struct/callback
about which one applies:

- **Borrowed, callback-scoped** (`device` kind: `{API}Sample`, frame/telemetry
  callbacks). The pointer is valid only for the duration of the callback
  that hands it to you. Copy out what you need before returning; never
  store the pointer itself. The SDK owns the memory and reuses or frees it
  the moment the callback returns.
- **Owned, explicitly transferred** (`library` kind: `{API}Buffer` from
  `Process()`). The SDK allocates the buffer with `new[]` inside its own
  binary and hands ownership to the caller. The caller must eventually pass
  it to the matching `FreeResult()` — allocating with `new[]` in the SDK's
  binary and freeing with the SDK's own `delete[]` (rather than, say, the
  caller's `free()`) keeps the allocator matched even across a
  DLL/framework boundary, which is not guaranteed in general when a Swift
  or Kotlin runtime and a C++ shared library each bring their own
  allocator.

Never let a function return a raw pointer without the header comment saying
which contract it follows — "who frees this, and when" is exactly the kind
of thing that's obvious to the person who wrote the facade and invisible to
everyone else.

## The simulator is not a toy stub

(`device` kind only — a `library` SDK has no external hardware to simulate;
its `Reference{Channel}` just needs to be correct, see the factory section
above.)

`Simulator{Channel}` exists so UI and application code can be built,
demoed, and tested *before hardware exists*, and so integration tests don't
need physical devices in CI. That only works if it behaves like the real
thing under stress, not just on the happy path: fabricate the failure modes
your actual hardware has (connection drops with automatic reconnect,
degraded signal, low battery / overheating equivalents, occasional
malformed or delayed samples) at a realistic cadence. A simulator that only
ever emits clean data trains the app to handle a world that doesn't exist,
and every failure-handling code path in the app goes untested until real
hardware — which is exactly the class of bug you built the simulator to
catch early.

## Layered structure (optional, stricter alternative)

Some teams want an explicit three-tier split instead of the flatter
`include/` + `src/` the scaffolder generates:

- `SDK/Public/` — only what the consumer sees: interfaces, types, the
  factory declaration. No implementation details, ever.
- `SDK/Internal/` — headers of concrete implementations (not exported, not
  installed, not part of the public interface contract).
- `SDK/Private/` — `.cpp` files free to pull in heavy or proprietary
  dependencies (a vendor SDK, Boost, a proprietary codec) that must never
  leak into a public or even internal header.

This is the same separation the scaffold already enforces (pure-C public
surface vs. STL-free-to-use internals) with an extra split between
"internal header" and "internal .cpp with heavy deps." It's worth doing
when the SDK genuinely has a proprietary or heavyweight dependency that
should be invisible even to other internal code that doesn't need it.
Don't add it by default — it's ceremony without payoff for a small SDK.
