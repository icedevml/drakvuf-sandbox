import logging
import os
import pathlib
import signal
import subprocess
import time
from typing import Dict, List, Optional, Tuple

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


def _find_device_models(vm_name: str) -> List[Tuple[int, int]]:
    """
    Returns (pid, domid) of QEMU device model processes started by libxl for vm_name.
    libxl starts them as 'qemu-system-i386 -xen-domid <domid> ... -name <vm_name> ...'
    """
    found = []
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [arg.decode(errors="replace") for arg in args]
        if not args or not args[0].endswith("qemu-system-i386"):
            continue
        try:
            name = args[args.index("-name") + 1]
            domid = int(args[args.index("-xen-domid") + 1])
        except (ValueError, IndexError):
            continue
        if name == vm_name:
            found.append((int(proc.name), domid))
    return found


def _xen_domid_exists(domid: int) -> bool:
    result = subprocess.run(["xl", "list", str(domid)], capture_output=True)
    return result.returncode == 0


def xen_kill_leaked_device_models(vm_name: str, timeout: float = 30) -> None:
    """
    Kills QEMU device models left behind by a destroyed vm_name domain.

    When QEMU hangs (e.g. deadlocks in its main loop), 'xl destroy' can't finish
    tearing the domain down: it stays as a '(null)' dying domain held by the stuck
    device model, and the next restore of the VM fails until the process is killed.
    Only device models of vm_name whose domain is no longer vm_name are killed.
    """
    try:
        live_domid = xen_get_domid(vm_name)
    except (subprocess.CalledProcessError, ValueError):
        live_domid = None

    leaked = [
        (pid, domid)
        for pid, domid in _find_device_models(vm_name)
        if domid != live_domid
    ]
    for pid, domid in leaked:
        log.warning(
            "Killing leaked device model of %s (pid %d, domid %d)", vm_name, pid, domid
        )
        try:
            # SIGKILL: a deadlocked QEMU main loop doesn't handle SIGTERM
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + timeout
    for pid, domid in leaked:
        while time.monotonic() < deadline and (
            pathlib.Path(f"/proc/{pid}").exists() or _xen_domid_exists(domid)
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
