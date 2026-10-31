"""Tests for Tailscale proxy server/client integration and profile lifecycle."""

import asyncio
import importlib
import json
import os
import subprocess
import time

import httpx
import pytest

from tui_claude import proxy, sharing, tmp_mode


@pytest.fixture
def profiles_dir(tmp_path):
    path = tmp_path / "claude-profiles"
    (path / "work").mkdir(parents=True)
    (path / "personal").mkdir(parents=True)
    return str(path)


@pytest.fixture
def tui(tmp_path, monkeypatch):
    monkeypatch.setenv("TUI_CLAUDE_TEST_DIR", str(tmp_path))
    from tui_claude import main as main_module
    importlib.reload(main_module)
    main_module.refresh_profiles()
    yield main_module
    monkeypatch.delenv("TUI_CLAUDE_TEST_DIR", raising=False)
    importlib.reload(main_module)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --- URL & Tailscale helpers -------------------------------------------------

def test_normalize_upstream():
    assert proxy.normalize_upstream("100.78.40.97") == "http://100.78.40.97:8888"
    assert proxy.normalize_upstream("100.78.40.97:9000") == "http://100.78.40.97:9000"
    assert proxy.normalize_upstream("http://100.78.40.97:8888/") == "http://100.78.40.97:8888"
    assert proxy.normalize_upstream("https://my-host.ts.net") == "https://my-host.ts.net:8888"

    with pytest.raises(ValueError):
        proxy.normalize_upstream("   ")


def test_detect_tailscale_ip():
    def fake_run(cmd, **kwargs):
        assert cmd == ["tailscale", "ip", "-4"]
        return subprocess.CompletedProcess(cmd, 0, stdout="100.101.102.103\n", stderr="")

    assert proxy.detect_tailscale_ip(run=fake_run) == "100.101.102.103"


def test_detect_tailscale_ip_missing_binary():
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("tailscale")

    with pytest.raises(RuntimeError, match="Pass --host"):
        proxy.detect_tailscale_ip(run=fake_run)


# --- Token extraction --------------------------------------------------------

def test_extract_session_token_reads_selected_profile(profiles_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_CORPORATE_SESSION_TOKEN", raising=False)
    work_dir = os.path.join(profiles_dir, "work")
    write_json(os.path.join(work_dir, ".credentials.json"), {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-work-token",
            "expiresAt": (time.time() + 3600) * 1000,
        }
    })

    token = proxy.extract_session_token(work_dir, extra_paths=[])
    assert token == "sk-ant-oat01-work-token"


def test_extract_session_token_warns_when_expired(profiles_dir, capsys):
    work_dir = os.path.join(profiles_dir, "work")
    write_json(os.path.join(work_dir, ".credentials.json"), {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-expired",
            "expiresAt": (time.time() - 60) * 1000,
        }
    })

    token = proxy.extract_session_token(work_dir, extra_paths=[])
    assert token == "sk-ant-oat01-expired"
    assert "EXPIRED" in capsys.readouterr().out


def test_extract_session_token_falls_back_to_env(profiles_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CORPORATE_SESSION_TOKEN", "env-corp-token")
    empty_dir = os.path.join(profiles_dir, "personal")

    assert proxy.extract_session_token(empty_dir, extra_paths=[]) == "env-corp-token"


# --- ASGI Server & Client routing --------------------------------------------

def test_server_and_client_end_to_end_streaming(profiles_dir):
    work_dir = os.path.join(profiles_dir, "work")
    write_json(os.path.join(work_dir, ".credentials.json"), {
        "claudeAiOauth": {"accessToken": "sk-ant-oat01-secret-corp"}
    })

    captured = {}

    async def fake_anthropic(request: httpx.Request):
        captured["auth"] = request.headers.get("authorization")
        captured["beta"] = request.headers.get("anthropic-beta")
        captured["x_api_key"] = request.headers.get("x-api-key")
        captured["accept_encoding"] = request.headers.get("accept-encoding")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: {\"text\": \"hello from corp\"}\n\n",
        )

    anthropic_transport = httpx.MockTransport(fake_anthropic)
    server_app = proxy.create_server_app(
        work_dir,
        anthropic_url="https://api.anthropic.com",
        client_factory=lambda: httpx.AsyncClient(transport=anthropic_transport),
        extra_cred_paths=[],
    )

    server_transport = httpx.ASGITransport(app=server_app)
    client_app = proxy.create_client_app(
        "http://100.78.40.97:8888",
        client_factory=lambda: httpx.AsyncClient(transport=server_transport),
    )

    async def run_request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client_app),
            base_url="http://127.0.0.1:9999",
        ) as cli:
            return await cli.post(
                "/v1/messages",
                headers={
                    "x-api-key": "proxy-local",
                    "anthropic-beta": "claude-code-20250219",
                    "accept-encoding": "gzip, zstd",
                },
                content=b'{"model":"claude-sonnet-4-20250514"}',
            )

    res = asyncio.run(run_request())
    assert res.status_code == 200
    assert b"hello from corp" in res.content
    assert captured["auth"] == "Bearer sk-ant-oat01-secret-corp"
    assert "oauth-2025-04-20" in captured["beta"]
    assert "claude-code-20250219" in captured["beta"]
    assert captured["x_api_key"] is None


def test_server_returns_401_when_no_token_available(profiles_dir, monkeypatch):
    monkeypatch.delenv("CLAUDE_CORPORATE_SESSION_TOKEN", raising=False)
    empty_dir = os.path.join(profiles_dir, "personal")
    server_app = proxy.create_server_app(empty_dir, extra_cred_paths=[])

    async def run_request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server_app),
            base_url="http://100.78.40.97:8888",
        ) as cli:
            return await cli.post("/v1/messages", content=b"{}")

    res = asyncio.run(run_request())
    assert res.status_code == 401
    assert res.json()["error"]["type"] == "authentication_error"


def test_client_returns_502_when_tailscale_connection_drops():
    async def failing_handler(request: httpx.Request):
        raise httpx.ConnectError("Tailscale peer unreachable")

    client_app = proxy.create_client_app(
        "100.78.40.97",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)),
    )

    async def run_request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client_app),
            base_url="http://127.0.0.1:9999",
        ) as cli:
            return await cli.post("/v1/messages", content=b"{}")

    res = asyncio.run(run_request())
    assert res.status_code == 502
    assert res.json()["error"]["type"] == "proxy_connection_error"


# --- Automatic Profile Lifecycle (Isolated & Shared) -------------------------

def test_attach_and_detach_isolated_profile_clean_slate(profiles_dir):
    settings_path = os.path.join(profiles_dir, "work", "settings.json")
    assert not os.path.exists(settings_path)

    proxy.attach_client_profile(profiles_dir, "work", port=9999, upstream="100.78.40.97")

    assert os.path.isfile(settings_path)
    data = read_json(settings_path)
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "proxy-local"
    assert tmp_mode.live_pins(profiles_dir).get("work") == 1

    proxy.detach_client_profile(profiles_dir)

    assert not os.path.exists(settings_path), "removed cleanly since it did not exist before"
    assert tmp_mode.live_pins(profiles_dir).get("work", 0) == 0


def test_attach_and_detach_preserves_existing_settings_and_env(profiles_dir):
    settings_path = os.path.join(profiles_dir, "work", "settings.json")
    write_json(settings_path, {
        "theme": "dark",
        "env": {"CUSTOM_VAR": "keep-me", "ANTHROPIC_BASE_URL": "https://custom.local"},
    })

    proxy.attach_client_profile(profiles_dir, "work", port=9999, upstream="100.78.40.97")
    assert read_json(settings_path)["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"

    proxy.detach_client_profile(profiles_dir)
    restored = read_json(settings_path)
    assert restored == {
        "theme": "dark",
        "env": {"CUSTOM_VAR": "keep-me", "ANTHROPIC_BASE_URL": "https://custom.local"},
    }


def test_shared_profile_proxy_never_pollutes_shared_pool(profiles_dir):
    write_json(os.path.join(profiles_dir, "work", "settings.json"), {"model": "opus"})
    sharing.enable_sharing(profiles_dir, "work")
    sharing.enable_sharing(profiles_dir, "personal")

    pool_settings = os.path.join(sharing.pool_dir(profiles_dir), "settings.json")
    work_settings = os.path.join(profiles_dir, "work", "settings.json")
    personal_settings = os.path.join(profiles_dir, "personal", "settings.json")

    proxy.attach_client_profile(profiles_dir, "work", port=9999, upstream="100.78.40.97")

    # `work` has the proxy env, but `_shared` and `personal` remain untouched!
    assert read_json(work_settings)["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"
    assert "env" not in read_json(pool_settings)
    assert "env" not in read_json(personal_settings)

    # Even if repair_sharing runs while proxy client is alive, it must not promote proxy env to pool.
    assert sharing.repair_sharing(profiles_dir, "work") == []
    assert "env" not in read_json(pool_settings)

    # Simulate user changing a non-proxy setting while using the proxy.
    current = read_json(work_settings)
    current["alwaysThinkingEnabled"] = True
    write_json(work_settings, current)

    proxy.detach_client_profile(profiles_dir)

    # Symlink to pool is restored, proxy env is gone, and non-proxy setting change is kept!
    assert os.path.islink(work_settings)
    assert read_json(pool_settings) == {"model": "opus", "alwaysThinkingEnabled": True}
    assert read_json(work_settings) == {"model": "opus", "alwaysThinkingEnabled": True}


def test_dead_proxy_process_is_auto_cleaned_on_refresh(tui):
    tui.add_profile("work")
    sharing.enable_sharing(tui.PROFILES_DIR, "work")

    # Attach with a dead PID (e.g. process was killed with SIGKILL)
    proxy.attach_client_profile(
        tui.PROFILES_DIR, "work", port=9999, upstream="100.78.40.97", pid=999999
    )
    work_settings = os.path.join(tui.PROFILES_DIR, "work", "settings.json")
    assert "ANTHROPIC_BASE_URL" in read_json(work_settings).get("env", {})

    # Next refresh automatically heals the profile and restores the pool symlink
    tui.refresh_profiles()

    assert os.path.islink(work_settings)
    assert "env" not in proxy.sharing.load_json(work_settings)
    assert tui.state["proxies"] == {"server": None, "client": None}


# --- CLI & Help flags --------------------------------------------------------

@pytest.mark.parametrize("argv,expected", [
    (["proxy", "--help"], "tui-claude proxy server"),
    (["proxy", "-h"], "tui-claude proxy client"),
    (["proxy", "server", "--help"], "--host IP"),
    (["proxy", "client", "--help"], "UPSTREAM"),
    (["proxy", "status", "--help"], "proxy status"),
    (["proxy", "stop", "--help"], "proxy stop"),
    (["proxy", "logs", "--help"], "proxy logs"),
])
def test_proxy_help_commands(tui, capsys, argv, expected):
    assert tui.main(argv) == 0
    assert expected in capsys.readouterr().out


def test_proxy_status_and_stop_when_idle(tui, capsys):
    assert tui.main(["proxy", "status"]) == 0
    out = capsys.readouterr().out
    assert "Proxy server: stopped" in out
    assert "Proxy client: stopped" in out

    assert tui.main(["proxy", "stop"]) == 0
    assert "No running proxy processes" in capsys.readouterr().out


def test_tui_renders_active_proxy_banners(tui):
    proxy.register_server_state(tui.PROFILES_DIR, "default", "100.78.40.97", 8888)
    proxy.attach_client_profile(tui.PROFILES_DIR, "default", 9999, "100.78.40.97")
    tui.refresh_profiles()

    screen = "".join(text for _, text in tui.get_screen_text())
    assert "Proxy server:" in screen
    assert "100.78.40.97:8888" in screen
    assert "Proxy client:" in screen
    assert "127.0.0.1:9999" in screen

    proxy.detach_client_profile(tui.PROFILES_DIR)
    proxy.unregister_server_state(tui.PROFILES_DIR)


def test_proxy_server_opens_tui_picker_when_profile_omitted(tui, monkeypatch):
    tui.add_profile("work")
    served = {}

    monkeypatch.setattr(tui, "run_tui", lambda: "work")
    monkeypatch.setattr(
        proxy,
        "serve_foreground",
        lambda pdir, prof, host, port: served.update(prof=prof, host=host, port=port) or 0,
    )

    rc = tui.main(["proxy", "server", "--host", "100.78.40.97", "--port", "8899"])
    assert rc == 0
    assert served == {"prof": "work", "host": "100.78.40.97", "port": 8899}


def test_proxy_logs_reads_recent_lines(tui, capsys):
    log_path = proxy.log_file_path(tui.PROFILES_DIR, "client")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write("line 1\nline 2\nline 3\n")

    assert tui.main(["proxy", "logs", "client", "--lines", "2"]) == 0
    assert capsys.readouterr().out == "line 2\nline 3\n"

