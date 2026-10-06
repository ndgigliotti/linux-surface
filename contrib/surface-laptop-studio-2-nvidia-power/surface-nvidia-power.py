"""TINCAN's Surface/NVIDIA auxiliary-power workaround (driver 595.71.05).

The unbound Surface SGPC sends D5 on each GPU wake, selecting auxiliary P4.
Use the published RM controls to report the real Linux power source and clear
that auxiliary restriction. Native NVIDIA power/thermal limits still apply.
Only open a GPU observed active; free every RM handle after each update.
"""
import argparse
import ctypes as c
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

GPU = Path("/sys/bus/pci/devices/0000:01:00.0")
AC = Path("/sys/class/power_supply/ADP1/online")
SGPC = Path("/sys/bus/platform/devices/MSHW0216:00")
FIRMWARE_HASH = "86e842eae5254dbb6e5d04bd298db01d9b4505d4cc357d6b781afb40258dea99"
U32, U64 = c.c_uint32, c.c_uint64
DRIVER = "595.71.05"
INTERVAL = 0.2
GUARD_EXIT_STATUS = 78
NVIDIA_NODES = (Path("/dev/nvidiactl"), Path("/dev/nvidia0"))
stopping = False


class Alloc(c.Structure):
    _fields_ = [(name, U32) for name in ("root", "parent", "new", "klass")] + [
        ("params", U64), ("size", U32), ("status", U32)
    ]


class Control(c.Structure):
    _fields_ = [(name, U32) for name in ("client", "object", "cmd", "flags")] + [
        ("params", U64), ("size", U32), ("status", U32)
    ]


class Device(c.Structure):
    _fields_ = [(name, U32) for name in
                ("id", "share", "target_client", "target_device", "flags")] + [
        ("va_size", U64), ("va_start", U64), ("va_limit", U64), ("va_mode", U32)
    ]


class Free(c.Structure):
    _fields_ = [(name, U32) for name in ("root", "parent", "old", "status")]


def log(**fields):
    print(json.dumps(fields), flush=True)


def text(path):
    return Path(path).read_text().strip()


def read_power_source():
    online = text(AC)
    if online not in ("0", "1"):
        raise RuntimeError("Unknown AC state")
    return online


def ioctl(fd, nr, params):
    data = bytearray(c.string_at(c.addressof(params), c.sizeof(params)))
    fcntl.ioctl(fd, 0xC0000000 | (len(data) << 16) | (ord("F") << 8) | nr, data)
    c.memmove(c.addressof(params), bytes(data), len(data))
    status = getattr(params, "status", 0)
    if status:
        raise RuntimeError(f"RM ioctl {nr:#x} status={status:#x}")


def allocate(fd, client, parent, handle, klass, params=None):
    req = Alloc(client, parent, handle, klass,
                c.addressof(params) if params is not None else 0,
                c.sizeof(params) if params is not None else 0, 0)
    ioctl(fd, 0x2B, req)
    return req.new


@contextmanager
def rm_device():
    ctl = gpu = None
    client = 0
    try:
        ctl = os.open("/dev/nvidiactl", os.O_RDWR | os.O_CLOEXEC)
        gpu = os.open("/dev/nvidia0", os.O_RDWR | os.O_CLOEXEC)
        ioctl(gpu, 201, c.c_int(ctl))
        client = allocate(ctl, 0, 0, 0, 0x41)
        device = allocate(ctl, client, client, 0x1001, 0x80, Device())
        subdevice = allocate(ctl, client, device, 0x1002, 0x2080, U32(0))
        yield ctl, client, subdevice
    finally:
        try:
            if client:
                ioctl(ctl, 0x29, Free(client, 0, client, 0))
        finally:
            for fd in (gpu, ctl):
                if fd is not None:
                    os.close(fd)


def control(fd, client, obj, cmd, state):
    value = U32(state)
    ioctl(fd, 0x2A, Control(client, obj, cmd, 0,
                           c.addressof(value), c.sizeof(value), 0))
    return value.value


def verify_hardware():
    if tuple(c.sizeof(kind) for kind in (Alloc, Control, Device, Free)) != (32, 32, 56, 16):
        raise RuntimeError("Unexpected RM parameter sizes")
    if text("/sys/class/dmi/id/product_name") != "Surface Laptop Studio 2":
        raise RuntimeError("Unsupported laptop")
    for name, expected in (("vendor", "0x10de"), ("device", "0x28a0"),
                           ("subsystem_vendor", "0x1414"), ("subsystem_device", "0x0083")):
        if text(GPU / name) != expected:
            raise RuntimeError(f"Unsupported GPU {name}")
    if DRIVER not in text("/proc/driver/nvidia/version"):
        raise RuntimeError(f"RM ABI has only been validated with NVIDIA {DRIVER}")
    if not SGPC.exists() or (SGPC / "driver").exists():
        raise RuntimeError("SGPC missing or already controlled by another driver")
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest()
              for path in Path("/sys/firmware/acpi/tables").glob("SSDT*")]
    if FIRMWARE_HASH not in hashes:
        raise RuntimeError("GPU firmware differs from the diagnosed table; review before applying")


def active_gpu_epoch():
    # Sysfs reads do not wake PCI devices. Suspended-time changes also detect a
    # rapid D3cold cycle that occurs entirely between polling intervals.
    if text(GPU / "power/runtime_status") != "active" or text(GPU / "power_state") != "D0":
        return None
    return text(GPU / "power/runtime_suspended_time")


def active_epoch():
    epoch = active_gpu_epoch()
    return None if epoch is None else (epoch, read_power_source())


def apply_power_source():
    with rm_device() as (fd, client, obj):
        # Re-read after allocation: the charger may have changed while RM was
        # resuming. SET_POWERSTATE retains NVIDIA's native AC/battery policy.
        online = read_power_source()
        source = 0 if online == "1" else 1
        control(fd, client, obj, 0x2080205B, source)
        control(fd, client, obj, 0x20802092, 0)
        reported = control(fd, client, obj, 0x2080205A, 0xFFFFFFFF)
        if reported != source:
            raise RuntimeError(f"Power-source readback mismatch {source} != {reported}")
        log(event="applied", source="AC" if source == 0 else "battery", auxiliary="P0")
    return online


def restore_firmware_restriction():
    if active_gpu_epoch() is None:
        return  # Do not wake an idle device just to restore the next wake's D5.
    with rm_device() as (fd, client, obj):
        try:
            online = read_power_source()
        except (OSError, RuntimeError) as error:
            # Restore the conservative auxiliary restriction without inventing
            # a source when the adapter disappeared or its read failed.
            log(event="restore_source_unavailable", error=str(error))
        else:
            try:
                control(fd, client, obj, 0x2080205B, 0 if online == "1" else 1)
            except (OSError, RuntimeError) as error:
                log(event="restore_source_failed", error=str(error))
        control(fd, client, obj, 0x20802092, 4)
        log(event="restored", auxiliary="P4")


def main():
    if not wait_for_devices():
        log(event="stopped")
        return 0
    try:
        verify_hardware()
    except (OSError, RuntimeError) as error:
        log(event="rejected", error=str(error))
        return GUARD_EXIT_STATUS
    try:
        read_power_source()
    except (OSError, RuntimeError) as error:
        # ADP1 probes asynchronously; source availability is not identity.
        log(event="source_unavailable", error=str(error))
        return 1
    previous = None
    log(event="started", driver=DRIVER, interval_ms=int(INTERVAL * 1000))
    try:
        while not stopping:
            epoch = active_epoch()
            if epoch is None:
                previous = None
            elif epoch != previous:
                applied_online = apply_power_source()
                # Allocation may have observed a different source than epoch.
                previous = (epoch[0], applied_online)
            time.sleep(INTERVAL)
    finally:
        try:
            restore_firmware_restriction()
        except (OSError, RuntimeError) as error:
            log(event="restore_failed", error=str(error))
        log(event="stopped")
    return 0


def wait_for_devices():
    # Stat only. Waiting in this startup process does not consume the service's
    # restart allowance and does not open either NVIDIA character device.
    waiting_logged = False
    while not stopping:
        if all(node.is_char_device() for node in NVIDIA_NODES):
            return True
        if not waiting_logged:
            log(event="waiting_for_devices")
            waiting_logged = True
        time.sleep(INTERVAL)
    return False


def request_stop(signum, frame):
    # Python executes this in the main thread. Avoid Event/Condition locks:
    # a handler can interrupt the same thread while it holds their lock.
    global stopping
    stopping = True


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    sys.exit(main())
