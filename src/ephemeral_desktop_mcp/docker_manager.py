"""One local Docker sandbox per MCP process; no host display mounts."""
import asyncio
from contextlib import asynccontextmanager
from functools import partial
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import string
import subprocess
import threading
import time
from typing import Literal
import uuid

import anyio
import docker
import httpx

log = logging.getLogger(__name__)
DESKTOP_HINT = ('Docker Desktop requires version >=4.34, Linux containers, Settings > Resources > '
                'Network > Enable host networking, and Enhanced Container Isolation disabled.')


class SandboxDockerManager:
    image = 'ephemeral-desktop-sandbox:latest'

    def __init__(self):
        self.docker = None
        self.container_id = None
        self.container = None
        self.http = None
        self.config = None
        self.session_id = None
        self.daemon_url = None
        self.novnc_url = None
        self.ports = []
        self.token = None
        self.operation_lock = asyncio.Lock()
        self.cleanup_lock = asyncio.Lock()
        self.sync_lock = threading.Lock()
        self.active_scope = None
        self.creation_done = asyncio.Event()
        self.creation_done.set()
        self.closing = False
        self.initialized_once = False

    async def _thread(self, fn, *args, **kwargs):
        return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))

    def _connect(self):
        if self.docker is not None:
            return
        try:
            client = None
            if os.environ.get('DOCKER_HOST'):
                endpoint = os.environ['DOCKER_HOST']
                if not endpoint.startswith(('unix://', 'npipe://')):
                    raise ValueError('Remote Docker daemons are unsupported; use local unix:// or npipe:// endpoint')
                client = docker.from_env(timeout=5)
            else:
                try:
                    result = subprocess.run(['docker', 'context', 'inspect', '--format', '{{json .Endpoints.docker}}'],
                                            capture_output=True, text=True, check=True, timeout=5)
                    endpoint = json.loads(result.stdout)['Host']
                except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
                    raise ValueError('Docker CLI/context inspect unavailable or invalid; install Docker and select a local context') from exc
                if not endpoint.startswith(('unix://', 'npipe://')):
                    raise ValueError('Remote Docker daemons are unsupported; select local unix:// or npipe:// context')
                client = docker.DockerClient(base_url=endpoint, timeout=5)
            info = client.info()
            if info.get('OSType') != 'linux':
                raise ValueError('Linux containers required; Windows containers unsupported')
            if any('rootless' in option for option in info.get('SecurityOptions', [])):
                raise ValueError('Rootless Docker unsupported: host localhost is not shared')
            self.docker = client
        except BaseException as exc:
            if client is not None:
                client.close()
            if isinstance(exc, docker.errors.DockerException):
                raise RuntimeError('Cannot access Docker endpoint; start Docker and grant endpoint access. '+DESKTOP_HINT) from exc
            raise

    @staticmethod
    def _configuration(workspace_path, mount_mode, forward_ports):
        if mount_mode not in ('ro', 'rw'):
            raise ValueError('mount_mode must be ro or rw')
        path = None
        if workspace_path is not None:
            path = str(Path(workspace_path).expanduser().resolve(strict=True))
            if not Path(path).is_dir():
                raise ValueError('workspace_path must be an existing directory')
        if not isinstance(forward_ports, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in forward_ports):
            raise ValueError('forward_ports must contain strict integers in 1..65535')
        return (path, mount_mode, tuple(dict.fromkeys(forward_ports)))

    @staticmethod
    def _allocate_ports(excluded):
        reservations = []
        try:
            for _ in range(128):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.bind(('127.0.0.1', 0))
                except BaseException:
                    s.close()
                    raise
                if s.getsockname()[1] in excluded:
                    s.close()
                else:
                    reservations.append(s)
                if len(reservations) == 3:
                    return reservations
            raise RuntimeError('Unable to reserve three non-declared localhost ports')
        except BaseException:
            for s in reservations: s.close()
            raise

    @asynccontextmanager
    async def _operation(self):
        if self.closing:
            raise RuntimeError('Sandbox is shutting down')
        async with self.operation_lock:
            if self.closing:
                raise RuntimeError('Sandbox is shutting down')
            with anyio.CancelScope() as scope:
                self.active_scope = scope
                try:
                    yield
                finally:
                    self.active_scope = None
            if scope.cancel_called:
                raise RuntimeError('Sandbox is shutting down')

    async def init(self, workspace_path: str | None = None, mount_mode: Literal['ro', 'rw'] = 'ro',
                   forward_ports: list[int] = [3000, 5173, 8000, 8080], reset_if_running: bool = False) -> dict:
        config = self._configuration(workspace_path, mount_mode, list(forward_ports))
        async with self._operation():
            # Cleanup must also wait for connection, image lookup and reset: each
            # blocking call can finish after its operation has been cancelled.
            self.creation_done.clear()
            try:
                await self._thread(self._connect)
                try:
                    await self._thread(self.docker.images.get, self.image)
                except docker.errors.ImageNotFound as exc:
                    raise RuntimeError('Sandbox image missing; run docker build -t ephemeral-desktop-sandbox:latest sandbox_image') from exc
                if self.container_id:
                    if not reset_if_running:
                        if config != self.config:
                            raise RuntimeError('Sandbox already running with different configuration; use reset_if_running=True')
                        current = await self.status()
                        if current['status'] != 'running':
                            raise RuntimeError('Sandbox session is not healthy; use reset_if_running=True: '+str(current['error']))
                        return self._init_result()
                    with anyio.CancelScope(shield=True):
                        await self._teardown()
                deadline = time.monotonic()+60
                for attempt in range(3):
                    await anyio.lowlevel.checkpoint()
                    self.config = config
                    self.session_id = str(uuid.uuid4())
                    self.token = secrets.token_urlsafe(32)
                    password = ''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(8))
                    reservations = self._allocate_ports(config[2])
                    self.ports = [s.getsockname()[1] for s in reservations]
                    self.daemon_url = f'http://127.0.0.1:{self.ports[0]}'
                    self.novnc_url = (f'http://127.0.0.1:{self.ports[1]}/vnc.html'
                                      f'#autoconnect=true&password={password}&view_only=true&resize=scale')
                    env = dict(zip(('DAEMON_PORT', 'NOVNC_PORT', 'VNC_PORT'), map(str, self.ports)))
                    env.update(SANDBOX_TOKEN=self.token, SANDBOX_SESSION_ID=self.session_id, VNC_PASSWORD=password)
                    volumes = {} if config[0] is None else {config[0]: {'bind': '/workspace', 'mode': config[1]}}
                    try:
                        # Capture ID before accepting cancellation from a blocking SDK call.
                        with anyio.CancelScope(shield=True):
                            container = await self._thread(self.docker.containers.create, self.image,
                                name='ephemeral-agent-sandbox-'+self.session_id,
                                labels={'vamo-mcp.session': self.session_id}, environment=env,
                                network_mode='host', shm_size='2g', auto_remove=True, init=True,
                                detach=True, volumes=volumes, cap_drop=['NET_RAW', 'NET_ADMIN'])
                            with self.sync_lock:
                                self.container = container
                                self.container_id = container.id
                            self.initialized_once = True
                            for s in reservations: s.close()
                            await self._thread(container.start)
                        self.http = httpx.AsyncClient(base_url=self.daemon_url, trust_env=False,
                            headers={'Authorization': 'Bearer '+self.token})
                        await self._ready(deadline)
                        if self.closing or self.container_id != container.id:
                            raise RuntimeError('Sandbox generation changed during initialization')
                        return self._init_result()
                    except BaseException as exc:
                        with anyio.CancelScope(shield=True):
                            try:
                                logs = '' if self.closing or not isinstance(exc, Exception) else await self._logs()
                            finally:
                                await self._teardown()
                        collision = isinstance(exc, PortCollision) or self._port_collision(logs)
                        if isinstance(exc, Exception) and collision and attempt < 2 and time.monotonic() < deadline:
                            continue
                        if not isinstance(exc, Exception):
                            raise
                        safe_logs = logs[-6000:]
                        raise RuntimeError(f'Sandbox startup failed: {exc}\n{safe_logs}\n{DESKTOP_HINT} '
                                           'For mount errors, authorize workspace in Docker Desktop File sharing.') from exc
                    finally:
                        for s in reservations: s.close()
                raise RuntimeError('Port allocation attempts exhausted')
            finally:
                self.creation_done.set()

    @staticmethod
    def _port_collision(logs):
        text = logs.casefold()
        return any(term in text for term in ('address already in use', 'eaddrinuse', 'errno 98', 'port is already in use'))

    async def _logs(self):
        container = self.container
        if container is None:
            return ''
        try:
            text = (await self._thread(container.logs)).decode('utf-8', errors='replace')
            for secret in (self.token, self.novnc_url):
                if secret: text = text.replace(secret, '[redacted]')
            # VNC password never appears in command logs; strip fragment defensively.
            if self.novnc_url:
                password = self.novnc_url.split('password=')[1].split('&')[0]
                text = text.replace(password, '[redacted]')
            return text
        except docker.errors.NotFound:
            return 'Container exited and auto-removed'
        except docker.errors.DockerException as exc:
            return f'Sandbox logs unavailable ({type(exc).__name__})'

    async def _health(self, client, session_id):
        response = await client.get('/health', timeout=httpx.Timeout(2, connect=1))
        if response.headers.get('X-Sandbox-Session') != session_id:
            raise PortCollision('Daemon port belongs to a different session')
        response.raise_for_status()
        if response.json() != {'status': 'ok', 'display': ':99', 'resolution': [1280, 800]}:
            raise RuntimeError('Invalid daemon health contract')

    async def _ready(self, deadline):
        error = 'not ready'
        while time.monotonic() < deadline:
            try:
                await self._health(self.http, self.session_id)
                r = await self.http.get(f'http://127.0.0.1:{self.ports[1]}/vnc.html',
                                       headers={'Authorization': ''}, timeout=httpx.Timeout(2, connect=1))
                if r.status_code != 200:
                    raise RuntimeError(f'noVNC readiness returned HTTP {r.status_code}')
                reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.ports[2]), 1)
                try:
                    if not (await asyncio.wait_for(reader.readexactly(4), 1)) == b'RFB ':
                        raise RuntimeError('Invalid VNC banner')
                finally:
                    writer.close(); await writer.wait_closed()
                if self._port_collision(await self._logs()):
                    raise PortCollision('Listener port occupied')
                return
            except PortCollision:
                raise
            except (httpx.HTTPError, OSError, ValueError, RuntimeError, asyncio.IncompleteReadError, TimeoutError) as exc:
                error = str(exc)
            logs = await self._logs()
            if self._port_collision(logs):
                raise PortCollision('Listener port occupied')
            await self._thread(self.container.reload)
            if self.container.status != 'running':
                raise RuntimeError('Sandbox exited during startup')
            if 'entered FATAL state' in logs:
                raise RuntimeError('Supervisor program failed during startup')
            await asyncio.sleep(.1)
        raise RuntimeError('Sandbox readiness deadline exceeded: '+error)

    def _init_result(self):
        return {'container_id': self.container_id, 'daemon_url': self.daemon_url,
                'novnc_url': self.novnc_url, 'forwarded_ports': list(self.config[2]),
                'network_mode': 'host', 'network_isolated': False}

    def _forget(self, generation):
        with self.sync_lock:
            if self.container_id != generation:
                return None
            client = self.http
            self.container_id = self.container = self.http = self.config = None
            self.session_id = self.daemon_url = self.novnc_url = self.token = None
            self.ports = []
            return client

    async def _running(self):
        if self.closing:
            raise RuntimeError('Sandbox is shutting down')
        generation, container = self.container_id, self.container
        if not generation:
            raise RuntimeError('Sandbox is not initialized; call sandbox_init first')
        try:
            await self._thread(container.reload)
        except docker.errors.NotFound as exc:
            with anyio.CancelScope(shield=True):
                client = self._forget(generation)
                if client is not None:
                    await client.aclose()
            raise RuntimeError('Sandbox container no longer exists; call sandbox_init first') from exc
        if generation != self.container_id or self.closing:
            raise RuntimeError('Sandbox generation changed during operation')
        if container.status != 'running':
            raise RuntimeError('Sandbox container is not running')

    async def request(self, path: str, payload: dict, timeout: float) -> dict:
        async with self._operation():
            await self._running()
            generation, client, session_id = self.container_id, self.http, self.session_id
            try:
                response = await client.post(path, json=payload, timeout=timeout)
            except httpx.TimeoutException as exc:
                raise RuntimeError('Sandbox response timed out; action may already have executed; no retry performed') from exc
            if generation != self.container_id or self.closing:
                raise RuntimeError('Sandbox generation changed during operation')
            if response.headers.get('X-Sandbox-Session') != session_id:
                raise RuntimeError('Daemon session mismatch; refusing unrelated service')
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f'Invalid daemon JSON (HTTP {response.status_code})') from exc
            if not isinstance(data, dict):
                raise RuntimeError('Invalid daemon JSON object')
            if response.is_error:
                raise RuntimeError(f'Daemon HTTP {response.status_code}: '+str(data.get('error') or data.get('detail') or data))
            if path == '/bash':
                if type(data.get('exit_code')) is not int or not all(isinstance(data.get(k), str) for k in ('stdout', 'stderr')):
                    raise RuntimeError('Invalid daemon bash contract')
            elif not (data.get('success') is True and isinstance(data.get('screenshot_base64'), str)
                      and len(data['screenshot_base64']) > 0 and isinstance(data.get('cursor'), list)
                      and len(data['cursor']) == 2 and all(type(v) is int for v in data['cursor'])):
                raise RuntimeError('Invalid daemon GUI contract')
            return data

    async def status(self) -> dict:
        while True:
            generation, container = self.container_id, self.container
            client, session_id, config = self.http, self.session_id, self.config
            daemon_url, novnc_url = self.daemon_url, self.novnc_url
            state = 'stopped' if self.initialized_once else 'not_initialized'
            error = None
            if generation:
                try:
                    await self._thread(container.reload)
                    if container.status != 'running':
                        state = 'stopped'
                    else:
                        state = 'running'
                        if client is None:
                            raise RuntimeError('Sandbox daemon is starting')
                        await self._health(client, session_id)
                except docker.errors.NotFound:
                    with anyio.CancelScope(shield=True):
                        old_client = self._forget(generation)
                        if old_client is not None:
                            await old_client.aclose()
                    state = 'stopped'
                except Exception as exc:
                    state, error = 'unhealthy', str(exc)
            if generation != self.container_id:
                continue
            present = generation is not None
            return {'status': state, 'container_id': generation,
                    'daemon_url': daemon_url if present else None, 'novnc_url': novnc_url if present else None,
                    'forwarded_ports': list(config[2]) if present and config else [],
                    'workspace_path': config[0] if present and config else None,
                    'mount_mode': config[1] if present and config else None,
                    'network_mode': 'host', 'network_isolated': False,
                    'display': ':99', 'resolution': [1280, 800], 'error': error}

    def _remove(self, container_id):
        try:
            container = self.docker.containers.get(container_id)
            try:
                container.kill()
            except docker.errors.NotFound:
                return
            except docker.errors.APIError as exc:
                if exc.status_code != 409: raise
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                try: container.reload()
                except docker.errors.NotFound: return
                if container.status not in ('running', 'removing'):
                    try:
                        container.remove(force=True)
                    except docker.errors.NotFound:
                        return
                    except docker.errors.APIError as exc:
                        if exc.status_code != 409:
                            raise
                time.sleep(.05)
            try:
                container.reload()
            except docker.errors.NotFound:
                return
            raise RuntimeError(f'Container removal deadline exceeded; run docker rm -f {container_id}')
        except docker.errors.NotFound:
            pass

    async def _teardown(self):
        own_id = self.container_id
        if own_id:
            await self._thread(self._remove, own_id)
        client = self._forget(own_id)
        if client is not None:
            await client.aclose()
        return {'destroyed': own_id is not None, 'container_id': own_id}

    async def destroy(self) -> dict:
        with anyio.CancelScope(shield=True):
            async with self.cleanup_lock:
                self.closing = True
                if self.active_scope:
                    self.active_scope.cancel()
                await self.creation_done.wait()
                try:
                    return await self._teardown()
                finally:
                    self.closing = False

    def close_sync(self) -> None:
        with self.sync_lock:
            self.closing = True
            own_id = self.container_id
        if not own_id or self.docker is None:
            return
        try:
            self._remove(own_id)
        except Exception:
            log.exception('Sandbox cleanup failed; manual removal required: docker rm -f %s', own_id)
            return
        self._forget(own_id)


class PortCollision(RuntimeError):
    """A newly created session failed to claim its reserved loopback ports."""
