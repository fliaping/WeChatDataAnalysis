import ctypes
import json
import os
import struct
from pathlib import Path
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from wechat_decrypt_tool import linux_support as linux
from wechat_decrypt_tool.image_key_memory_scan import _LinuxMemoryApi, MAX_MEMORY_REGION_SIZE
from wechat_decrypt_tool.media_helpers import _decrypt_wechat_dat_v3, _decrypt_wechat_dat_v4

def test_linux_reuses_account_keys_without_cross_account_fallback(tmp_path, monkeypatch):
    root = tmp_path / 'xwechat_files'
    (root / 'account-a/db_storage').mkdir(parents=True)
    (root / 'account-b/db_storage').mkdir(parents=True)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'dbPath':str(root),'myWxid':'account-a','decryptKey':'a'*64,
        'wxidConfigs':{'account-a':{'decryptKey':'b'*64},'account-b':{}}}))
    found = linux.weflow_accounts(config)
    assert len(found) == 1 and found[0]['account'] == 'account-a'
    assert found[0]['key'] == 'b'*64

def test_unstable_pair_is_retried_and_never_accepted(tmp_path, monkeypatch):
    source = tmp_path / 'a.db'; source.write_bytes(b'encrypted')
    (tmp_path / 'a.db-wal').write_bytes(b'wal')
    counter = iter(range(100))
    monkeypatch.setattr(linux, '_signature', lambda _: (next(counter), None))
    with pytest.raises(RuntimeError, match='stable DB/WAL'):
        linux._snapshot(source, tmp_path / 'copy.db')

@pytest.mark.skipif(not Path('/proc/self/mem').exists(), reason='Linux procfs only')
def test_linux_memory_provider_reads_own_buffer_and_closes():
    buffer = ctypes.create_string_buffer(b'local synthetic image key fixture')
    api = _LinuxMemoryApi(); handle = api.open_process(os.getpid())
    fd = handle[0]
    try:
        assert api.read_memory(handle, ctypes.addressof(buffer), 5) == b'local'
        assert all(r.size <= MAX_MEMORY_REGION_SIZE for r in handle[1])
        assert api.query_region(handle, 0) is not None
    finally:
        api.close_handle(handle)
    with pytest.raises(OSError): os.fstat(fd)

def test_linux_xor_and_v2_image_roundtrip():
    key, xor = b'0123456789abcdef', 0xa6
    image = b'\xff\xd8\xff' + bytes(range(256))*2 + b'\xff\xd9'
    encrypted = bytes(v ^ xor for v in image)
    assert _decrypt_wechat_dat_v3(encrypted, xor) == image
    head, tail = image[:100], image[-20:]
    aes = AES.new(key, AES.MODE_ECB).encrypt(pad(head, 16))
    dat = struct.pack('<6sLLx', b'\x07\x08V2\x08\x07', len(head), len(tail)) + aes + image[100:-20] + bytes(v ^ xor for v in tail)
    assert _decrypt_wechat_dat_v4(dat, xor, key) == image
    with pytest.raises(ValueError): _decrypt_wechat_dat_v4(dat, xor, b'wrong image key!')

def test_linux_capture_rejects_unknown_account_before_debugger(tmp_path):
    with pytest.raises(ValueError, match='Unknown local account'):
        linux.capture_linux_key('not-a-local-account', tmp_path / 'absent.json')

def test_verified_store_works_without_weflow(tmp_path, monkeypatch):
    root = tmp_path / 'xwechat_files'
    (root / 'local/db_storage').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(linux, 'get_account_keys_from_store', lambda account: {'db_key': 'c'*64})
    items = linux.available_accounts(tmp_path / 'absent.json')
    assert len(items) == 1 and items[0]['account'] == 'local'
    assert items[0]['key'] == 'c'*64

@pytest.mark.parametrize('xor_value,fmt', [('166', None), ('a6', 'hex')])
def test_sync_verifies_decimal_weflow_and_hex_store_image_keys(tmp_path, monkeypatch, xor_value, fmt):
    source = tmp_path / 'account/db_storage'; source.mkdir(parents=True)
    image_dir = source.parent / 'msg/attach/chat/2026-10/Img'; image_dir.mkdir(parents=True)
    image = b'\xff\xd8\xff' + b'image fixture' * 40 + b'\xff\xd9'
    aes = AES.new(b'0123456789abcdef', AES.MODE_ECB).encrypt(pad(image[:100], 16))
    dat = struct.pack('<6sLLx', b'\x07\x08V2\x08\x07', 100, 20) + aes + image[100:-20] + bytes(v ^ 166 for v in image[-20:])
    (image_dir / 'fixture_t.dat').write_bytes(dat)
    monkeypatch.setattr(linux, 'get_output_databases_dir', lambda: tmp_path / 'output')
    saved = []
    monkeypatch.setattr(linux, 'upsert_account_keys_in_store', lambda *a, **kw: saved.append(kw))
    result = linux.sync_account({'account':'account','source':source,'key':'c'*64,'xor':xor_value,'xor_format':fmt,'aes':'0123456789abcdef'})
    assert result['image_key_verified'] is True
    assert saved[-1]['image_xor_key'] == 'a6'
