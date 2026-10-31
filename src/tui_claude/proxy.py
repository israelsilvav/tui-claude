"""Tailscale proxy (server + client) integrated with tui-claude profiles.

Architecture:
- `tui-claude proxy server [PROFILE]`: runs on the work machine, binds to the
  Tailscale IP (or `--host`), reads `.credentials.json` directly from the chosen
  profile, injects `Authorization: Bearer <token>` and forwards requests to
  `https://api.anthropic.com`.
- `tui-claude proxy client <UPSTREAM> [PROFILE]`: runs on the personal machine,
  listens on `127.0.0.1:<port>`, forwards requests to the work machine's proxy
  server, and automatically configures `ANTHROPIC_BASE_URL` and
  `ANTHROPIC_AUTH_TOKEN` in the target profile's `settings.json` for as long as
  the client proxy is running — restoring the profile untouched when stopped
  (or on the next refresh if the process died unexpectedly).
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from . import sharing, tmp_mode
from .links import is_link, link_file, points_to, remove_link

PROXY_DIR_NAME = "_proxy"
REAL_ANTHROPIC_API = "https://api.anthropic.com"
DEFAULT_SERVER_PORT = 8888
DEFAULT_CLIENT_HOST = "127.0.0.1"
DEFAULT_CLIENT_PORT = 9999
PROXY_AUTH_TOKEN = "proxy-local"

PROXY_USAGE = """\
usage: tui-claude proxy server [PROFILE] [--host IP] [--port PORT] [-d|--daemon]
       tui-claude proxy client <UPSTREAM> [PROFILE] [--port PORT] [-d|--daemon]
       tui-claude proxy status
       tui-claude proxy stop [server|client|all]
       tui-claude proxy logs [server|client]

commands:
  server    Serve a profile's credentials over Tailscale (work machine)
  client    Connect to a remote proxy server and bind it to a profile
  status    Show active proxy server/client processes
  stop      Stop running proxy daemon(s) and restore profile settings
  logs      Show recent log output from a background proxy daemon

options:
  -h, --help    Show this help message and exit"""

SERVER_USAGE = """\
usage: tui-claude proxy server [PROFILE] [--host IP] [--port PORT] [-d|--daemon]

Serve a Claude Code profile's credentials to your Tailscale network.
If PROFILE is omitted, opens the interactive profile selector.

positional arguments:
  PROFILE               Profile name to read .credentials.json from

options:
  --host IP             IP address to bind (default: auto-detected via `tailscale ip -4`)
  --port PORT           Port to listen on (default: 8888)
  -d, --daemon          Run in the background as a daemon
  -h, --help            Show this help message and exit"""

CLIENT_USAGE = """\
usage: tui-claude proxy client <UPSTREAM> [PROFILE] [--port PORT] [-d|--daemon]

Start a local proxy forwarding to UPSTREAM and automatically configure PROFILE
to route Claude Code traffic through it while running.
When stopped, PROFILE's original settings are restored automatically.

positional arguments:
  UPSTREAM              Tailscale IP (e.g. 100.78.40.97) or URL (http://100.78.40.97:8888)
  PROFILE               Profile to attach the proxy to (default: current active profile)

options:
  --port PORT           Local port on 127.0.0.1 (default: 9999)
  --pick                Open the interactive TUI to pick a profile
  -d, --daemon          Run in the background as a daemon
  -h, --help            Show this help message and exit"""

STATUS_USAGE = """\
usage: tui-claude proxy status

Show the status of any running proxy server or client processes."""

STOP_USAGE = """\
usage: tui-claude proxy stop [server|client|all]

Stop the running proxy server, client, or both (default: all) and restore
any profile settings modified by the client proxy."""

LOGS_USAGE = """\
usage: tui-claude proxy logs [server|client] [--lines N]

Print recent log lines from the proxy server or client daemon."""


# --- filesystem & process state helpers --------------------------------------

def proxy_dir(profiles_dir):
    return os.path.join(profiles_dir, PROXY_DIR_NAME)


def state_file(profiles_dir, kind):
    return os.path.join(proxy_dir(profiles_dir), f"{kind}.json")


def log_file_path(profiles_dir, kind):
    return os.path.join(proxy_dir(profiles_dir), f"{kind}.log")


def _process_alive(pid, start_time=None):
    if not isinstance(pid, int) or pid <= 0:
        return False
    return tmp_mode._alive(pid, start_time or "")


def read_state(profiles_dir, kind):
    data = sharing.load_json(state_file(profiles_dir, kind))
    if not data:
        return None
    pid = data.get("pid")
    start_time = data.get("start_time", "")
    data["alive"] = _process_alive(pid, start_time)
    return data


def write_state(profiles_dir, kind, data):
    os.makedirs(proxy_dir(profiles_dir), exist_ok=True)
    sharing.save_json(state_file(profiles_dir, kind), data)


def remove_state(profiles_dir, kind):
    with contextlib.suppress(FileNotFoundError):
        os.remove(state_file(profiles_dir, kind))


# --- URL & network helpers ---------------------------------------------------

def normalize_upstream(upstream, default_port=DEFAULT_SERVER_PORT):
    """Turn '100.78.40.97' or 'http://100.78.40.97:8888/' into a clean base URL."""
    raw = (upstream or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Upstream address cannot be empty.")
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    scheme, rest = raw.split("://", 1)
    host_port = rest.split("/", 1)[0]
    if not host_port:
        raise ValueError(f"Invalid upstream address: {upstream}")
    if ":" not in host_port:
        raw = f"{scheme}://{host_port}:{default_port}"
    return raw


def detect_tailscale_ip(run=subprocess.run):
    """Return the machine's Tailscale IPv4 address via `tailscale ip -4`."""
    try:
        res = run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Tailscale CLI ('tailscale') not found. Pass --host <IP> explicitly."
        ) from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Could not detect Tailscale IP via 'tailscale ip -4'. "
            "Make sure Tailscale is connected or pass --host <IP>."
        ) from exc

    for line in res.stdout.splitlines():
        ip = line.strip()
        if ip:
            return ip
    raise RuntimeError("No IPv4 address returned by 'tailscale ip -4'. Pass --host <IP>.")


# --- credential extraction (server) ------------------------------------------

def extract_session_token(profile_dir=None, extra_paths=None):
    """Read the OAuth / session token from the given profile directory."""
    candidates = []
    if profile_dir:
        candidates.append(os.path.join(profile_dir, ".credentials.json"))
    if extra_paths is not None:
        candidates.extend(extra_paths)
    else:
        candidates.extend([
            os.path.expanduser("~/.claude/.credentials.json"),
            os.path.expanduser("~/.config/claude-code/config.json"),
        ])

    for path in candidates:
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    config = json.load(handle)
                oauth = config.get("claudeAiOauth")
                if isinstance(oauth, dict) and oauth.get("accessToken"):
                    expires_at = oauth.get("expiresAt")
                    if expires_at and expires_at < time.time() * 1000:
                        print(
                            f"[WARN] OAuth token in {path} is EXPIRED! "
                            "Run 'claude' on the server machine to refresh the session.",
                            flush=True,
                        )
                    return oauth.get("accessToken")
                token = config.get("sessionToken") or config.get("accessToken")
                if token:
                    return token
        except Exception as exc:
            print(f"[WARN] Failed reading credentials from {path}: {exc}", flush=True)

    return os.environ.get("CLAUDE_CORPORATE_SESSION_TOKEN")


# --- ASGI applications (server & client) -------------------------------------

def create_server_app(profile_dir, anthropic_url=REAL_ANTHROPIC_API, client_factory=None, extra_cred_paths=None):
    """ASGI app that injects the profile's token and streams from Anthropic."""
    if client_factory is None:
        def client_factory():
            return httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def inject_and_forward(request: Request):
        path = request.path_params.get("path", "v1/messages")
        body = await request.body()

        token = extract_session_token(profile_dir, extra_paths=extra_cred_paths)
        if not token:
            print("[ERROR] No corporate/profile session token found!", flush=True)
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "type": "authentication_error",
                        "message": "Token corporativo não encontrado no servidor proxy.",
                    }
                },
            )

        headers = dict(request.headers)
        headers.pop("x-api-key", None)
        headers.pop("authorization", None)
        headers["Authorization"] = f"Bearer {token}"

        if token.startswith("sk-ant-oat"):
            current_beta = headers.get("anthropic-beta", "")
            if "oauth-2025-04-20" not in current_beta:
                headers["anthropic-beta"] = (
                    f"{current_beta},oauth-2025-04-20" if current_beta else "oauth-2025-04-20"
                )

        headers.pop("host", None)
        headers.pop("content-length", None)
        headers.pop("connection", None)
        headers.pop("accept-encoding", None)

        target_url = f"{anthropic_url.rstrip('/')}/{path.lstrip('/')}"
        client = client_factory()

        try:
            req = client.build_request(
                request.method,
                target_url,
                content=body,
                headers=headers,
                params=request.query_params,
            )
            res = await client.send(req, stream=True)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
            await client.aclose()
            return JSONResponse(
                status_code=502,
                content={"error": {"type": "upstream_error", "message": str(exc)}},
            )

        if res.status_code >= 400:
            print(f"[WARN] Anthropic API returned status {res.status_code} for /{path}", flush=True)

        async def stream_to_anthropic():
            try:
                async for chunk in res.aiter_bytes():
                    yield chunk
            finally:
                await res.aclose()
                await client.aclose()

        media_type = res.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            stream_to_anthropic(),
            status_code=res.status_code,
            media_type=media_type,
        )

    methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    return Starlette(routes=[
        Route("/{path:path}", inject_and_forward, methods=methods),
    ])


def create_client_app(upstream_url, client_factory=None):
    """ASGI app that forwards local Claude Code requests to the Tailscale server."""
    upstream_base = normalize_upstream(upstream_url)
    if client_factory is None:
        def client_factory():
            return httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    async def intercept_and_route(request: Request):
        path = request.path_params.get("path", "v1/messages")
        body = await request.body()
        headers = dict(request.headers)

        headers.pop("host", None)
        headers.pop("content-length", None)
        headers.pop("connection", None)
        headers.pop("accept-encoding", None)

        target_url = f"{upstream_base.rstrip('/')}/{path.lstrip('/')}"
        client = client_factory()

        try:
            req = client.build_request(
                request.method,
                target_url,
                content=body,
                headers=headers,
                params=request.query_params,
            )
            response = await client.send(req, stream=True)
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
            await client.aclose()
            print("\n[WARN] Connection dropped. Tailscale is re-establishing the route...", flush=True)
            return JSONResponse(
                status_code=502,
                content={"error": {"type": "proxy_connection_error", "message": str(exc)}},
            )

        async def stream_to_work():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                print("\n[WARN] Connection dropped during stream. Reconnecting on next request...", flush=True)
            finally:
                await response.aclose()
                await client.aclose()

        media_type = response.headers.get("content-type", "text/event-stream")
        return StreamingResponse(
            stream_to_work(),
            status_code=response.status_code,
            media_type=media_type,
        )

    methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    return Starlette(routes=[
        Route("/{path:path}", intercept_and_route, methods=methods),
    ])


# --- Automatic profile settings lifecycle (Client) ---------------------------

def is_profile_proxied(profiles_dir, profile):
    """Return True if a live proxy client is currently attached to `profile`."""
    state = read_state(profiles_dir, "client")
    return bool(state and state.get("alive") and state.get("profile") == profile)


def attach_client_profile(profiles_dir, profile, port, upstream, host=DEFAULT_CLIENT_HOST, pid=None, log_file=None):
    """Configure `profile`'s settings.json to use the local client proxy.

    If `profile` shares data with the `_shared` pool, `settings.json` is
    temporarily unlinked from the pool and replaced by a local copy so other
    shared profiles are never redirected to the proxy.
    """
    # Clean any previous stale state first so we record the real baseline.
    clean_dead_proxies(profiles_dir)
    existing = read_state(profiles_dir, "client")
    if existing and not existing.get("alive"):
        detach_client_profile(profiles_dir)

    pid = os.getpid() if pid is None else pid
    profile_path = os.path.join(profiles_dir, profile)
    settings_path = os.path.join(profile_path, "settings.json")
    pool_settings = os.path.join(sharing.pool_dir(profiles_dir), "settings.json")

    was_shared_link = (
        is_link(settings_path)
        or points_to(settings_path, pool_settings)
        or sharing.is_shared(profiles_dir, profile)
    )
    had_file = os.path.exists(settings_path)
    data = sharing.load_json(settings_path) if had_file else {}

    had_env_dict = isinstance(data.get("env"), dict)
    env_dict = dict(data.get("env")) if had_env_dict else {}
    had_base_url = "ANTHROPIC_BASE_URL" in env_dict
    prev_base_url = env_dict.get("ANTHROPIC_BASE_URL")
    had_auth_token = "ANTHROPIC_AUTH_TOKEN" in env_dict
    prev_auth_token = env_dict.get("ANTHROPIC_AUTH_TOKEN")

    # Detach from the shared pool before writing proxy env keys so _shared is untouched.
    if is_link(settings_path) or points_to(settings_path, pool_settings):
        remove_link(settings_path)

    base_url = f"http://{host}:{port}"
    env_dict["ANTHROPIC_BASE_URL"] = base_url
    env_dict["ANTHROPIC_AUTH_TOKEN"] = PROXY_AUTH_TOKEN
    data["env"] = env_dict
    sharing.save_json(settings_path, data)

    tmp_mode.register_pin(profiles_dir, profile, pid=pid)
    write_state(profiles_dir, "client", {
        "pid": pid,
        "start_time": tmp_mode._start_time(pid) or "",
        "profile": profile,
        "host": host,
        "port": port,
        "upstream": normalize_upstream(upstream),
        "base_url": base_url,
        "log_file": log_file,
        "was_shared_link": was_shared_link,
        "had_file": had_file,
        "had_env_dict": had_env_dict,
        "had_base_url": had_base_url,
        "prev_base_url": prev_base_url,
        "had_auth_token": had_auth_token,
        "prev_auth_token": prev_auth_token,
    })


def detach_client_profile(profiles_dir):
    """Restore the profile's `settings.json` and remove the client proxy state."""
    state = sharing.load_json(state_file(profiles_dir, "client"))
    if not state:
        return None

    profile = state.get("profile")
    pid = state.get("pid")
    if pid:
        tmp_mode.remove_pin(profiles_dir, pid)

    if profile:
        profile_path = os.path.join(profiles_dir, profile)
        settings_path = os.path.join(profile_path, "settings.json")
        pool_settings = os.path.join(sharing.pool_dir(profiles_dir), "settings.json")

        if os.path.isdir(profile_path):
            data = sharing.load_json(settings_path) if os.path.exists(settings_path) else {}
            env_dict = data.get("env")
            if isinstance(env_dict, dict):
                if state.get("had_base_url"):
                    env_dict["ANTHROPIC_BASE_URL"] = state.get("prev_base_url")
                else:
                    env_dict.pop("ANTHROPIC_BASE_URL", None)

                if state.get("had_auth_token"):
                    env_dict["ANTHROPIC_AUTH_TOKEN"] = state.get("prev_auth_token")
                else:
                    env_dict.pop("ANTHROPIC_AUTH_TOKEN", None)

                if not env_dict and not state.get("had_env_dict"):
                    data.pop("env", None)

            was_shared = state.get("was_shared_link") or sharing.is_shared(profiles_dir, profile)
            if was_shared and os.path.isdir(sharing.pool_dir(profiles_dir)):
                # Preserve any non-proxy settings changed during the session in the pool.
                if is_link(settings_path) or points_to(settings_path, pool_settings):
                    remove_link(settings_path)
                elif os.path.exists(settings_path):
                    os.remove(settings_path)

                if data or os.path.exists(pool_settings):
                    sharing.save_json(pool_settings, data)
                link_file(pool_settings, settings_path)
            else:
                if not data and not state.get("had_file"):
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(settings_path)
                else:
                    sharing.save_json(settings_path, data)

    remove_state(profiles_dir, "client")
    return profile


def register_server_state(profiles_dir, profile, host, port, pid=None, log_file=None):
    """Record a running proxy server and pin its profile."""
    clean_dead_proxies(profiles_dir)
    pid = os.getpid() if pid is None else pid
    tmp_mode.register_pin(profiles_dir, profile, pid=pid)
    write_state(profiles_dir, "server", {
        "pid": pid,
        "start_time": tmp_mode._start_time(pid) or "",
        "profile": profile,
        "host": host,
        "port": port,
        "log_file": log_file,
    })


def unregister_server_state(profiles_dir):
    state = sharing.load_json(state_file(profiles_dir, "server"))
    if not state:
        return None
    pid = state.get("pid")
    if pid:
        tmp_mode.remove_pin(profiles_dir, pid)
    remove_state(profiles_dir, "server")
    return state.get("profile")


def clean_dead_proxies(profiles_dir):
    """Clean up state and restore profile settings if a proxy process died."""
    cleaned = []
    client_state = read_state(profiles_dir, "client")
    if client_state and not client_state.get("alive"):
        restored = detach_client_profile(profiles_dir)
        if restored:
            cleaned.append(f"client ({restored})")

    server_state = read_state(profiles_dir, "server")
    if server_state and not server_state.get("alive"):
        restored = unregister_server_state(profiles_dir)
        if restored:
            cleaned.append(f"server ({restored})")

    return cleaned


def active_proxies(profiles_dir):
    """Return {"server": state_or_None, "client": state_or_None} for live processes."""
    clean_dead_proxies(profiles_dir)
    server = read_state(profiles_dir, "server")
    client = read_state(profiles_dir, "client")
    return {
        "server": server if (server and server.get("alive")) else None,
        "client": client if (client and client.get("alive")) else None,
    }


# --- Foreground & Daemon runners ---------------------------------------------

def _wait_for_port(host, port, pid, timeout=5.0):
    """Wait until (host, port) accepts TCP connections or `pid` exits."""
    deadline = time.time() + timeout
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    while time.time() < deadline:
        if not _process_alive(pid):
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((connect_host, port)) == 0:
                return True
        time.sleep(0.05)
    return _process_alive(pid)


def _install_signal_handlers():
    """Translate SIGTERM into SystemExit so `finally:` blocks always run."""
    def _handle_term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_term)


def serve_foreground(profiles_dir, profile, host, port):
    """Run the proxy server in the current process until interrupted."""
    import uvicorn

    profile_dir = os.path.join(profiles_dir, profile)
    token = extract_session_token(profile_dir)
    if token:
        print(f"[OK] Session token found for profile '{profile}' (prefix: {token[:12]}...)", flush=True)
    else:
        print(
            f"[WARN] No session token found in {profile_dir}/.credentials.json yet.",
            flush=True,
        )

    register_server_state(profiles_dir, profile, host, port, log_file=log_file_path(profiles_dir, "server"))
    _install_signal_handlers()
    app = create_server_app(profile_dir)
    print(f"Proxy server listening on http://{host}:{port} (profile: '{profile}')", flush=True)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        unregister_server_state(profiles_dir)
        print(f"\nProxy server for profile '{profile}' stopped.", flush=True)
    return 0


def client_foreground(profiles_dir, profile, upstream, host=DEFAULT_CLIENT_HOST, port=DEFAULT_CLIENT_PORT):
    """Run the proxy client in the current process and manage `profile`'s settings."""
    import uvicorn

    upstream_url = normalize_upstream(upstream)
    attach_client_profile(
        profiles_dir,
        profile,
        port=port,
        upstream=upstream_url,
        host=host,
        log_file=log_file_path(profiles_dir, "client"),
    )
    _install_signal_handlers()
    app = create_client_app(upstream_url)
    print(
        f"Proxy client active on http://{host}:{port} -> {upstream_url}\n"
        f"Profile '{profile}' is now automatically routed through the proxy.\n"
        f"Press Ctrl+C to stop and restore '{profile}' to normal.",
        flush=True,
    )
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        detach_client_profile(profiles_dir)
        print(f"\nProxy client stopped. Profile '{profile}' restored to normal.", flush=True)
    return 0


def spawn_daemon(profiles_dir, kind, args_list, host, port):
    """Spawn a background proxy process and wait until it is listening."""
    os.makedirs(proxy_dir(profiles_dir), exist_ok=True)
    log_path = log_file_path(profiles_dir, kind)
    cmd = [sys.executable, "-P", "-m", "tui_claude.main"] + args_list
    env = dict(os.environ)

    with open(log_path, "a", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    ready = _wait_for_port(host, port, proc.pid, timeout=6.0)
    if not ready:
        return None, log_path
    # Wait briefly for the child to write its state file.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        st = read_state(profiles_dir, kind)
        if st and st.get("pid") == proc.pid:
            break
        time.sleep(0.05)
    return proc.pid, log_path


def stop_proxy(profiles_dir, target="all", timeout=5.0):
    """Stop running proxy process(es) and ensure profile cleanup."""
    clean_dead_proxies(profiles_dir)
    kinds = ["client", "server"] if target == "all" else [target]
    stopped = []

    for kind in kinds:
        state = read_state(profiles_dir, kind)
        if not state:
            continue
        pid = state.get("pid")
        profile = state.get("profile")
        if state.get("alive") and pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + timeout
            while time.time() < deadline and _process_alive(pid, state.get("start_time", "")):
                time.sleep(0.05)
            if _process_alive(pid, state.get("start_time", "")):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)

        # Ensure cleanup even if SIGKILL was required or signal handler didn't finish.
        if kind == "client" and os.path.exists(state_file(profiles_dir, "client")):
            detach_client_profile(profiles_dir)
        elif kind == "server" and os.path.exists(state_file(profiles_dir, "server")):
            unregister_server_state(profiles_dir)

        stopped.append((kind, profile, pid))

    return stopped


def format_status(profiles_dir):
    """Human-readable status report for `tui-claude proxy status`."""
    active = active_proxies(profiles_dir)
    lines = []
    server = active["server"]
    client = active["client"]

    if server:
        lines.append(
            f"Proxy server: RUNNING (pid {server['pid']})\n"
            f"  Profile:   {server['profile']}\n"
            f"  Listening: http://{server['host']}:{server['port']}\n"
            f"  Logs:      {server.get('log_file') or log_file_path(profiles_dir, 'server')}"
        )
    else:
        lines.append("Proxy server: stopped")

    if client:
        lines.append(
            f"Proxy client: RUNNING (pid {client['pid']})\n"
            f"  Profile:   {client['profile']} (auto-configured)\n"
            f"  Listening: http://{client['host']}:{client['port']}\n"
            f"  Upstream:  {client['upstream']}\n"
            f"  Logs:      {client.get('log_file') or log_file_path(profiles_dir, 'client')}"
        )
    else:
        lines.append("Proxy client: stopped")

    return "\n".join(lines)


def read_logs(profiles_dir, kind, lines=50):
    path = log_file_path(profiles_dir, kind)
    if not os.path.exists(path):
        return f"No log file found for proxy {kind} ({path})."
    with open(path, encoding="utf-8", errors="replace") as handle:
        all_lines = handle.readlines()
    return "".join(all_lines[-lines:])

