# Agent Handoff Specification: `ephemeral-desktop-mcp` called vamo-mcp

> **Target Reader:** AI Coding Harness / Autonomous Software Engineering Agent (Claude Code, OpenCode, PI Agent, etc.)  
> **Objective:** Build a complete, production-ready, low-latency **Model Context Protocol (MCP) Server** and **Docker Sandbox Image** that provides an isolated, disposable virtual desktop (`Xvfb` + `Openbox` + `Chromium`) for AI Computer Use and Browser Use without hijacking the host user's physical mouse, keyboard, or screen.

---

## 1. Context & Problem Statement

When AI coding agents perform **Computer Use** (GUI automation, clicking, typing, taking screenshots) or **Browser Use** directly on the host machine, they hijack the physical cursor and active window focus. This blocks the human developer from using their computer while the agent works.

### Core Solution
Create an **Ephemeral Desktop Sandbox MCP** consisting of:
1. **A Pre-warmed Docker Container ("The Agent VM"):** Runs a headless X11 server (`Xvfb`) at `1280x800`, a lightweight window manager (`Openbox`), `Chromium`, an internal low-latency HTTP daemon (`FastAPI` on port `8000`) to execute `xdotool` actions and capture in-memory screenshots in `<15ms`, and an optional `noVNC` server (`port 6080`) so the user can watch the screen in a browser tab if desired.
2. **Transparent Localhost Bridge:** The container transparently forwards traffic aimed at common development ports on its own `localhost` (e.g., `3000`, `5173`, `8080`, `4200`) to `host.docker.internal`, allowing the agent inside the container to test web apps running on the host without URL modifications.
3. **Host MCP Server (`stdio`):** Exposes tools (`sandbox_init`, `sandbox_computer_action`, `sandbox_bash`, `sandbox_status`, `sandbox_destroy`) to any MCP-compatible harness, managing the Docker lifecycle automatically.

---

## 2. Target Directory Structure

Create the project with the following exact structure:

```text
ephemeral-desktop-mcp/
├── README.md
├── pyproject.toml                 # Host MCP Server dependencies (FastMCP, docker, httpx, pydantic)
├── Makefile                       # Build & test automation scripts
├── sandbox_image/                 # Docker image context for the ephemeral VM
│   ├── Dockerfile
│   ├── supervisord.conf           # Process manager for Xvfb, Openbox, x11vnc, noVNC, and Agent Daemon
│   ├── entrypoint.sh              # Sets up socat localhost port forwarding & launches supervisord
│   └── agent_daemon/              # Internal low-latency HTTP server inside container
│       ├── requirements.txt
│       └── main.py                # FastAPI server executing xdotool, scrot/mss, and bash commands
└── src/
    └── ephemeral_desktop_mcp/
        ├── __init__.py
        ├── docker_manager.py      # Manages warm container lifecycle, volumes, and network bridge
        └── server.py              # FastMCP server exposing tools to the AI harness
```

---

## 3. Component 1: The Sandbox Docker Image (`sandbox_image/`)

### 3.1. Technical Requirements
- **Base Image:** `ubuntu:24.04` (or `debian:bookworm-slim`)
- **Resolution & Display:** `DISPLAY=:99`, Resolution `1280x800x24` (optimal aspect ratio and token cost for LLM vision models).
- **Packages Required:**
  - X11 & GUI: `xvfb`, `openbox`, `x11vnc`, `novnc`, `websockify`, `xdotool`, `scrot`, `imagemagick`, `dbus-x11`, `xclip`
  - Browser: `chromium` or `google-chrome-stable` (configured to run with `--no-sandbox --disable-dev-shm-usage --disable-gpu --start-maximized`)
  - System & Networking: `supervisor`, `socat`, `iproute2`, `curl`, `wget`, `git`, `sudo`, `python3`, `python3-pip`, `python3-venv`, `nodejs`, `npm`
- **User Setup:** Run desktop session as a non-root user `agent` (`UID 1000`) with passwordless `sudo` enabled (`agent ALL=(ALL) NOPASSWD:ALL`), so the agent can install any system packages at runtime while Chromium still runs cleanly.

### 3.2. Transparent Host-Port Forwarding (`entrypoint.sh`)
When the agent runs a dev server on the Host (e.g., `npm run dev` on host port `3000`) and opens `http://localhost:3000` inside the container's Chromium, it must resolve to the Host's port `3000`.
- Implement a loop in `entrypoint.sh` that reads an environment variable `FORWARD_PORTS="3000,3001,4200,5000,5173,8000,8080,8888"` (Note: move the internal daemon port to `9999` so `8000` remains free for forwarding!).
- For each port in `FORWARD_PORTS`, spawn a background `socat` process:
  ```bash
  socat TCP-LISTEN:${PORT},fork,reuseaddr,bind=127.0.0.1 TCP:host.docker.internal:${PORT} &
  ```

### 3.3. Internal Low-Latency Daemon (`sandbox_image/agent_daemon/main.py`)
Running `docker exec` for every mouse move or screenshot adds `100ms-300ms` of container attach overhead. Instead, run a FastAPI server inside the container on `0.0.0.0:9999`.

#### Endpoints Required on Port `9999`:

1. **`GET /health`**
   - Verifies that `Xvfb` display `:99` is responsive and returns `{"status": "ok", "display": ":99", "resolution": [1280, 800]}`.

2. **`POST /computer`**
   - **Request Body Schema:**
     ```json
     {
       "action": "mouse_move | left_click | right_click | double_click | triple_click | middle_click | left_click_drag | scroll | type | key | wait | screenshot | cursor_position",
       "coordinate": [640, 400],
       "start_coordinate": [100, 100],
       "text": "string to type or key combo like Return, Control_L+l",
       "scroll_direction": "up | down | left | right",
       "scroll_amount": 5,
       "duration": 1.0
     }
     ```
   - **Behavior:**
     - Executes the corresponding `xdotool` command against `DISPLAY=:99`.
     - Waits `150ms` after mutating actions (clicks/keys) for UI repaint.
     - Automatically takes a full-screen PNG screenshot (using Python `mss` library for `<10ms` in-memory capture, with `scrot` as fallback), compresses/encodes it as Base64, and returns:
       ```json
       {
         "success": true,
         "cursor": [640, 400],
         "screenshot_base64": "iVBORw0KGgoAAAANSUhEUg...",
         "error": null
       }
       ```

3. **`POST /bash`**
   - **Request Body Schema:**
     ```json
     {
       "command": "google-chrome http://localhost:3000 &",
       "cwd": "/workspace",
       "timeout": 30
     }
     ```
   - **Behavior:**
     - Runs the command inside `/bin/bash` with `DISPLAY=:99` exported.
     - Handles background commands (`&`) properly without hanging `subprocess` (by redirecting stdout/stderr or detaching if needed, or using `asyncio.create_subprocess_shell` with timeout).
     - Returns `{"exit_code": 0, "stdout": "...", "stderr": "..."}`.

---

## 4. Component 2: Host MCP Server (`src/ephemeral_desktop_mcp/`)

### 4.1. Docker Lifecycle Manager (`docker_manager.py`)
The `SandboxDockerManager` class manages the container using the official Python `docker` SDK:
- **Container Naming:** `ephemeral-agent-sandbox-{session_id}`
- **Runtime Flags:**
  - `image="ephemeral-desktop-sandbox:latest"`
  - `shm_size="2g"` (Mandatory so Chromium does not crash on heavy pages)
  - `extra_hosts={"host.docker.internal": "host-gateway"}` (Enables Linux & macOS/Windows host networking bridge)
  - `ports={'9999/tcp': None, '6080/tcp': None}` (Dynamically binds host ephemeral ports to avoid conflicts if multiple coding agents run simultaneously)
  - `volumes={host_workspace_path: {'bind': '/workspace', 'mode': mount_mode}}` where `mount_mode` defaults to `'ro'` (Read-Only) or `'rw'` based on configuration.
  - `auto_remove=True` (Ensures zero leftover containers when stopped).
- **Cleanup Guarantee:** Register Python `atexit` and `SIGINT`/`SIGTERM` signal handlers so if the AI Harness closes the MCP `stdio` pipe, the container is immediately killed.

### 4.2. MCP Tools Specification (`server.py`)
Use the official `mcp` Python SDK (`FastMCP`). Note that returning screenshots via `FastMCP`'s `Image` return type allows multimodal LLMs (like Claude and GPT-4o) to visually inspect the virtual screen natively.

#### Tool 1: `sandbox_init`
- **Description:** Starts or resets the isolated disposable desktop sandbox container. Must be called before running GUI or isolated browser tests.
- **Arguments:**
  - `workspace_path: Optional[str] = None` (Host path to mount at `/workspace` inside the container)
  - `mount_mode: Literal["ro", "rw"] = "ro"` (Mount workspace read-only by default for safety)
  - `forward_ports: list[int] = [3000, 5173, 8000, 8080]` (Host localhost ports to mirror inside the container)
  - `reset_if_running: bool = False`
- **Returns:** JSON string containing `container_id`, `novnc_url` (e.g., `http://localhost:54321/vnc.html?autoconnect=true`), `daemon_url`, and forwarded ports list.

#### Tool 2: `sandbox_computer_action`
- **Description:** Performs GUI actions (mouse click, move, type, key press, scroll, or screenshot) inside the isolated `1280x800` virtual desktop without touching the user's physical mouse or screen. Automatically returns an updated screenshot of the virtual screen.
- **Arguments:**
  - `action: Literal["mouse_move", "left_click", "right_click", "double_click", "triple_click", "middle_click", "left_click_drag", "scroll", "type", "key", "wait", "screenshot", "cursor_position"]`
  - `coordinate: Optional[list[int]] = None` (`[x, y]` within `1280x800`)
  - `start_coordinate: Optional[list[int]] = None` (Used for `left_click_drag`)
  - `text: Optional[str] = None` (Text to type or X11 key name/combo like `"Return"`, `"BackSpace"`, `"ctrl+l"`, `"alt+F4"`)
  - `scroll_direction: Optional[Literal["up", "down", "left", "right"]] = "down"`
  - `scroll_amount: int = 5`
  - `duration: float = 1.0`
- **Returns:** A list containing text metadata (action status + cursor coordinates) AND an `mcp.types.ImageContent` (`mimeType="image/png"`, `data=screenshot_base64`).

#### Tool 3: `sandbox_open_url`
- **Description:** High-level helper that launches Chromium (if not already running) inside the sandbox pointed at `url`, waits for the page to render, and returns a visual screenshot.
- **Arguments:**
  - `url: str` (e.g., `"http://localhost:3000"`)
  - `wait_seconds: float = 2.0`
- **Returns:** `ImageContent` screenshot of the loaded page + status text.

#### Tool 4: `sandbox_bash`
- **Description:** Executes an arbitrary shell command inside the disposable Ubuntu container (has passwordless `sudo`, full internet access, and isolated filesystem). Ideal for installing packages, running destructive tests, or checking logs.
- **Arguments:**
  - `command: str`
  - `cwd: str = "/workspace"`
  - `timeout: int = 30`
  - `take_screenshot_after: bool = False`
- **Returns:** `stdout`, `stderr`, `exit_code`, and optional `ImageContent` if `take_screenshot_after=True`.

#### Tool 5: `sandbox_destroy`
- **Description:** Immediately terminates and wipes the disposable sandbox container.

---

## 5. Implementation Details & Edge-Case Handling

When writing the code, pay strict attention to the following technical nuances:

1. **Key Mapping Normalization in `agent_daemon/main.py`:**
   LLMs frequently pass key names in varying formats (e.g., `"Enter"` vs `"Return"`, `"Ctrl+C"` vs `"ctrl+c"`, `"Esc"` vs `"Escape"`, `"Cmd+L"`). Create a normalization dictionary in `main.py` before passing keys to `xdotool key`:
   ```python
   KEY_MAP = {
       "enter": "Return",
       "return": "Return",
       "esc": "Escape",
       "escape": "Escape",
       "backspace": "BackSpace",
       "tab": "Tab",
       "space": "space",
       "up": "Up",
       "down": "Down",
       "left": "Left",
       "right": "Right",
       "pageup": "Page_Up",
       "pagedown": "Page_Down",
       "cmd": "ctrl",
       "command": "ctrl",
       "meta": "Super_L",
   }
   ```

2. **Preventing Zombie Subprocesses in `/bash`:**
   If an agent executes `chromium http://localhost:3000 &` via `/bash`, standard `subprocess.run(..., capture_output=True)` in Python will hang forever because the backgrounded `chromium` process inherits the open `stdout`/`stderr` file descriptors.
   - **Fix:** When running commands in `/bash`, or when backgrounding GUI apps, redirect output or close file descriptors (`close_fds=True`), or provide a dedicated `nohup ... >/tmp/gui.log 2>&1 &` wrapper in `sandbox_open_url`.

3. **Chromium First-Run Popups & Crash Bubbles:**
   Launch Chromium with the following flags so the agent never wastes turns clicking away "Set as default browser" or keyring modals:
   ```bash
   chromium \
     --no-sandbox \
     --disable-dev-shm-usage \
     --disable-gpu \
     --password-store=basic \
     --no-first-run \
     --no-default-browser-check \
     --disable-infobars \
     --disable-session-crashed-bubble \
     --window-position=0,0 \
     --window-size=1280,800
   ```

4. **Fast In-Memory Screenshots with `mss`:**
   Use the Python `mss` library connected to `DISPLAY=:99` inside `agent_daemon/main.py` and draw a small red circle (`4px` radius using `Pillow` `ImageDraw`) at the current cursor coordinates `(x, y)` obtained from `xdotool getmouselocation --shell`.
   - **Why draw the cursor?** `Xvfb` screenshots often do not render the X11 hardware cursor sprite, leaving the LLM blind to where its mouse actually is. Drawing a small visible crosshair/dot on the PNG before encoding to Base64 dramatically improves LLM click accuracy!

---

## 6. Step-by-Step Execution Plan for the Agent

Follow these phases sequentially. Verify each phase before moving to the next:

### Phase 1: Scaffold & Build `sandbox_image`
1. Create `sandbox_image/agent_daemon/requirements.txt` (`fastapi`, `uvicorn`, `pydantic`, `mss`, `Pillow`).
2. Implement `sandbox_image/agent_daemon/main.py` with `/health`, `/computer` (with key normalization + `Pillow` cursor dot rendering), and `/bash`.
3. Create `sandbox_image/supervisord.conf` managing:
   - `xvfb` (`Xvfb :99 -screen 0 1280x800x24 -ac +extension GLX +render -noreset`)
   - `openbox` (`openbox-session`)
   - `x11vnc` (`x11vnc -display :99 -forever -shared -nopw -rfbport 5900`)
   - `novnc` (`websockify --web=/usr/share/novnc/ 6080 localhost:5900`)
   - `agent_daemon` (`uvicorn main:app --host 0.0.0.0 --port 9999`)
4. Create `sandbox_image/entrypoint.sh` (sets up `socat` forwarding for `FORWARD_PORTS` to `host.docker.internal` and execs `supervisord`).
5. Create `sandbox_image/Dockerfile` and build the image tagged as `ephemeral-desktop-sandbox:latest`.

### Phase 2: Verify the Container Standalone
1. Run a test container manually via CLI.
2. Hit `http://localhost:<mapped_9999>/health` with `curl`.
3. Hit `POST /computer` with `{"action": "screenshot"}` and verify a valid `1280x800` PNG Base64 is returned in `<50ms`.

### Phase 3: Implement the Host MCP Server
1. Create `pyproject.toml` using `uv` or standard `pip` (`mcp[cli]`, `docker`, `httpx`, `pydantic`).
2. Implement `src/ephemeral_desktop_mcp/docker_manager.py` with lazy initialization, port discovery, health-check polling on startup, and `atexit` cleanup.
3. Implement `src/ephemeral_desktop_mcp/server.py` exposing `sandbox_init`, `sandbox_computer_action`, `sandbox_open_url`, `sandbox_bash`, and `sandbox_destroy`.

### Phase 4: End-to-End Integration Test & Documentation
1. Write an automated test script `tests/test_e2e.py` that starts a dummy HTTP server on the Host (`port 8765`), initializes the sandbox forwarding `8765`, opens `http://localhost:8765` inside the container's Chromium, clicks a button via `sandbox_computer_action`, and asserts the screenshot and state change.
2. Document the exact MCP configuration snippet in `README.md` for:
   - **Claude Code** (`claude mcp add ...` or `.mcp.json`)
   - **OpenCode / Cursor / PI Agent** (`mcpServers` JSON block)

---

## 7. Ready-to-Use Harness Configuration (Target Output for `README.md`)

Once built, the human user will register this MCP server in their harness using:

```json
{
  "mcpServers": {
    "ephemeral-sandbox": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/TO/ephemeral-desktop-mcp",
        "run",
        "ephemeral-desktop-mcp"
      ]
    }
  }
}
```