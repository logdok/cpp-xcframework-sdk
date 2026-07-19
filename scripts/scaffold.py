#!/usr/bin/env python3
"""Scaffold a cross-platform C++ SDK with a C-ABI boundary and an
XCFramework build pipeline, following the layered/Pimpl/interface/factory
pattern documented in ../references/architecture-patterns.md.

Two --kind profiles are available:
  device  (default) -- discovers/connects to an external device over a
           swappable transport, with async streaming callbacks and a
           SimulatorTransport that fabricates hardware failures. Use for
           BLE sensors, cameras, printers, medical devices, IoT, ...
  library -- a self-contained synchronous compute object (Create/Process/
           Destroy), no discovery/session/transport at all, with an
           explicit-ownership output buffer and an optional progress
           callback. Use for codecs, image/signal processing, parsers,
           on-device ML inference, ...

Usage:
    python3 scaffold.py --sdk-name SensorSDK --entity-name Sensor \
        --api-prefix SNS --namespace sns --output /path/to/repo

    python3 scaffold.py --kind library --sdk-name ImageCodecSDK \
        --entity-name Encoder --api-prefix ICS --namespace ics \
        --output /path/to/repo

Run with --help for the full option list. Safe to re-run: it refuses to
overwrite an existing output directory unless --force is given.
"""
import argparse
import datetime
import os
import re
import stat
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(SKILL_DIR, "assets")

# (template filename, output path relative to repo root, executable)
# {sdk} / {entity} / {entity_lower} / {channel} are resolved per-invocation
# since output paths depend on user-supplied names.
FILE_MAP_DEVICE = [
    ("PublicHeader.h.template", "{sdk}/include/{sdk}.h", False),
    ("domain_types.h.template", "{sdk}/src/domain/types.h", False),
    ("IChannel.h.template", "{sdk}/src/transport/I{channel}.h", False),
    ("SimulatorChannel.h.template", "{sdk}/src/transport/Simulator{channel}.h", False),
    ("SimulatorChannel.cpp.template", "{sdk}/src/transport/Simulator{channel}.cpp", False),
    ("ChannelFactory.h.template", "{sdk}/src/transport/{channel}Factory.h", False),
    ("ChannelFactory.cpp.template", "{sdk}/src/transport/{channel}Factory.cpp", False),
    ("Session.h.template", "{sdk}/src/session/{entity}Session.h", False),
    ("Session.cpp.template", "{sdk}/src/session/{entity}Session.cpp", False),
    ("Manager.h.template", "{sdk}/src/discovery/{entity}Manager.h", False),
    ("Manager.cpp.template", "{sdk}/src/discovery/{entity}Manager.cpp", False),
    ("version.cpp.template", "{sdk}/src/version.cpp", False),
    ("capi.cpp.template", "{sdk}/src/capi/{entity_lower}_c_api.cpp", False),
    ("CMakeLists.txt.template", "{sdk}/CMakeLists.txt", False),
    ("module.modulemap.template", "{sdk}/framework/module.modulemap", False),
    ("smoke_test.cpp.template", "{sdk}/tools/smoke_test.cpp", False),
    ("build_xcframework.sh.template", "build_xcframework.sh", True),
    ("CLAUDE.md.template", "CLAUDE.md", False),
    ("gitignore.template", ".gitignore", False),
]

# library profile: a self-contained synchronous compute object, no
# discovery/session/transport. {channel} here names the swappable backend
# *implementation* (e.g. "Backend", "Codec", "Engine"), not a comms channel.
FILE_MAP_LIBRARY = [
    ("PublicHeaderLibrary.h.template", "{sdk}/include/{sdk}.h", False),
    ("domain_types_library.h.template", "{sdk}/src/domain/types.h", False),
    ("ProcessorBackend.h.template", "{sdk}/src/backend/I{channel}.h", False),
    ("ReferenceBackend.h.template", "{sdk}/src/backend/Reference{channel}.h", False),
    ("ReferenceBackend.cpp.template", "{sdk}/src/backend/Reference{channel}.cpp", False),
    ("BackendFactory.h.template", "{sdk}/src/backend/{channel}Factory.h", False),
    ("BackendFactory.cpp.template", "{sdk}/src/backend/{channel}Factory.cpp", False),
    ("Processor.h.template", "{sdk}/src/core/{entity}.h", False),
    ("Processor.cpp.template", "{sdk}/src/core/{entity}.cpp", False),
    ("version.cpp.template", "{sdk}/src/version.cpp", False),
    ("capi_library.cpp.template", "{sdk}/src/capi/{entity_lower}_c_api.cpp", False),
    ("CMakeLists.library.txt.template", "{sdk}/CMakeLists.txt", False),
    ("module.modulemap.template", "{sdk}/framework/module.modulemap", False),
    ("smoke_test_library.cpp.template", "{sdk}/tools/smoke_test.cpp", False),
    ("build_xcframework.sh.template", "build_xcframework.sh", True),
    ("CLAUDE.library.md.template", "CLAUDE.md", False),
    ("gitignore.template", ".gitignore", False),
]

FILE_MAPS = {"device": FILE_MAP_DEVICE, "library": FILE_MAP_LIBRARY}
DEFAULT_CHANNEL_NAME = {"device": "Transport", "library": "Backend"}

PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def pascal_case(s: str) -> str:
    parts = re.split(r"[\s_\-]+", s.strip())
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def git_author_default() -> str:
    try:
        name = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "user.email"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        if name and email:
            return f"{name} ({email})"
        if name:
            return name
    except Exception:
        pass
    return "Your Name"


def build_context(args: argparse.Namespace) -> dict:
    sdk_name = pascal_case(args.sdk_name)
    entity_name = pascal_case(args.entity_name)
    channel_name = pascal_case(args.channel_name)
    api_prefix = args.api_prefix.strip()
    namespace = args.namespace.strip()

    return {
        "SDK_NAME": sdk_name,
        "SDK_NAME_UPPER": sdk_name.upper(),
        "SDK_NAME_LOWER": sdk_name.lower(),
        "ENTITY_NAME": entity_name,
        "ENTITY_NAME_LOWER": entity_name.lower(),
        "CHANNEL_NAME": channel_name,
        "API_PREFIX": api_prefix,
        "NAMESPACE": namespace,
        "BUNDLE_ID": args.bundle_id or f"com.example.{sdk_name.lower()}",
        "AUTHOR": args.author or git_author_default(),
        "YEAR": str(args.year or datetime.date.today().year),
        "DEPLOYMENT_TARGET_MACOS": args.macos_target,
        "DEPLOYMENT_TARGET_IOS": args.ios_target,
    }


def render(text: str, ctx: dict) -> str:
    def sub(m):
        key = m.group(1)
        if key not in ctx:
            raise KeyError(f"Unresolved placeholder {{{{{key}}}}} -- add it to build_context()")
        return ctx[key]

    return PLACEHOLDER_RE.sub(sub, text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kind", choices=["device", "library"], default="device",
                    help="device: discover/connect/stream from external hardware (default). "
                         "library: self-contained synchronous compute object, no transport/session at all.")
    p.add_argument("--sdk-name", required=True, help="e.g. SensorSDK (PascalCase; library/target/framework name)")
    p.add_argument("--entity-name", required=True,
                    help="e.g. Sensor for --kind device (the thing the SDK discovers/connects to), "
                         "or Encoder/Processor for --kind library (the compute object)")
    p.add_argument("--channel-name", default=None,
                    help="the swappable-implementation interface name: Transport/Link/Channel for --kind device "
                         "(default: Transport), Backend/Codec/Engine for --kind library (default: Backend)")
    p.add_argument("--api-prefix", required=True, help="short uppercase C-API prefix, e.g. SNS")
    p.add_argument("--namespace", required=True, help="lowercase internal C++ namespace, e.g. sns")
    p.add_argument("--bundle-id", default=None, help="default: com.example.<sdkname-lower>")
    p.add_argument("--author", default=None, help="default: git config user.name/email")
    p.add_argument("--year", type=int, default=None, help="default: current year")
    p.add_argument("--macos-target", default="13.0")
    p.add_argument("--ios-target", default="16.0")
    p.add_argument("--output", required=True, help="repo root to scaffold into")
    p.add_argument("--force", action="store_true", help="overwrite files if the output dir already exists")
    args = p.parse_args()

    if args.channel_name is None:
        args.channel_name = DEFAULT_CHANNEL_NAME[args.kind]

    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", args.api_prefix):
        p.error("--api-prefix must start with a capital letter and contain only letters/digits (e.g. SNS)")
    if not re.fullmatch(r"[a-z][a-z0-9]*", args.namespace):
        p.error("--namespace must be lowercase letters/digits, starting with a letter (e.g. sns)")

    ctx = build_context(args)
    out_root = os.path.abspath(args.output)

    if os.path.exists(out_root) and os.listdir(out_root) and not args.force:
        print(f"error: {out_root} already exists and is not empty (use --force to overwrite)", file=sys.stderr)
        return 1

    written = []
    for template_name, rel_path_pattern, executable in FILE_MAPS[args.kind]:
        rel_path = rel_path_pattern.format(
            sdk=ctx["SDK_NAME"], entity=ctx["ENTITY_NAME"], entity_lower=ctx["ENTITY_NAME_LOWER"], channel=ctx["CHANNEL_NAME"]
        )
        template_path = os.path.join(ASSETS_DIR, template_name)
        with open(template_path, "r", encoding="utf-8") as f:
            content = render(f.read(), ctx)

        out_path = os.path.join(out_root, rel_path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        if executable:
            st = os.stat(out_path)
            os.chmod(out_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        written.append(rel_path)

    print(f"Scaffolded {ctx['SDK_NAME']} into {out_root}:")
    for rel_path in written:
        print(f"  {rel_path}")
    print()
    print("Next steps:")
    print(f"  cd {out_root}")
    print(f"  cmake -S {ctx['SDK_NAME']} -B {ctx['SDK_NAME']}/build-macos -DCMAKE_BUILD_TYPE=Release")
    print(f"  cmake --build {ctx['SDK_NAME']}/build-macos --target {ctx['SDK_NAME_LOWER']}_smoke_test")
    print(f"  ./{ctx['SDK_NAME']}/build-macos/{ctx['SDK_NAME_LOWER']}_smoke_test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
