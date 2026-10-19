import sys
import pytest
from fastapi.testclient import TestClient

@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux source service')
def test_linux_service_starts_without_windows_or_macos_native_core(monkeypatch):
    from wechat_decrypt_tool.api import app
    from wechat_decrypt_tool import native_core_client
    def forbidden():
        raise AssertionError('Linux must not load the proprietary native core')
    monkeypatch.setattr(native_core_client, 'configure_native_core_entrypoint', forbidden)
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
        result = client.get('/api/linux/status')
        assert result.status_code == 200
        assert result.json()['supported'] is True
