import json
import os
import sys
from urllib.request import Request, urlopen

public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
cron_secret = os.getenv("CRON_SECRET", "").strip()

if not public_url or not cron_secret:
    print("PUBLIC_URL and CRON_SECRET are required", file=sys.stderr)
    raise SystemExit(2)

req = Request(
    f"{public_url}/tasks/payment-reminders",
    data=b"{}",
    headers={
        "Content-Type": "application/json",
        "X-Cron-Secret": cron_secret,
    },
    method="POST",
)

with urlopen(req, timeout=45) as response:
    body = response.read().decode("utf-8")
    print(body)
    if response.status >= 400:
        raise SystemExit(1)
    try:
        json.loads(body)
    except Exception:
        pass
