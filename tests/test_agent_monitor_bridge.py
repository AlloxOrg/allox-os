"""The real-Agent harness must preserve tool identity, not turn calls into prose."""

import importlib.util
import json
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1]/'examples/qwen_codex_117/run_lab_on_117.py'
_spec = importlib.util.spec_from_file_location('allox_test_lab_bridge', _path)
assert _spec is not None and _spec.loader is not None
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_bridge_keeps_namespace_and_function_identity():
    request, mapping = bridge.chat_request({'tools': [{'type': 'namespace', 'name': 'functions',
        'tools': [{'type': 'function', 'name': 'exec_command',
                   'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}}}]}]}, 'lab')
    assert request['tools'][0]['function']['name'] == 'functions__exec_command'
    result = bridge.responses_result({'choices': [{'message': {'tool_calls': [{'id': 'call-1',
        'function': {'name': 'functions__exec_command', 'arguments': '{"cmd":"true"}'}}]}}]}, mapping, 'lab')
    call = result['output'][0]
    assert call['type'] == 'function_call'
    assert call['namespace'] == 'functions'
    assert call['name'] == 'exec_command'
    assert call['call_id'] == 'call-1'


def test_bridge_preserves_tool_result_history():
    request, _ = bridge.chat_request({'input': [
        {'type': 'function_call', 'namespace': 'functions', 'name': 'exec_command',
         'call_id': 'call-1', 'arguments': '{"cmd":"true"}'},
        {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'exit 0'}]}, 'lab')
    assert request['messages'][0]['tool_calls'][0]['function']['name'] == 'functions__exec_command'
    assert request['messages'][1] == {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'exit 0'}


def test_bridge_excludes_tools_outside_test_scope():
    request, mapping = bridge.chat_request({'tools': [{'type': 'function', 'name': 'web_search'}]}, 'lab')
    assert 'tools' not in request
    assert not mapping


def test_bridge_collects_developer_messages_at_the_beginning():
    request, _ = bridge.chat_request({'instructions': 'base', 'input': [
        {'role': 'user', 'content': 'task'}, {'role': 'developer', 'content': 'later policy'}]}, 'lab')
    assert request['messages'] == [{'role': 'system', 'content': 'base\n\nlater policy'},
                                   {'role': 'user', 'content': 'task'}]


def test_bridge_rejects_empty_model_results():
    with pytest.raises(ValueError):
        bridge.responses_result({'choices': [{'message': {'content': None}}]}, {}, 'lab')


def test_bridge_carries_only_reported_usage_and_provider_metrics():
    result = bridge.responses_result({'choices': [{'message': {'content': 'OK'}}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5},
        'metrics': {'queue_time_ms': 3}}, {}, 'lab')
    assert result['usage']['input_tokens'] == 4
    assert result['metrics'] == {'queue_time_ms': 3}
    missing = bridge.responses_result({'choices': [{'message': {'content': 'OK'}}]}, {}, 'lab')
    assert 'usage' not in missing and 'metrics' not in missing


def test_bridge_normalizes_explicit_argv_without_changing_its_meaning():
    arguments = bridge.normalize_arguments('exec_command', '{"cmd":"[\\"python3\\",\\"a b.py\\"]"}')
    assert json.loads(arguments)['cmd'] == "python3 'a b.py'"


def test_bridge_converts_only_explicit_advertised_xml_calls():
    mapping = {'exec_command': ('exec_command', 'function', None)}
    text = '<tool_call><function=exec_command><parameter=cmd>["python3","task.py"]</parameter></function></tool_call>'
    result = bridge.responses_result({'choices': [{'message': {'content': text}}]}, mapping, 'lab')
    assert result['output'][0]['type'] == 'function_call'
    assert json.loads(result['output'][0]['arguments'])['cmd'] == 'python3 task.py'
    assert bridge.explicit_xml_call(text.replace('exec_command', 'unknown'), mapping) is None
    assert bridge.explicit_xml_call('Please execute python3 task.py', mapping) is None
