"""Visible, note-scoped Microsoft Edge capture. Page text is untrusted data."""
from __future__ import annotations

import json
import subprocess

from .model import LibraryError, canonical_url, normalized_text, now
from .sources import stable_id


# Fixed code: no caller or page-supplied JavaScript is interpolated here.
VISIBLE_NOTE_JS = r"""(() => {
  const visible = e => {const s=getComputedStyle(e), r=e.getBoundingClientRect(); return e.getClientRects().length && r.width>0 && r.height>0 && r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth && s.display!=='none' && s.visibility!=='hidden' && s.opacity!=='0' && !e.closest('[hidden],[aria-hidden="true"]')};
  const root = [...document.querySelectorAll('#noteContainer, .note-container, [data-note-id]')].find(visible);
  if (!root) {
    const gates=[...document.querySelectorAll('h1, [role="alert"], .error-title, .login-title, .status-title, .risk-container, .risk-page, .deleted-note, .note-deleted, .login-container, .login-panel')].filter(visible).slice(0,8).map(e=>(e.innerText||'').slice(0,160)).join(' ');
    if (/已删除|已刪除|已下架|不存在|not found|removed|deleted/i.test(gates)) return JSON.stringify({status:'deleted'});
    if (/风险|風險|异常|異常|安全验证|安全驗證|captcha|risk|verify/i.test(gates)) return JSON.stringify({status:'risk'});
    if (/登录|登入|扫码|掃碼|log in|sign in/i.test(gates)) return JSON.stringify({status:'login'});
    return JSON.stringify({status:'missing'});
  }
  const pick = selectors => {for (const sel of selectors) {const e=root.querySelector(sel); if(e && visible(e)) return (e.innerText||'').trim()} return ''};
  const title=pick(['.note-title','[data-testid="note-title"]','h1','.title']).slice(0,300);
  const text=pick(['.note-content','.note-text','[data-testid="note-content"]','.desc']).slice(0,200000);
  const media=[...root.querySelectorAll('.note-slider img, .slider-container img, .swiper img, .media-container img, .note-content img')].filter(visible);
  const images=media.map(e=>(e.currentSrc||e.src).slice(0,2048)).filter(Boolean).slice(0,32);
  const unclassified_images=Math.min(32,[...root.querySelectorAll('img')].filter(visible).filter(e=>!media.includes(e)).length);
  return JSON.stringify({status:'ok',location:location.href.slice(0,4096),title,text,images,unclassified_images});
})()"""


def _apple_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '') + '"'


def edge_runner(url: str, script: str) -> dict:
    """Create and close only our tab; bounded process and no browser launch."""
    try:
        active = subprocess.run(["pgrep", "-x", "Microsoft Edge"], capture_output=True, timeout=2)
        if active.returncode: raise LibraryError("interactive Edge session is unavailable", "needs_login", 409)
        apple = ('tell application "Microsoft Edge"\n'
                 'if not running then error "Edge is unavailable"\n'
                 'if (count of windows) is 0 then error "interactive window is unavailable"\n'
                 'set sourceTab to make new tab at end of tabs of front window with properties {URL:' + _apple_quote(url) + '}\n'
                 'try\n'
                 'repeat 10 times\n'
                 'delay 0.7\n'
                 'set resultText to execute sourceTab javascript ' + _apple_quote(script) + '\n'
                 'if resultText is not missing value and resultText contains "status" then exit repeat\n'
                 'end repeat\n'
                 'on error errText\n'
                 'close sourceTab\n'
                 'error errText\n'
                 'end try\n'
                 'close sourceTab\n'
                 'return resultText\nend tell')
        result = subprocess.run(["osascript", "-e", apple], capture_output=True, text=True, timeout=18)
    except subprocess.TimeoutExpired:
        raise LibraryError("Edge capture timed out", "source_timeout", 504) from None
    except OSError:
        raise LibraryError("interactive Edge permission is unavailable", "needs_login", 409) from None
    if result.returncode:
        raise LibraryError("Edge permission or interactive session is unavailable", "needs_login", 409)
    try: return json.loads(result.stdout)
    except (ValueError, TypeError): raise LibraryError("visible note capture is invalid", "source_failed", 502) from None


class EdgeNoteAdapter:
    def __init__(self, runner=edge_runner): self.runner = runner

    def capture(self, url: str) -> dict:
        expected = stable_id("xhs", url)
        data = self.runner(canonical_url(url), VISIBLE_NOTE_JS)
        if not isinstance(data, dict): raise LibraryError("visible note capture is invalid", "source_failed", 502)
        if data.get("status") in {"login", "risk", "deleted"}:
            status = {"login": "needs_login", "risk": "blocked", "deleted": "failed"}[data["status"]]
            return {"terminal": status, "reason": "visible note reports " + data["status"]}
        if data.get("status") != "ok": return {"terminal": "needs_login", "reason": "visible note is unavailable"}
        location = data.get("location")
        if not isinstance(location, str) or stable_id("xhs", location) != expected:
            raise LibraryError("visible note identity changed", "identity_pending", 422)
        title, body = data.get("title"), data.get("text")
        images = data.get("images")
        unclassified = data.get("unclassified_images", 0)
        if not isinstance(title, str) or not isinstance(body, str) or not isinstance(images, list) or len(images) > 32:
            raise LibraryError("visible note capture is invalid", "source_failed", 502)
        if type(unclassified) is not int or not 0 <= unclassified <= 32:
            raise LibraryError("visible note image count is invalid", "source_failed", 502)
        if any(not isinstance(v, str) or len(v) > 2048 for v in images):
            raise LibraryError("visible image reference is invalid", "source_failed", 502)
        if not normalized_text(body) and not images and not unclassified:
            return {"terminal": "needs_login", "reason": "visible note content is unavailable"}
        return {"source_id": expected, "canonical_url": canonical_url(location), "title": title[:300] or "XHS note",
                "body": body[:200000], "images": list(dict.fromkeys(images)), "unclassified_images": unclassified,
                "platform": "xhs", "fetched_at": now(),
                "provenance": {"source": "edge-visible-dom"}}
