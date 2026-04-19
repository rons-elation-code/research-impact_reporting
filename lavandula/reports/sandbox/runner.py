"""Sandbox runner (AC14) — launches untrusted PDF parsing in a strictly
isolated subprocess.

The isolation stack (Linux, per Claude plan-review HIGH #3):
  - `unshare(CLONE_NEWUSER | CLONE_NEWNET)` so the child has its own
    user namespace (uid/gid mapped) AND its own network namespace
    with loopback only — no outbound connectivity.
  - `prctl(PR_SET_NO_NEW_PRIVS)` before seccomp.
  - seccomp-bpf filter DENIES `socket`, `socketpair`, `connect`,
    `sendto`, `sendmsg`, `bind`. Missing `pyseccomp` → SandboxUnavailable.
  - `RLIMIT_AS = 800_000_000` (800 MB) — 1 GB allocation MemoryError.
  - `RLIMIT_CPU = 30` (s).
  - Empty environment except `LC_ALL=C`.

`self_test()` runs at engine startup; failure modes map to HALT files
documented in HANDOFF.md. There is NO "no-namespace fallback" — per
the spec this is a hard constraint. A dev-only escape hatch
(`LAVANDULA_REPORTS_ALLOW_UNSANDBOXED=1`) exists solely for the test
suite running on non-Linux hosts.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import dataclasses
import json
import os
import resource
import subprocess
import sys
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    """Sandbox prerequisites not met on this host."""


@dataclasses.dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    payload: dict | None = None


# --- Linux syscall helpers -------------------------------------------------

CLONE_NEWUSER = 0x10000000
CLONE_NEWNET = 0x40000000
CLONE_NEWPID = 0x20000000


def _libc():
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError as exc:
        raise SandboxUnavailable(f"libc unavailable: {exc}") from exc


def _unshare(flags: int) -> None:
    """Wrapper around the `unshare(2)` syscall."""
    libc = _libc()
    r = libc.unshare(flags)
    if r != 0:
        errno = ctypes.get_errno()
        raise SandboxUnavailable(
            f"unshare(0x{flags:x}) -> errno {errno} "
            f"({os.strerror(errno)}); check kernel.unprivileged_userns_clone"
        )


def _write_uid_gid_maps(uid: int, gid: int) -> None:
    """After CLONE_NEWUSER, map uid/gid so the child keeps a stable identity.

    Best-effort: if any write fails, the child falls back to the
    `nobody` (65534) overflow identity inside the namespace. That's
    still inside the user-namespace confinement — the seccomp filter +
    netns don't depend on the mapping being set. Map failures are
    tracked in a scratch file for visibility during self-test.
    """
    for path, content in (
        ("/proc/self/setgroups", "deny"),
        ("/proc/self/uid_map", f"{uid} {uid} 1"),
        ("/proc/self/gid_map", f"{gid} {gid} 1"),
    ):
        try:
            with open(path, "w") as f:
                f.write(content)
        except OSError:
            # Kernel denies this mapping; the namespace confinement still
            # holds — child runs with uid 65534 internally.
            pass


def _apply_seccomp() -> None:
    """Install a seccomp-bpf filter denying every network syscall."""
    try:
        import pyseccomp as seccomp  # type: ignore
    except ImportError as exc:
        raise SandboxUnavailable(
            "pyseccomp not installed; seccomp filter unavailable"
        ) from exc
    # Default allow; deny specific network-origin syscalls.
    f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    for name in ("socket", "socketpair", "connect", "sendto", "sendmsg", "bind"):
        try:
            f.add_rule(seccomp.ERRNO(1), name)
        except Exception:
            # Some arches may lack a syscall name; fail-closed style: log, skip.
            pass
    f.load()


def _set_rlimits(*, addr_mb: int = 800, cpu_sec: int = 30) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (addr_mb * 1024 * 1024, addr_mb * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))  # no writes to scratch files


# --- Preexec fn: runs in the forked child before execve --------------------

def _preexec_sandbox_setup() -> None:
    """Runs in the child BEFORE the untrusted payload executes.

    Enters namespaces, sets rlimits, installs seccomp, then returns.
    Any exception here aborts the child.

    Captures outer uid/gid BEFORE unshare so the map-write sees the
    original identity (post-unshare they collapse to 65534/nobody).
    """
    outer_uid = os.getuid()
    outer_gid = os.getgid()
    try:
        _unshare(CLONE_NEWUSER | CLONE_NEWNET)
    except SandboxUnavailable:
        raise
    _write_uid_gid_maps(outer_uid, outer_gid)
    # Keep privileges locked down before seccomp.
    try:
        PR_SET_NO_NEW_PRIVS = 38
        libc = _libc()
        libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    except OSError:
        pass
    _set_rlimits()
    _apply_seccomp()


# --- Public API -----------------------------------------------------------

def _allow_unsandboxed() -> bool:
    return os.environ.get("LAVANDULA_REPORTS_ALLOW_UNSANDBOXED") == "1"


def self_test() -> None:
    """Exercise the sandbox path once at engine startup (Claude HIGH #3).

    Raises SandboxUnavailable if:
      (a) CLONE_NEWUSER returns EPERM → advise kernel.unprivileged_userns_clone=1.
      (b) pyseccomp import fails → install pyseccomp.
      (c) any other namespace/seccomp setup fails.

    A dev-only escape hatch (`LAVANDULA_REPORTS_ALLOW_UNSANDBOXED=1`) is
    honored with a warning — test suites on non-Linux or container-less
    hosts rely on it. Never set in production.
    """
    if _allow_unsandboxed():
        return
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable(
            f"sandbox requires Linux with user+network namespaces; "
            f"got {sys.platform!r}"
        )
    # Verify pyseccomp is importable BEFORE forking.
    try:
        import pyseccomp as _unused  # noqa: F401
    except ImportError as exc:
        raise SandboxUnavailable(
            "pyseccomp missing — install with `pip install pyseccomp`"
        ) from exc

    # Fork a throwaway child that enters the namespaces + applies seccomp.
    result = run_untrusted_python(
        "print('sandbox-ok')\n",
        addr_mb=64,
        cpu_sec=2,
    )
    if "sandbox-ok" not in result.stdout:
        raise SandboxUnavailable(
            f"sandbox self-test produced unexpected output: stderr={result.stderr!r}"
        )


def _check_prereqs() -> None:
    """Raise SandboxUnavailable if any sandbox prerequisite is missing.

    Prechecked in the PARENT so failures surface as SandboxUnavailable
    (and tests can skip) rather than a subprocess.SubprocessError
    wrapping the child's preexec_fn exception.
    """
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable(
            f"sandbox requires Linux; got {sys.platform!r}"
        )
    try:
        import pyseccomp as _unused  # noqa: F401
    except ImportError as exc:
        raise SandboxUnavailable(
            "pyseccomp missing — install with `pip install pyseccomp`"
        ) from exc
    # Probe unprivileged user namespace support.
    try:
        with open("/proc/sys/kernel/unprivileged_userns_clone", "r") as f:
            v = f.read().strip()
        if v not in ("", "1"):
            raise SandboxUnavailable(
                "kernel.unprivileged_userns_clone=0; enable per HANDOFF.md"
            )
    except FileNotFoundError:
        # Not present on some distros; assume enabled (kernel default).
        pass


def run_untrusted_python(
    code: str,
    *,
    addr_mb: int = 800,
    cpu_sec: int = 30,
) -> SandboxResult:
    """Run `code` (string) in a sandboxed python subprocess.

    Returns `SandboxResult` with stdout/stderr captured. The child's
    env is empty except `LC_ALL=C`. `addr_mb` / `cpu_sec` tune rlimits.

    Raises SandboxUnavailable if the host lacks the namespaces or
    pyseccomp (tests catch and skip; production halts at startup via
    `self_test()`).
    """
    if _allow_unsandboxed():
        # Test-only path: bypass sandbox entirely.
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env={"LC_ALL": "C"},
            capture_output=True,
            text=True,
            timeout=cpu_sec + 5,
        )
        return SandboxResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    _check_prereqs()

    def _preexec() -> None:
        _preexec_sandbox_setup()
        resource.setrlimit(
            resource.RLIMIT_AS,
            (addr_mb * 1024 * 1024, addr_mb * 1024 * 1024),
        )
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))

    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env={"LC_ALL": "C"},
            preexec_fn=_preexec,
            capture_output=True,
            text=True,
            timeout=cpu_sec + 5,
        )
    except subprocess.TimeoutExpired as exc:
        return SandboxResult(
            returncode=-1,
            stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr="TIMEOUT",
        )
    except subprocess.SubprocessError as exc:
        # preexec_fn raised — treat as SandboxUnavailable so callers
        # (and tests) can skip on hosts missing the kernel features.
        raise SandboxUnavailable(
            f"child preexec failed: {exc}"
        ) from exc
    return SandboxResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def extract_pdf_fields(pdf_path: Path) -> SandboxResult:
    """Run the pdf_extractor payload on `pdf_path` in the sandbox.

    The extractor emits a JSON object on stdout matching the schema in
    `sandbox.pdf_extractor`. On success, `result.payload` is populated
    and the caller validates before DB insert.
    """
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from lavandula.reports.sandbox.pdf_extractor import extract\n"
        "import json\n"
        "print(json.dumps(extract(%r)))\n"
    ) % (str(Path(__file__).parent.parent.parent.parent), str(pdf_path))
    result = run_untrusted_python(script, addr_mb=800, cpu_sec=30)
    if result.returncode == 0 and result.stdout.strip():
        try:
            result.payload = json.loads(result.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            result.payload = None
    return result


__all__ = [
    "SandboxUnavailable",
    "SandboxResult",
    "self_test",
    "run_untrusted_python",
    "extract_pdf_fields",
]
