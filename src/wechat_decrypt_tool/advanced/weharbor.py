"""WeHarbor's authenticated prepare/commit API and discovered MCP tools."""
import json
from urllib.parse import urlsplit
import httpx
from .store import config


class BridgeError(ValueError):
    pass


def validate_origin(value):
    if not isinstance(value, str) or chr(92) in value:
        raise BridgeError('WeHarbor 地址格式不正确')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeError('WeHarbor 地址必须是 HTTP(S) 服务地址，不能包含凭据、查询参数或片段')
    return value.rstrip('/')


class WeHarbor:
    def __init__(self, settings=None, transport=None):
        self.settings = settings if settings is not None else config()
        self.origin = validate_origin(self.settings['origin'])
        self.transport = transport

    def path(self, suffix):
        # Loopback workspace listens directly; public origins use /workspace/.
        parsed = urlsplit(self.origin)
        prefix = '' if parsed.port == 8766 or parsed.path.rstrip('/').endswith('/workspace') else '/workspace'
        return self.origin + prefix + suffix

    async def request(self, method, suffix, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.settings.get('token', ''), 'Accept': 'application/json, text/event-stream'}
        try:
            async with httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                response = await client.request(method, self.path(suffix), headers=headers, **kwargs)
        except httpx.HTTPError:
            raise BridgeError('无法连接 WeHarbor，请检查地址、网络和服务状态') from None
        if not 200 <= response.status_code < 300:
            # Never echo arbitrary upstream bodies (may contain tokens).
            raise BridgeError('WeHarbor 请求失败（HTTP %s）' % response.status_code)
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise BridgeError('WeHarbor 响应必须是 JSON 对象')
            return result
        except ValueError:
            for line in response.text.splitlines():
                if line.startswith('data:'):
                    try:
                        result = json.loads(line[5:])
                        if isinstance(result, dict) and ('result' in result or 'error' in result):
                            return result
                    except ValueError:
                        pass
            raise BridgeError('WeHarbor 未返回 JSON/MCP 响应，请检查服务地址') from None

    async def rpc(self, method, params=None, **kwargs):
        result = await self.request('POST', '/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, **kwargs)
        if result.get('error'):
            raise BridgeError('WeHarbor MCP 请求失败')
        payload = result.get('result', {})
        if not isinstance(payload, dict):
            raise BridgeError('WeHarbor MCP 结果格式不正确')
        return payload

    async def tools(self):
        tools = (await self.rpc('tools/list', timeout=5)).get('tools', [])
        if not isinstance(tools, list) or any(not isinstance(t, dict) or not isinstance(t.get('name'), str) for t in tools):
            raise BridgeError('WeHarbor 工具列表格式不正确')
        return tools

    async def call(self, name, arguments):
        result = await self.rpc('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise BridgeError('WeHarbor 工具拒绝执行，请在 WeHarbor 检查桌面状态和操作回执')
        return result

    async def read(self, path, params):
        return await self.request('GET', '/data/' + path, params=params)
