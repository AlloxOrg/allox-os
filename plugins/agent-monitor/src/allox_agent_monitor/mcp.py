"""Duck-typed async MCP client adapter; client latency is NOT remote execution time."""
import time

from allox_agent_monitor.sdk import payload


class TracedMCPClient:
    def __init__(self, client, tracer):
        self.client, self.tracer = client, tracer

    async def _request(self, method, phase, *args, **kwargs):
        with self.tracer.phase(phase) as span:
            started = time.perf_counter_ns()
            try:
                result = await getattr(self.client, method)(*args, **kwargs)
                span.set_result(result)
                return result
            finally:
                span.metric('mcp.client_request_ns', time.perf_counter_ns()-started)

    async def initialize(self, *args, **kwargs):
        return await self._request('initialize', 'mcp_initialize', *args, **kwargs)

    async def list_tools(self, *args, **kwargs):
        return await self._request('list_tools', 'mcp_discovery', *args, **kwargs)

    async def call_tool(self, name, *args, **kwargs):
        with self.tracer.span(name, 'tool', input=payload({'args': args, 'kwargs': kwargs},
                                                        self.tracer.capture_content)) as span:
            result = await self._request('call_tool', 'mcp_call', name, *args, **kwargs)
            error = result.get('isError') if isinstance(result, dict) else getattr(result, 'isError', False)
            if error is True:
                span.mark_error('MCPToolError')
            span.set_result(result)
            return result
