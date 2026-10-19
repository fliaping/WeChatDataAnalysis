# Linux / WeHarbor

This fork adds a Linux source adapter; the Windows/macOS native capture binaries
are not used on Linux. The existing Python SQLCipher reader and media decoder
handle database pages, committed WAL transactions, XOR images and V2 AES/XOR
images. Keys can be reused from the owner's local WeFlow configuration. A
read-only `/proc/<pid>/mem` provider also enables the existing verified image
key scanner on Linux (same user and kernel ptrace permissions are required).

Run the API on loopback, behind an authenticated proxy. Set `HOME`,
`WECHAT_TOOL_DATA_DIR`, `WECHAT_TOOL_OUTPUT_DIR`, and optionally
`WECHAT_TOOL_WEFLOW_CONFIG`. Install `requirements-linux.txt`, then start
`PYTHONPATH=src uvicorn wechat_decrypt_tool.api:app --host 127.0.0.1 --port 10392`.
`GET /api/linux/status` returns availability without keys.
`POST /api/linux/sync` requires `X-WeHarbor-Action: sync`. It imports all configured
accounts, authenticates databases and checks SQLite integrity before atomic
publication. Changed DB/WAL pairs are recopied until stable. Previously verified
outputs remain available if a source is busy or a key fails.

Build the embedded UI using `NUXT_APP_BASE_URL=/analysis/` and
`NUXT_PUBLIC_API_BASE=/analysis/api npm run generate` in `frontend/`.
Set `WECHAT_TOOL_UI_DIR` to the generated `.output/public` directory.
Never publish key stores, decrypted databases, or exported chat history to Git.

If no working key is available, the Linux source card exposes an explicit
`POST /api/linux/capture` action (same owner header and origin checks). It uses
the MIT wcdb-key-tool implementation pinned at 79f1b5b, included with its license
under `vendor/wcdb_key_tool`. Install GDB and grant the container ptrace access.
The user must manually log out and log in within 120 seconds; the integration
never performs logout. A captured key is stored only after database
authentication. Its upstream OpenSSL decrypt path and global GDB cleanup are
not used; decryption stays in the tested Python reader.

WeHarbor runs the API as the desktop user. Its optional
`WECHAT_TOOL_CAPTURE_HELPER` names a root-owned, fixed sudo entry installed by
the image; JSON on stdin selects database or image capture. This keeps ptrace
privileges out of the web process. The helper must accept no command arguments,
ignore caller Python paths, and validate local account/process ownership. Do
not grant sudo access to the API interpreter or to arbitrary scripts under the
user-writable profile.
