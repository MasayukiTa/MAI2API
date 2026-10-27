"""Script-side notifications for MAI2API.

The keeper/guardian -- not a human, not an AI -- report their own
state changes:
  * Windows toast on this PC (best effort, always attempted),
  * ntfy.sh push to a private random topic, if one is configured in
    notify_config.json ({"ntfy_topic": "..."}). Subscribe to the
    topic in the ntfy app (or any client) to get phone alerts.
    Messages carry status text only, never tokens or secrets.

notify() never raises: a failed notification must not kill the
watcher it serves.
"""
import json
import os
import subprocess
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
def _state_dir():
    env = os.environ.get("MAI2API_STATE_DIR")
    if env:
        return env
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        return os.path.join(lad, "MAI2API")
    return HERE


CONFIG = os.path.join(_state_dir(), "notify_config.json")

TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template=""ToastGeneric""><text>$($args[0])</text><text>$($args[1])</text></binding></visual></toast>")
$appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _toast(title, body):
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             TOAST_PS, title, body],
            timeout=20, capture_output=True,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        return True
    except Exception:
        return False


def _ntfy(title, body, priority):
    try:
        cfg = json.load(open(CONFIG, encoding="utf-8-sig"))
        topic = cfg.get("ntfy_topic")
        if not topic:
            return False
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", method="POST",
            data=(f"{title}: {body}").encode("utf-8"),
            headers={"Title": "MAI2API", "Priority": priority,
                     "Tags": "satellite"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def notify(event, detail="", priority="default"):
    title = f"MAI2API {event}"
    ok_t = _toast(title, detail)
    ok_n = _ntfy(title, detail, priority)
    print(f"notify [{event}] toast={'ok' if ok_t else 'skip/fail'} "
          f"ntfy={'ok' if ok_n else 'off/fail'} :: {detail}",
          flush=True)
    return ok_t or ok_n


if __name__ == "__main__":
    notify("テスト通知", "notify.py からのテストです")
