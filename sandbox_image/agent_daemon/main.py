"""Private X11 automation daemon and supervisor startup gates."""
import os
import re
import subprocess
import sys
import time

SUPERVISOR = ['supervisorctl', '-c', '/etc/supervisor/supervisord.conf']


def run(argv, *, timeout=5, **kwargs):
    return subprocess.run(argv, check=True, timeout=timeout, **kwargs)


def browser_argv(url: str) -> list[str]:
    return ['chromium', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
            '--password-store=basic', '--no-first-run', '--no-default-browser-check',
            '--disable-infobars', '--disable-session-crashed-bubble',
            '--window-position=0,0', '--window-size=1280,800', '--start-maximized',
            '--ozone-platform=x11', '--user-data-dir=/home/agent/.config/chromium', url]


def wm_ready():
    p = run(['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'], timeout=2, capture_output=True, text=True)
    match = re.search(r'window id # (0x[0-9a-fA-F]+)', p.stdout)
    if not match or int(match[1], 16) == 0:
        return False
    window = run(['xprop', '-id', match[1], '_NET_SUPPORTING_WM_CHECK'],
                 timeout=2, capture_output=True, text=True)
    owner = re.search(r'window id # (0x[0-9a-fA-F]+)', window.stdout)
    return bool(owner and int(owner[1], 16) == int(match[1], 16))


def browser_ready():
    p = subprocess.run(['xdotool', 'search', '--onlyvisible', '--class', 'chromium'],
                       capture_output=True, timeout=2)
    return p.returncode == 0 and bool(p.stdout.strip())


def gate(mode):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            run(['xdpyinfo'], timeout=2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if mode == 'wait-x' or wm_ready():
                return
        except (subprocess.SubprocessError, OSError):
            pass
        time.sleep(.1)
    raise SystemExit(f'{mode} readiness deadline exceeded')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('wait-x', 'wait-wm'):
        gate(sys.argv[1])
        raise SystemExit(0)
    elif len(sys.argv) == 3 and sys.argv[1] == 'browser':
        os.execvp('chromium', browser_argv(sys.argv[2]))
    else:
        raise SystemExit('Usage: main.py wait-x|wait-wm|browser URL')


# CLI gates exit before importing HTTP/capture dependencies.
import asyncio
import base64
import io
import secrets
import signal
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit

import mss
from mss.exception import ScreenShotError
from PIL import Image, ImageDraw
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

StrictInt = Annotated[int, Field(strict=True)]
Coordinate = Annotated[list[StrictInt], Field(min_length=2, max_length=2)]
FiniteDuration = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Action = Literal['mouse_move', 'left_click', 'right_click', 'double_click',
                 'triple_click', 'middle_click', 'left_click_drag', 'scroll',
                 'type', 'key', 'wait', 'screenshot', 'cursor_position']
KEY_MAP = {'enter': 'Return', 'return': 'Return', 'esc': 'Escape', 'escape': 'Escape',
           'backspace': 'BackSpace', 'tab': 'Tab', 'space': 'space',
           'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
           'pageup': 'Page_Up', 'pagedown': 'Page_Down', 'cmd': 'ctrl',
           'command': 'ctrl', 'ctrl': 'ctrl', 'control': 'ctrl', 'alt': 'alt',
           'shift': 'shift', 'super': 'Super_L', 'meta': 'Super_L'}


def normalize_key(text):
    tokens = text.split('+')
    if any(not t or '\0' in t for t in tokens):
        raise ValueError('key must contain nonempty keysyms without NUL')
    tokens = [KEY_MAP.get(t.casefold(), t) for t in tokens]
    modifiers = {'ctrl', 'Control_L', 'Control_R', 'alt', 'Alt_L', 'Alt_R', 'Super_L', 'Super_R', 'Meta_L', 'Meta_R'}
    if any(t in modifiers for t in tokens) and not any(t in ('shift', 'Shift_L', 'Shift_R') for t in tokens):
        tokens = [t.lower() if len(t) == 1 and 'A' <= t <= 'Z' else t for t in tokens]
    combo = '+'.join(tokens)
    if combo.startswith('-'):
        raise ValueError('key must not begin with -')
    return combo


class ComputerRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Action
    coordinate: Coordinate | None = None
    start_coordinate: Coordinate | None = None
    text: str | None = None
    scroll_direction: Literal['up', 'down', 'left', 'right'] | None = 'down'
    scroll_amount: Annotated[StrictInt, Field(gt=0)] = 5
    duration: FiniteDuration = 1.0

    @model_validator(mode='after')
    def validate_action(self):
        for c in (self.coordinate, self.start_coordinate):
            if c is not None and not (0 <= c[0] < 1280 and 0 <= c[1] < 800):
                raise ValueError('coordinate outside 1280x800 display')
        if self.action in ('mouse_move', 'left_click_drag') and self.coordinate is None:
            raise ValueError('coordinate required')
        if self.action == 'left_click_drag' and self.start_coordinate is None:
            raise ValueError('start_coordinate required')
        if self.text is not None and '\0' in self.text:
            raise ValueError('text must not contain NUL')
        if self.action in ('type', 'key') and self.text is None:
            raise ValueError('text required')
        if self.action == 'key':
            normalize_key(self.text)
        if self.scroll_direction is None:
            self.scroll_direction = 'down'
        return self


class BashRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command: str
    cwd: str = '/workspace'
    timeout: Annotated[StrictInt, Field(gt=0)] = 30

    @model_validator(mode='after')
    def validate_command(self):
        if not self.command.strip() or '\0' in self.command:
            raise ValueError('command must be nonempty without NUL')
        if '\0' in self.cwd or not os.path.isabs(self.cwd) or not os.path.isdir(self.cwd):
            raise ValueError('cwd must be an existing absolute directory')
        return self


class OpenUrlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str
    wait_seconds: FiniteDuration = 2.0

    @model_validator(mode='after')
    def validate_url(self):
        parsed = urlsplit(self.url)
        if '\0' in self.url or parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('url must be HTTP/HTTPS with a hostname')
        parsed.port  # validates range and malformed ports
        return self


_capture = None
_last_frame = None


def close_capture():
    global _capture, _last_frame
    if _capture is not None:
        _capture.close()
        _capture = None
    _last_frame = None


def cursor_position():
    location = run(['xdotool', 'getmouselocation', '--shell'], capture_output=True, text=True)
    fields = dict(line.split('=', 1) for line in location.stdout.splitlines() if '=' in line)
    return int(fields['X']), int(fields['Y'])


def capture_screen() -> dict:
    global _capture, _last_frame
    x, y = cursor_position()
    frame = None
    try:
        if _capture is None:
            _capture = mss.MSS(display=':99')
        shot = _capture.grab({'left': 0, 'top': 0, 'width': 1280, 'height': 800})
        if shot.size != (1280, 800):
            raise RuntimeError(f'Unexpected capture size: {shot.size}')
        # Compare every freshly captured pixel; only encoding is reused, never X11 reads.
        if _last_frame is not None and _last_frame[:2] == (x, y) and shot.raw == _last_frame[2]:
            return {'success': True, 'cursor': [x, y],
                    'screenshot_base64': _last_frame[3], 'error': None}
        image = Image.frombytes('RGB', shot.size, shot.raw, 'raw', 'BGRX')
        frame = shot.raw
    except ScreenShotError:
        _last_frame = None
        png = run(['scrot', '-z', '-'], capture_output=True).stdout
        with Image.open(io.BytesIO(png)) as source:
            image = source.convert('RGB')
    if image.size != (1280, 800):
        raise RuntimeError(f'Unexpected capture size: {image.size}')
    ImageDraw.Draw(image).ellipse((x-4, y-4, x+4, y+4), fill='red')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', compress_level=1, optimize=False)
    encoded = base64.b64encode(buffer.getvalue()).decode('ascii')
    if frame is not None:
        _last_frame = (x, y, frame, encoded)
    return {'success': True, 'cursor': [x, y], 'screenshot_base64': encoded, 'error': None}


def computer(req):
    def xdo(*args, timeout=5, **kwargs):
        return run(['xdotool', *map(str, args)], timeout=timeout, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, **kwargs)
    def move(coordinate):
        # xdotool --sync waits forever when pointer is already at destination.
        if cursor_position() != tuple(coordinate):
            xdo('mousemove', '--sync', *coordinate)
    action = req.action
    if action == 'left_click_drag':
        move(req.start_coordinate)
        try:
            xdo('mousedown', 1)
            move(req.coordinate)
        finally:
            xdo('mouseup', 1)
    elif action in ('mouse_move', 'left_click', 'middle_click', 'right_click', 'double_click', 'triple_click', 'scroll'):
        if req.coordinate is not None:
            move(req.coordinate)
        if action in ('left_click', 'middle_click', 'right_click'):
            xdo('click', {'left_click': 1, 'middle_click': 2, 'right_click': 3}[action])
        elif action in ('double_click', 'triple_click'):
            xdo('click', '--repeat', 2 if action == 'double_click' else 3, '--delay', 100, 1)
        elif action == 'scroll':
            xdo('click', '--repeat', req.scroll_amount, '--delay', 100,
                {'up': 4, 'down': 5, 'left': 6, 'right': 7}[req.scroll_direction],
                timeout=5 + .1*req.scroll_amount)
    elif action == 'type':
        if req.text.isascii():
            xdo('type', '--clearmodifiers', '--delay', 0, '--', req.text, timeout=5+.02*len(req.text))
        else:
            run(['xclip', '-selection', 'clipboard', '-in'], input=req.text.encode('utf-8'),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5+.02*len(req.text))
            xdo('key', '--clearmodifiers', 'ctrl+v')
    elif action == 'key':
        xdo('key', '--clearmodifiers', normalize_key(req.text))
    elif action == 'wait':
        time.sleep(req.duration)
    if action not in ('mouse_move', 'screenshot', 'cursor_position', 'wait'):
        time.sleep(.15)
    return capture_screen()


def open_url(req):
    status = subprocess.run([*SUPERVISOR, 'status', 'chromium'], capture_output=True, text=True, timeout=5)
    fields = status.stdout.split()
    if len(fields) < 2 or fields[0] != 'chromium':
        raise RuntimeError('Cannot read Chromium supervisor state')
    if fields[1] not in ('RUNNING', 'STARTING'):
        run([*SUPERVISOR, 'start', 'chromium'], capture_output=True, timeout=15)
    deadline = time.monotonic() + 10
    while not browser_ready():
        if time.monotonic() >= deadline:
            raise RuntimeError('Chromium window readiness deadline exceeded')
        time.sleep(.1)
    with open('/tmp/chromium-open.log', 'ab') as output:
        run(browser_argv(req.url), stdin=subprocess.DEVNULL, stdout=output, stderr=output, timeout=10)
    time.sleep(req.wait_seconds)
    return {**capture_screen(), 'url': req.url}


@asynccontextmanager
async def lifespan(app):
    app.state.gui = ThreadPoolExecutor(max_workers=1, thread_name_prefix='private-x11')
    try:
        yield
    finally:
        await asyncio.get_running_loop().run_in_executor(app.state.gui, close_capture)
        app.state.gui.shutdown(wait=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Nonfinite input cannot be echoed through JSONResponse's strict JSON encoder.
    return JSONResponse(status_code=422, content={'detail': [
        {key: error[key] for key in ('type', 'loc', 'msg')} for error in exc.errors()]})


@app.middleware('http')
async def session_header(request, call_next):
    supplied = request.headers.get('authorization', '')
    token = os.environ.get('SANDBOX_TOKEN', '')
    if not token or not secrets.compare_digest(supplied.encode(), ('Bearer '+token).encode()):
        return JSONResponse(status_code=401, content={'detail': 'Unauthorized'})
    response = await call_next(request)
    response.headers['X-Sandbox-Session'] = os.environ['SANDBOX_SESSION_ID']
    return response


def health_check():
    info = run(['xdpyinfo'], timeout=2, capture_output=True, text=True).stdout
    if not re.search(r'dimensions:\s+1280x800 pixels', info) or not wm_ready() or not browser_ready():
        raise RuntimeError('Desktop geometry, window manager or Chromium not ready')
    status = run([*SUPERVISOR, 'status'], capture_output=True, text=True).stdout.splitlines()
    expected = {'xvfb', 'openbox', 'chromium', 'x11vnc', 'novnc', 'agent_daemon'}
    if {row.split()[0] for row in status if len(row.split()) > 1 and row.split()[1] == 'RUNNING'} != expected:
        raise RuntimeError('Supervisor programs not all RUNNING')
    return {'status': 'ok', 'display': ':99', 'resolution': [1280, 800]}


@app.get('/health')
async def health():
    try:
        return await asyncio.to_thread(health_check)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


async def gui_response(fn, req):
    try:
        return await asyncio.get_running_loop().run_in_executor(app.state.gui, fn, req)
    except Exception as exc:
        return JSONResponse(status_code=500, content={'success': False, 'cursor': None,
                            'screenshot_base64': None, 'error': str(exc)})


@app.post('/computer')
async def computer_endpoint(req: ComputerRequest):
    return await gui_response(computer, req)


@app.post('/open_url')
async def open_url_endpoint(req: OpenUrlRequest):
    return await gui_response(open_url, req)


async def terminate_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Group may outlive its shell. Check group after grace period regardless of shell state.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await proc.wait()


@app.post('/bash')
async def bash_endpoint(req: BashRequest):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            launch = asyncio.create_task(asyncio.create_subprocess_exec('/bin/bash', '-lc', req.command,
                cwd=req.cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True))
            try:
                proc = await asyncio.shield(launch)
            except asyncio.CancelledError:
                proc = await launch
                await asyncio.shield(terminate_group(proc))
                raise
        except OSError as exc:
            raise HTTPException(500, f'Cannot start shell: {exc}') from exc
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), req.timeout)
        except TimeoutError:
            timed_out = True
            await asyncio.shield(terminate_group(proc))
        except asyncio.CancelledError:
            await asyncio.shield(terminate_group(proc))
            raise
        stdout = os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0).decode('utf-8', errors='replace')
        stderr = os.pread(err.fileno(), os.fstat(err.fileno()).st_size, 0).decode('utf-8', errors='replace')
        if timed_out:
            stderr += f'\nCommand timed out after {req.timeout} seconds'
        return {'exit_code': 124 if timed_out else proc.returncode, 'stdout': stdout, 'stderr': stderr}
