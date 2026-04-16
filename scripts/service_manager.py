#!/usr/bin/env python3
"""Service manager for agent background processes.

Services that listen on a port should use 8082-8090.
Port is optional — polling/background services don't need one.

Usage:
    python3 service_manager.py list                          # Show all managed services
    python3 service_manager.py start <name> [<port>] [--auto-start] -- <cmd...>  # Start a service
    python3 service_manager.py stop <name>                   # Stop a service (keeps entry)
    python3 service_manager.py remove <name>                 # Stop and remove a service
    python3 service_manager.py restart <name>                # Restart a service
    python3 service_manager.py status <name>                 # Check one service
    python3 service_manager.py health                        # Health check all services
    python3 service_manager.py cleanup                       # Remove dead entries
    python3 service_manager.py auto-start                    # Start all services with auto_start:true that are not running
"""

import fcntl, json, os, signal, subprocess, sys, tempfile, time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
import psutil

SERVICES_FILE = Path("/agent/memory/services.json")
STATE_FILE = Path("/agent/memory/state.json")
HEARTBEAT_DIR = Path("/agent/memory/heartbeats")
ALLOWED_PORTS = range(8082, 8091)  # 8082-8090
# How long (seconds) before a service heartbeat is considered stale.
# Services should write heartbeats at least this often.
HEARTBEAT_STALE_THRESHOLD = 600  # 10 minutes


_LOCK_PATH = str(SERVICES_FILE) + ".lock"


@contextmanager
def _flock(timeout=10):
    """Acquire an exclusive file lock on services.json for safe read-modify-write."""
    with open(_LOCK_PATH, "a+") as lock_f:
        try:
            # Non-blocking attempt with manual timeout
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Could not acquire services.json lock within {timeout}s"
                        )
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass


def _load():
    if SERVICES_FILE.exists():
        try:
            return json.loads(SERVICES_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save(services):
    fd, tmp = tempfile.mkstemp(dir=str(SERVICES_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(services, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(SERVICES_FILE))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_pid(name, pid):
    """Send SIGTERM, wait up to 3s, then SIGKILL. Returns True on success."""
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        print(f"Stopped '{name}' (PID {pid})")
        return True
    except Exception as e:
        print(f"Error stopping '{name}': {e}")
        return False


def _find_orphan_pids(command, exclude_pids=None):
    """Find PIDs of running processes matching the service command.

    Uses psutil instead of pgrep for native Python process enumeration.
    Returns a list of (pid, cmdline) tuples.
    """
    if not command:
        return []

    # Extract identifying token from the command list
    non_flag = [a for a in command if not a.startswith("-")]
    pattern = None
    # Prefer: first non-interpreter arg ending with a script extension
    for arg in non_flag[1:]:
        if arg.endswith((".py", ".sh", ".js")):
            pattern = arg
            break
    # Fallback: first non-interpreter arg containing "/" (a path)
    if not pattern:
        for arg in non_flag[1:]:
            if "/" in arg:
                pattern = arg
                break
    # Last resort: second non-flag token
    if not pattern:
        pattern = (
            non_flag[1] if len(non_flag) >= 2 else (non_flag[0] if non_flag else None)
        )
    if not pattern:
        return []

    exclude = exclude_pids or set()
    my_pid = os.getpid()
    orphans = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info["pid"]
            cmdline_list = proc.info["cmdline"]
            if not cmdline_list or pid == my_pid or pid == 1 or pid in exclude:
                continue
            cmdline = " ".join(cmdline_list)
            if pattern not in cmdline:
                continue
            if "service_manager.py" in cmdline:
                continue
            orphans.append((pid, cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return orphans


def _port_in_use(port):
    """Check if a port is in use via socket connect (works for any protocol)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(("127.0.0.1", int(port)))
            return result == 0
    except (OSError, ValueError):
        return False


def _find_port_holders(port):
    """Find PIDs holding a port using psutil. Returns list of (pid, cmdline) tuples."""
    try:
        port = int(port)
        holder_pids = set()
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                holder_pids.add(conn.pid)
        if not holder_pids:
            return []
        holders = []
        for pid in holder_pids:
            try:
                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline()) or proc.name()
                holders.append((pid, cmdline))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                holders.append((pid, "unknown"))
        return holders
    except Exception:
        return []


def _update_state_services(services):
    """Sync running services to state.json."""
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return
        running = {}
        for name, info in services.items():
            pid = info.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                running[name] = {
                    "port": info.get("port"),
                    "pid": info["pid"],
                    "started": info.get("started", "unknown"),
                }
        state["services"] = running
        fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(STATE_FILE))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def cmd_list():
    services = _load()
    if not services:
        print("No managed services.")
        return
    print(f"{'Name':<20} {'Port':<6} {'PID':<8} {'Status':<10} {'Started'}")
    print("-" * 70)
    for name, info in sorted(services.items()):
        pid = info.get("pid")
        port = info.get("port")
        alive = _pid_alive(pid) if isinstance(pid, int) else False
        status = "running" if alive else "stopped"
        started = info.get("started") or "?"
        port_str = str(port) if port is not None else "n/a"
        pid_str = str(pid) if pid is not None else "n/a"
        print(f"{name:<20} {port_str:<6} {pid_str:<8} {status:<10} {started}")


def cmd_start(name, port, command, auto_start=False):
    if not command:
        print("Error: no command specified after '--'")
        sys.exit(1)

    if port is not None:
        port = int(port)
        if port not in ALLOWED_PORTS:
            print(
                f"Error: port must be in {ALLOWED_PORTS.start}-{ALLOWED_PORTS.stop - 1}"
            )
            sys.exit(1)

    with _flock():
        services = _load()

        # Check if name already running
        existing_pid = services[name].get("pid") if name in services else None
        if isinstance(existing_pid, int) and _pid_alive(existing_pid):
            print(f"Error: '{name}' is already running (PID {existing_pid})")
            sys.exit(1)

        # Check for orphan processes matching the command
        orphans = _find_orphan_pids(
            command,
            exclude_pids={existing_pid} if isinstance(existing_pid, int) else None,
        )
        if orphans:
            print(f"Error: found existing process(es) matching '{name}':")
            for pid, cmdline in orphans:
                print(f"  PID {pid}: {cmdline}")
            print(f"\nKill them manually (e.g. kill <pid>) before starting.")
            sys.exit(1)

        # Check if port in use
        if port is not None and _port_in_use(port):
            holders = _find_port_holders(port)
            if holders:
                print(f"Error: port {port} is already in use:")
                for hpid, hcmd in holders:
                    print(f"  PID {hpid}: {hcmd}")
            else:
                print(f"Error: port {port} is already in use")
            print(f"\nKill the process(es) holding the port before starting.")
            sys.exit(1)

        # Start the process
        log_dir = Path("/agent/memory/logs")
        log_dir.mkdir(exist_ok=True)
        stdout_log = log_dir / f"service-{name}.stdout.log"
        stderr_log = log_dir / f"service-{name}.stderr.log"

        with open(stdout_log, "w") as out, open(stderr_log, "w") as err:
            proc = subprocess.Popen(
                command,
                stdout=out,
                stderr=err,
                cwd="/agent",  # Ensure scripts/ and services/ are importable
                start_new_session=True,  # Detach from parent
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        # Preserve extra fields (auto_start, health_url, etc.) from existing entry
        existing = services.get(name, {})
        extra = {
            k: v
            for k, v in existing.items()
            if k
            not in ("pid", "port", "command", "started", "stdout_log", "stderr_log")
        }
        services[name] = {
            **extra,
            "pid": proc.pid,
            "port": port,
            "command": command,
            "started": now,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
        }
        if auto_start:
            services[name]["auto_start"] = True
        _save(services)
        _update_state_services(services)

    # Wait briefly and verify it's still running
    time.sleep(1)
    if _pid_alive(proc.pid):
        port_msg = f" on port {port}" if port is not None else ""
        print(f"Started '{name}'{port_msg} (PID {proc.pid})")
        print(f"  stdout: {stdout_log}")
        print(f"  stderr: {stderr_log}")
    else:
        # Read tail of stderr log for error context
        error_tail = ""
        try:
            content = stderr_log.read_text()
            lines = content.strip().splitlines()[-20:]
            error_tail = "\n".join(lines)
        except Exception:
            pass
        print(f"Error: '{name}' started but exited immediately.")
        if error_tail:
            print(error_tail)
        sys.exit(1)


def cmd_stop(name):
    with _flock():
        services = _load()
        if name not in services:
            print(f"Error: no service named '{name}'")
            sys.exit(1)

        pid = services[name].get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            if not _kill_pid(name, pid):
                sys.exit(1)
        else:
            print(f"'{name}' was not running")

        # Preserve entry so the service can be restarted from the UI
        services[name]["pid"] = None
        services[name]["started"] = None
        _save(services)
        _update_state_services(services)


def cmd_remove(name):
    """Stop (if running) and remove a service entry entirely."""
    with _flock():
        services = _load()
        if name not in services:
            print(f"Error: no service named '{name}'")
            sys.exit(1)

        pid = services[name].get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            _kill_pid(name, pid)

        del services[name]
        _save(services)
        _update_state_services(services)
        print(f"Removed '{name}'")


def cmd_restart(name):
    with _flock():
        services = _load()
        if name not in services:
            print(f"Error: no service named '{name}'")
            sys.exit(1)

        # Get the stored service information
        info = services[name]
        port = info.get("port")
        command = info.get("command")

        if not command:
            print(f"Error: no command stored for service '{name}'")
            sys.exit(1)

        pid = info.get("pid")
        was_running = isinstance(pid, int) and _pid_alive(pid)

        # Stop the service if it's running
        if was_running:
            if not _kill_pid(name, pid):
                sys.exit(1)

        # Check for orphan processes
        orphans = _find_orphan_pids(
            command, exclude_pids={pid} if isinstance(pid, int) else None
        )
        if orphans:
            print(f"Error: orphan process(es) still running for '{name}':")
            for opid, cmdline in orphans:
                print(f"  PID {opid}: {cmdline}")
            print(f"\nKill them manually before restarting.")
            sys.exit(1)

        # Check if port in use (could be held by an unrelated process)
        if port is not None and _port_in_use(port):
            holders = _find_port_holders(port)
            if holders:
                print(f"Error: port {port} is already in use:")
                for hpid, hcmd in holders:
                    print(f"  PID {hpid}: {hcmd}")
            else:
                print(f"Error: port {port} is already in use")
            print(f"\nKill the process(es) holding the port before restarting.")
            sys.exit(1)

        # Start the service with the stored command
        # Reuse existing log paths if available, otherwise generate new ones
        if info.get("stdout_log") and info.get("stderr_log"):
            stdout_log = Path(info["stdout_log"])
            stderr_log = Path(info["stderr_log"])
        else:
            log_dir = Path("/agent/memory/logs")
            log_dir.mkdir(exist_ok=True)
            stdout_log = log_dir / f"service-{name}.stdout.log"
            stderr_log = log_dir / f"service-{name}.stderr.log"

        with open(stdout_log, "a") as out, open(stderr_log, "a") as err:
            proc = subprocess.Popen(
                command,
                stdout=out,
                stderr=err,
                cwd="/agent",  # Ensure scripts/ and services/ are importable
                start_new_session=True,  # Detach from parent
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        # Preserve extra fields (auto_start, health_url, etc.) from existing entry
        extra = {
            k: v
            for k, v in info.items()
            if k
            not in ("pid", "port", "command", "started", "stdout_log", "stderr_log")
        }
        services[name] = {
            **extra,
            "pid": proc.pid,
            "port": port,
            "command": command,
            "started": now,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
        }
        _save(services)
    _update_state_services(services)

    # Wait briefly and verify it's still running
    time.sleep(1)
    if _pid_alive(proc.pid):
        port_msg = f" on port {port}" if port is not None else ""
        status_msg = "Restarted" if was_running else "Started"
        print(f"{status_msg} '{name}'{port_msg} (PID {proc.pid})")
        print(f"  stdout: {stdout_log}")
        print(f"  stderr: {stderr_log}")
    else:
        # Read tail of stderr log for error context
        error_tail = ""
        try:
            content = stderr_log.read_text()
            lines = content.strip().splitlines()[-20:]
            error_tail = "\n".join(lines)
        except Exception:
            pass
        print(f"Error: '{name}' started but exited immediately.")
        if error_tail:
            print(error_tail)
        sys.exit(1)


def cmd_status(name):
    services = _load()
    if name not in services:
        print(f"No service named '{name}'")
        sys.exit(1)

    info = services[name]
    pid = info.get("pid", 0)
    port = info.get("port")
    alive = _pid_alive(pid) if isinstance(pid, int) else False
    port_up = _port_in_use(port) if port is not None else None

    pid_str = str(pid) if pid is not None else "n/a"
    print(f"Service: {name}")
    print(f"  PID:      {pid_str} ({'alive' if alive else 'stopped'})")
    if port is not None:
        print(f"  Port:     {port} ({'responding' if port_up else 'not responding'})")
    else:
        print(f"  Port:     n/a")
    print(f"  Command:  {' '.join(info.get('command', []))}")
    print(f"  Started:  {info.get('started') or '?'}")
    print(f"  Logs:     {info.get('stdout_log') or '?'}")


def _read_heartbeat(name: str) -> float | None:
    """Read the last heartbeat timestamp for a service. Returns epoch or None."""
    hb_file = HEARTBEAT_DIR / f"{name}.heartbeat"
    if not hb_file.exists():
        return None
    try:
        ts_str = hb_file.read_text().strip()
        return float(ts_str)
    except (ValueError, OSError):
        return None


def cmd_health():
    services = _load()
    if not services:
        print("No managed services.")
        return

    all_ok = True
    now = time.time()
    for name, info in sorted(services.items()):
        pid = info.get("pid", 0)
        port = info.get("port")
        alive = _pid_alive(pid) if isinstance(pid, int) else False
        if port is not None:
            port_up = _port_in_use(port)
            status = "OK" if (alive and port_up) else "DEGRADED" if alive else "DOWN"
            port_label = f"port {port}"
        else:
            status = "OK" if alive else "DOWN"
            port_label = "no port"

        # Check heartbeat staleness (only if PID is alive — a dead PID is already DOWN)
        hb_note = ""
        if alive:
            last_hb = _read_heartbeat(name)
            if last_hb is not None:
                age = now - last_hb
                if age > HEARTBEAT_STALE_THRESHOLD:
                    status = "STUCK"
                    hb_note = f" [heartbeat {int(age)}s ago, threshold {HEARTBEAT_STALE_THRESHOLD}s]"
                else:
                    hb_note = f" [heartbeat {int(age)}s ago]"
            # No heartbeat file = service doesn't support heartbeats yet; don't penalize

        if status not in ("OK",):
            all_ok = False
        pid_str = str(pid) if pid is not None else "n/a"
        print(f"  {status:<10} {name} ({port_label}, PID {pid_str}){hb_note}")

    print()
    print("Overall:", "HEALTHY" if all_ok else "ISSUES DETECTED")


def cmd_cleanup():
    with _flock():
        services = _load()
        removed = []
        for name in list(services.keys()):
            pid = services[name].get("pid")
            if not (isinstance(pid, int) and _pid_alive(pid)):
                removed.append(name)
                del services[name]
        _save(services)
        _update_state_services(services)
    if removed:
        print(f"Removed {len(removed)} dead entries: {', '.join(removed)}")
    else:
        print("No dead entries to clean up.")


def cmd_auto_start():
    """Start all services with auto_start:true that are not currently running.

    Designed to be called at the start of every heartbeat. Never exits non-zero
    so a single service failure does not abort the heartbeat.
    """
    services = _load()
    started, skipped, failed = [], [], []

    for name, info in services.items():
        if not info.get("auto_start"):
            continue

        pid = info.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            skipped.append(name)
            continue

        command = info.get("command")
        port = info.get("port")

        if not command:
            print(f"[auto-start] Skipping '{name}': no command stored")
            failed.append(name)
            continue

        if port is not None and _port_in_use(port):
            print(f"[auto-start] Skipping '{name}': port {port} already in use")
            failed.append(name)
            continue

        try:
            log_dir = Path("/agent/memory/logs")
            log_dir.mkdir(exist_ok=True)
            stdout_log = Path(
                info.get("stdout_log") or log_dir / f"service-{name}.stdout.log"
            )
            stderr_log = Path(
                info.get("stderr_log") or log_dir / f"service-{name}.stderr.log"
            )

            with open(stdout_log, "a") as out, open(stderr_log, "a") as err:
                proc = subprocess.Popen(
                    command,
                    stdout=out,
                    stderr=err,
                    cwd="/agent",
                    start_new_session=True,
                )

            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            with _flock():
                services = _load()
                # Preserve all extra fields (auto_start, health_url, etc.)
                extra = {
                    k: v
                    for k, v in services.get(name, info).items()
                    if k
                    not in (
                        "pid",
                        "port",
                        "command",
                        "started",
                        "stdout_log",
                        "stderr_log",
                    )
                }
                services[name] = {
                    **extra,
                    "pid": proc.pid,
                    "port": port,
                    "command": command,
                    "started": now,
                    "stdout_log": str(stdout_log),
                    "stderr_log": str(stderr_log),
                }
                _save(services)
            _update_state_services(services)

            time.sleep(0.5)
            if _pid_alive(proc.pid):
                port_msg = f" on port {port}" if port is not None else ""
                print(f"[auto-start] Started '{name}'{port_msg} (PID {proc.pid})")
                started.append(name)
            else:
                print(
                    f"[auto-start] '{name}' started but exited immediately — check {stderr_log}"
                )
                failed.append(name)

        except Exception as e:
            print(f"[auto-start] Failed to start '{name}': {e}")
            failed.append(name)

    if skipped and not started and not failed:
        print(
            f"[auto-start] All auto-start services already running: {', '.join(skipped)}"
        )
    if started:
        print(f"[auto-start] Started: {', '.join(started)}")
    if failed:
        print(f"[auto-start] Failed: {', '.join(failed)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "start":
        # start <name> [<port>] [--auto-start] -- <cmd...>
        start_args = sys.argv[2:]
        auto_start_flag = "--auto-start" in start_args
        if auto_start_flag:
            start_args = [a for a in start_args if a != "--auto-start"]
        if len(start_args) >= 2 and start_args[1] == "--":
            cmd_start(start_args[0], None, start_args[2:], auto_start=auto_start_flag)
        elif len(start_args) >= 3 and start_args[2] == "--":
            cmd_start(
                start_args[0], start_args[1], start_args[3:], auto_start=auto_start_flag
            )
        else:
            print(
                "Usage: service_manager.py start <name> [<port>] [--auto-start] -- <command...>"
            )
            sys.exit(1)
    elif cmd in ("stop", "remove", "restart", "status"):
        if len(sys.argv) < 3:
            print(f"Usage: service_manager.py {cmd} <name>")
            sys.exit(1)
        {
            "stop": cmd_stop,
            "remove": cmd_remove,
            "restart": cmd_restart,
            "status": cmd_status,
        }[cmd](sys.argv[2])
    elif cmd == "health":
        cmd_health()
    elif cmd == "cleanup":
        cmd_cleanup()
    elif cmd == "auto-start":
        cmd_auto_start()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
