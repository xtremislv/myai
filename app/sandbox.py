"""Run Python the assistant wrote, to analyse data (pandas, numpy) and make dashboards.

This host is a container without user namespaces, seccomp tools or firewall rights, so the
isolation is layered rather than a kernel sandbox:

1. The code runs as the unprivileged user `nobody`, which can't read /root (the app, .env,
   the model, the vault key) or anything else that isn't world-readable.
2. Its own interpreter (/opt/agent-sandbox, Python with numpy and pandas), started with -I
   (no environment variables, no user site, the script's folder not on the import path) and a
   clean environment with no proxy settings.
3. Limits set before any of its code runs: CPU seconds, memory, file size, open files,
   processes; plus a wall-clock timeout that kills the whole process group.
4. A Python audit hook, also installed first, refuses sockets, starting processes, ctypes,
   writing outside the run's own folder, and reading or listing anything but that folder, the
   Python install and shared system libraries. Audit hooks are defence in depth, not a proof:
   code written to get around them could reach services on 127.0.0.1 as `nobody` (Redis
   has no password here). Inputs are the person's own files and the model's own code, but a
   web page or email could try to steer that code, which is why the layers above exist.

Each run gets a fresh folder that is deleted afterwards. Input files are copied into
input/ (and next to the code), anything the code writes to output/ is returned, and a dashboard.json it writes
becomes a dashboard.
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

PYTHON = os.environ.get("SANDBOX_PYTHON", "/opt/agent-sandbox/bin/python")
BASE = Path(os.environ.get("SANDBOX_DIR", "/var/lib/agent-sandbox"))
USER = "nobody"
WALL_SECONDS = 60
CPU_SECONDS = 40
MEMORY_BYTES = 2 * 1024**3
FILE_BYTES = 20 * 1024**2
MAX_INPUT_BYTES = 25 * 1024**2
MAX_OUTPUT_FILES = 10
MAX_OUTPUT_BYTES = 5 * 1024**2
MAX_CODE_CHARS = 20_000
_slots = threading.BoundedSemaphore(2)

BOOT = r'''
import os, sys, resource
work = os.path.realpath(os.getcwd())
L = resource
for limit, value in ((L.RLIMIT_CPU, %(cpu)d), (L.RLIMIT_AS, %(mem)d), (L.RLIMIT_FSIZE, %(fsize)d),
                     (L.RLIMIT_NOFILE, 64), (L.RLIMIT_NPROC, 64), (L.RLIMIT_CORE, 0)):
    resource.setrlimit(limit, (value, value))
for name in ("numpy", "pandas", "json", "csv", "math", "statistics", "datetime", "collections",
             "itertools", "re", "decimal", "fractions", "random", "textwrap", "string"):
    try:
        __import__(name)
    except Exception:
        pass

BLOCKED = ("socket.", "subprocess.", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
           "os.system", "os.kill", "os.killpg", "os.setuid", "os.setgid", "ctypes.", "pty.", "_posixsubprocess",
           "gc.get_referrers", "gc.get_referents", "gc.get_objects", "sys.addaudithook", "resource.setrlimit",
           "os.putenv", "os.unsetenv", "os.chroot", "fcntl.ioctl", "sys.remote_exec", "webbrowser.")
PATH_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.link",
               "os.symlink", "os.truncate", "os.utime", "shutil.rmtree", "shutil.copyfile", "shutil.move",
               "os.chdir", "os.chflags", "os.lchown", "os.lchmod"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
LIST_EVENTS = {"os.listdir", "os.scandir", "glob.glob", "os.readlink"}

def _under(real, roots):
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in roots)

# What the code may read: its own folder, the Python install, shared libraries and data (time
# zones, certificates), and facts about its own process and the CPU. Nothing else, even where a
# file is world-readable: other people's data must never be one open() away.
READ_ROOTS = tuple({work, *(os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)),
                    "/usr/lib", "/usr/lib64", "/usr/local/lib", "/usr/share/zoneinfo", "/etc/localtime",
                    "/etc/ssl", "/dev/null", "/dev/urandom", "/proc/self", os.path.realpath("/proc/self"),
                    "/proc/cpuinfo", "/proc/meminfo", "/sys/devices/system/cpu", "/sys/fs/cgroup"})

def inside(path):
    try:
        return _under(os.path.realpath(os.fsdecode(path)), (work,))
    except Exception:
        return False

def readable(path):
    try:
        return _under(os.path.realpath(os.fsdecode(path)), READ_ROOTS)
    except Exception:
        return False

def hook(event, args):
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int):
            return
        writing = (mode is not None and any(c in str(mode) for c in "wax+")) or bool((flags or 0) & WRITE_FLAGS)
        if writing and not inside(path):
            raise PermissionError("the code may only write inside its own folder (use output/)")
        if not writing and not readable(path):
            raise PermissionError("the code may only read its own files (input/ and its folder)")
        return
    if event in LIST_EVENTS:
        target = args[0] if args else "."
        if isinstance(target, (str, bytes, os.PathLike)) and not readable(target or "."):
            raise PermissionError("the code may only look inside its own folder")
        return
    if event.startswith(BLOCKED):
        raise PermissionError(f"not allowed in the sandbox: {event} (no network, no other programs)")
    if event in PATH_EVENTS:
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)) and not inside(value):
                raise PermissionError("the code may only change files inside its own folder")

sys.addaudithook(hook)
sys.argv = ["main.py"]
with open("main.py", encoding="utf-8") as f:
    source = f.read()
exec(compile(source, "main.py", "exec"), {"__name__": "__main__", "__builtins__": __builtins__})
'''


@dataclass
class Result:
    ok: bool
    stdout: str
    stderr: str
    outputs: dict[str, bytes] = field(default_factory=dict)
    dashboard: str | None = None  # the text of dashboard.json, if the code wrote one
    timed_out: bool = False


class SandboxUnavailable(RuntimeError):
    pass


def available() -> bool:
    return Path(PYTHON).exists()


def _owner() -> tuple[int, int]:
    entry = pwd.getpwnam(USER)
    return entry.pw_uid, entry.pw_gid


def _safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name.strip().lstrip(".")
    return "".join(c if c.isalnum() or c in "-_. " else "_" for c in base)[:100] or "file"


def run(code: str, inputs: dict[str, bytes] | None = None) -> Result:
    """Run `code` with `inputs` (name -> bytes, placed in input/). Blocks up to WALL_SECONDS."""
    if not available():
        raise SandboxUnavailable("the code runner isn't installed on this server (scripts/setup_sandbox.sh)")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"the code is too long (over {MAX_CODE_CHARS} characters)")
    inputs = inputs or {}
    if sum(len(v) for v in inputs.values()) > MAX_INPUT_BYTES:
        raise ValueError("the input files are too big (25 MB in all)")
    if not _slots.acquire(timeout=30):
        raise SandboxUnavailable("the code runner is busy; try again in a moment")
    try:
        return _run(code, inputs)
    finally:
        _slots.release()


def _run(code: str, inputs: dict[str, bytes]) -> Result:
    uid, gid = _owner()
    BASE.mkdir(mode=0o711, parents=True, exist_ok=True)
    os.chmod(BASE, 0o711)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=BASE))
    try:
        (work / "input").mkdir()
        (work / "output").mkdir()
        for name, data in inputs.items():  # in input/, and beside main.py for code that forgets the folder
            (work / "input" / _safe_name(name)).write_bytes(data)
            if _safe_name(name) not in ("main.py", "boot.py", "dashboard.json"):
                (work / _safe_name(name)).write_bytes(data)
        (work / "main.py").write_text(code, encoding="utf-8")
        (work / "boot.py").write_text(BOOT % {"cpu": CPU_SECONDS, "mem": MEMORY_BYTES, "fsize": FILE_BYTES},
                                      encoding="utf-8")
        for path in (work, *work.rglob("*")):
            os.chown(path, uid, gid)
        os.chmod(work, 0o700)
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1", "MPLCONFIGDIR": str(work),
        }
        with open(work / ".stdout", "wb") as out, open(work / ".stderr", "wb") as err:
            proc = subprocess.Popen(
                [PYTHON, "-I", "-B", "boot.py"], cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=out,
                stderr=err, user=uid, group=gid, extra_groups=[], process_group=0, close_fds=True,
            )
            timed_out = False
            try:
                proc.wait(timeout=WALL_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                proc.wait()
        stdout = (work / ".stdout").read_bytes()[:200_000].decode("utf-8", "replace")
        stderr = (work / ".stderr").read_bytes()[-20_000:].decode("utf-8", "replace")
        outputs: dict[str, bytes] = {}
        total = 0
        for path in sorted((work / "output").rglob("*")):
            if path.is_symlink() or not path.is_file() or len(outputs) >= MAX_OUTPUT_FILES:
                continue
            data = path.read_bytes()
            total += len(data)
            if total > MAX_OUTPUT_BYTES:
                break
            outputs[str(path.relative_to(work / "output"))] = data
        dash_path = work / "dashboard.json"
        dashboard = None
        if dash_path.is_file() and not dash_path.is_symlink():
            dashboard = dash_path.read_bytes()[:200_000].decode("utf-8", "replace")
        return Result(ok=proc.returncode == 0 and not timed_out, stdout=stdout, stderr=stderr,
                      outputs=outputs, dashboard=dashboard, timed_out=timed_out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
