"""Work actually executed by the LLM Agent, inside its Session sandbox."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from allox_agent_monitor import Tracer
from allox_agent_monitor.transport import SessionQueueTransport

mode = os.environ['E2E_MODE']
options = {'action_batch_size': 8, 'action_flush_interval': .05} if mode == 'batch' else {}
tracer = Tracer(SessionQueueTransport(), enabled=mode != 'off', delivery='queued', batch_size=32,
                strict=True, **options)
steps = int(os.environ.get('E2E_STEPS', '12'))
ambiguous = os.environ.get('E2E_AMBIGUOUS') == '1'
rows = []
started = time.perf_counter_ns()
with tracer:
    for index in range(steps):
        fields = {'turn_id': 'inner-turn-'+str(index)} if ambiguous else {}
        with tracer.span('action-'+str(index), 'turn', **fields):
            def work():
                file = Path('probe-'+str(index)+'.txt')
                content = 'allox-e2e-'+str(index)
                file.write_text(content)
                assert file.read_text() == content
                renamed = file.with_suffix('.renamed')
                file.rename(renamed)
                assert renamed.read_text() == content
                os.chmod(renamed, 0o600)
                child = json.loads(subprocess.check_output([sys.executable, '-c',
                    'import os,json; print(json.dumps({"pid":os.getpid(),"ppid":os.getppid()}))']))
                connection = http.client.HTTPConnection('127.0.0.1', int(os.environ['E2E_PORT']), timeout=5)
                connection.request('GET', '/fixture/'+str(index))
                response = connection.getresponse()
                body = response.read()
                assert response.status == 200 and body == b'pong'
                connection.close()
                sock_path = '/tmp/allox-e2e-'+str(index)+'.sock'
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(sock_path)
                    listener.listen(1)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(sock_path)
                        peer, _ = listener.accept()
                        with peer:
                            client.sendall(b'ping')
                            assert peer.recv(4) == b'ping'
                os.unlink(sock_path)
                assert child['pid'] != os.getpid() and child['ppid'] == os.getpid()
                time.sleep(.001)
                return {'index': index, 'bytes': len(content), 'child': child}
            rows.append(tracer.call_tool('file-network-socket-child', work))
total = time.perf_counter_ns() - started
result = {'agent': os.environ['ALLOX_AGENT_ID'], 'session': os.environ['ALLOX_SESSION_ID'],
          'steps': steps, 'rows': rows, 'health': tracer.health(), 'wall_ns_including_close': total,
          'management_token_absent': 'ALLOX_WORKSPACE_TOKEN' not in os.environ}
Path('proof.json').write_text(json.dumps(result))
print(json.dumps({'proof': 'proof.json', 'steps': steps, 'complete': tracer.health()['complete']}))
