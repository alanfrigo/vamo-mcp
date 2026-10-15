"""MCP stdio entry point. Stdout belongs exclusively to JSON-RPC."""
import asyncio
import atexit
from contextlib import asynccontextmanager
import json
import logging
import os
import queue
import signal
import sys
import threading
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from .docker_manager import SandboxDockerManager

StrictInt = Annotated[int, Field(strict=True)]
Port = Annotated[StrictInt, Field(ge=1, le=65535)]
Coordinate = Annotated[list[StrictInt], Field(min_length=2, max_length=2)]
Duration = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]


@asynccontextmanager
async def lifespan(server):
    manager = SandboxDockerManager()
    atexit.register(manager.close_sync)
    try:
        yield manager
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await manager.destroy()
            except Exception:
                logging.exception('Sandbox cleanup failed; retained ID %s', manager.container_id)
        if manager.container_id is None:
            atexit.unregister(manager.close_sync)


mcp = FastMCP('vamo-mcp', lifespan=lifespan, log_level='WARNING')


def result(data, *, image=None, error=False):
    content = [TextContent(type='text', text=json.dumps(data, ensure_ascii=False))]
    if image:
        content.append(ImageContent(type='image', mimeType='image/png', data=image))
    return CallToolResult(content=content, isError=error)


def failure(exc):
    return result({'error': str(exc)}, error=True)


@mcp.tool(structured_output=False)
async def sandbox_init(ctx: Context, workspace_path: str | None = None,
                       mount_mode: Literal['ro', 'rw'] = 'ro',
                       forward_ports: list[Port] = [3000, 5173, 8000, 8080],
                       reset_if_running: bool = False) -> str:
    """Start disposable private desktop. Host networking is NOT isolated: all host services are reachable.
    forward_ports is a declared list, not forwarding or an allowlist. rw mounts change real host files;
    destroy cannot undo writes. Docker with sudo/Chromium --no-sandbox is not an adversarial VM boundary.
    Docker Desktop needs >=4.34, host networking enabled, ECI disabled, Linux containers.
    """
    try:
        data = await ctx.request_context.lifespan_context.init(workspace_path, mount_mode, list(forward_ports), reset_if_running)
        return json.dumps(data)
    except Exception as exc:
        return failure(exc)


@mcp.tool(structured_output=False)
async def sandbox_computer_action(ctx: Context,
    action: Literal['mouse_move', 'left_click', 'right_click', 'double_click', 'triple_click',
                    'middle_click', 'left_click_drag', 'scroll', 'type', 'key', 'wait', 'screenshot', 'cursor_position'],
    coordinate: Coordinate | None = None, start_coordinate: Coordinate | None = None,
    text: str | None = None, scroll_direction: Literal['up', 'down', 'left', 'right'] | None = 'down',
    scroll_amount: PositiveInt = 5, duration: Duration = 1.0) -> CallToolResult:
    """Act on private 1280x800 X11 desktop, never host cursor. Returns current PNG with cursor overlay.
    Drag requires both coordinates. Keys use X keysyms or aliases. No automatic action retries.
    """
    payload = dict(action=action, coordinate=coordinate, start_coordinate=start_coordinate, text=text,
                   scroll_direction=scroll_direction, scroll_amount=scroll_amount, duration=duration)
    timeout = max(15, duration+10, 15+.02*len(text or ''), 15+.1*scroll_amount)
    try:
        data = await ctx.request_context.lifespan_context.request('/computer', payload, timeout)
        return result({'action': action, **{k: data[k] for k in ('success', 'cursor', 'error')}},
                      image=data['screenshot_base64'])
    except Exception as exc:
        return failure(exc)


@mcp.tool(structured_output=False)
async def sandbox_open_url(ctx: Context, url: str, wait_seconds: Duration = 2.0) -> CallToolResult:
    """Open HTTP/HTTPS in sandbox Chromium (restart if stopped), wait, return real PNG.
    Waiting does not prove HTTP success or network-idle; error pages remain visible in screenshot.
    """
    try:
        data = await ctx.request_context.lifespan_context.request('/open_url',
                            {'url': url, 'wait_seconds': wait_seconds}, wait_seconds+30)
        return result({'url': url, **{k: data[k] for k in ('success', 'cursor', 'error')}}, image=data['screenshot_base64'])
    except Exception as exc:
        return failure(exc)


@mcp.tool(structured_output=False)
async def sandbox_bash(ctx: Context, command: str, cwd: str = '/workspace', timeout: PositiveInt = 30,
                       take_screenshot_after: bool = False) -> CallToolResult:
    """Run bash as agent with passwordless sudo. Host network services reachable; no network isolation.
    rw workspace writes affect real host files and are NOT reverted by destroy. No implicit init.
    Background jobs do not hold response open; timeout kills shell process group, not deliberate setsid escapes.
    """
    manager = ctx.request_context.lifespan_context
    try:
        data = await manager.request('/bash', {'command': command, 'cwd': cwd, 'timeout': timeout}, timeout+10)
    except Exception as exc:
        return failure(exc)
    image = None
    error = data['exit_code'] != 0
    if take_screenshot_after:
        try:
            shot = await manager.request('/computer', {'action': 'screenshot'}, 15)
            image = shot['screenshot_base64']
        except Exception as exc:
            data['screenshot_error'] = str(exc)
            error = True
    return result(data, image=image, error=error)


@mcp.tool(structured_output=False)
async def sandbox_status(ctx: Context) -> str:
    """Inspect session without creating it. network_mode=host, network_isolated=false;
    forwarded_ports is only declared metadata, not an allowlist. rw changes persist on host after destroy.
    """
    try:
        return json.dumps(await ctx.request_context.lifespan_context.status())
    except Exception as exc:
        return failure(exc)


@mcp.tool(structured_output=False)
async def sandbox_destroy(ctx: Context) -> str:
    """Immediately kill own sandbox, discard private desktop/profile/filesystem. Idempotent.
    Does not remove or revert host workspace files. Never removes another session.
    """
    try:
        return json.dumps(await ctx.request_context.lifespan_context.destroy())
    except Exception as exc:
        return failure(exc)


@asynccontextmanager
async def _stdin_lines():
    loop = asyncio.get_running_loop()
    if sys.platform == 'win32':
        import msvcrt

        requests = queue.SimpleQueue()
        fd = os.dup(sys.stdin.fileno())

        def deliver(future, data, error):
            if not future.done():
                if error is None:
                    future.set_result(data)
                else:
                    future.set_exception(error)

        def read_pipe():
            try:
                while (future := requests.get()) is not None:
                    try:
                        data, error = os.read(fd, 65536), None
                    except OSError as exc:
                        data, error = None, exc
                    try:
                        loop.call_soon_threadsafe(deliver, future, data, error)
                    except RuntimeError:  # Loop already closed after cancellation.
                        break
            finally:
                os.close(fd)

        # ponytail: Windows may leave one native read blocked until process exit;
        # daemon/raw fd avoids executor joins and stdin's buffered finalization lock.
        worker = threading.Thread(target=read_pipe, name='MCP stdin', daemon=True)
        try:
            msvcrt.setmode(fd, os.O_BINARY)
            worker.start()
        except BaseException:
            os.close(fd)
            raise

        async def read_chunk():
            future = loop.create_future()
            requests.put(future)
            return await future

        def close():
            requests.put(None)
    else:
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)

        async def read_chunk():
            return await reader.read(65536)

        close = transport.close

    async def lines():
        pending = bytearray()
        while chunk := await read_chunk():
            start = len(pending)
            pending.extend(chunk)
            consumed = 0
            while (end := pending.find(b'\n', start)) != -1:
                yield pending[consumed:end+1].decode('utf-8', errors='replace')
                consumed = start = end+1
            if consumed:
                del pending[:consumed]
        if pending:
            yield pending.decode('utf-8', errors='replace')

    try:
        yield lines()
    finally:
        close()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    async def serve():
        loop = asyncio.get_running_loop()
        with anyio.CancelScope() as scope:
            def stop(signum, frame):
                loop.call_soon_threadsafe(scope.cancel)
            old = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                # SDK v1 default AsyncFile readline shields a blocking thread from SIGTERM.
                async with _stdin_lines() as stdin:
                    async with stdio_server(stdin=stdin) as (incoming, outgoing):
                        await mcp._mcp_server.run(incoming, outgoing, mcp._mcp_server.create_initialization_options())
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
    anyio.run(serve, backend='asyncio')


if __name__ == '__main__':
    main()
