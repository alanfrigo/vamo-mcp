"""Real Docker/MCP acceptance checks. Run with --standalone, --benchmark or --observe."""
import argparse
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import secrets
import socket
import string
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

import docker
import httpx
from PIL import Image
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from contextlib import asynccontextmanager
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def local_docker():
    from ephemeral_desktop_mcp.docker_manager import SandboxDockerManager
    manager=SandboxDockerManager()
    manager._connect()
    return manager.docker


PAGE = r'''<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;width:100%;height:100%;background:#112233;color:white;font:18px sans-serif}
#inc{position:absolute;left:100px;top:120px;width:240px;height:100px}
#text{position:absolute;left:400px;top:120px;width:350px;height:100px}
#target{position:absolute;left:100px;top:280px;width:240px;height:120px;background:#556677;user-select:none}
#scroll{position:absolute;left:500px;top:280px;width:250px;height:150px;overflow:scroll}
#inside{width:1500px;height:1500px;background:linear-gradient(#333,#999)}
</style><button id="inc">Incrementar <span id="count">0</span></button>
<textarea id="text"></textarea><div id="target">Eventos</div><div id="scroll"><div id="inside"></div></div>
<script>
const $=id=>document.getElementById(id);
function send(path,obj){return fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)})}
function metrics(){const c={};for(const id of ['inc','text','target','scroll']){const r=$(id).getBoundingClientRect();c[id]=[r.x+r.width/2,r.y+r.height/2]}
send('/metrics',{screenX,screenY,outerHeight,innerHeight,devicePixelRatio,centers:c})}
function value(){send('/value',{text:$('text').value,scrollTop:$('scroll').scrollTop,scrollLeft:$('scroll').scrollLeft})}
$('inc').onclick=async()=>{let s=await(await fetch('/click',{method:'POST'})).json();$('count').textContent=s.count;document.body.style.background='#225544'};
$('text').addEventListener('input',value);$('scroll').addEventListener('scroll',value);
$('target').oncontextmenu=e=>e.preventDefault();
for(const name of ['mousedown','mouseup','mousemove','click','contextmenu','auxclick'])$('target').addEventListener(name,e=>send('/event',{type:name,button:e.button,buttons:e.buttons,detail:e.detail,x:e.clientX,y:e.clientY,screenX:e.screenX,screenY:e.screenY}));
$('scroll').scrollTo(500,500);value();metrics();window.addEventListener('resize',metrics);
</script>'''


@contextmanager
def fixture():
    state = {'count': 0, 'events': [], 'text': '', 'scrollTop': 500, 'scrollLeft': 500, 'metrics': None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, data, status=200, content_type='application/json'):
            body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(status); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == '/state': self.reply(state)
            elif self.path == '/missing': self.reply('<body style="background:#771122">Real 404</body>', 404, 'text/html')
            else: self.reply(PAGE, content_type='text/html; charset=utf-8')
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or '{}')
            if self.path == '/click': state['count'] += 1
            elif self.path == '/metrics': state['metrics'] = data
            elif self.path == '/event': state['events'].append(data)
            elif self.path == '/value': state.update(data)
            self.reply({'count': state['count']})
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    except OSError as exc:
        raise RuntimeError('Fixture requires free 127.0.0.1:8765; occupant will not be killed') from exc
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield state
    finally: server.shutdown(); server.server_close(); thread.join()


@asynccontextmanager
async def session():
    params = StdioServerParameters(command=sys.executable, args=['-m', 'ephemeral_desktop_mcp.server'], cwd=str(ROOT))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            yield client


async def call(client, name, arguments=None, *, error=False):
    response = await asyncio.wait_for(client.call_tool(name, arguments or {}), 90)
    assert bool(response.isError) == error, (name, response)
    try:
        data = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        assert error and response.content[0].text
        data = {'error': response.content[0].text}
    images = [c for c in response.content if c.type == 'image']
    for image in images: png(image.data)
    return data, images


async def action(client, name, **kw):
    return await call(client, 'sandbox_computer_action', {'action': name, **kw})


async def until(predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while not predicate():
        if time.monotonic() > deadline: raise AssertionError('Expected state transition not observed')
        await asyncio.sleep(.02)


def center(state, name):
    m = state['metrics']; assert m['devicePixelRatio'] == 1, m
    x, y = m['centers'][name]
    return [round(m['screenX']+x), round(m['screenY']+m['outerHeight']-m['innerHeight']+y)]


async def visual(client, state):
    data, images = await call(client, 'sandbox_open_url', {'url': 'http://localhost:8765', 'wait_seconds': .3})
    await until(lambda: state['metrics'] is not None)
    m = state['metrics']; toolbar = m['screenY']+m['outerHeight']-m['innerHeight']
    before = png(images[0].data); assert before.getpixel((20, round(toolbar+250))) == (17,34,51)
    await action(client, 'left_click', coordinate=center(state,'inc'))
    await until(lambda: state['count']==1)
    data, images = await action(client, 'screenshot')
    assert png(images[0].data).getpixel((20, round(toolbar+250))) == (34,85,68)
    await action(client, 'mouse_move', coordinate=[800,600])
    data, _ = await action(client, 'cursor_position'); assert data['cursor']==[800,600]
    p = center(state,'target')
    for name, event, button in [('right_click','contextmenu',2), ('middle_click','auxclick',1)]:
        state['events'].clear(); await action(client,name,coordinate=p)
        await until(lambda:any(e['type']==event and e['button']==button for e in state['events']))
    for name, detail in [('double_click',2), ('triple_click',3)]:
        state['events'].clear(); await action(client,name,coordinate=p)
        await until(lambda:any(e['type']=='click' and e['detail']==detail for e in state['events']))
        await asyncio.sleep(.6)  # separate native multi-click sequences
    state['events'].clear(); start=[p[0]-50,p[1]]; end=[p[0]+50,p[1]+20]
    await action(client,'left_click_drag',start_coordinate=start,coordinate=end)
    await until(lambda:any(e['type']=='mouseup' for e in state['events']))
    assert any(e['type']=='mousedown' and e['button']==0 and [e['screenX'],e['screenY']]==start for e in state['events'])
    assert any(e['type']=='mousemove' and e['buttons']==1 and [e['screenX'],e['screenY']]==end for e in state['events'])
    assert any(e['type']=='mouseup' and e['buttons']==0 and [e['screenX'],e['screenY']]==end for e in state['events'])
    for direction, field, sign in [('down','scrollTop',1),('up','scrollTop',-1),('right','scrollLeft',1),('left','scrollLeft',-1)]:
        old=state[field]; await action(client,'scroll',coordinate=center(state,'scroll'),scroll_direction=direction,scroll_amount=2)
        await until(lambda:(state[field]-old)*sign>0)
    await action(client,'left_click',coordinate=center(state,'text'))
    literal='ação 日本語 "quotes"\n$(touch /tmp/not-created)'
    await action(client,'type',text=literal); await until(lambda:state['text']==literal)
    result,_=await call(client,'sandbox_bash',{'command':'test ! -e /tmp/not-created'}); assert result['exit_code']==0
    await action(client,'key',text='Ctrl+A'); await action(client,'key',text='BackSpace')
    await until(lambda:state['text']=='')
    ascii_text='ASCII "quoted"\n$(touch /tmp/not-created)'
    await action(client,'type',text=ascii_text); await until(lambda:state['text']==ascii_text)
    await action(client,'key',text='Enter'); await until(lambda:state['text']==ascii_text+'\n')
    await action(client,'key',text='Esc')
    for key in ('Cmd+L','Control_L+l'):
        await action(client,'key',text=key); await action(client,'type',text='http://localhost:8765')
        state['metrics']=None
        await action(client,'key',text='Enter'); await asyncio.sleep(.3)
        await until(lambda:state['metrics'] is not None)
    start=time.monotonic(); await action(client,'wait',duration=.2); assert time.monotonic()-start>=.2
    await action(client,'mouse_move',coordinate=[800,600]); count=state['count']
    for payload in ({'action':'mouse_move','coordinate':[1280,0]}, {'action':'mouse_move','coordinate':[-1,0]},
                    {'action':'mouse_move','coordinate':[1]}, {'action':'mouse_move','coordinate':[1.5,0]},
                    {'action':'mouse_move','coordinate':[True,0]}, {'action':'left_click_drag','coordinate':[1,2]},
                    {'action':'key','text':''}, {'action':'wait','duration':float('nan')}):
        await call(client,'sandbox_computer_action',payload,error=True)
    await call(client,'sandbox_bash',{'command':'true','timeout':0},error=True)
    data,_=await action(client,'cursor_position'); assert data['cursor']==[800,600] and state['count']==count
    await call(client,'sandbox_open_url',{'url':'javascript:alert(1)'},error=True)
    await call(client,'sandbox_open_url',{'url':'http://localhost:bad'},error=True)
    await call(client,'sandbox_bash',{'command':'supervisorctl -c /etc/supervisor/supervisord.conf stop chromium'})
    status,_=await call(client,'sandbox_status');assert status['status']=='unhealthy'
    await call(client,'sandbox_bash',{'command':'printf diagnostic-access'})
    await call(client,'sandbox_open_url',{'url':'http://localhost:8765','wait_seconds':.3})
    data,images=await call(client,'sandbox_open_url',{'url':'http://localhost:8765/missing','wait_seconds':.3})
    assert png(images[0].data).getpixel((20,round(toolbar+250)))==(119,17,34)
    await call(client,'sandbox_open_url',{'url':'http://localhost:8765','wait_seconds':.3})
    print('visual OK: real host localhost, pixels/click state, all 13 actions, Unicode/literal keys, validation, browser recovery/404')


async def integrated():
    with fixture() as state, tempfile.TemporaryDirectory() as directory:
        async with session() as client:
            tools=await client.list_tools()
            assert {t.name for t in tools.tools}=={'sandbox_init','sandbox_status','sandbox_destroy','sandbox_bash','sandbox_computer_action','sandbox_open_url'}
            data,_=await call(client,'sandbox_status'); assert data['status']=='not_initialized'
            await call(client,'sandbox_bash',{'command':'true'},error=True)
            data,_=await call(client,'sandbox_init',{'workspace_path':directory,'forward_ports':[8765,8765]})
            first=data['container_id']; assert data['forwarded_ports']==[8765]
            await visual(client,state)
            data,_=await call(client,'sandbox_init',{'workspace_path':directory,'forward_ports':[8765]}); assert data['container_id']==first
            await call(client,'sandbox_init',{'workspace_path':directory,'mount_mode':'rw','forward_ports':[8765]},error=True)
            await call(client,'sandbox_bash',{'command':'sudo touch /workspace/forbidden'},error=True)
            assert not Path(directory,'forbidden').exists()
            await call(client,'sandbox_bash',{'command':'touch /tmp/reset-marker /home/agent/.config/chromium/reset-profile-marker'})
            data,_=await call(client,'sandbox_init',{'workspace_path':directory,'mount_mode':'rw','forward_ports':[8765],'reset_if_running':True})
            assert data['container_id']!=first
            await call(client,'sandbox_bash',{'command':'test ! -e /tmp/reset-marker && test ! -e /home/agent/.config/chromium/reset-profile-marker && sudo touch /workspace/persisted'})
            assert Path(directory,'persisted').exists()
            shell,images=await call(client,'sandbox_bash',{'command':'printf out; printf err >&2; exit 7','take_screenshot_after':True},error=True)
            assert shell=={'exit_code':7,'stdout':'out','stderr':'err'} and len(images)==1
            shell,_=await call(client,'sandbox_bash',{'command':'#'+'x'*70000+'\nprintf large-request-ok'})
            assert shell['stdout']=='large-request-ok' and shell['exit_code']==0
            start=time.monotonic(); shell,_=await call(client,'sandbox_bash',{'command':'sleep 60 & echo $!'})
            assert time.monotonic()-start<3
            await call(client,'sandbox_bash',{'command':'kill -0 '+shell['stdout'].strip()})
            shell,_=await call(client,'sandbox_bash',{'command':'echo partial; sleep 30','timeout':1},error=True)
            assert shell['exit_code']==124 and 'partial' in shell['stdout']
            await call(client,'sandbox_bash',{'command':'chromium http://localhost:8765 & echo launched'})
            await call(client,'sandbox_bash',{'command':'mkdir -p "/tmp/with space"'})
            shell,_=await call(client,'sandbox_bash',{'command':'pwd','cwd':'/tmp/with space'}); assert shell['stdout'].strip()=='/tmp/with space'
            await call(client,'sandbox_bash',{'command':'true','cwd':'/absent'},error=True)
            await call(client,'sandbox_bash',{'command':'test ! -d /absent'})
            # Preserve shell output even when screenshot cannot be taken; disposable session only.
            shell,images=await call(client,'sandbox_bash',{'command':'supervisorctl -c /etc/supervisor/supervisord.conf stop xvfb >/dev/null; printf kept; printf diagnostic >&2; exit 7','take_screenshot_after':True},error=True)
            assert shell['stdout']=='kept' and shell['stderr']=='diagnostic' and shell['exit_code']==7 and shell['screenshot_error'] and not images
            await call(client,'sandbox_destroy'); assert Path(directory,'persisted').exists()
            data,_=await call(client,'sandbox_destroy'); assert data=={'destroyed':False,'container_id':None}
            data,_=await call(client,'sandbox_status'); assert data['status']=='stopped' and data['workspace_path'] is None
        print('MCP/mount/shell OK: six tools, guards, reuse/reset, ro/rw persistence, shell errors/background/timeout/screenshot failure')
    await concurrency_and_failures()
    await shutdown_checks()


async def disappeared(client, cid):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        try: await asyncio.to_thread(client.containers.get,cid)
        except docker.errors.NotFound: return
        await asyncio.sleep(.05)
    raise AssertionError(f'Own container leaked: {cid}')


async def concurrency_and_failures():
    from ephemeral_desktop_mcp.docker_manager import SandboxDockerManager
    defaults=[]
    try:
        for port in (9999,6080,5900):
            s=socket.socket()
            try:s.bind(('127.0.0.1',port));s.listen();defaults.append(s)
            except OSError:s.close()  # existing host occupant stays untouched
        async with session() as a, session() as b:
            x,_=await call(a,'sandbox_init'); y,_=await call(b,'sandbox_init')
            assert x['container_id']!=y['container_id'] and x['daemon_url']!=y['daemon_url'] and x['novnc_url']!=y['novnc_url']
            await action(a,'mouse_move',coordinate=[100,200]); await action(b,'mouse_move',coordinate=[700,600])
            z,_=await action(a,'cursor_position'); assert z['cursor']==[100,200]
            with httpx.Client(trust_env=False) as h:
                assert h.get(x['daemon_url']+'/health',headers={'Authorization':'Bearer wrong'}).status_code==401
            pending=asyncio.create_task(b.call_tool('sandbox_computer_action',{'action':'wait','duration':60}))
            await asyncio.sleep(.2)
            start=time.monotonic();await call(b,'sandbox_destroy');assert time.monotonic()-start<5
            try:await pending
            except Exception:pass
            await call(b,'sandbox_init')
            pending=asyncio.create_task(b.call_tool('sandbox_bash',{'command':'sleep 60','timeout':90}))
            await asyncio.sleep(.2)
            start=time.monotonic();await call(b,'sandbox_destroy');assert time.monotonic()-start<5
            try:await pending
            except Exception:pass
            await call(a,'sandbox_bash',{'command':'supervisorctl -c /etc/supervisor/supervisord.conf stop agent_daemon'},error=True)
            await call(a,'sandbox_bash',{'command':'true'},error=True)
            z,_=await call(a,'sandbox_status'); assert z['status']=='unhealthy'
            await call(a,'sandbox_destroy'); await call(b,'sandbox_destroy')
        for s in defaults:
            with socket.create_connection(s.getsockname(),timeout=1): pass
    finally:
        for s in defaults:s.close()
    m=SandboxDockerManager()
    try:
        m.image='vamo-mcp-missing-'+uuid.uuid4().hex
        try:await m.init()
        except RuntimeError as e:assert 'docker build' in str(e)
        else:raise AssertionError('missing image accepted')
        try:await m.init('/missing-'+uuid.uuid4().hex)
        except FileNotFoundError:pass
        else:raise AssertionError('missing workspace accepted')
    finally:await m.destroy()
    from unittest.mock import patch
    import mcp.client.stdio as transport
    original_spawn=transport._create_platform_compatible_process
    async def unavailable(*a,**kw):
        kw['env']={**kw['env'],'DOCKER_HOST':'unix:///missing-'+uuid.uuid4().hex+'.sock'}
        return await original_spawn(*a,**kw)
    with patch.object(transport,'_create_platform_compatible_process',unavailable):
        async with session() as client:
            await call(client,'sandbox_init',error=True)
            state,_=await call(client,'sandbox_status');assert state['container_id'] is None
    async with session() as client:
        await call(client,'sandbox_init',{'workspace_path':'/missing-'+uuid.uuid4().hex},error=True)
    # Hold one allocated port only after reservation release: real bind collision, no fake health.
    m=SandboxDockerManager(); original=m._allocate_ports; blocker=[]; attempts=[]
    def allocate(excluded):
        sockets=original(excluded); attempts.append([s.getsockname()[1] for s in sockets])
        if len(attempts)==1:
            class Reservation:
                def __init__(self,s):self.s=s;self.address=s.getsockname();self.closed=False
                def getsockname(self):return self.address
                def close(self):
                    if not self.closed:
                        self.closed=True;self.s.close()
                        b=socket.socket();b.bind(self.address);b.listen();blocker.append(b)
            sockets[0]=Reservation(sockets[0])
        return sockets
    m._allocate_ports=allocate
    try:
        data=await m.init(); assert len(attempts)==2 and int(data['daemon_url'].rsplit(':',1)[1])!=attempts[0][0]
        with socket.create_connection(blocker[0].getsockname(),timeout=1):pass
    finally:
        await m.destroy()
        for b in blocker:b.close()
    print('concurrency/failures OK: independent X99/ports, defaults occupied, collision retry, wrong token, daemon stopped, missing image/workspace')


async def shutdown_checks():
    import signal
    from unittest.mock import patch
    import mcp.client.stdio as transport
    client=local_docker()
    for mode in ('eof', 'sigint', 'sigterm') if os.name=='posix' else ('eof',):
        for active in ('idle','bash','gui','init'):
            proc=[]; own=[]; pending=None
            original=transport._create_platform_compatible_process
            async def spawn(*a,**kw):
                p=await original(*a,**kw);proc.append(p);return p
            try:
                with patch.object(transport,'_create_platform_compatible_process',spawn):
                    async with session() as s:
                        if active=='init':
                            # Record only containers created by this server's create request.
                            before={c.id for c in client.containers.list(all=True,filters={'label':'vamo-mcp.session'})}
                            pending=asyncio.create_task(s.call_tool('sandbox_init',{}))
                            deadline=time.monotonic()+10
                            while not own:
                                own=[c.id for c in client.containers.list(all=True,filters={'label':'vamo-mcp.session'}) if c.id not in before]
                                if time.monotonic()>deadline:raise AssertionError('init produced no container')
                                await asyncio.sleep(.01)
                        else:
                            data,_=await call(s,'sandbox_init');own=[data['container_id']]
                            if active!='idle':
                                name='sandbox_bash' if active=='bash' else 'sandbox_computer_action'
                                payload={'command':'echo active; sleep 60','timeout':90} if active=='bash' else {'action':'wait','duration':60}
                                pending=asyncio.create_task(s.call_tool(name,payload))
                                await asyncio.sleep(.2)
                        start=time.monotonic()
                        if mode=='eof':await proc[0].stdin.aclose()
                        else:proc[0].send_signal(signal.SIGINT if mode=='sigint' else signal.SIGTERM)
                        for cid in own:await disappeared(client,cid)
                        assert time.monotonic()-start<5
                        await asyncio.wait_for(proc[0].wait(),5)
                        if pending:
                            try:await pending
                            except Exception:pass
                print(f'shutdown OK: {mode} during {active}')
            finally:
                if pending and not pending.done():pending.cancel()
                for cid in own:
                    try:client.containers.get(cid).remove(force=True)
                    except docker.errors.NotFound:pass
    client.close()

async def observe():
    with fixture() as state:
        async with session() as client:
            info,_=await call(client,'sandbox_init',{'forward_ports':[8765]})
            await call(client,'sandbox_open_url',{'url':'http://localhost:8765','wait_seconds':.3})
            print('container_id='+info['container_id'], flush=True)
            print('novnc_url='+info['novnc_url'], flush=True)
            await asyncio.to_thread(input,'Enter: clique MCP; depois Enter: encerrar.\n')
            await action(client,'left_click',coordinate=center(state,'inc'))
            await until(lambda:state['count']==1)
            print('count=1; background=#225544',flush=True)
            await asyncio.to_thread(input,'Enter: encerrar sessão.\n')


async def benchmark():
    def stats(values):
        ordered=sorted(values)
        return {'p50_ms':round(ordered[len(ordered)//2],3),'p95_ms':round(ordered[int(len(ordered)*.95)-1],3)}
    with fixture() as state:
        async with session() as client:
            info,_=await call(client,'sandbox_init',{'forward_ports':[8765]})
            await call(client,'sandbox_open_url',{'url':'http://localhost:8765','wait_seconds':.3})
            docker_client=local_docker(); c=docker_client.containers.get(info['container_id'])
            script='''import main,time,json,statistics
def measure(fn):
 for _ in range(10):fn()
 values=[]
 for _ in range(100):
  t=time.perf_counter_ns();fn();values.append((time.perf_counter_ns()-t)/1e6)
 values.sort();return {'p50_ms':round(statistics.median(values),3),'p95_ms':round(values[94],3)}
s=main.mss.MSS(display=':99')
measurements={'mss_grab':measure(lambda:s.grab({'left':0,'top':0,'width':1280,'height':800})),
              'capture_cursor_png_base64':measure(main.capture_screen)}
# Alternate real cursor before starting timer; every frame requires fresh overlay/encoding.
values=[]
for i in range(110):
 main.run(['xdotool','mousemove','--sync',str(600+i%2), '500'],stdout=main.subprocess.DEVNULL)
 start=time.perf_counter_ns();main.capture_screen();elapsed=(time.perf_counter_ns()-start)/1e6
 if i>=10:values.append(elapsed)
values.sort();measurements['capture_changed_cursor']={'p50_ms':round(statistics.median(values),3),'p95_ms':round(values[94],3)}
print(json.dumps(measurements))
main.close_capture();s.close()
'''
            r=await asyncio.to_thread(c.exec_run,['/opt/venv/bin/python','-c',script],user='agent',workdir='/opt/agent_daemon')
            assert r.exit_code==0,r.output.decode(); measurements=json.loads(r.output)
            env=c.attrs['Config']['Env']; token=next(v.split('=',1)[1] for v in env if v.startswith('SANDBOX_TOKEN='))
            async with httpx.AsyncClient(base_url=info['daemon_url'],trust_env=False,headers={'Authorization':'Bearer '+token}) as h:
                for _ in range(10):(await h.post('/computer',json={'action':'screenshot'})).raise_for_status()
                values=[]
                for _ in range(100):
                    start=time.perf_counter_ns();r=await h.post('/computer',json={'action':'screenshot'});r.raise_for_status()
                    values.append((time.perf_counter_ns()-start)/1e6)
                measurements['http_screenshot']=stats(values)
            for _ in range(10):await client.call_tool('sandbox_computer_action',{'action':'screenshot'})
            values=[]
            for _ in range(100):
                start=time.perf_counter_ns();r=await client.call_tool('sandbox_computer_action',{'action':'screenshot'});assert not r.isError
                values.append((time.perf_counter_ns()-start)/1e6)
            measurements['mcp_screenshot']=stats(values)
            values=[]
            for _ in range(10):
                start=time.perf_counter_ns();await action(client,'key',text='Escape');values.append((time.perf_counter_ns()-start)/1e6)
            measurements['mcp_mutating_key']=stats(values)
            print(json.dumps({'platform':sys.platform,'samples':100,'warmups':10,'measurements':measurements},indent=2))
            assert measurements['mss_grab']['p95_ms']<10,measurements
            assert measurements['capture_cursor_png_base64']['p95_ms']<15,measurements
            assert measurements['http_screenshot']['p95_ms']<50,measurements
            docker_client.close()


ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'ephemeral-desktop-sandbox:latest'


def png(data):
    with Image.open(io.BytesIO(base64.b64decode(data))) as image:
        image.load()
        assert image.format == 'PNG' and image.size == (1280, 800)
        return image.convert('RGB')


@contextmanager
def standalone_container():
    client = local_docker()
    reservations = []
    for _ in range(3):
        s = socket.socket(); s.bind(('127.0.0.1', 0)); reservations.append(s)
    ports = [s.getsockname()[1] for s in reservations]
    env = dict(zip(('DAEMON_PORT', 'NOVNC_PORT', 'VNC_PORT'), map(str, ports)))
    env.update(SANDBOX_TOKEN=secrets.token_urlsafe(32), SANDBOX_SESSION_ID=str(uuid.uuid4()),
               VNC_PASSWORD=''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(8)))
    container = client.containers.create(IMAGE, environment=env, network_mode='host',
        shm_size='2g', init=True, auto_remove=True, cap_drop=['NET_RAW', 'NET_ADMIN'])
    try:
        for s in reservations: s.close()
        container.start()
        with httpx.Client(base_url=f'http://127.0.0.1:{ports[0]}', trust_env=False,
                          headers={'Authorization': 'Bearer '+env['SANDBOX_TOKEN']}, timeout=10) as http:
            deadline = time.monotonic()+60
            while True:
                try:
                    response = http.get('/health')
                    if response.status_code == 200:
                        assert response.headers['X-Sandbox-Session'] == env['SANDBOX_SESSION_ID']
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError(container.logs().decode())
                time.sleep(.1)
            yield container, http, env, ports
    finally:
        for s in reservations: s.close()
        try: container.remove(force=True)
        except docker.errors.NotFound: pass
        client.close()


def standalone():
    with standalone_container() as (c, h, env, ports):
        assert h.get('/health').json() == {'status': 'ok', 'display': ':99', 'resolution': [1280, 800]}
        assert h.get('/health', headers={'Authorization': ''}).status_code == 401
        assert h.post('/bash', headers={'Authorization': ''}, json={'command': 'touch /tmp/unauthorized'}).status_code == 401
        for endpoint in ('/computer','/bash','/open_url'):
            denied=h.post(endpoint,headers={'Authorization':'','Content-Type':'application/json'},content='{broken')
            assert denied.status_code==401 and 'X-Sandbox-Session' not in denied.headers
        def shell(command, **kw):
            r = h.post('/bash', json={'command': command, **kw}); r.raise_for_status(); return r.json()
        assert shell('test ! -e /tmp/unauthorized')['exit_code'] == 0
        assert shell('id -u')['stdout'].strip() == '1000'
        assert shell('sudo -n id -u')['stdout'].strip() == '0'
        assert shell('touch /workspace/writable')['exit_code'] == 0
        shot = h.post('/computer', json={'action': 'screenshot'}); shot.raise_for_status(); png(shot.json()['screenshot_base64'])
        moved = h.post('/computer', json={'action': 'mouse_move', 'coordinate': [400, 500]}).json()
        assert moved['cursor'] == [400, 500]
        invalid=h.post('/computer',content='{"action":"wait","duration":NaN}',headers={'Content-Type':'application/json'})
        assert invalid.status_code==422,invalid.text
        assert h.post('/computer',content='{broken',headers={'Content-Type':'application/json'}).status_code==422
        for body in ({'action': 'mouse_move', 'coordinate': [1280, 0]},
                     {'action': 'mouse_move', 'coordinate': [True, 0]},
                     {'action': 'mouse_move', 'coordinate': [1.5, 0]},
                     {'action': 'mouse_move', 'coordinate': [1]},
                     {'action': 'left_click_drag', 'coordinate': [2, 3]},
                     {'action': 'key', 'text': ''}):
            assert h.post('/computer', json=body).status_code == 422, body
        assert h.post('/computer', json={'action': 'cursor_position'}).json()['cursor'] == [400, 500]
        assert h.post('/bash', json={'command': 'true', 'timeout': 0}).status_code == 422
        assert h.post('/open_url', json={'url': 'javascript:alert(1)'}).status_code == 422
        result = shell("printf out; printf err >&2; exit 7")
        assert result == {'stdout': 'out', 'stderr': 'err', 'exit_code': 7}, result
        start = time.monotonic(); result = shell('sleep 60 & echo $!')
        assert time.monotonic()-start < 3
        assert shell('kill -0 '+result['stdout'].strip())['exit_code'] == 0
        result = shell('echo $$ > /tmp/timeout-group; echo partial; sleep 30', timeout=1)
        assert result['exit_code'] == 124 and 'partial' in result['stdout'] and 'timed out' in result['stderr']
        assert shell('kill -0 -- -$(cat /tmp/timeout-group) 2>/dev/null')['exit_code'] != 0
        assert shell("mkdir -p '/tmp/space dir'")['exit_code'] == 0
        assert shell('pwd', cwd='/tmp/space dir')['stdout'].strip() == '/tmp/space dir'
        assert h.post('/bash', json={'command': 'true', 'cwd': '/absent'}).status_code == 422
        fallback = '''import main, base64, io
from PIL import Image
from mss.exception import ScreenShotError
main.mss.MSS=lambda **kw: (_ for _ in ()).throw(ScreenShotError('forced'))
r=main.capture_screen()
image=Image.open(io.BytesIO(base64.b64decode(r['screenshot_base64'])));image.load()
assert image.size==(1280,800) and image.convert('RGB').getpixel((400,500))==(255,0,0)
assert r['cursor']==[400,500]
import os
os.rename('/usr/bin/scrot','/usr/bin/scrot.hidden')
try:
 try: main.capture_screen()
 except FileNotFoundError: pass
 else: raise AssertionError('capture failure hidden')
finally: os.rename('/usr/bin/scrot.hidden','/usr/bin/scrot')
print('fallback OK')
'''
        r = c.exec_run(['/opt/venv/bin/python', '-c', fallback], workdir='/opt/agent_daemon')
        assert r.exit_code == 0, r.output.decode()
        assert shell("ss -xl | fgrep '@/tmp/.X11-unix/X99'")['exit_code'] == 1
        listeners = shell('ss -ltnH')['stdout']
        for line in listeners.splitlines():
            if any(f':{p} ' in line for p in ports):
                assert '127.0.0.1:' in line, line
        processes = shell('ps -eo uid=,comm=')['stdout'].splitlines()
        for name in ('Xvfb','openbox','chromium','uvicorn'):
            matches=[line.split() for line in processes if line.split()[-1]==name]
            assert matches and all(row[0]=='1000' for row in matches), (name,matches)
        assert not any(':6099 ' in line for line in listeners.splitlines())
        for coordinate in ([0,0],[1279,799]):
            image=png(h.post('/computer',json={'action':'mouse_move','coordinate':coordinate}).json()['screenshot_base64'])
            assert image.getpixel(tuple(coordinate))==(255,0,0)
        shell('supervisorctl -c /etc/supervisor/supervisord.conf stop chromium openbox')
        shell('xsetroot -solid "#123456"')
        first=png(h.post('/computer',json={'action':'screenshot'}).json()['screenshot_base64'])
        assert first.getpixel((100,100))==(18,52,86)
        for _ in range(2):
            assert png(h.post('/computer',json={'action':'screenshot'}).json()['screenshot_base64']).getpixel((100,100))==(18,52,86)
        shell('xsetroot -solid "#654321"')
        changed=png(h.post('/computer',json={'action':'screenshot'}).json()['screenshot_base64'])
        assert changed.getpixel((100,100))==(101,67,33)
        cursor=png(h.post('/computer',json={'action':'mouse_move','coordinate':[100,100]}).json()['screenshot_base64'])
        assert cursor.getpixel((100,100))==(255,0,0)
        assert cursor.getpixel((1279,799))==(101,67,33)
        print('standalone OK: auth, PNG, UID/sudo, strict input, shell/background/timeout, scrot fallback, private X11')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--standalone', action='store_true')
    parser.add_argument('--benchmark', action='store_true')
    parser.add_argument('--observe', action='store_true')
    args = parser.parse_args()
    if args.standalone:
        standalone()
    elif args.benchmark:
        asyncio.run(benchmark())
    elif args.observe:
        asyncio.run(observe())
    else:
        asyncio.run(integrated())
