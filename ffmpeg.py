#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Cross-build the ARM64 FFmpeg payload with apt-installed tools and libraries."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from kernel import build_environment


FEATURES = {
    "hwaccel": "h264_v4l2request,hevc_v4l2request,mpeg2_v4l2request,cavs_v4l2request",
    "decoder": "h264,hevc,mpeg1video,mpeg1_v4l2m2m,mpeg2video,mpeg4,mpeg4_v4l2m2m,vc1,vc1_v4l2m2m,wmv3,wmv3_v4l2m2m,cavs,rawvideo,mp2,mp3,ac3,eac3,aac,mjpeg,mjpeg_v4l2m2m",
    "encoder": "h264_v4l2m2m,hevc_v4l2m2m,mjpeg_v4l2m2m,rawvideo,aac",
    "parser": "h264,hevc,mpegvideo,mpeg4video,vc1,cavsvideo,mjpeg,mpegaudio,ac3,aac",
    "demuxer": "mpegts,mpegvideo,m4v,vc1,cavsvideo,mjpeg,rawvideo,h264,hevc,ac3,aac,mp3",
    "muxer": "mpegts,rawvideo,h264,hevc,null,framemd5",
    "protocol": "file,pipe",
    "filter": "hivxedei,hivxebob,format,scale,null,fps",
}
LIBRARIES = ("avcodec", "avformat", "avdevice", "avfilter", "avutil", "swresample", "swscale")
SYSTEM_LIBRARY_DIRS = (
    Path("/usr/aarch64-linux-gnu/lib"),
    Path("/lib/aarch64-linux-gnu"),
    Path("/usr/lib/aarch64-linux-gnu"),
)


def run(command, *, cwd, env, capture=False):
    print("+ " + shlex.join(str(part) for part in command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def source_version(source):
    for name in ("VERSION", "RELEASE"):
        path = source / name
        if path.is_file():
            version = path.read_text().strip()
            if re.fullmatch(r"[A-Za-z0-9._+\-]+", version):
                return version
    raise RuntimeError("FFmpeg must provide a nonempty VERSION or RELEASE file")


def hash_tree(root, destination):
    # Include local source changes, not merely the last Git commit. Never consult
    # a parent repository, which may describe an unrelated checkout.
    entries = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
                entries.append(f"{digest}  symlink:{path.relative_to(root).as_posix()}\n")
            elif path.is_file():
                entries.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}\n")
    destination.write_text("".join(sorted(entries)))
    return sha256(destination)


def collect_licenses(source, licenses):
    for name in ("COPYING.GPLv2", "COPYING.GPLv3", "COPYING.LGPLv2.1", "COPYING.LGPLv3"):
        shutil.copy2(source / name, licenses / f"FFmpeg-{name}")
    shutil.copy2(source / "LICENSE.md", licenses / "FFmpeg-LICENSE.txt")

    # Keep the notices in the actual source snapshot, not hand-maintained license
    # paraphrases. Include all three IJG materials named by LICENSE.md, even when
    # only IDCT is selected; some utility fallbacks can also be compiled out.
    materials = (
        ("libavutil/avsscanf.c", "Always-built avsscanf", b"Rich Felker"),
        ("libavutil/fixed_dsp.c", "Always-built fixed-point DSP", b"MIPS Technologies"),
        ("libavutil/fixed_dsp.h", "Fixed-point DSP header", b"MIPS Technologies"),
        ("libavutil/uuid.c", "Always-built UUID utilities", b"Theodore Ts'o"),
        ("libavutil/sha.c", "Always-built SHA utilities and attribution", b"Aaron D. Gifford"),
        ("libavutil/mathematics.c", "Always-built mathematics, including Boost-derived code",
         b"Boost Software License"),
        ("libavutil/libm.h", "Math fallback header; conservative inclusion of Boost notice",
         b"Boost Software License"),
        ("libavcodec/aom_film_grain_template.c", "Film-grain template used by ITU-T T.35 support",
         b"Two Orioles"),
        ("libavcodec/jfdctfst.c", "IJG material identified by LICENSE.md",
         b"Independent JPEG Group"),
        ("libavcodec/jfdctint_template.c", "IJG material identified by LICENSE.md",
         b"Independent JPEG Group"),
        ("libavcodec/jrevdct.c", "IJG IDCT selected by the configured video decoders",
         b"Independent JPEG Group"),
    )
    records = []
    for relative, reason, required in materials:
        path = source / relative
        contents = path.read_bytes()
        # These reviewed C files carry complete notices in block comments,
        # including embedded (not only leading) Boost and BSD notices.
        blocks = [match for match in re.finditer(rb"/\*.*?\*/", contents, re.DOTALL)
                  if re.search(rb"copyright|permission|license|warranty", match[0], re.IGNORECASE)]
        notice = b"\n\n".join(match[0] for match in blocks) + b"\n"
        if not blocks or required not in notice:
            raise RuntimeError(f"Expected third-party notice missing from {relative}; review source licensing")
        destination = licenses / "notices" / (relative + ".txt")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(notice)
        records.append({
            "source_path": relative, "source_sha256": sha256(path), "selection_reason": reason,
            "notice_path": destination.relative_to(licenses).as_posix(),
            "notice_sha256": sha256(destination),
            "source_comment_lines": [
                {"first": contents.count(b"\n", 0, match.start()) + 1,
                 "last": contents.count(b"\n", 0, match.end()) + 1}
                for match in blocks],
        })
    (licenses / "IJG-ACKNOWLEDGMENT.txt").write_text(
        "this software is based in part on the work of the Independent JPEG Group\n\n"
        "FFmpeg's IJG-derived files are adaptations, not the original libjpeg files:\n"
        "libavcodec/jfdctfst.c uses FFmpeg's DCT interface and in-place integer blocks;\n"
        "libavcodec/jfdctint_template.c uses FFmpeg's bit-depth template and DCT interface;\n"
        "libavcodec/jrevdct.c uses FFmpeg's IDCT interface and sparse-block optimizations.\n"
        "The complete source headers, including the IJG conditions and the IDCT modification\n"
        "description, are reproduced verbatim in notices/libavcodec/. Preserve these notices\n"
        "with the matching source and document any additional local additions, deletions\n"
        "or changes to these files when publishing that source.\n")
    write_json(licenses / "THIRD-PARTY-NOTICES.json", {
        "materials": records,
        "coverage": "Reviewed source notices for this builder's configuration, with conservative "
                    "inclusion of all IJG files named by LICENSE.md and math fallbacks. "
                    "Not a per-object inclusion report or an exhaustive license audit of changed sources.",
        "external_libraries": "System libdrm, libudev and libc are not copied into the payload. "
                              "Redistributing those libraries separately requires their own notices and source compliance.",
    })


def check_configuration(build, workspace):
    config = (build / "ffbuild/config.mak").read_text()
    match = re.search(r"^FFMPEG_CONFIGURATION=(.*)$", config, re.MULTILINE)
    if not match:
        raise RuntimeError("configure did not produce FFMPEG_CONFIGURATION")
    configuration = match.group(1)
    if re.search(r"/home/|/root/|/runner/|/workspace|/tmp/|/opt/|/var/jenkins_home", configuration):
        raise RuntimeError("Public FFMPEG_CONFIGURATION contains a build-host path")
    if str(workspace) in configuration:
        raise RuntimeError("Public FFMPEG_CONFIGURATION contains the workspace path")
    required = [f"CONFIG_{name.upper()}=yes" for name in LIBRARIES]
    required += ["CONFIG_FFMPEG=yes", "CONFIG_FFPROBE=yes", "CONFIG_V4L2_M2M=yes",
                 "CONFIG_V4L2_REQUEST=yes", "CONFIG_LIBDRM=yes", "CONFIG_LIBUDEV=yes"]
    for kind, names in FEATURES.items():
        required += [f"CONFIG_{name.upper()}_{kind.upper()}=yes" for name in names.split(",")]
    missing = [value for value in required if value not in config.splitlines()]
    if missing:
        raise RuntimeError("configure disabled required features: " + ", ".join(missing))
    return configuration


def audit_elf(path, cwd, env, *, public_abi=True):
    header = run(["aarch64-linux-gnu-readelf", "-h", path], cwd=cwd, env=env, capture=True)
    if not re.search(r"Machine:\s+AArch64\s*$", header, re.MULTILINE):
        raise RuntimeError(f"Not an AArch64 ELF: {path}")
    dynamic = run(["aarch64-linux-gnu-readelf", "-d", path], cwd=cwd, env=env, capture=True)
    versions = run(["aarch64-linux-gnu-readelf", "--version-info", path],
                   cwd=cwd, env=env, capture=True)
    glibc = sorted(set(re.findall(r"Name: GLIBC_(\d+(?:\.\d+)+)", versions)),
                   key=lambda value: tuple(map(int, value.split("."))))
    for version in glibc:
        if tuple(map(int, version.split("."))) > (2, 36):
            raise RuntimeError(f"{path.name} requires GLIBC_{version}, newer than 2.36")
    if public_abi and "Name: GLIBC_PRIVATE" in versions:
        raise RuntimeError(f"{path.name} requires the private glibc ABI")
    return {
        "sha256": sha256(path),
        "needed": re.findall(r"\(NEEDED\).*?\[(.*?)\]", dynamic),
        "soname": re.findall(r"\(SONAME\).*?\[(.*?)\]", dynamic),
        "runpath": re.findall(r"\((?:RUNPATH|RPATH)\).*?\[(.*?)\]", dynamic),
        "glibc_required": glibc,
        "elf_header": header,
        "dynamic_section": dynamic,
        "symbol_versions": versions,
    }


def audit_payload(payload, cwd, env):
    private = payload / "lib/hivxe-ffmpeg"
    records = {}
    queue = []
    for path in sorted(payload.rglob("*")):
        if path.is_file() and not path.is_symlink():
            record = audit_elf(path, cwd, env)
            expected = "$ORIGIN/../lib/hivxe-ffmpeg" if path.parent.name == "bin" else "$ORIGIN"
            if record["runpath"] != [expected]:
                raise RuntimeError(f"Incorrect relocatable runpath in {path}: {record['runpath']}")
            records[path.relative_to(payload).as_posix()] = record
            queue.extend(record["needed"])
    dependencies = {}
    while queue:
        name = queue.pop()
        if name in dependencies:
            continue
        if "/" in name:
            raise RuntimeError(f"Absolute or path-based DT_NEEDED is not relocatable: {name}")
        bundled = private / name
        if bundled.is_file():
            dependencies[name] = {"provider": "payload", "path": bundled.relative_to(payload).as_posix()}
            continue
        if name.startswith(("libav", "libsw")):
            raise RuntimeError(f"FFmpeg dependency is missing from the private payload: {name}")
        candidate = next((directory / name for directory in SYSTEM_LIBRARY_DIRS
                          if (directory / name).is_file()), None)
        if candidate is None:
            raise RuntimeError(f"ARM64 runtime dependency is missing: {name}; run scripts/install-deps.sh")
        record = audit_elf(candidate, cwd, env, public_abi=False)
        record["provider"] = "apt-installed ARM64 system runtime (not bundled)"
        record["path"] = str(candidate)
        dependencies[name] = record
        queue.extend(record["needed"])
    if not any(record["glibc_required"] for record in records.values()):
        raise RuntimeError("No glibc ABI requirements were found in the payload")
    return {"payload": records, "dependencies": dependencies, "glibc_ceiling": "2.36"}


def build_ffmpeg(workspace, jobs):
    source = workspace / "ffmpeg"
    kernel = workspace / "kernel"
    if not (source / "configure").is_file() or not (kernel / "Makefile").is_file():
        raise RuntimeError("workspace must contain ffmpeg/configure and kernel/Makefile")
    version = source_version(source)
    output = workspace / "out/ffmpeg"
    build = output / "build"
    uapi = workspace / "out/uapi"
    kernel_build = workspace / "out/uapi-build"
    payload = output / "payload"
    stage = output / "stage"
    metadata = output / "metadata"
    licenses = output / "licenses"
    (output / "SHA256SUMS").unlink(missing_ok=True)
    for path in (payload, stage, metadata, licenses, uapi):
        if path.exists():
            shutil.rmtree(path)
    for path in (build, payload / "bin", payload / "lib/hivxe-ffmpeg", stage,
                 metadata, licenses, output / "tmp", output / "home", kernel_build):
        path.mkdir(parents=True, exist_ok=True)
    env = build_environment()
    env.update({
        "HOME": str(output / "home"), "TMPDIR": str(output / "tmp"),
        "PKG_CONFIG_PATH": "", "PKG_CONFIG_LIBDIR": "/usr/lib/aarch64-linux-gnu/pkgconfig",
        "PKG_CONFIG_SYSROOT_DIR": "/",
    })
    if "SOURCE_DATE_EPOCH" in os.environ:
        env["SOURCE_DATE_EPOCH"] = os.environ["SOURCE_DATE_EPOCH"]
    compiler = run(["aarch64-linux-gnu-gcc", "-dumpfullversion"], cwd=build, env=env, capture=True).strip()
    cxx = run(["aarch64-linux-gnu-g++", "-dumpfullversion"], cwd=build, env=env, capture=True).strip()
    target = run(["aarch64-linux-gnu-gcc", "-dumpmachine"], cwd=build, env=env, capture=True).strip()
    if target != "aarch64-linux-gnu":
        raise RuntimeError(f"Expected an ARM64 cross compiler, got {target}")
    run(["make", "-C", kernel, f"O={kernel_build}", "ARCH=arm64", "LLVM=1",
         "HOSTCC=gcc", "HOSTCXX=g++", "CROSS_COMPILE=aarch64-linux-gnu-",
         f"-j{jobs}", "headers_install", f"INSTALL_HDR_PATH={uapi}"], cwd=build, env=env)
    header = uapi / "include/linux/histb-vpss.h"
    if not header.is_file() or not re.search(r"^#define\s+VIDIOC_HISTB_VPSS_DEI\b", header.read_text(), re.MULTILINE):
        raise RuntimeError("Exported kernel UAPI lacks the HiSTB VPSS DEI ioctl")
    # Compile against the actual exported UAPI, rather than accepting a matching
    # comment or an incompatible ioctl definition. This object is never shipped.
    ioctl_probe = output / "tmp/vpss-uapi.c"
    ioctl_probe.write_text('#include <linux/histb-vpss.h>\n'
                           '_Static_assert(_IOC_TYPE(VIDIOC_HISTB_VPSS_DEI) == \'V\', "ioctl type");\n'
                           '_Static_assert(_IOC_SIZE(VIDIOC_HISTB_VPSS_DEI) == sizeof(struct histb_vpss_dei_request), "ioctl ABI");\n'
                           'int main(void) { return 0; }\n')
    run(["aarch64-linux-gnu-gcc", "--sysroot=/", f"-I{uapi / 'include'}", "-c", ioctl_probe,
         "-o", output / "tmp/vpss-uapi.o"], cwd=build, env=env)
    ioctl_probe.unlink()
    (output / "tmp/vpss-uapi.o").unlink()
    packages = {}
    for package in ("libdrm", "libudev"):
        packages[package] = {
            "version": run(["pkg-config", "--modversion", package], cwd=build, env=env, capture=True).strip(),
            "flags": run(["pkg-config", "--cflags", "--libs", package], cwd=build, env=env, capture=True).strip(),
            "libdir": run(["pkg-config", "--variable=libdir", package], cwd=build, env=env, capture=True).strip(),
        }
        if packages[package]["libdir"] not in ("/usr/lib/aarch64-linux-gnu", "/lib/aarch64-linux-gnu"):
            raise RuntimeError(f"pkg-config selected a non-ARM64 {package}: {packages[package]}")
    # Bare <drm_fourcc.h> must resolve to this kernel's export before the
    # pkg-config libdrm include directory; <drm/histb_drm.h> uses the same
    # definitions. Keep pkg-config flags for system libdrm's userspace APIs.
    flags = (f"-I../../uapi/include -I../../uapi/include/drm -ffile-prefix-map={workspace}=. "
             f"-fmacro-prefix-map={workspace}=. -fdebug-prefix-map={workspace}=.")
    env.update({"CFLAGS": flags, "CXXFLAGS": flags, "ASFLAGS": flags})
    # Linker response files avoid both shell expansion and Make's additional
    # eval expansion when shared-library rules are generated.
    runpath_flags = {
        "runpath-exe.ldflags": "-z origin --enable-new-dtags -rpath '$ORIGIN/../lib/hivxe-ffmpeg'\n",
        "runpath-so.ldflags": "-z origin --enable-new-dtags -rpath '$ORIGIN'\n",
    }
    for name, contents in runpath_flags.items():
        (build / name).write_text(contents)
    env["LDEXEFLAGS"] = "-Wl,@runpath-exe.ldflags"
    env["LDSOFLAGS"] = "-Wl,@runpath-so.ldflags"
    configure = [
        "../../../ffmpeg/configure", "--prefix=/usr/local", "--libdir=/usr/local/lib/hivxe-ffmpeg",
        "--shlibdir=/usr/local/lib/hivxe-ffmpeg", "--arch=aarch64", "--target-os=linux",
        "--enable-cross-compile", "--cc=aarch64-linux-gnu-gcc", "--cxx=aarch64-linux-gnu-g++",
        "--host-cc=gcc", "--ar=aarch64-linux-gnu-ar", "--nm=aarch64-linux-gnu-nm",
        "--ranlib=aarch64-linux-gnu-ranlib", "--strip=aarch64-linux-gnu-strip",
        "--pkg-config=pkg-config", "--sysroot=/", "--extra-libs=-ludev", "--disable-everything",
        "--disable-autodetect", "--enable-ffmpeg", "--enable-ffprobe",
        *(f"--enable-{name}" for name in LIBRARIES),
        "--enable-gpl", "--enable-version3", "--enable-shared", "--disable-static",
        "--disable-doc", "--disable-debug", "--disable-stripping", "--disable-version-tracking",
        "--enable-pthreads", "--enable-v4l2-m2m", "--enable-v4l2-request",
        "--enable-libdrm", "--enable-libudev", "--extra-version=hivxe",
        *(f"--enable-{kind}={names}" for kind, names in FEATURES.items()),
    ]
    run(configure, cwd=build, env=env)
    configuration = check_configuration(build, workspace)
    # Set the source release explicitly: a source snapshot nested in a new Git
    # repository must not inherit the enclosing repository's unrelated version.
    run(["make", f"-j{jobs}", f"REVISION={version}"], cwd=build, env=env)
    run(["make", f"DESTDIR={stage}", f"REVISION={version}", "install"], cwd=build, env=env)
    installed = stage / "usr/local"
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2(installed / "bin" / name, payload / "bin" / f"hivxe-{name}")
    private = payload / "lib/hivxe-ffmpeg"
    for library in LIBRARIES:
        candidates = sorted((installed / "lib/hivxe-ffmpeg").glob(f"lib{library}.so.*"))
        if not candidates:
            raise RuntimeError(f"make install did not install lib{library}")
        for path in candidates:
            if path.is_symlink():
                target_name = os.readlink(path)
                if Path(target_name).name != target_name:
                    raise RuntimeError(f"Unexpected library symlink target: {path} -> {target_name}")
                (private / path.name).symlink_to(target_name)
            else:
                shutil.copy2(path, private / path.name)
    abi = audit_payload(payload, build, env)
    write_json(metadata / "ABI.json", abi)
    (metadata / "CONFIGURATION.txt").write_text("FFMPEG_CONFIGURATION=" + configuration + "\n")
    shutil.copy2(build / "config.h", metadata / "config.h")
    shutil.copy2(build / "config_components.h", metadata / "config_components.h")
    shutil.copy2(build / "libavutil/ffversion.h", metadata / "ffversion.h")
    (metadata / "APT-PACKAGES.txt").write_text(run(
        ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\n",
         "gcc-aarch64-linux-gnu", "g++-aarch64-linux-gnu", "libc6:arm64",
         "libdrm-dev:arm64", "libudev-dev:arm64"], cwd=build, env=env, capture=True))
    source_digest = hash_tree(source, metadata / "SOURCE-SHA256SUMS")
    uapi_digest = hash_tree(uapi, metadata / "UAPI-SHA256SUMS")
    kernel_version = run(["make", "-s", "-C", kernel, f"O={kernel_build}", "ARCH=arm64",
                          "kernelversion"], cwd=build, env=env, capture=True).strip()
    write_json(metadata / "BUILD.json", {
        "ffmpeg_source_version": version,
        "ffmpeg_built_version_header": (build / "libavutil/ffversion.h").read_text(),
        "source_manifest_sha256": source_digest, "kernel_version": kernel_version,
        "uapi_manifest_sha256": uapi_digest, "histb_vpss_header_sha256": sha256(header),
        "kernel_source_location": "workspace/kernel (source of the freshly exported UAPI)",
        "uapi_include_directories": ["../../uapi/include", "../../uapi/include/drm"],
        "configure_argv": configure, "configure_environment": {
            name: env[name] for name in ("CFLAGS", "CXXFLAGS", "ASFLAGS", "LDEXEFLAGS", "LDSOFLAGS",
                                       "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "PKG_CONFIG_SYSROOT_DIR")},
        "linker_response_files": runpath_flags,
        "make_revision": version, "target": target, "gcc": compiler, "g++": cxx,
        "build_dependencies": "host apt tools and ARM64 multiarch development packages",
        "dependency_pkg_config": packages,
        "runtime": "AArch64 Linux with system libdrm/libudev; GLIBC <= 2.36 (build on Debian 12 for Debian 12/13 compatibility)",
        "source_location": "workspace/ffmpeg (publish the complete matching source tree with this payload)",
        "payload_license": "GPL-3.0-or-later (--enable-gpl --enable-version3), with retained third-party notices",
        "build_scripts_license": "GPL-2.0-only; separate from the generated FFmpeg payload's license",
        "source_delivery": "Not performed by this builder. When conveying the payload, provide the complete "
                           "matching Corresponding Source under GPLv3 section 6, including local modifications, "
                           "required interface definitions and scripts controlling compilation and installation. "
                           "Source and UAPI hash manifests identify inputs; they are not source delivery or a written offer. "
                           "Retain third-party notices and document modifications to IJG-derived files.",
        "third_party_notice_index": "licenses/THIRD-PARTY-NOTICES.json",
    })
    collect_licenses(source, licenses)
    sums = []
    for root in (payload, metadata, licenses):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                sums.append(f"{sha256(path)}  {path.relative_to(output).as_posix()}\n")
    (output / "SHA256SUMS").write_text("".join(sorted(sums)))
    shutil.rmtree(stage)
    print(f"FFmpeg {version}: {payload}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--jobs", type=int, required=True)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        build_ffmpeg(args.workspace.resolve(), args.jobs)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ffmpeg: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
