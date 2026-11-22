import logging
import os
import pathlib
import signal
import subprocess
import time
from typing import Dict, Optional

log = logging.getLogger(__name__)


def xen_is_vm_running(vm_name: str) -> bool:
    result = subprocess.run(["xl", "list", vm_name], capture_output=True)
    if result.returncode == 0:
        return True
    elif b"is an invalid domain identifier" in result.stderr:
        return False
    else:
        raise RuntimeError(f"Unexpected xl list output: {result.stderr}")


def xen_create_vm(
    vm_name: str,
    config_path: str,
    pause: bool = False,
    timeout: Optional[float] = None,
) -> None:
    try:
        subprocess.run(
            ["xl", "create", *(["-p"] if pause else []), config_path],
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Failed to launch VM {vm_name}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Failed to launch VM {vm_name} within {timeout} seconds")


def xen_unpause_vm(vm_name: str, timeout: Optional[float] = None) -> None:
    try:
        subprocess.run(["xl", "unpause", vm_name], check=True, timeout=timeout)
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Failed to unpause VM {vm_name}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Failed to unpause VM {vm_name} within {timeout} seconds")


def xen_restore_vm(
    vm_name: str,
    config_path: str,
    snapshot_path: str,
    pause: bool = False,
) -> None:
    try:
        subprocess.run(
            ["xl", "restore", *(["-p"] if pause else []), config_path, snapshot_path],
            check=True,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Failed to restore VM {vm_name}")


def xen_save_vm(
    vm_name: str,
    snapshot_path: str,
    pause: bool = False,
) -> None:
    try:
        subprocess.run(
            ["xl", "save", *(["-p"] if pause else []), vm_name, snapshot_path],
            check=True,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Failed to save VM {vm_name}")


def xen_destroy_vm(vm_name: str) -> None:
    try:
        subprocess.run(["xl", "destroy", vm_name], check=True)
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Failed to pause VM {vm_name}")


def xen_get_domid(vm_name: str) -> int:
    output = subprocess.check_output(["xl", "domid", vm_name], text=True)
    return int(output.strip())


def xen_get_device_model_pid(domid: int) -> Optional[int]:
    """Returns the PID of the QEMU device model libxl started for the domain"""
    try:
        output = subprocess.check_output(
            ["xenstore-read", f"/local/domain/{domid}/image/device-model-pid"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return int(output.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _is_device_model_process(pid: int, domid: int) -> bool:
    """Checks that pid is still the QEMU device model of domid (and not a reused PID)"""
    try:
        args = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    try:
        return args[args.index(b"-xen-domid") + 1] == str(domid).encode()
    except (ValueError, IndexError):
        return False


def _xen_domid_exists(domid: int) -> bool:
    result = subprocess.run(["xl", "list", str(domid)], capture_output=True)
    return result.returncode == 0


def xen_kill_device_model(
    vm_name: str, domid: int, pid: int, grace_period: float = 5, timeout: float = 30
) -> None:
    """
    Makes sure the device model of an already destroyed domain is gone.

    On 'xl destroy' libxl only sends SIGHUP to the device model, which QEMU handles
    in its main loop. If that main loop is stuck (e.g. deadlocked), QEMU never exits,
    the domain stays as a '(null)' dying domain and the next restore of the VM fails.
    In that case kill it with SIGKILL and wait for Xen to finish destroying the domain.
    """
    deadline = time.monotonic() + grace_period
    while _is_device_model_process(pid, domid) and time.monotonic() < deadline:
        time.sleep(0.2)
    if not _is_device_model_process(pid, domid):
        return

    log.warning(
        "Device model of %s (pid %d, domid %d) did not exit after xl destroy, killing it",
        vm_name,
        pid,
        domid,
    )
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (
        _is_device_model_process(pid, domid) or _xen_domid_exists(domid)
    ):
        time.sleep(0.5)
    if _xen_domid_exists(domid):
        log.error(
            "Domain %d of %s still exists after killing its device model",
            domid,
            vm_name,
        )


def parse_xen_commandline(xen_commandline: str) -> Dict[str, str]:
    opts = xen_commandline.split(" ")
    elements = {}
    for opt in opts:
        if not opt.strip():
            continue

        if "=" not in opt:
            elements[opt] = "1"
        else:
            k, v = opt.split("=", 1)
            elements[k] = v

    return elements


def get_xen_info() -> Dict[str, str]:
    xl_info_out = subprocess.check_output(["xl", "info"], text=True)
    xl_info_lines = xl_info_out.strip().split("\n")

    elements = {}
    for line in xl_info_lines:
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        elements[k] = v
    return elements


def xen_insert_cd(domain, drive, iso):
    subprocess.run(["xl", "cd-insert", domain, drive, iso], check=True)


def xen_eject_cd(domain, drive):
    subprocess.run(["xl", "cd-eject", domain, drive], check=True)
