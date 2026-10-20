#!/usr/bin/env python3
"""Forward MCP JSON lines to the analysis service without logging credentials."""
import json
import os
import sys
import urllib.request
from pathlib import Path


def main():
    endpoint = os.environ.get('WECHAT_ANALYSIS_MCP_URL', 'http://127.0.0.1:10392/api/advanced/mcp')
    token_file = Path(os.environ.get('WECHAT_ANALYSIS_MCP_TOKEN_FILE',
        str(Path(os.environ.get('WECHAT_TOOL_DATA_DIR', '.')) / 'advanced/mcp.token')))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for line in sys.stdin:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError('Expected JSON object')
            token = os.environ.get('WECHAT_ANALYSIS_MCP_TOKEN') or token_file.read_text().strip()
            request = urllib.request.Request(endpoint, data=json.dumps(message).encode(), headers={
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with opener.open(request, timeout=90) as response:
                result = response.read()
            if result and 'id' in message:
                print(result.decode(), flush=True)
        except Exception:
            if isinstance(message, dict) and 'id' in message:
                print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'error': {
                    'code': -32603, 'message': 'Analysis MCP unavailable; check service URL and token file'}}), flush=True)


if __name__ == '__main__':
    main()
