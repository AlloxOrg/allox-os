"""Disposable real-Agent test. Read provider credentials from SSH stdin, never a file."""

from __future__ import annotations

import http.client
import json
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path('/data/hy/Allox/agent-monitoring-main-20261030-v2')


def chat_request(request, model):
    messages = []
    if request.get('instructions'):
        messages.append({'role': 'system', 'content': request['instructions']})
    inputs = request.get('input', [])
    if isinstance(inputs, str):
        inputs = [{'role': 'user', 'content': inputs}]
    for item in inputs:
        kind = item.get('type', 'message')
        if kind == 'function_call':
            function_name = (item.get('namespace', '')+'__' if item.get('namespace') else '')+item['name']
            messages.append({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': item['call_id'], 'type': 'function', 'function': {
                    'name': function_name.replace('.', '__'), 'arguments': item['arguments']}}]})
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'],
                             'content': str(item.get('output', ''))})
        elif kind == 'message':
            content = item.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(part.get('text', '') for part in content
                                    if part.get('type') in ('input_text', 'output_text', 'text'))
            role = item.get('role', 'user')
            messages.append({'role': 'system' if role == 'developer' else role, 'content': content})
    tools, mapping = [], {}
    flattened = []
    for tool in request.get('tools') or []:
        if tool.get('type') == 'namespace':
            flattened.extend({**inner, '_namespace': tool['name']} for inner in tool['tools'])
        else:
            flattened.append(tool)
    for tool in flattened:
        kind = tool.get('type')
        if kind not in ('function', 'custom'):
            continue
        original = tool['name']
        # Test scope is shell execution only; do not expose web/subagent tools.
        if original not in ('exec_command', 'write_stdin', 'shell_command'):
            continue
        namespace = tool.get('_namespace')
        name = ((namespace+'__' if namespace else '')+original).replace('.', '__')
        if name in mapping:
            raise ValueError('tool name collision')
        mapping[name] = (original, kind, namespace)
        parameters = tool.get('parameters') if kind == 'function' else {
            'type': 'object', 'properties': {'input': {'type': 'string'}}, 'required': ['input']}
        tools.append({'type': 'function', 'function': {'name': name,
            'description': tool.get('description', ''), 'parameters': parameters or {'type': 'object'}}})
    result = {'model': model, 'messages': messages, 'stream': False,
              'max_tokens': min(request.get('max_output_tokens') or 2048, 2048),
              'chat_template_kwargs': {'enable_thinking': False}}
    if tools:
        result.update(tools=tools, tool_choice='auto', parallel_tool_calls=False)
    # This provider rejects later developer/system messages. Retain all their text
    # in one leading system message, as the previous Qwen compatibility shim did.
    system = [message['content'] for message in messages if message['role'] == 'system']
    result['messages'] = ([{'role': 'system', 'content': '\n\n'.join(system)}] if system else [])+[
        message for message in messages if message['role'] != 'system']
    return result, mapping


def normalize_arguments(name, arguments):
    parsed = json.loads(arguments)
    if name == 'exec_command' and isinstance(parsed, dict):
        command = parsed.get('cmd')
        if isinstance(command, str) and command.strip().startswith('['):
            try:
                command = json.loads(command)
            except ValueError:
                pass
        if isinstance(command, list) and command and all(isinstance(item, str) for item in command):
            parsed['cmd'] = shlex.join(command)
    return json.dumps(parsed)


def explicit_xml_call(content, mapping):
    """Accept only an explicit, well-formed call to an advertised tool, never prose."""
    if not isinstance(content, str):
        return None
    match = re.fullmatch(r'\s*<tool_call>\s*<function=([\w.]+)>(.*?)</function>\s*</tool_call>\s*', content, re.DOTALL)
    if not match or match[1] not in mapping:
        return None
    parameters = {}
    rest = match[2]
    pattern = re.compile(r'\s*<parameter=([\w]+)>(.*?)</parameter>', re.DOTALL)
    while rest.strip():
        parameter = pattern.match(rest)
        if not parameter or parameter[1] in parameters:
            return None
        text = parameter[2].strip()
        try:
            parameters[parameter[1]] = json.loads(text)
        except ValueError:
            parameters[parameter[1]] = text
        rest = rest[parameter.end():]
    return {'id': 'call_'+uuid.uuid4().hex, 'type': 'function',
            'function': {'name': match[1], 'arguments': json.dumps(parameters)}}


def responses_result(value, mapping, model):
    choices = value.get('choices') or []
    if not choices:
        raise ValueError('model returned no choices')
    message = choices[0]['message']
    calls = message.get('tool_calls') or []
    converted = explicit_xml_call(message.get('content'), mapping) if not calls else None
    if converted:
        calls = [converted]
    output = []
    for call in calls:
        function = call['function']
        name, kind, namespace = mapping[function['name']]
        item = {'id': 'fc_'+uuid.uuid4().hex, 'call_id': call['id'], 'name': name,
                'status': 'completed'}
        if kind == 'function':
            item.update(type='function_call', arguments=normalize_arguments(name, function['arguments']))
            if namespace:
                item['namespace'] = namespace
        else:
            item.update(type='custom_tool_call', input=json.loads(function['arguments'])['input'])
        output.append(item)
    if message.get('content') and not converted:
        output.append({'id': 'msg_'+uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text',
            'text': message['content'], 'annotations': []}]})
    if not output:
        raise ValueError('model returned neither text nor a tool call')
    usage = value.get('usage', {})
    result = {'id': 'resp_'+uuid.uuid4().hex, 'object': 'response', 'created_at': int(time.time()),
              'status': 'completed', 'model': model, 'output': output}
    if all(type(usage.get(name)) is int for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
        result['usage'] = {'input_tokens': usage['prompt_tokens'],
                          'output_tokens': usage['completion_tokens'],
                          'total_tokens': usage['total_tokens']}
    if isinstance(value.get('metrics'), dict):
        result['metrics'] = value['metrics']
    return result


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        server = self.server
        status, result = 502, {'error': {'message': 'lab gateway request failed'}}
        started = time.perf_counter_ns()
        upstream_status = None
        tool_names = []
        output_types = []
        error_detail = None
        try:
            if self.path != '/v1/responses' or not secrets.compare_digest(
                self.headers.get('Authorization', ''), 'Bearer '+server.gateway_token
            ):
                self.send_error(403)
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2*1024*1024:
                raise ValueError('invalid request size')
            request = json.loads(self.rfile.read(length))
            tool_names = [item.get('name', item.get('type')) for item in request.get('tools') or []]
            body, mapping = chat_request(request, server.config['model'])
            target = urlsplit(server.config['base_url'])
            connection_type = http.client.HTTPSConnection if target.scheme == 'https' else http.client.HTTPConnection
            connection = connection_type(target.hostname, target.port, timeout=90)
            try:
                connection.request('POST', target.path.rstrip('/')+'/chat/completions',
                    json.dumps(body).encode(), {'Content-Type': 'application/json',
                    server.config['auth_header']: ('Bearer ' if server.config['auth_header'] == 'Authorization' else '')+server.config['key']})
                response = connection.getresponse()
                upstream_status = response.status
                raw = response.read(8*1024*1024)
                if response.status != 200:
                    try:
                        error = json.loads(raw)
                        error_detail = str(error.get('error', error.get('detail', 'unavailable')))
                        error_detail = error_detail.replace(server.config['key'], '[REDACTED]')[:600]
                    except (ValueError, AttributeError):
                        error_detail = 'non-JSON upstream failure'
                    raise ValueError('upstream rejected inference')
                result = responses_result(json.loads(raw), mapping, server.config['model'])
                output_types = [item['type'] for item in result['output']]
                status = 200
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - no raw provider errors/credentials in logs
            result = {'error': {'message': 'lab gateway: '+type(exc).__name__}}
        finally:
            server.requests.append({'http_status': status, 'upstream_http_status': upstream_status,
                'elapsed_ns': time.perf_counter_ns()-started, 'tool_names': tool_names,
                'response_output_types': output_types, 'upstream_error': error_detail})
        encoded = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main():
    if not ROOT.is_dir() or Path.cwd().resolve() != ROOT:
        raise SystemExit('run only in the dedicated 117 experiment directory')
    config = json.load(sys.stdin)
    if not config.get('key') or not config.get('model'):
        raise SystemExit('missing in-memory provider configuration')
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    name = 'allox-monitor-lab-'+stamp
    result = ROOT / 'real-agent-results' / name
    result.mkdir(parents=True, mode=0o700)
    print('RESULT_DIR', result, flush=True)
    bridge = json.loads(subprocess.check_output(['docker', 'network', 'inspect', 'bridge'], text=True))[0]
    address = bridge['IPAM']['Config'][0]['Gateway']
    gateway = ThreadingHTTPServer((address, 0), Gateway)
    gateway.config, gateway.gateway_token, gateway.requests = config, secrets.token_urlsafe(32), []
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    metadata = {'passed': False, 'model': config['model'], 'runtime': 'kata-allox-ebpf',
                'provider_credential_in_guest': False, 'cpu_binding': 'none; user override',
                'protocol_bridge': 'test-only Responses to Chat Completions',
                'main_commit': '44977ad', 'snapshot_backend': 'directory fixture; no rollback tested'}
    container = None
    try:
        command = ['docker', 'run', '-d', '--name', name, '--runtime', 'kata-allox-ebpf',
            '--cgroupns', 'host', '--cap-add', 'SYS_ADMIN', '--shm-size', '512m',
            '--security-opt', 'seccomp=unconfined', '--security-opt', 'apparmor=unconfined',
            '-e', 'PYTHONPATH=', '-e', 'PYTHONDONTWRITEBYTECODE=1',
            '-e', 'TMPDIR=/dev/shm', '-e', 'ALLOX_QWEN_MODEL='+config['model'],
            '-e', f'ALLOX_QWEN_UPSTREAM=http://{address}:{gateway.server_port}/v1',
            '-e', 'ALLOX_GATEWAY_TOKEN='+gateway.gateway_token]
        for source, target in (
            (ROOT/'dist', '/dist/core'),
            (ROOT/'plugins/agent-monitor/dist', '/dist/monitor'),
            (Path(__file__).with_name('guest_smoke.py'), '/opt/guest_smoke.py'),
            (Path('/data/act/bin/qwen-codex-exec'), '/usr/local/bin/qwen-codex-exec'),
            (Path('/opt/openssl-1.1.1q/libssl.so.1.1'), '/usr/local/lib/qwen-test/libssl.so.1.1'),
            (Path('/opt/openssl-1.1.1q/libcrypto.so.1.1'), '/usr/local/lib/qwen-test/libcrypto.so.1.1'),
        ):
            if not source.exists():
                raise RuntimeError('missing required test artifact')
            command += ['-v', f'{source}:{target}:ro']
        command += ['-v', f'{result}:/results', 'allox-ebpf-test:20260907', 'sh', '-ec',
            ('python3 -m pip install -q --no-deps /dist/core/*.whl /dist/monitor/*.whl; '
            'python3 -m allox.runtime.bootstrap; python3 /opt/guest_smoke.py')]
        container = subprocess.check_output(command, text=True, timeout=90).strip()
        metadata['container'] = container
        print('KATA_STARTED', container, flush=True)
        (result/'runtime.json').write_text(json.dumps(metadata, indent=2))
        exit_code = subprocess.check_output(['docker', 'wait', container], text=True, timeout=1050).strip()
        console = subprocess.check_output(['docker', 'logs', container], text=True)
        (result/'console.log').write_text(console.replace(config['key'], '[REDACTED]'))
        metadata['container_exit_code'] = int(exit_code)
        if (result/'report.json').exists():
            report = json.loads((result/'report.json').read_text())
            metadata['passed'] = report.get('passed', False)
            print(json.dumps({'passed': metadata['passed'], 'checks': report.get('checks'),
                              'error': report.get('error')}), flush=True)
        else:
            print('Guest exited before producing a report; see console.log', flush=True)
    finally:
        if container:
            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        gateway.shutdown()
        gateway.server_close()
        metadata.update(test_container_removed=True, gateway_requests=gateway.requests)
        (result/'runtime.json').write_text(json.dumps(metadata, indent=2))
    return 0 if metadata['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
