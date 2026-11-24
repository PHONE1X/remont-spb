#!/usr/bin/env python3
"""Сторож заявок с лендинга: читает письма FormSubmit через Gmail API и шлёт их в Telegram.

Зачем: FormSubmit пишет на wowprofi4@gmail.com. Gmail может положить письмо в «Спам»,
а юзеру нужно узнать о заявке сразу. Сторож забирает имя/телефон/проблему и присылает
уведомление в Telegram.

Запуск: python3 leads-watch.py           — один прогон
        python3 leads-watch.py --loop    — вечный цикл (раз в 2 минуты)
Состояние: leads-watch.json (обработанные message id).
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "leads-watch.json"
CREDS = Path(os.path.expanduser("~/.config/opencode/mcp/gmail/credentials.json"))
GCP = Path(os.path.expanduser("~/.config/opencode/mcp/gmail/gcp-oauth.keys.json"))
NOTIFY = os.path.expanduser("~/.config/opencode/tg-bot/notify.mjs")
LOG = Path(os.path.expanduser("~/WORKSPACE/logs/leads-watch.log"))
JOURNAL = Path(os.path.expanduser("~/WORKSPACE/logs/leads.jsonl"))

QUERY = 'from:submissions@formsubmit.co newer_than:7d'
SUBJECT_HINTS = ("заявка с лендинга", "new submission", "заявка с сайта")


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%F %T')} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def token() -> str:
    creds = json.loads(CREDS.read_text())
    if creds.get("access_token") and creds.get("expiry_date", 0) > (time.time() + 120) * 1000:
        return creds["access_token"]
    keys = json.loads(GCP.read_text())["installed"]
    body = urllib.parse.urlencode({
        "client_id": keys["client_id"],
        "client_secret": keys["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    creds["access_token"] = data["access_token"]
    creds["expiry_date"] = int((time.time() + data.get("expires_in", 3600)) * 1000)
    CREDS.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    return creds["access_token"]


def api(path: str, params: dict | None = None) -> dict:
    url = "https://gmail.googleapis.com/gmail/v1/users/me/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def decode_part(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")


def collect_body(payload: dict) -> str:
    out = []
    if payload.get("body", {}).get("data"):
        out.append(decode_part(payload["body"]["data"]))
    for part in payload.get("parts", []) or []:
        mime = part.get("mimeType", "")
        if mime in ("text/html", "text/plain") and part.get("body", {}).get("data"):
            out.append(decode_part(part["body"]["data"]))
        elif part.get("parts"):
            out.append(collect_body(part))
    return "\n".join(out)


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</tr>|</p>|</div>|</h\d>", "\n", html)
    html = re.sub(r"(?i)<th[^>]*>", "\n", html)
    html = re.sub(r"(?i)<td[^>]*>", "|", html)
    html = re.sub(r"<[^>]+>", "", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">")):
        html = html.replace(a, b)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n", html)
    return html.strip()


KEYMAP = {
    "имя": "Имя", "name": "Имя",
    "телефон": "Телефон", "phone": "Телефон", "тел": "Телефон",
    "проблема": "Проблема", "message": "Проблема", "сообщение": "Проблема",
    "email": "Email", "почта": "Email", "e-mail": "Email",
}


def parse_lead(html: str) -> dict:
    """FormSubmit присылает поля как <strong>Имя: </strong><br><pre>значение</pre>."""
    out = {}

    # основной формат: strong-заголовок + pre со значением
    for field, value in re.findall(r"(?is)<strong[^>]*>(.*?)</strong>\s*(?:<br\s*/?>)?\s*<pre[^>]*>(.*?)</pre>", html):
        key = html_to_text(field).lower().strip().strip(":|").strip()
        val = html_to_text(value).strip()
        name = KEYMAP.get(key)
        if name and val and name not in out:
            out[name] = val

    # старый формат с таблицей <th>/<td>
    if not out:
        for field, value in re.findall(r"(?is)<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", html):
            key = html_to_text(field).lower().strip().strip(":|")
            val = html_to_text(value).strip()
            name = KEYMAP.get(key)
            if name and val and name not in out:
                out[name] = val

    text = html_to_text(html)
    flat = " ".join(text.split())
    if not out.get("Телефон"):
        m = re.search(r"(\+7[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})", flat)
        if m:
            out["Телефон"] = m.group(1)
    return out


def notify(text: str) -> None:
    try:
        subprocess.run(["node", NOTIFY, text], timeout=45, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        log(f"notify failed: {str(e)[:120]}")


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": []}


def save_state(st: dict) -> None:
    st["seen"] = st["seen"][-500:]
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def run_once() -> int:
    st = load_state()
    seen = set(st["seen"])
    found = 0
    listing = api("messages", {"q": QUERY, "maxResults": 25})
    for item in listing.get("messages", []) or []:
        mid = item["id"]
        if mid in seen:
            continue
        msg = api(f"messages/{mid}", {"format": "full"})
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        subj = headers.get("subject", "").lower()
        if not any(h in subj for h in SUBJECT_HINTS):
            continue
        lead = parse_lead(collect_body(payload))
        name = lead.get("Имя", "—")
        phone = lead.get("Телефон", "—")
        problem = lead.get("Проблема", "")
        email = lead.get("Email", "")
        text = (f"🔔 Заявка с сайта\n"
                f"Имя: {name}\n"
                f"Телефон: {phone}\n"
                + (f"Почта: {email}\n" if email else "")
                + (f"\n{problem[:400]}" if problem else ""))
        log(f"заявка: {name} {phone}")
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "name": name,
                                "phone": phone, "problem": problem, "email": email,
                                "msg_id": mid}, ensure_ascii=False) + "\n")
        notify(text)
        st["seen"].append(mid)
        found += 1
    save_state(st)
    log(f"прогон завершён, новых заявок: {found}")
    return found


def main() -> None:
    if "--loop" not in sys.argv:
        run_once()
        return
    log("leads-watch запущен (цикл 120 с)")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            log(f"ошибка прогона: {str(e)[:200]}")
        time.sleep(120)


if __name__ == "__main__":
    main()
