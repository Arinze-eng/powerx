# telegram_file_handler.py
"""Handler to download a file sent to the PowerX Telegram bot, upload it
to the Novita sandbox, and optionally forward it to a user‑provided VPS.

The implementation is deliberately lightweight – it does not depend on any
frameworks that are already part of PowerX. You can import and call the
`process_telegram_file(file_id: str, vps=None)` function from your bot entry
point.

```python
from bot.telegram_file_handler import process_telegram_file

# Example usage inside your Telegram‑Bot update handler:
#   file_id = update.message.document.file_id
#   result = await process_telegram_file(file_id, vps={"host": "1.2.3.4", "user": "root"})
```

The function performs:
1️⃣ Calls the Telegram Bot API `getFile` to obtain a download URL.
2️⃣ Streams the file into a temporary location under `/tmp`.
3️⃣ Uses the nanobot `novita_sandbox` tool (available as `novita_sandbox`
   action=upload) to place the file inside the sandbox.
4️⃣ If a `vps` dict is supplied, the file is copied via `scp` to the
   remote host (the sandbox must have SSH access to the VPS – you can set
   up SSH keys in the sandbox beforehand).
5️⃣ Returns a dictionary with the sandbox path and, if applicable, the
   remote VPS path.
"""
import os
import json
import subprocess
import requests
from pathlib import Path

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def _get_telegram_file_path(file_id: str) -> str:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
    resp = requests.get(url, params={"file_id": file_id})
    resp.raise_for_status()
    data = resp.json()
    return data["result"]["file_path"]

def _download_telegram_file(file_path: str) -> Path:
    dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    r = requests.get(dl_url, stream=True)
    r.raise_for_status()
    local_path = Path("/tmp") / Path(file_path).name
    with open(local_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    return local_path

def _upload_to_sandbox(local_path: Path) -> str:
    result = subprocess.run(
        ["novita_sandbox", "action=upload", f"source={local_path}"],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout)
    return info["path"]

def _copy_to_vps(local_path: Path, vps: dict) -> str:
    user = vps.get("user", "root")
    host = vps["host"]
    remote_path = f"{user}@{host}:{vps.get('dest', '/tmp/')}{local_path.name}"
    subprocess.run(["scp", str(local_path), remote_path], check=True)
    return remote_path

def process_telegram_file(file_id: str, vps: dict | None = None) -> dict:
    tf_path = _get_telegram_file_path(file_id)
    local_file = _download_telegram_file(tf_path)
    sandbox_path = _upload_to_sandbox(local_file)
    result = {"sandbox_path": sandbox_path}
    if vps:
        result["vps_path"] = _copy_to_vps(local_file, vps)
    return result

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: telegram_file_handler.py <file_id>")
        sys.exit(1)
    print(process_telegram_file(sys.argv[1]))
