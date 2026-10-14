"""
基于 QtWebEngine 的控制台页面抓取器。

为什么需要它
------------
千问 Token Plan 既没有本机 CLI，也没有对外的用量 OpenAPI，
唯一稳定的数据入口就是控制台页面自己。项目已经依赖 PySide6，
而 PySide6_WebEngine 是随 PySide6 一起装上的，所以这里直接复用
Qt 自带的 Chromium —— 不必引入 Playwright / Selenium，
也不必让用户额外下载浏览器内核。

工作方式
--------
1. 一个**持久化** QWebEngineProfile（目录 config_dir()/web-profile）。
   用户在登录窗口里登录一次，Cookie / LocalStorage 就长期留在磁盘上，
   之后每次抓取都是带登录态的。
2. 在 DocumentCreation 时机往**主世界**注入一段脚本，monkey-patch
   window.fetch 与 XMLHttpRequest，把页面自己发出的接口响应录进
   window.__amCap。这样不用猜后端 API 路径，也不用写脆弱的 DOM 选择器：
   控制台改版时，只要它还是靠接口渲染数字，我们就能拿到。
3. 一个**隐藏**的 QWebEngineView 加载目标页面，等 SPA 把接口打完，
   再 runJavaScript 把录制结果读回 Python，交给平台适配器解析。

线程模型
--------
QtWebEngine 只能在 GUI 线程使用，而 APScheduler 的 _tick 跑在工作线程。
所以 fetch_page() 会把任务通过 qtbridge.post 编组到主线程，
工作线程用 threading.Event 阻塞等待；若调用方本来就在主线程
（例如设置窗口的「测试连通」按钮），则改用嵌套 QEventLoop，
避免自己等自己造成死锁。

失败策略
--------
QtWebEngine 缺失、页面加载失败、超时、未登录……全部以
PageResult(ok=False, error=...) 返回，绝不抛异常到调用方，
让适配器可以降级成 UNKNOWN / EXPIRED 快照而不是让轮询线程崩掉。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------- 注入脚本

# 录接口响应。装在 MainWorld，这样页面自身的 fetch/XHR 才会走我们的补丁。
_CAPTURE_JS = r"""
(function () {
  if (window.__amCapInstalled) { return; }
  window.__amCapInstalled = true;
  window.__amCap = [];
  var LIMIT = 120;          // 最多保留多少条
  var MAXLEN = 400000;      // 单条响应体上限，防止把二进制/超大列表塞回来

  function push(url, body) {
    try {
      if (body === null || body === undefined) { return; }
      if (typeof body !== 'string') { body = JSON.stringify(body); }
      if (!body) { return; }
      if (body.charAt(0) !== '{' && body.charAt(0) !== '[') { return; }  // 只要 JSON
      if (body.length > MAXLEN) { body = body.slice(0, MAXLEN); }
      window.__amCap.push({ u: String(url || '').slice(0, 1000), b: body });
      while (window.__amCap.length > LIMIT) { window.__amCap.shift(); }
    } catch (e) { /* 录制失败不能影响页面本身 */ }
  }

  // --- fetch ---
  var of = window.fetch;
  if (typeof of === 'function') {
    window.fetch = function (input, init) {
      var url = (typeof input === 'string') ? input : ((input && input.url) || '');
      var p = of.apply(this, arguments);
      try {
        return p.then(function (resp) {
          try {
            var c = resp.clone();
            c.text().then(function (t) { push(url, t); }, function () {});
          } catch (e) {}
          return resp;
        });
      } catch (e) { return p; }
    };
  }

  // --- XMLHttpRequest ---
  try {
    var oo = XMLHttpRequest.prototype.open;
    var os = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.__amUrl = u;
      return oo.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      var self = this;
      try {
        this.addEventListener('load', function () {
          try { push(self.__amUrl, self.responseText); } catch (e) {}
        });
      } catch (e) {}
      return os.apply(this, arguments);
    };
  } catch (e) {}
})();
"""

# 探测：当前录到几条。用于判断 SPA 是否已经把接口打完（数量连续两轮不变即认为稳定）。
_PROBE_JS = "(window.__amCap ? window.__amCap.length : -1)"

# 回读。只回传与额度相关、体积可控的那部分，避免 runJavaScript 搬运几 MB 字符串。
_READ_JS = r"""
(function () {
  var caps = (window.__amCap || []);
  var re = /credit|plan|quota|usage|token|subscri|balance|limit|remain|package|order/i;
  var keep = [];
  for (var i = 0; i < caps.length; i++) {
    var c = caps[i];
    if (!re.test(c.u) && !re.test(c.b.slice(0, 20000))) { continue; }
    keep.push({ u: c.u, b: c.b.length > 200000 ? c.b.slice(0, 200000) : c.b });
    if (keep.length >= 40) { break; }
  }
  return JSON.stringify({
    url: String(location.href || ''),
    title: String(document.title || ''),
    caps: keep,
    text: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 6000)
  });
})()
"""


@dataclass
class PageResult:
    """一次页面抓取的结果。ok=False 时看 error。"""
    ok: bool
    url: str = ""
    title: str = ""
    captures: list = field(default_factory=list)   # [{"u": 接口地址, "b": 响应体字符串}]
    text: str = ""                                 # body.innerText 摘要（DOM 兜底用）
    error: str = ""

    def json_captures(self) -> list:
        """把 captures 里能解析成 JSON 的返回，附带原始 URL。"""
        out = []
        for c in self.captures:
            body = c.get("b") if isinstance(c, dict) else None
            if not body:
                continue
            try:
                out.append((c.get("u", ""), json.loads(body)))
            except Exception:
                continue
        return out


# ---------------------------------------------------------------- 登录态判定

_LOGIN_HINTS = ("login", "signin", "sign-in", "passport", "sso", "auth",
                "account/verify", "oauth")


def looks_logged_out(url: str, title: str, text: str) -> bool:
    """粗略判断当前是否被踢到了登录页。"""
    u = (url or "").lower()
    t = (title or "").lower()
    if any(h in u for h in _LOGIN_HINTS):
        return True
    if any(h in t for h in ("登录", "登陆", "sign in", "log in")):
        return True
    body = (text or "")[:1200]
    if ("登录" in body or "请先登录" in body) and "退出" not in body:
        return True
    return False


# ---------------------------------------------------------------- 引擎

def _enum(cls, name):
    """兼容 PySide6 的扁平/嵌套两种枚举写法：
    QWebEngineScript.DocumentCreation 或 QWebEngineScript.InjectionPoint.DocumentCreation。"""
    for holder_name in ("InjectionPoint", "ScriptWorldId"):
        holder = getattr(cls, holder_name, None)
        if holder is not None:
            v = getattr(holder, name, None)
            if v is not None:
                return v
    return getattr(cls, name)


class _Engine:
    """主线程单例：持有持久化 profile 与隐藏 view。

    所有方法都必须在 GUI 线程调用。
    """

    _inst: "Optional[_Engine]" = None
    _err: str = ""

    @classmethod
    def instance(cls) -> "_Engine":
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
        from PySide6.QtWebEngineWidgets import QWebEngineView

        from .config import config_dir

        d = Path(config_dir()) / "web-profile"
        d.mkdir(parents=True, exist_ok=True)

        # 命名 profile 才会持久化；off-the-record profile 关窗即失忆。
        self.profile = QWebEngineProfile("AgentMonitor")
        self.profile.setPersistentStoragePath(str(d))
        self.profile.setCachePath(str(d / "cache"))

        script = QWebEngineScript()
        script.setName("am-capture")
        script.setSourceCode(_CAPTURE_JS)
        script.setInjectionPoint(_enum(QWebEngineScript, "DocumentCreation"))
        script.setWorldId(_enum(QWebEngineScript, "MainWorld"))
        script.setRunsOnSubFrames(False)
        self.profile.scripts().insert(script)

        # 永不 show()：离屏渲染照样跑 JS、照样发请求。
        self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        self.view.resize(1366, 900)
        self._busy = False

    @property
    def page(self):
        return self.view.page()

    def capture(self, url: str, settle_ms: int, on_done: Callable[[PageResult], None]):
        """加载 url，等接口打完，把结果交给 on_done（主线程回调）。"""
        from PySide6.QtCore import QUrl, QTimer

        if self._busy:
            on_done(PageResult(ok=False, error="上一次抓取尚未结束"))
            return
        self._busy = True

        state = {
            "done": False,
            "last": -1,
            "stable": 0,
            "deadline": time.monotonic() + max(3.0, settle_ms / 1000.0),
        }
        try:
            self.page.loadFinished.disconnect()
        except Exception:
            pass

        def finish(res: PageResult):
            if state["done"]:
                return
            state["done"] = True
            self._busy = False
            try:
                self.page.loadFinished.disconnect()
            except Exception:
                pass
            on_done(res)

        def read():
            self.page.runJavaScript(_READ_JS, got)

        def got(raw):
            try:
                data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                data = {}
            url_now = str(data.get("url") or self.page.url().toString() or "")
            title = str(data.get("title") or "")
            text = str(data.get("text") or "")
            caps = data.get("caps") or []
            if not caps:
                finish(PageResult(ok=False, url=url_now, title=title, text=text,
                                  error="页面已加载，但没有捕获到任何接口响应"))
                return
            finish(PageResult(ok=True, url=url_now, title=title,
                              captures=caps, text=text))

        def poll():
            if state["done"]:
                return
            if time.monotonic() >= state["deadline"]:
                read()
                return
            self.page.runJavaScript(_PROBE_JS, probed)

        def probed(n):
            if state["done"]:
                return
            try:
                n = int(n)
            except Exception:
                n = -1
            if n > 0 and n == state["last"]:
                state["stable"] += 1
                if state["stable"] >= 2:      # 连续两轮无新增 → 认为打完了
                    read()
                    return
            else:
                state["stable"] = 0
            state["last"] = n
            QTimer.singleShot(400, poll)

        def loaded(_ok: bool):
            # 加载失败也别急着放弃：SPA 常在重定向链里先报一次 false。
            QTimer.singleShot(400, poll)

        self.page.loadFinished.connect(loaded)
        target = QUrl(url)
        cur = self.page.url().toString()
        if cur and cur != "about:blank" and self.page.url() == target:
            self.view.reload()                # 同地址 setUrl 未必触发重新加载
        else:
            self.view.load(target)


def open_login_window(url: str, title: str = "登录") -> tuple[bool, str]:
    """弹出可见的登录窗口，复用抓取用的持久化 profile。

    用户在里面登录一次，Cookie 就落盘了，之后 fetch_page 自动带登录态。
    非模态，立刻返回 (是否成功, 失败原因)。
    """
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QDialog, QVBoxLayout

    try:
        eng = _Engine.instance()
    except Exception as e:
        return False, f"浏览器初始化失败：{e}"

    dlg = QDialog()
    dlg.setWindowTitle(title)
    dlg.resize(1000, 760)
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(0, 0, 0, 0)
    view = QWebEngineView()
    view.setPage(QWebEnginePage(eng.profile, view))
    lay.addWidget(view)
    view.load(QUrl(url))

    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    # 必须持强引用：Qt 对象一旦失去 Python 引用就会被 GC，窗口凭空消失
    _KEEPALIVE.append((dlg, view))
    dlg.destroyed.connect(lambda *_: _drop(dlg))
    dlg.show()
    return True, ""


_KEEPALIVE: list = []


def _drop(dlg):
    for i, (d, _v) in enumerate(_KEEPALIVE):
        if d is dlg:
            del _KEEPALIVE[i]
            return


# ---------------------------------------------------------------- 对外入口

def is_available() -> tuple[bool, str]:
    """QtWebEngine 是否可用（打包时漏带内核会在这里暴露）。"""
    try:
        from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
        from PySide6.QtWebEngineWidgets import QWebEngineView   # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"QtWebEngine 不可用：{e}"


_BUSY = threading.Lock()


def fetch_page(url: str, settle_ms: int = 8000, timeout: float = 40.0) -> PageResult:
    """任意线程可调。返回 PageResult，永不抛异常。

    settle_ms：页面加载完成后最多再等多久让 SPA 把接口打完。
    timeout  ：整体墙钟上限。
    """
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is None:
        return PageResult(ok=False, error="Qt 应用未初始化，无法使用浏览器抓取")

    ok, err = is_available()
    if not ok:
        return PageResult(ok=False, error=err)

    if app.thread().isCurrentThread():
        # 主线程：不能 self-wait，用嵌套事件循环推进
        return _run_on_main(url, settle_ms, timeout)

    box: dict = {}
    ev = threading.Event()

    def job():
        try:
            box["r"] = _run_on_main(url, settle_ms, timeout)
        except Exception as e:
            box["r"] = PageResult(ok=False, error=f"浏览器抓取异常：{e}")
        finally:
            ev.set()

    try:
        from . import qtbridge
        qtbridge.post(job)
    except Exception as e:
        return PageResult(ok=False, error=f"无法调度到主线程：{e}")

    if not ev.wait(timeout + 10):
        return PageResult(ok=False, error=f"浏览器抓取超时（>{int(timeout)}s）")
    return box.get("r") or PageResult(ok=False, error="浏览器抓取无结果")


def _run_on_main(url: str, settle_ms: int, timeout: float) -> PageResult:
    from PySide6.QtCore import QEventLoop, QTimer

    if not _BUSY.acquire(blocking=False):
        return PageResult(ok=False, error="浏览器正忙，本次跳过")
    try:
        eng = _Engine.instance()
    except Exception as e:
        _BUSY.release()
        return PageResult(ok=False, error=f"浏览器初始化失败：{e}")

    box: dict = {}
    loop = QEventLoop()

    def done(res: PageResult):
        box["r"] = res
        loop.quit()

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(int(timeout * 1000))

    try:
        eng.capture(url, settle_ms, done)
        loop.exec()
    finally:
        guard.stop()
        guard.deleteLater()
        _BUSY.release()

    return box.get("r") or PageResult(ok=False, error=f"浏览器抓取超时（>{int(timeout)}s）")
