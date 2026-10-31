#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Cross-build HiSTB ARM64 kernels and their matching V4L2 FFmpeg userspace.

Kernel sources come from workspace/kernel, handoff sources from scripts/boot.
CVPM firmware uses the bundled boot C51 image or an explicitly supplied replacement.
1004 shares Image/modules across independently packaged mxl214 and r858 RF variants.
No initramfs, rootfs, device installation, downloads or private BSP path is used.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import platform
import shlex
import shutil
import struct
import subprocess
import tarfile
import sys
import zlib

BOARDS = ("1001", "1002", "1004", "1006", "ec6110t")
DTBS_1004 = {
    "mxl214": "hi3798cv200-dvbip-1004-arm64.dtb",
    "r858": "hi3798cv200-dvbip-1004-r858-arm64.dtb",
}
MIB = 1024 * 1024
CV200_C51_BYTES = 21468
CV200_C51_SHA256 = "1ae4b66809f09bce59469c71bcba649856c66f9bcd24366cf0f5b741697959e0"
CV200_C51_DEFAULT = "scripts/boot/hi3798cv200-pm-c51.bin"
CV200_C51_SOURCE = (
    "https://github.com/JasonFreeLab/HiSTBLinuxV100R005C00SPC060/blob/"
    "a82e5fee18a794ecdf28401b21c25cddec0d21fb/"
    "source/component/pm/c51/output_ca_release.txt"
)
ANDROID_OFFSET = 0x20000
UIMAGE_OFFSET = 0x24000
EC_BOOTARGS = (
    "console=ttyAMA0,115200 "
    "root=/dev/mmcblk0p5 rootfstype=ext4 rootwait rw "
    "blkdevparts=mmcblk0:1M(fastboot),1M(bootargs),1M(deviceinfo),16M(kernel),-(rootfs) "
    "mem=1G loglevel=4 clk_ignore_unused fw_devlink=off net.ifnames=0"
)
def build_environment() -> dict[str, str]:
    """Keep installed host tools, not inherited Make/compiler overrides."""
    env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "SOURCE_DATE_EPOCH")
           if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin")
    if "/usr/sbin" not in env["PATH"].split(os.pathsep):
        env["PATH"] += os.pathsep + "/usr/sbin"
    env.update({"LANG": "C", "LC_ALL": "C"})
    return env


ENV = build_environment()
ENV.update({
    "SOURCE_DATE_EPOCH": "0", "KBUILD_BUILD_TIMESTAMP": "Thu Jan 1 00:00:00 UTC 1970",
    "KBUILD_BUILD_USER": "community", "KBUILD_BUILD_HOST": "community",
    "KBUILD_BUILD_VERSION": "1",
})


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(args: list[object], *, capture: bool = False) -> str:
    command = [str(arg) for arg in args]
    print("+ " + shlex.join(command), flush=True)
    result = subprocess.run(command, env=ENV, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inside(path: Path, root: Path) -> Path:
    require(path.resolve().is_relative_to(root), f"path escapes {root}: {path}")
    return path


def output_directory(path: Path, root: Path) -> Path:
    inside(path, root)
    require(not path.is_symlink(), f"output directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_file(path: Path, root: Path) -> Path:
    inside(path, root)
    require(not path.is_symlink(), f"output file must not be a symlink: {path}")
    require(not path.exists() or path.is_file(), f"not a regular output file: {path}")
    return path


def make_command(kernel: Path, out: Path) -> list[object]:
    return ["make", "--no-print-directory", "-C", kernel, f"O={out}",
            "ARCH=arm64", "LLVM=1", "LLVM_IAS=1", "CROSS_COMPILE=aarch64-linux-gnu-",
            "CC=clang", "LD=ld.lld", "HOSTCC=gcc", "HOSTCXX=g++", "LOCALVERSION="]


def read_config(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line[2:-11]] = "n"
    return values


def validate_config(path: Path, suffix: str, ec: bool) -> None:
    values = read_config(path)
    for key in ("ARM64", "MODULES", "MODULE_UNLOAD", "MODVERSIONS", "SMP"):
        require(values.get("CONFIG_" + key) == "y", f"missing ABI option CONFIG_{key}=y")
    require(values.get("CONFIG_LOCALVERSION") == json.dumps(suffix), "board LOCALVERSION mismatch")
    require(values.get("CONFIG_LOCALVERSION_AUTO", "n") == "n", "automatic release suffix enabled")
    require(values.get("CONFIG_CPU_BIG_ENDIAN", "n") == "n", "handoff requires little-endian ARM64")
    require(values.get("CONFIG_INITRAMFS_SOURCE", '""') == '""', "embedded initramfs is not supported")
    require(values.get("CONFIG_EXTRA_FIRMWARE", '""') == '""', "embedded external firmware is not supported")
    require(values.get("CONFIG_HI3798MV310_IRQ_PROBE", "n") == "n", "RAM IRQ diagnostics enabled")
    if ec:
        for key in ("HISILICON_FESTA_PHY", "FHANDLE", "AUTOFS_FS", "SECCOMP"):
            require(values.get("CONFIG_" + key) == "y", f"missing EC flashboot option {key}")


def validate_dtb(path: Path, board: str, variant: str | None = None) -> str:
    raw = path.read_bytes()
    require(len(raw) >= 40, "DTB is shorter than its header")
    magic, size = struct.unpack_from(">II", raw)
    require(magic == 0xd00dfeed and size == len(raw), "DTB size/magic mismatch")
    compatibles = run(["fdtget", "-t", "s", path, "/", "compatible"], capture=True).split()
    soc = "hi3798mv310" if board == "ec6110t" else "hi3798cv200"
    require("hisilicon," + soc in compatibles, f"DTB SoC mismatch for {board}")
    expected = "huawei,ec6110t" if board == "ec6110t" else "dvbip," + board
    alternatives = {expected}
    if board != "ec6110t":
        alternatives.add("hisilicon,hi3798cv200-ip" + board)
    require(bool(alternatives.intersection(compatibles)), f"DTB board mismatch for {board}")
    if board == "1004" and variant is not None:
        require(variant in DTBS_1004, f"unknown 1004 variant: {variant}")
        require(("dvbip,1004-r858" in compatibles) == (variant == "r858"),
                f"DTB RF variant mismatch for {variant}")
    model = run(["fdtget", "-t", "s", path, "/", "model"], capture=True).strip()
    expected_model = "Huawei EC6110-T Hi3798MV310 ARM64" if board == "ec6110t" else f"DVBIP {board} ARM64"
    require(model == expected_model, f"unexpected DTB model: {model}")
    bootargs = run(["fdtget", "-t", "s", path, "/chosen", "bootargs"], capture=True).strip()
    if board == "ec6110t":
        require(bootargs == EC_BOOTARGS, "EC flashboot bootargs mismatch")
    else:
        root = "6" if board == "1004" else "5"
        require(f"root=/dev/mmcblk0p{root}" in bootargs.split(), "DTB root partition mismatch")
        require(f"dvbip.model={board}" in bootargs.split(), "DTB bootargs board mismatch")
    require(not any(arg.startswith("rdinit=") or arg == "maxcpus=1" for arg in bootargs.split()),
            "RAM diagnostics found in flashboot DTB")
    return soc


def validate_image(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(64)
    require(len(header) == 64 and header[56:60] == b"ARM\x64", "not an ARM64 Linux Image")
    text_offset, image_size, flags = struct.unpack_from("<QQQ", header, 8)
    require(text_offset == 0, "handoff branches to 0x02000000 and requires TEXT_OFFSET=0")
    require(not flags & 1, "big-endian ARM64 Image is unsupported")
    # The header covers BSS as well as file-backed code/data.
    require(0 < image_size <= 0x10000000 - 0x02000000, "Image runtime footprint overlaps DTB")
    require(64 <= path.stat().st_size <= image_size, "invalid ARM64 Image file size")


def cvpm_enabled(dtb: Path) -> bool:
    result = subprocess.run(
        ["fdtget", "-t", "s", str(dtb), "/standby-controller", "compatible"],
        env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0 or "FDT_ERR_NOTFOUND" in result.stderr,
            f"cannot inspect standby controller: {result.stderr.strip()}")
    return result.returncode == 0 and result.stdout.strip() == "hisilicon,hi3798cv200-pm"


def read_c51_firmware(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RuntimeError(
            f"CV200 PM requires the C51 image: {path}. "
            f"Restore {CV200_C51_DEFAULT} or use --c51-firmware PATH "
            "with the original BSP output_ca_release.txt or a raw binary. "
            "PM will not be disabled automatically.") from error
    if data.lstrip().startswith(b"0x"):
        require(not re.sub(rb"0x[0-9a-fA-F]{8}|[\s,]", b"", data),
                "C51 word array must contain only 32-bit hexadecimal words and separators")
        data = b"".join(struct.pack("<I", int(word, 16))
                        for word in re.findall(rb"0x[0-9a-fA-F]{8}", data))
    require(len(data) == CV200_C51_BYTES,
            f"C51 image must be {CV200_C51_BYTES} bytes, got {len(data)}; PM was not built")
    digest = hashlib.sha256(data).hexdigest()
    require(digest == CV200_C51_SHA256,
            f"C51 SHA256 mismatch: expected {CV200_C51_SHA256}, got {digest}; PM was not built")
    return data


def build_cvpm(boot: Path, directory: Path, defines: list[str], c51: bytes) -> None:
    include = output_file(directory / "hi3798cv200-pm-c51.inc", directory)
    words = struct.unpack(f"<{len(c51) // 4}I", c51)
    include.write_text("".join(
        ",".join(f"0x{word:08x}" for word in words[n:n + 8]) + ",\n"
        for n in range(0, len(words), 8)))
    bridge = directory / "hi3798cv200-pm-bridge"
    run(["clang", "--target=armv7a-linux-gnueabi", "-c", "-o", bridge.with_suffix(".o"),
         boot / "hi3798cv200-pm-bridge.S"])
    run(["ld.lld", "-m", "armelf_linux_eabi", "--build-id=none", "-Ttext=0", "-e", "_start",
         "-o", bridge.with_suffix(".elf"), bridge.with_suffix(".o")])
    bridge_bin = bridge.with_suffix(".bin")
    run(["llvm-objcopy", "-O", "binary", bridge.with_suffix(".elf"), bridge_bin])
    require(bridge_bin.stat().st_size <= 256, "A32 warm bridge exceeds reviewed bound")
    flags = ["--target=aarch64-linux-gnu", "-march=armv8-a", "-mgeneral-regs-only",
             "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-fno-pic",
             "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-Os", *defines,
             f"-DDVBIP_PM_BRIDGE_PATH={json.dumps(str(bridge_bin))}",
             f"-DDVBIP_PM_C51_PATH={json.dumps(str(include))}"]
    objects = []
    for source, name in (("hi3798cv200-arm64-linux-entry.S", "arm64-entry.o"),
                         ("hi3798cv200-pm.c", "hi3798cv200-pm.o")):
        obj = output_file(directory / name, directory)
        run(["clang", *flags, "-c", "-o", obj, boot / source])
        objects.append(obj)
    elf = output_file(directory / "arm64-entry.elf", directory)
    run(["ld.lld", "-m", "aarch64elf", "--build-id=none", "-T", boot / "hi3798cv200-pm.lds",
         f"-Map={directory / 'arm64-entry.map'}", "-o", elf, *objects])
    run(["llvm-objcopy", "-O", "binary", elf, directory / "arm64-entry.bin"])


def build_bundle(image: Path, dtb: Path, board: str, directory: Path, boot: Path,
                 c51: bytes | None = None) -> tuple[Path, list[str]]:
    flags = ["-DHISTB_HI3798MV310=1"] if board == "ec6110t" else []
    if board == "1002":
        flags.append("-DDVBIP_IP1002_PRE_SMP_APLL_1200=1")
    deep_pm = cvpm_enabled(dtb)
    if deep_pm:
        require(board in ("1001", "1002", "1004", "1006"),
                "CVPM firmware requires a DVBIP Hi3798CV200 board")
        flags.append("-DHISTB_CV200_DEEP_PM=1")
    # Deliberately no RAM diagnostic, extended snapshot, or initramfs options.
    entry_object = directory / "arm64-entry.o"
    entry_elf = directory / "arm64-entry.elf"
    entry_bin = directory / "arm64-entry.bin"
    bundle_object = directory / "bundle.o"
    bundle_elf = directory / "bundle.elf"
    bundle_bin = directory / "bundle.bin"
    for name in (entry_object, entry_elf, entry_bin, bundle_object, bundle_elf,
                 bundle_bin, directory / "bundle.map", directory / "build-flags.txt"):
        output_file(name, directory)
    if deep_pm:
        require(c51 is not None, "CV200 PM handoff requires validated C51 firmware")
        build_cvpm(boot, directory, flags, c51)
    else:
        run(["clang", "--target=aarch64-linux-gnu", *flags, "-c", "-o", entry_object,
             boot / "hi3798cv200-arm64-linux-entry.S"])
        run(["ld.lld", "-m", "aarch64elf", "--build-id=none", "-Ttext=0x11000000", "-e", "_start",
             "-o", entry_elf, entry_object])
        run(["llvm-objcopy", "-O", "binary", entry_elf, entry_bin])
    require(0 < entry_bin.stat().st_size <= 53248, "entry stub overlaps handoff mailbox")
    require(0 < dtb.stat().st_size <= 16 * MIB, "DTB overlaps entry stub")
    defines = [f"-D{name}={json.dumps(str(path))}" for name, path in
               (("ARM64_ENTRY_BIN", entry_bin), ("ARM64_DTB_BIN", dtb), ("ARM64_IMAGE_BIN", image))]
    run(["clang", "--target=armv7a-linux-gnueabi", *flags, "-c", "-x", "assembler-with-cpp",
         *defines, "-o", bundle_object, boot / "hi3798cv200-arm32-to-arm64-bundle.S"])
    run(["ld.lld", "-m", "armelf_linux_eabi", "--build-id=none", "-Ttext=0x12000000", "-e", "_start",
         f"-Map={directory / 'bundle.map'}", "-o", bundle_elf, bundle_object])
    run(["llvm-objcopy", "-O", "binary", bundle_elf, bundle_bin])
    require(0 < bundle_bin.stat().st_size <= (64 if board == "ec6110t" else 16) * MIB,
            "bundle exceeds its load window")
    (directory / "build-flags.txt").write_text("".join(flag + "\n" for flag in flags))
    return bundle_bin, flags


def make_uimage(payload: bytes, release: str) -> bytes:
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    name = ("Linux-" + release).encode("ascii")[:32].ljust(32, b"\0")
    header = struct.pack(">7I4B32s", 0x27051956, 0, 0, len(compressed), 0x12000000, 0x12000000,
                         zlib.crc32(compressed) & 0xffffffff, 5, 2, 2, 1, name)
    header = header[:4] + struct.pack(">I", zlib.crc32(header) & 0xffffffff) + header[8:]
    return header + compressed


def validate_uimage(data: bytes, payload: bytes, release: str) -> None:
    require(len(data) >= 64, "truncated uImage header")
    fields = struct.unpack(">7I4B32s", data[:64])
    magic, header_crc, _stamp, size, load, entry, data_crc, os_id, arch, kind, compression, name = fields
    header = data[:4] + b"\0" * 4 + data[8:64]
    require(magic == 0x27051956 and zlib.crc32(header) & 0xffffffff == header_crc,
            "uImage header CRC mismatch")
    require(size == len(data) - 64 and zlib.crc32(data[64:]) & 0xffffffff == data_crc,
            "uImage data size/CRC mismatch")
    require((load, entry, os_id, arch, kind, compression) == (0x12000000, 0x12000000, 5, 2, 2, 1),
            "uImage execution metadata mismatch")
    require(name.rstrip(b"\0") == ("Linux-" + release).encode("ascii")[:32], "uImage name mismatch")
    require(gzip.decompress(data[64:]) == payload, "uImage payload mismatch")


def android_template(size: int, offset: int) -> bytearray:
    data = bytearray(size)
    struct.pack_into("<8s10I", data, offset, b"ANDROID!", 0, 0x03008000, 0, 0x04000000,
                     0, 0, 0x03000100, 0x4000, 0, 0)
    return data


def package_container(board: str, directory: Path, uimage: bytes) -> tuple[str, str]:
    if board in ("1001", "1006", "1002"):
        compact = board == "1002"
        size, header_offset, payload_offset = (16 * MIB, ANDROID_OFFSET, UIMAGE_OFFSET) if compact else (10 * MIB, 0, 0x4000)
        name = "kernel.p4.img" if compact else "trustedcore.img"
        require(len(uimage) <= size - payload_offset, f"uImage exceeds {name} capacity")
        # Generated template: no vendor partition or original image is read.
        data = android_template(size, header_offset)
        struct.pack_into("<I", data, header_offset + 8, len(uimage))
        data[payload_offset:payload_offset + len(uimage)] = uimage
        identity = hashlib.sha1(uimage + struct.pack("<III", len(uimage), 0, 0) + (b"\0" * 4 if compact else b"")).digest()
        data[header_offset + 576:header_offset + 596] = identity
        require(data[header_offset:header_offset + 8] == b"ANDROID!", "Android container magic mismatch")
        require(struct.unpack_from("<I", data, header_offset + 8)[0] == len(uimage), "Android kernel size mismatch")
        require(data[header_offset + 576:header_offset + 596] == identity, "Android container ID mismatch")
        kind = "generated compact Android/uImage" if compact else "trustedcore Android/uImage"
    else:
        size = (16 if board == "ec6110t" else 11) * MIB
        name = "kernel.img" if board == "ec6110t" else "recovery.img"
        payload_offset = 0
        require(len(uimage) <= size, f"uImage exceeds {name} capacity")
        data = bytearray(size)
        data[:len(uimage)] = uimage
        require(data[:4] == b"\x27\x05\x19\x56", "legacy container magic mismatch")
        kind = "zero-padded legacy ARM uImage"
    require(len(data) == size, f"wrong fixed container size for {name}")
    require(data[payload_offset:payload_offset + len(uimage)] == uimage, "container payload mismatch")
    require(not any(data[payload_offset + len(uimage):]), "container trailing padding is not zero")
    output_file(directory / name, directory).write_bytes(data)
    return name, kind


def stage_modules(make: list[object], stage: Path, out: Path, release: str, destination: Path) -> int:
    run([*make, "modules_install", f"INSTALL_MOD_PATH={stage}", "INSTALL_MOD_STRIP=", "DEPMOD=depmod"])
    modules = stage / "lib/modules"
    require(modules.is_dir() and not modules.is_symlink(), "missing module staging directory")
    require(sorted(path.name for path in modules.iterdir()) == [release], "staging contains a foreign module release")
    release_dir = modules / release
    require(not release_dir.is_symlink(), "module release directory is a symlink")
    for name in ("build", "source"):
        link = release_dir / name
        if link.is_symlink():
            link.unlink()
        else:
            require(not link.exists(), f"unexpected non-symlink module {name} path")
    count = 0
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            require(not Path(os.readlink(path)).is_absolute(), f"absolute module symlink: {path}")
            inside(path, stage)
        elif path.is_file():
            require(path.is_relative_to(release_dir), f"non-module payload in staging: {path}")
            if ".ko" in path.name:
                # modinfo reads plain and distribution-supported compressed modules.
                vermagic = run(["modinfo", "-F", "vermagic", path], capture=True).split()
                require(vermagic and vermagic[0] == release and "aarch64" in vermagic,
                        f"module vermagic/architecture mismatch: {path}")
                require("modversions" in vermagic and "SMP" in vermagic,
                        f"module configuration ABI mismatch: {path}")
                count += 1
    require(count > 0, "no kernel modules were installed")
    require((out / "Module.symvers").stat().st_size > 0, "missing module symbol CRC manifest")
    run(["depmod", "-b", stage, "-F", out / "System.map", release])
    with destination.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for path in sorted(stage.rglob("*")):
                    info = archive.gettarinfo(str(path), arcname=path.relative_to(stage).as_posix())
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)
    return count


def clean_1004_outputs(directory: Path, selected: list[str]) -> None:
    """Remove only known generated aliases/deselected variants, never O= objects."""
    for name in ("recovery.img", "kernel.uImage", "bundle.bin", *DTBS_1004.values()):
        output_file(directory / name, directory).unlink(missing_ok=True)
    obsolete = [directory / "handoff"]
    variants = directory / "variants"
    inside(variants, directory)
    require(not variants.is_symlink(), "variant output directory must not be a symlink")
    require(not variants.exists() or variants.is_dir(), "variant output path is not a directory")
    obsolete.extend(variants / variant for variant in DTBS_1004 if variant not in selected)
    for path in obsolete:
        inside(path, directory)
        require(not path.is_symlink(), f"generated output directory must not be a symlink: {path}")
        if path.exists():
            require(path.is_dir(), f"generated output path is not a directory: {path}")
            shutil.rmtree(path)


def build_board(workspace: Path, kernel: Path, board: str, jobs: int,
                c51_firmware: Path, variant_1004: str = "all") -> None:
    directory = output_directory(workspace / "out/kernel" / board, workspace / "out")
    out = output_directory(directory / "build", directory)
    variants = ([*DTBS_1004] if variant_1004 == "all" else [variant_1004]) if board == "1004" else []
    require(all(variant in DTBS_1004 for variant in variants), "unknown 1004 variant")
    if board == "1004":
        clean_1004_outputs(directory, variants)
    else:
        output_directory(directory / "handoff", directory)
    stage = directory / "modules"
    inside(stage, directory)
    require(not stage.is_symlink(), "module staging directory must not be a symlink")
    # Only the current board's generated staging is reset. Preserve O= objects.
    if stage.exists():
        require(stage.is_dir(), "module staging path is not a directory")
        shutil.rmtree(stage)
    stage.mkdir()
    for name in ("SHA256SUMS", "metadata.json"):
        output_file(directory / name, directory).unlink(missing_ok=True)
    ec = board == "ec6110t"
    suffix = "-ec6110t-mv310" if ec else "-dvbip-" + board
    config_dir = kernel / "arch/arm64/configs"
    base = config_dir / ("ec6110t_mv310_defconfig" if ec else "hi3798cv200_defconfig")
    fragment = None if ec else config_dir / f"dvbip_{board}_defconfig"
    dtb_names = {variant: DTBS_1004[variant] for variant in variants} if board == "1004" else {
        "": "hi3798mv310-ec6110t.dtb" if ec else f"hi3798cv200-dvbip-{board}-arm64.dtb",
    }
    inputs = [kernel / "Makefile", base]
    inputs.extend(kernel / "arch/arm64/boot/dts/hisilicon" / (name[:-4] + ".dts")
                  for name in dtb_names.values())
    if fragment:
        inputs.append(fragment)
    for path in inputs:
        inside(path, kernel)
        require(path.is_file(), f"missing kernel input: {path}")
    # The board fragment owns its production ABI suffix.
    configured_suffix = read_config(fragment or base).get("CONFIG_LOCALVERSION")
    if configured_suffix is not None:
        suffix = json.loads(configured_suffix)
    expected_prefix = "-ec6110t-mv310" if ec else "-dvbip-" + board
    require(isinstance(suffix, str) and
            re.fullmatch(re.escape(expected_prefix) + r"(?:-[A-Za-z0-9][A-Za-z0-9._+-]*)?", suffix),
            f"LOCALVERSION does not identify board {board}: {suffix!r}")
    make = make_command(kernel, out)
    version = run([*make, "-s", "kernelversion"], capture=True).strip()
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+_-]*", version) is not None,
            f"unexpected kernel source version: {version!r}")
    require(len(version + suffix) < 64, "kernel release exceeds module ABI name limit")
    run([*make, base.name])
    if fragment:
        run(["sh", kernel / "scripts/kconfig/merge_config.sh", "-m", "-O", out, out / ".config", fragment])
    config_args: list[object] = [kernel / "scripts/config", "--file", out / ".config",
        "--set-str", "LOCALVERSION", suffix, "--disable", "LOCALVERSION_AUTO",
        "--enable", "MODULES", "--enable", "MODULE_UNLOAD", "--enable", "MODVERSIONS",
        "--set-str", "INITRAMFS_SOURCE", "", "--set-str", "EXTRA_FIRMWARE", "",
        "--disable", "HI3798MV310_IRQ_PROBE"]
    if ec:
        config_args += ["--enable", "HISILICON_FESTA_PHY", "--enable", "FHANDLE",
                        "--enable", "AUTOFS_FS", "--enable", "SECCOMP"]
    run(config_args)
    run([*make, "olddefconfig"])
    validate_config(out / ".config", suffix, ec)
    run([*make, f"-j{jobs}", "prepare"])
    release = run([*make, "-s", "kernelrelease"], capture=True).strip()
    require(release == version + suffix, f"unexpected board kernel release: {release!r}")
    # Resolve every selected DTB's PM requirements before compiling the one
    # shared Image/module set. Each handoff is still paired with its own DTB.
    dtbs = {}
    soc = ""
    for variant, dtb_name in dtb_names.items():
        run([*make, f"-j{jobs}", "hisilicon/" + dtb_name])
        dtb = out / "arch/arm64/boot/dts/hisilicon" / dtb_name
        # Change only the generated DTB, never the source DTS or any rootfs.
        if ec:
            run(["fdtput", "-t", "s", dtb, "/chosen", "bootargs", EC_BOOTARGS])
        soc = validate_dtb(dtb, board, variant or None)
        dtbs[variant] = dtb
    c51 = read_c51_firmware(c51_firmware) if any(cvpm_enabled(dtb) for dtb in dtbs.values()) else None
    run([*make, f"-j{jobs}", "Image", "modules"])
    image = out / "arch/arm64/boot/Image"
    validate_image(image)
    for source, name in ((image, "Image"), (out / ".config", "kernel.config"),
                         (out / "Module.symvers", "Module.symvers")):
        shutil.copyfile(source, output_file(directory / name, directory))
    boot = workspace / "scripts/boot"
    names = ["Image", "kernel.config", "Module.symvers", "modules.tar.gz", "metadata.json"]
    variant_metadata = {}
    all_flags = set()
    for variant, dtb in dtbs.items():
        destination = output_directory(directory / "variants" / variant, directory) if variant else directory
        handoff = output_directory(destination / "handoff", destination)
        shutil.copyfile(dtb, output_file(destination / dtb.name, destination))
        bundle, flags = build_bundle(image, dtb, board, handoff, boot, c51)
        all_flags.update(flags)
        payload = bundle.read_bytes()
        shutil.copyfile(bundle, output_file(destination / "bundle.bin", destination))
        uimage = make_uimage(payload, release)
        validate_uimage(uimage, payload, release)
        uimage_path = output_file(destination / "kernel.uImage", destination)
        uimage_path.write_bytes(uimage)
        run(["dumpimage", "-l", uimage_path], capture=True)
        container, kind = package_container(board, destination, uimage)
        details = {
            "dtb": (destination / dtb.name).relative_to(directory).as_posix(),
            "container": (destination / container).relative_to(directory).as_posix(),
            "container_size": (destination / container).stat().st_size,
            "container_format": kind, "handoff_flags": flags,
        }
        if variant:
            details.update({"dtb_sha256": sha256(destination / dtb.name),
                            "container_sha256": sha256(destination / container)})
            variant_metadata[variant] = details
        names.extend((destination / name).relative_to(directory).as_posix()
                     for name in (dtb.name, "bundle.bin", "kernel.uImage", container))
    module_count = stage_modules(make, stage, out, release, output_file(directory / "modules.tar.gz", directory))
    inputs.extend(boot / name for name in (
        "hi3798cv200-arm32-to-arm64-bundle.S", "hi3798cv200-arm64-linux-entry.S"))
    if c51 is not None:
        inputs.extend(boot / name for name in (
            "hi3798cv200-pm.c", "hi3798cv200-pm.h", "hi3798cv200-pm.lds",
            "hi3798cv200-pm-monitor.S", "hi3798cv200-pm-bridge.S"))
        if c51_firmware.is_relative_to(workspace):
            inputs.append(c51_firmware)
    inputs.append(Path(__file__).resolve())
    metadata = {
        "board": board, "soc": soc, "kernel_source": "kernel", "kernel_version": version,
        "kernel_release": release, "kernel_abi": "little-endian AArch64",
        "module_count": module_count, "boot_mode": "flashboot", "ram_diagnostics": False,
        "external_firmware_included": c51 is not None,
        "device_boot_verified": False, "source_date_epoch": 0,
        "source_inputs_sha256": {path.relative_to(workspace).as_posix(): sha256(path) for path in inputs},
        "toolchain": {name: run([name, "--version"], capture=True).splitlines()[0]
                      for name in ("clang", "ld.lld", "gcc", "make")},
        "load_addresses": {"bundle": "0x12000000", "Image": "0x02000000",
                           "dtb": "0x10000000", "handoff": "0x11000000", "smp_mailbox": "0x1100f000"},
    }
    if board == "1004":
        metadata.update({"format": "dvbip-kernel-build-v2", "image": "Image",
                         "modules": "modules.tar.gz", "variants": variant_metadata})
    else:
        metadata.update(details)
    if "-DHISTB_CV200_DEEP_PM=1" in all_flags:
        metadata["resident_monitor"] = {
            "abi": "CVPM-1", "cpu_lifecycle": "PSCI-1.0-SMC",
            "soc": "hi3798cv200", "embedded_vendor_c51": True,
            "c51_redistribution_rights": "not granted by monitor source license",
            "system_suspend": "CVPM SiP, not vendor SYSTEM_SUSPEND",
            "c51_provenance": {
                "source": CV200_C51_SOURCE,
                "input": c51_firmware.relative_to(workspace).as_posix()
                         if c51_firmware.is_relative_to(workspace) else "external",
                "bytes": len(c51), "sha256": hashlib.sha256(c51).hexdigest(),
            },
            "deep_standby_hardware_verified": False,
        }
    output_file(directory / "metadata.json", directory).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    (directory / "SHA256SUMS").write_text("".join(f"{sha256(directory / name)}  {name}\n" for name in sorted(names)))
    print(f"Built {release}: {directory}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("kernel", "ffmpeg", "all"), default="kernel",
                        help="build component (default: kernel)")
    parser.add_argument("--boards",
                        help="kernel boards: one, comma-separated, or all: " + ",".join(BOARDS))
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 32))
    parser.add_argument("--1004-variant", dest="variant_1004", choices=("all", *DTBS_1004),
                        help="1004 RF variants to build (default: all)")
    parser.add_argument("--c51-firmware", type=Path,
                        help=f"C51 binary or BSP word array override (default: {CV200_C51_DEFAULT})")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.target in ("kernel", "all") and args.boards is None:
        parser.error("kernel builds require explicit --boards")
    if args.target == "ffmpeg" and any(value is not None for value in
                                      (args.boards, args.variant_1004, args.c51_firmware)):
        parser.error("--boards, --1004-variant and --c51-firmware apply to kernel builds")
    boards = list(BOARDS) if args.boards == "all" else args.boards.split(",") if args.boards else []
    if (args.target != "ffmpeg" and not boards) or any(board not in BOARDS for board in boards) or len(set(boards)) != len(boards):
        parser.error("--boards must be all or a unique comma-separated subset of " + ",".join(BOARDS))
    if args.variant_1004 is not None and "1004" not in boards:
        parser.error("--1004-variant requires board 1004")
    try:
        require(sys.version_info >= (3, 11), "Install Python 3.11 or newer")
        require(platform.system() == "Linux", "Use a Linux build host")
        workspace = Path(__file__).resolve().parents[1]
        require(not any(character.isspace() for character in str(workspace)),
                "Linux Kbuild requires a workspace path without whitespace")
        kernel = workspace / "kernel"
        require(kernel.is_dir() and not kernel.is_symlink() and (kernel / "Makefile").is_file(),
                "Incomplete checkout: workspace/kernel must be a real source directory; run repo sync")
        tools = ["make", "gcc", "g++"]
        if args.target in ("kernel", "all"):
            tools += ["clang", "ld.lld", "llvm-objcopy", "fdtget", "fdtput", "depmod", "modinfo", "dumpimage"]
        if args.target in ("ffmpeg", "all"):
            require((workspace / "ffmpeg/configure").is_file(),
                    "Incomplete checkout: missing ffmpeg/configure; run repo sync")
            tools += ["aarch64-linux-gnu-" + name for name in
                      ("gcc", "g++", "ar", "nm", "ranlib", "strip", "readelf")]
            tools += ["clang", "pkg-config", "dpkg-query"]
        for name in tools:
            require(shutil.which(name, path=ENV["PATH"]) is not None,
                    f"missing command {name}; run scripts/install-deps.sh")
        firmware = (args.c51_firmware or workspace / CV200_C51_DEFAULT).expanduser().resolve()
        for board in boards:
            build_board(workspace, kernel, board, args.jobs, firmware, args.variant_1004 or "all")
        if args.target in ("ffmpeg", "all"):
            subprocess.run([sys.executable, str(workspace / "scripts/ffmpeg.py"),
                            "--workspace", str(workspace), "--jobs", str(args.jobs)],
                           env=build_environment(), check=True)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
