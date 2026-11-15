"""117-only real connectivity check; credentials arrive via SSH stdin, memory only."""
import http.client
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

online = set()
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    first, _, last = part.partition('-')
    online.update(range(int(first), int(last or first) + 1))
selected = sorted(online)[-24:]
assert len(selected) == 24
os.sched_setaffinity(0, selected)
config = json.load(sys.stdin)
target = urlsplit(config['base_url'])
headers = {'Content-Type': 'application/json', config['auth_header']: 'Bearer ' + config['key']}
kind = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
connection = kind(target.hostname, target.port, timeout=60)
report = {'cpus': selected, 'host': target.hostname, 'configured_model': config['model']}
try:
    started = time.perf_counter_ns()
    connection.request('GET', target.path.rstrip('/') + '/models', headers=headers)
    response = connection.getresponse()
    raw = response.read(2*1024*1024)
    report['models_status'] = response.status
    if response.status == 200:
        report['models'] = [m['id'] for m in json.loads(raw).get('data', [])]
    body = {'model': config['model'], 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
            'max_tokens': 32, 'stream': False, 'chat_template_kwargs': {'enable_thinking': False}}
    if 'tools' in sys.argv:
        body['messages'][0]['content'] = 'Call exec_command with cmd equal to true.'
        body.update(tools=[{'type': 'function', 'function': {'name': 'exec_command',
            'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}, 'required': ['cmd']}}}],
            tool_choice='auto', parallel_tool_calls=False)
        body['max_tokens'] = 128
    connection.request('POST', target.path.rstrip('/') + '/chat/completions', json.dumps(body), headers)
    response = connection.getresponse()
    raw = response.read(2*1024*1024)
    report['chat_status'] = response.status
    value = json.loads(raw)
    report.update(chat_status=response.status, has_choice=bool(value.get('choices')),
                  elapsed_ms=(time.perf_counter_ns()-started)/1e6)
except Exception as exc:
    report['error_type'] = type(exc).__name__  # Never include a raw provider body/config.
finally:
    connection.close()
print(json.dumps(report), flush=True)
raise SystemExit(0 if report.get('chat_status') == 200 and report.get('has_choice') else 1)
