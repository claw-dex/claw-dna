#!/usr/bin/env python3
"""Service manager for agent background processes.

Ports 8080-8081 are reserved for the core services (Caddy, Streamlit).
Services that listen on a port should use 8083-8090.
Port is optional — polling/background services don't need one.

Usage:
    python3 service-manager.py list                          # Show all managed services
    python3 service-manager.py start <name> [<port>] -- <cmd...>  # Start a service
    python3 service-manager.py stop <name>                   # Stop a service (keeps entry)
    python3 service-manager.py remove <name>                 # Stop and remove a service
    python3 service-manager.py restart <name>                # Restart a service
    python3 service-manager.py status <name>                 # Check one service
    python3 service-manager.py health                        # Health check all services
    python3 service-manager.py cleanup                       # Remove dead entries
"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timezone

SERVICES_FILE = Path("/agent/memory/services.json")
STATE_FILE = Path("/agent/memory/state.json")
ALLOWED_PORTS = range(8083, 8091)  # 8083-8090


def _load():
    if SERVICES_FILE.exists():
        try:
            return json.loads(SERVICES_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save(services):
    tmp = str(SERVICES_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(services, f, indent=2)
    os.replace(tmp, str(SERVICES_FILE))


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
        pattern = non_flag[1] if len(non_flag) >= 2 else (non_flag[0] if non_flag else None)
    if not pattern:
        return []

    try:
        result = subprocess.run(
            ["pgrep", "-af", pattern],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []

    exclude = exclude_pids or set()
    my_pid = os.getpid()
    orphans = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmdline = parts[1]
        if pid == my_pid or pid == 1 or pid in exclude:
            continue
        if cmdline.startswith("pgrep ") or "service-manager.py" in cmdline:
            continue
        orphans.append((pid, cmdline))
    return orphans


def _port_in_use(port):
    """Check if a port is in use via curl."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"http://localhost:{port}/"],
            capture_output=True, text=True, timeout=2
        )
        return r.stdout.strip() != "000"
    except Exception:
        return False


def _find_port_holders(port):
    """Find PIDs holding a port using lsof. Returns list of (pid, cmdline) tuples."""
    try:
        r = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        pids = set()
        for line in r.stdout.strip().split("\n"):
            try:
                pids.add(int(line.strip()))
            except ValueError:
                continue
        if not pids:
            return []
        # Get command lines for all PIDs
        ps = subprocess.run(
            ["ps", "-p", ",".join(str(p) for p in pids), "-o", "pid=,args="],
            capture_output=True, text=True, timeout=5,
        )
        holders = []
        if ps.returncode == 0:
            for line in ps.stdout.strip().split("\n"):
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    try:
                        holders.append((int(parts[0]), parts[1]))
                    except ValueError:
                        continue
        # Include any PIDs that ps didn't report
        reported = {h[0] for h in holders}
        for pid in pids - reported:
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
        tmp = str(STATE_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, str(STATE_FILE))


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


def cmd_start(name, port, command):
    if not command:
        print("Error: no command specified after '--'")
        sys.exit(1)

    if port is not None:
        port = int(port)
        if port not in ALLOWED_PORTS:
            print(f"Error: port must be in {ALLOWED_PORTS.start}-{ALLOWED_PORTS.stop - 1}")
            sys.exit(1)

    services = _load()

    # Check if name already running
    existing_pid = services[name].get("pid") if name in services else None
    if isinstance(existing_pid, int) and _pid_alive(existing_pid):
        print(f"Error: '{name}' is already running (PID {existing_pid})")
        sys.exit(1)

    # Check for orphan processes matching the command
    orphans = _find_orphan_pids(command, exclude_pids={existing_pid} if isinstance(existing_pid, int) else None)
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
            start_new_session=True,  # Detach from parent
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    services[name] = {
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
    orphans = _find_orphan_pids(command, exclude_pids={pid} if isinstance(pid, int) else None)
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
            start_new_session=True,  # Detach from parent
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    services[name] = {
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


def cmd_health():
    services = _load()
    if not services:
        print("No managed services.")
        return

    all_ok = True
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
        if status != "OK":
            all_ok = False
        pid_str = str(pid) if pid is not None else "n/a"
        print(f"  {status:<10} {name} ({port_label}, PID {pid_str})")

    print()
    print("Overall:", "HEALTHY" if all_ok else "ISSUES DETECTED")


def cmd_cleanup():
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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "start":
        # start <name> -- <cmd...>        (no port)
        # start <name> <port> -- <cmd...> (with port)
        if len(sys.argv) >= 4 and sys.argv[3] == "--":
            cmd_start(sys.argv[2], None, sys.argv[4:])
        elif len(sys.argv) >= 5 and sys.argv[4] == "--":
            cmd_start(sys.argv[2], sys.argv[3], sys.argv[5:])
        else:
            print("Usage: service-manager.py start <name> [<port>] -- <command...>")
            sys.exit(1)
    elif cmd == "stop":
        cmd_stop(sys.argv[2])
    elif cmd == "remove":
        cmd_remove(sys.argv[2])
    elif cmd == "restart":
        cmd_restart(sys.argv[2])
    elif cmd == "status":
        cmd_status(sys.argv[2])
    elif cmd == "health":
        cmd_health()
    elif cmd == "cleanup":
        cmd_cleanup()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
