"""网页版聊天机器人驱动（基于 CDP，无 Playwright 依赖）。

用受管的 Edge 实例操作国产 AI 的网页版对话框：没有 API Key 也能用真实模型。

与上一版的区别
--------------
上一版依赖 Playwright，体积大且难以打进 exe。现在改用 **CDP（Chrome DevTools Protocol）**：
用 ``--remote-debugging-port`` 拉起独立 profile 的 Edge，通过 WebSocket 执行 JS。
依赖只有 ``websockets``（约 200 KB），打包后依然能跑，双击即用。

安全边界
--------
- 只操作**自己拉起来**的那个 Edge 实例（独立 user-data-dir），不碰用户日常浏览器配置；
- 不读取 cookie、不导出会话、不注入跨站脚本；
- 登录由用户自己在浏览器窗口里完成，程序只负责把消息打进对话框。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .cdp import CDPError, CDPSession, EdgeProcess, find_edge

logger = logging.getLogger("edgeagent.web")

#: 轮询间隔与"连续无变化即视为结束"的次数
POLL_INTERVAL = 0.8
STABLE_ROUNDS = 3
DEFAULT_TIMEOUT = 120.0


class WebAgentError(RuntimeError):
    """网页版驱动失败。``code`` 供前端区分处理。"""

    def __init__(self, message: str, *, code: str = "web_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class SiteAdapter:
    """一个网页版站点的操作配方。

    选择器写成候选列表：站点改版通常只换 class，按序探测比死写一个活得久。
    """

    id: str
    label: str
    url: str
    input_selectors: tuple[str, ...]
    reply_selectors: tuple[str, ...]
    send_selectors: tuple[str, ...] = ()
    login_hint: str = ""
    ready_selectors: tuple[str, ...] = ()
    enter_to_send: bool = True
    #: 代码块是否带行号（通义会把行号也塞进 pre.textContent，导致 JSON 前面多个 "1"）
    strip_line_numbers: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "login_hint": self.login_hint,
        }


SITE_ADAPTERS: dict[str, SiteAdapter] = {
    "deepseek": SiteAdapter(
        id="deepseek",
        label="DeepSeek",
        url="https://chat.deepseek.com/",
        input_selectors=("#chat-input", "textarea#chat-input", "textarea", "div[contenteditable='true']"),
        reply_selectors=(".ds-markdown", "div[class*='ds-markdown']", "div[class*='markdown']"),
        send_selectors=(
            "div[role='button'][aria-disabled='false']",
            "div[class*='send']",
            "button[type='button']",
        ),
        ready_selectors=("#chat-input", "textarea", "div[contenteditable='true']"),
        login_hint="打开 DeepSeek 后在窗口里登录，登录成功后回到面板点「已完成登录」。",
    ),
    "tongyi": SiteAdapter(
        id="tongyi",
        label="通义千问",
        # 注意：根域名 www.qianwen.com 是产品首页（营销页），/chat 才是对话界面
        url="https://www.qianwen.com/chat",
        input_selectors=("textarea", "div[contenteditable='true']"),
        reply_selectors=(".tongyi-markdown", "div[class*='markdown']", "div[class*='answer']"),
        send_selectors=("div[class*='send']", "button[type='button']"),
        ready_selectors=("textarea", "div[contenteditable='true']"),
        login_hint="通义需要阿里云账号登录，登录后再点「已完成登录」。",
        strip_line_numbers=True,
    ),
    "doubao": SiteAdapter(
        id="doubao",
        label="豆包",
        url="https://www.doubao.com/chat/",
        input_selectors=("textarea", "div[contenteditable='true']"),
        reply_selectors=("div[class*='message-content']", "div[class*='markdown']"),
        send_selectors=("button[class*='send']", "div[class*='send-btn']", "button[type='button']"),
        ready_selectors=("textarea", "div[contenteditable='true']"),
        login_hint="豆包需要手机号或抖音账号登录。",
    ),
    "zhipu": SiteAdapter(
        id="zhipu",
        label="智谱清言",
        url="https://chatglm.cn/main/alltoolsdetail",
        input_selectors=("textarea", "div[contenteditable='true']", "[role='textbox']"),
        reply_selectors=("div[class*='markdown']", "div[class*='message']", "div[class*='answer']"),
        send_selectors=("button[class*='send']", "[aria-label*='发送']", "button[type='button']"),
        ready_selectors=("textarea", "div[contenteditable='true']"),
        login_hint="智谱清言需要手机号或微信登录。",
    ),
    "qianfan": SiteAdapter(
        id="qianfan",
        label="文心一言",
        # yiyan.baidu.com 会 302 到 wenxin.baidu.com，直接用新域名省一次跳转
        url="https://wenxin.baidu.com/",
        input_selectors=("textarea", "div[contenteditable='true']", "[role='textbox']"),
        reply_selectors=("div[class*='markdown']", "div[class*='answer']", "div[class*='message']"),
        send_selectors=("button[class*='send']", "[aria-label*='发送']", "button[type='button']"),
        ready_selectors=("textarea", "div[contenteditable='true']"),
        login_hint="文心一言需要百度账号登录。",
    ),
    "hunyuan": SiteAdapter(
        id="hunyuan",
        label="腾讯元宝",
        # 元宝用 Quill 富文本编辑器，输入框是 div.ql-editor（已实测）
        url="https://yuanbao.tencent.com/chat/",
        input_selectors=("div.ql-editor", "div[contenteditable='true']", "textarea"),
        reply_selectors=("div[class*='markdown']", "div[class*='message']", "div[class*='answer']"),
        send_selectors=("button[class*='send']", "[aria-label*='发送']", "button[type='button']"),
        ready_selectors=("div.ql-editor", "div[contenteditable='true']"),
        login_hint="腾讯元宝支持微信或 QQ 登录。",
    ),
    "kimi": SiteAdapter(
        id="kimi",
        label="Kimi",
        url="https://www.kimi.com/",
        input_selectors=("div[contenteditable='true']", ".chat-input-editor", "textarea"),
        reply_selectors=(".markdown", "div[class*='segment-content']", "div[class*='markdown']"),
        send_selectors=("div[class*='send-button']", "button[type='button']"),
        ready_selectors=("div[contenteditable='true']", "textarea"),
        login_hint="Kimi 支持手机号登录。",
    ),
}

SITE_ALIASES: dict[str, str] = {
    "moonshot": "kimi",
    "chatglm": "zhipu",
    "glm": "zhipu",
    "wenxin": "qianfan",
    "ernie": "qianfan",
    "yiyan": "qianfan",
    "yuanbao": "hunyuan",
    "tencent": "hunyuan",
    "qwen": "tongyi",
    "aliyun": "tongyi",
    "ark": "doubao",
    "bytedance": "doubao",
    "deep-seek": "deepseek",
}


def get_adapter(site_id: str) -> SiteAdapter:
    """按 id 取适配器，支持别名；未知站点抛错。"""
    key = (site_id or "").strip().lower()
    key = SITE_ALIASES.get(key, key)
    adapter = SITE_ADAPTERS.get(key)
    if adapter is None:
        raise WebAgentError(
            f"暂不支持站点 {site_id!r}，当前支持：{', '.join(SITE_ADAPTERS)}",
            code="site_unsupported",
        )
    return adapter


def list_sites() -> list[dict[str, Any]]:
    return [adapter.to_public() for adapter in SITE_ADAPTERS.values()]


def driver_available() -> bool:
    """网页版是否可用：需要 websockets 与 Edge 同时具备。"""
    try:
        import websockets  # noqa: F401
    except Exception:
        return False
    return find_edge() is not None


def driver_status() -> dict[str, Any]:
    """给前端/排障用的能力说明。"""
    try:
        import websockets

        ws_ok = True
        ws_version = getattr(websockets, "__version__", "")
    except Exception:
        ws_ok = False
        ws_version = ""
    edge = find_edge()
    return {
        "available": bool(ws_ok and edge),
        "websockets": ws_ok,
        "websockets_version": ws_version,
        "edge_path": edge or "",
    }


def _diff_text(previous: str, current: str) -> str:
    """按最长公共前缀取增量：网页回复整段重绘，只能这样推进。"""
    if current.startswith(previous):
        return current[len(previous) :]
    if previous and previous in current:
        return current[current.index(previous) + len(previous) :]
    return current


def _strip_line_numbers(text: str) -> str:
    """去掉代码块里的行号前缀。

    通义千问的代码块把行号也渲染进 ``pre.textContent``，抓下来是
    ``1{"ok": true}``，JSON 解析直接报 "Extra data"。这里按行剥掉
    ``12 `` / ``12{`` 这类前缀，只保留真正的代码内容。
    """
    out: list[str] = []
    for line in text.splitlines():
        if line.strip() in {"```", "```json", "```JSON"}:
            out.append(line)
            continue
        match = re.match(r"^\s*(\d{1,3})(\s+)(\S.*)$", line)
        if match:
            out.append(match.group(3))
            continue
        match = re.match(r"^\s*(\d{1,3})([\{\[\]\}\"].*)$", line)
        if match:
            out.append(match.group(2))
            continue
        out.append(line)
    return "\n".join(out)


def _reply_complete(text: str) -> bool:
    """判断回复是否"说完了"：包在 ``` 里的 JSON 必须真正闭合。

    不能用"``` 出现偶数次"来判断——文本是我们自己包的，永远成对，
    结果半截 JSON 也会被当成完整。必须看花括号/方括号是否配平。
    """
    body = (text or "").strip()
    if not body:
        return False

    # 剥掉 ``` 包装，取出里面的正文
    if body.startswith("```"):
        parts = body.split("```")
        if len(parts) < 3:
            return False
        body = parts[1]
        body = body.split("\n", 1)[1] if "\n" in body else body
    body = body.strip()
    if not body:
        return False

    # 纯文本回答（没有 JSON）视为完整
    if not body.startswith("{"):
        return True

    # JSON：括号必须配平，且以 } 结尾
    if not body.endswith("}"):
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in body:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth == 0 and not in_string


def _js_array(values: tuple[str, ...]) -> str:
    """把选择器元组变成 JS 数组字面量（json.dumps 保证引号安全）。"""
    return json.dumps(list(values), ensure_ascii=False)


@dataclass
class WebChatClient:
    """用 CDP 驱动网页版聊天，实现与 LLMClient 相同的最小接口。"""

    site_id: str
    profile_dir: str
    headless: bool = False
    timeout: float = DEFAULT_TIMEOUT
    edge_path: str = ""
    _edge: EdgeProcess | None = None
    _session: CDPSession | None = None
    _started: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------------- 元信息 ----------------
    @property
    def provider(self) -> str:
        return "web"

    @property
    def model(self) -> str:
        return f"web:{self.site_id}"

    @property
    def enabled(self) -> bool:
        return driver_available()

    @property
    def adapter(self) -> SiteAdapter:
        return get_adapter(self.site_id)

    # ---------------- 生命周期 ----------------
    def _ensure_edge(self) -> EdgeProcess:
        """保证有一个活着的 Edge 实例。"""
        if self._edge is not None and self._edge.alive:
            return self._edge

        edge = EdgeProcess(self.profile_dir, headless=self.headless)
        if self.edge_path:
            edge.edge_path_override = self.edge_path  # type: ignore[attr-defined]
        edge.start(self.adapter.url)
        self._edge = edge
        self._session = None
        return edge

    async def _ensure_session(self) -> CDPSession:
        """保证有一条连到目标站点的 CDP 会话。

        刚启动 Edge 时经常只有一个 ``about:blank``，目标页还在加载。所以这里
        连上任意标签后主动 ``Page.navigate`` 到站点，再等地址变成目标域名——
        比"祈祷 target 列表刚好有它"可靠得多。
        """
        # 启动浏览器是同步阻塞操作（等端口最多 40 秒），放到线程里执行，
        # 否则整个事件循环被卡住，前端所有请求都会 Failed to fetch。
        edge = await asyncio.to_thread(self._ensure_edge)

        if self._session is not None:
            return self._session

        host = self.adapter.url.split("/")[2]
        chosen: dict[str, Any] | None = None

        # 先给它几秒自然加载的机会（正常情况这时页面已经在了）
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            targets = [t for t in edge.page_targets() if t.get("webSocketDebuggerUrl")]
            if targets:
                site_pages = [t for t in targets if host in str(t.get("url", ""))]
                if site_pages:
                    chosen = site_pages[-1]
                    break
                chosen = chosen or self._pick_target(targets)
            await asyncio.sleep(0.4)

        if chosen is None:
            edge.new_tab(self.adapter.url)
            await asyncio.sleep(1.2)
            targets = [t for t in edge.page_targets() if t.get("webSocketDebuggerUrl")]
            if not targets:
                raise WebAgentError(
                    "浏览器没有可用标签页，请重试或手动关闭残留的 Edge 窗口。",
                    code="no_target",
                )
            chosen = self._pick_target(targets)

        session = CDPSession(str(chosen["webSocketDebuggerUrl"]))
        await session.connect()

        # 连上的可能还是 about:blank，主动导航过去
        try:
            current = str(await session.eval("location.href") or "")
        except CDPError:
            current = ""
        if host not in current:
            logger.info("当前页面是 %s，导航到 %s", current[:60], self.adapter.url)
            try:
                await session.call("Page.enable", timeout=10)
                await session.call("Page.navigate", {"url": self.adapter.url}, timeout=20)
            except CDPError as exc:
                logger.debug("Page.navigate 失败，改用 location.assign：%s", exc)
                await session.eval(f"location.assign({json.dumps(self.adapter.url)});")
            for _ in range(50):  # 最多等 20 秒
                await asyncio.sleep(0.4)
                try:
                    current = str(await session.eval("location.href") or "")
                except CDPError:
                    continue
                if host in current:
                    break

        self._session = session
        logger.info("已连接 CDP：%s", current[:80])
        return session

    def _pick_target(self, targets: list[dict[str, Any]]) -> dict[str, Any]:
        """挑出真正要操作的那个标签页。

        首次启动 Edge 会额外开出 ``edge://sync-confirmation-dialog`` 之类的内部页面，
        直接取第一个会连错窗口，所以这里优先匹配站点域名，其次跳过 ``edge://`` 内部页。
        """
        host = self.adapter.url.split("/")[2]
        site_pages = [t for t in targets if host in str(t.get("url", ""))]
        if site_pages:
            return site_pages[-1]
        normal_pages = [t for t in targets if not str(t.get("url", "")).startswith("edge://")]
        return (normal_pages or targets)[-1]

    async def close(self) -> None:
        """关闭会话并结束 Edge 进程。

        **必须先发 ``Browser.close``**：Chromium 只在正常退出时把 Cookie 刷进磁盘，
        直接杀进程会导致"下次切换模型又得重新登录"。
        """
        if self._session is not None:
            try:
                await self._session.call("Browser.close", timeout=8)
                logger.info("已请求浏览器正常退出")
            except CDPError as exc:
                logger.debug("Browser.close 未响应（可能已断开）：%s", exc)
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        if self._edge is not None:
            await asyncio.to_thread(self._edge.stop)
            self._edge = None

    async def _set_headless(self, flag: bool) -> None:
        """切换有头/无头模式（需要重启浏览器，Cookie 在磁盘上不会丢）。

        日常对话用无头（不弹窗口），只有"登录"这一步需要有头。
        """
        if self.headless == flag:
            return
        if self._edge is None:
            # 还没启动过浏览器，直接设标志即可，省一次重启
            self.headless = flag
            return
        logger.info("浏览器模式切换：headless %s → %s", self.headless, flag)
        await self.close()
        self.headless = flag
        await asyncio.to_thread(self._ensure_edge)
        await self._ensure_session()

    async def switch_to_headless(self) -> bool:
        """登录完成后切到无头模式，之后不再弹出浏览器窗口。"""
        if self.headless:
            return False
        await self._set_headless(True)
        logger.info("已切换到无头模式，后续对话不再弹出窗口")
        return True

    async def open_for_login(self) -> str:
        """以有头模式打开**当前站点**，让用户登录；返回站点 URL。

        必须真的导航过去：只复用已有的 CDP 会话会导致"切到文心，浏览器里还是元宝"，
        用户以为没打开成功。
        """
        await self._set_headless(False)
        session = await self._ensure_session()

        # 把窗口提到最前，否则容易被其它窗口挡住，看起来像"没打开"
        try:
            await session.call("Page.bringToFront", timeout=10)
        except CDPError:
            pass

        # 导航到当前站点；这一步只要页面能渲染出来即可，不强制要求输入框
        # （未登录页、加载慢的站点可能暂时没有输入框）
        await self._goto_new_chat(session, wait_input=False)
        return self.adapter.url

    async def check_login(self) -> bool:
        """检查当前页面是否已登录（以"能否找到输入框"为准）。

        不关闭浏览器：登录态与页面都保留，用户确认后可以直接发消息。
        """
        session = await self._ensure_session()

        # 切换站点后，浏览器可能还停在上一家的页面。先确认地址属于当前站点，
        # 否则会把"上一家的登录态"当成当前站点的（实测会误报 6ms 就"已登录"）。
        host = self.adapter.url.split("/")[2]
        try:
            current = str(await session.eval("location.href") or "")
        except CDPError:
            current = ""
        navigated = False
        if host not in current:
            await self._goto_new_chat(session)
            # 刚拉起浏览器时 Cookie 还在从磁盘恢复，立刻检查会误判"未登录"。
            # 先等一下，后面还会再复查一次。
            await asyncio.sleep(1.5)
            navigated = True

        script = f"""
(() => {{
  const SELS = {_js_array(self.adapter.input_selectors)};
  for (const sel of SELS) {{
    try {{
      const nodes = document.querySelectorAll(sel);
      for (const node of nodes) {{
        if (node.offsetWidth > 0 || node.offsetHeight > 0) return true;
      }}
    }} catch (e) {{ }}
  }}
  return false;
}})()
"""
        try:
            has_input = bool(await session.eval(script, timeout=10))
        except CDPError:
            return False

        if not has_input:
            return False

        # 光有输入框不够：未登录的落地页同样有输入框（可以试用），
        # 必须再看页面是不是登录页——否则会把"没登录"当成"已登录"，
        # 后面的发送和抓取全部对不上。
        try:
            on_login_page = bool(await session.eval(self._js_login_page(), timeout=10))
        except CDPError:
            on_login_page = False
        if not on_login_page:
            return True

        logger.info("检测到登录页，判定为未登录")
        if navigated:
            # 浏览器刚启动时 Cookie 从磁盘恢复到内存需要几秒，这里轮询最多 8 秒
            for _ in range(8):
                await asyncio.sleep(1.0)
                try:
                    still_login = bool(await session.eval(self._js_login_page(), timeout=8))
                except Exception:
                    # 导航过程中执行上下文会短暂失效（可能抛 TimeoutError / CDPError）
                    still_login = True
                if not still_login:
                    logger.info("复查后已登录（Cookie 已恢复）")
                    return True
        return False

    def _js_login_page(self) -> str:
        """页面上是否有明显的"登录/扫码"提示。"""
        return r"""
(() => {
  const text = (document.body && document.body.innerText) || '';
  if (text.length > 30000) return false;
  // 已登录的明确标志
  if (/退出登录|退出账号/.test(text)) return false;
  if (/扫码登录|扫码即可登录|立即登录|登录后|请先登录|未登录/.test(text)) return true;
  // 页面上只要还有独立的“登录”入口，就说明没登录
  if (text.indexOf('登录') >= 0) return true;
  const url = location.href || '';
  return /\/login|\/sign_in|passport|account\./i.test(url);
})()
"""

    # ---------------- 页面操作 ----------------
    async def _ensure_focus(self, session: CDPSession) -> None:
        """把标签页提到前台，并用真实鼠标点击输入框。

        CDP 的 ``Input.*`` 事件等价于真人操作，但前提是**标签页处于活动状态、元素拿到焦点**。
        刚导航到新对话页时焦点在 body 上，直接 insertText 会落到空气里——
        表现就是"文字写进去了，回车却发不出去"。
        """
        # 注意：这里**不能**调 Page.bringToFront。
        # 它会把浏览器窗口顶到系统最前，每发一条消息就抢一次焦点，
        # 用户在用别的程序时会被反复打断。无头模式下本来也不需要置前。
        spot = await session.eval(self._js_input_spot(), timeout=10)
        if not isinstance(spot, dict) or "x" not in spot:
            return
        for event_type in ("mousePressed", "mouseReleased"):
            try:
                await session.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event_type,
                        "x": float(spot["x"]),
                        "y": float(spot["y"]),
                        "button": "left",
                        "clickCount": 1,
                    },
                    timeout=10,
                )
            except CDPError as exc:
                logger.debug("点击输入框失败：%s", exc)
                return
        await asyncio.sleep(0.2)

    def _js_input_spot(self) -> str:
        """返回输入框中心坐标（供鼠标点击）。"""
        adapter = self.adapter
        return f"""
(() => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  for (const sel of INPUTS) {{
    let nodes = [];
    try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
    for (const node of nodes) {{
      if (!visible(node)) continue;
      try {{ node.scrollIntoView({{ block: 'center' }}); }} catch (e) {{ }}
      const r = node.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      return {{ x: r.x + r.width / 2, y: r.y + r.height / 2 }};
    }}
  }}
  return null;
}})()
"""

    async def _send_text(self, session: CDPSession, text: str) -> None:
        """把文本写进输入框并发送，**并校验真的发出去了**。

        两个踩过的坑：
        1. 只"设值 + 点 button[type=button]"会点到"取消"之类，静默失败；
        2. 用 JS 合成事件（``el.click()`` / ``dispatchEvent``）时 ``isTrusted=false``，
           部分站点的输入框逻辑不认。

        所以这里走 CDP 的 **原生输入通道**：``Input.insertText`` 写文本、
        ``Input.dispatchKeyEvent`` 发 Enter、必要时 ``Input.dispatchMouseEvent`` 点按钮——
        这些是浏览器级事件，和真人操作同一条路径。
        """
        adapter = self.adapter

        # 0) 记录发送前的页面状态，"是否真的发出去"要用它对比
        baseline_count = await self._reply_count(session)
        baseline_text = await self._last_reply(session)

        # 0.1) 把标签页提到前台并真实点一下输入框，否则后面的原生输入会落空
        await self._ensure_focus(session)

        # 1) 确认输入框存在并聚焦
        focused = await session.eval(self._js_focus_input(), timeout=20)
        if not isinstance(focused, dict) or not focused.get("ok"):
            url = focused.get("url") if isinstance(focused, dict) else ""
            hint = adapter.login_hint
            if any(token in str(url) for token in ("login", "sign_in", "passport")):
                raise WebAgentError(
                    f"{adapter.label} 当前是未登录状态，请先在浏览器窗口里登录。{hint}",
                    code="not_logged_in",
                )
            raise WebAgentError(
                f"在 {adapter.label} 页面找不到输入框。可能未登录或页面结构已变化。{hint}",
                code="input_not_found",
            )

        # 2) 用真实键盘全选并删除。
        #    不能用 innerText='' 清空：通义用 Slate、豆包用 ProseMirror，
        #    这类编辑器的 DOM 由 React state 驱动，直接改 DOM 会把内部结构弄坏，
        #    之后任何输入都写不进去（表现就是"文字没有写进输入框"）。
        await self._clear_input(session)

        # 3) 写入文本。实测结论：Slate（通义）**认 Input.insertText**——写入后
        #    发送按钮会从 disabled 变 enabled；反而 execCommand 会把文字塞进占位符
        #    且不触发 React state 更新。所以这里以 insertText 为主，execCommand 仅兜底。
        tag = str(focused.get("tag", "")).lower()
        try:
            await session.call("Input.insertText", {"text": text}, timeout=40)
        except CDPError as exc:
            logger.warning("Input.insertText 失败，改用 execCommand：%s", exc)
            await session.eval(self._js_exec_insert(text), timeout=30)

        await asyncio.sleep(0.4)
        state = await session.eval(self._js_box_state(), timeout=10)

        if isinstance(state, dict) and state.get("empty"):
            logger.info("insertText 未生效，改用 execCommand 重试")
            try:
                await session.eval(self._js_exec_insert(text), timeout=30)
            except CDPError as exc:
                logger.warning("execCommand 重试失败：%s", exc)
            await asyncio.sleep(0.4)
            state = await session.eval(self._js_box_state(), timeout=10)

        if isinstance(state, dict) and state.get("empty"):
            raise WebAgentError(
                f"文字没有写进 {adapter.label} 的输入框（已尝试原生输入与 execCommand）。"
                "请在浏览器窗口里手动点一下输入框再重试。",
                code="fill_rejected",
            )

        # 4) 发送：优先点"发送"按钮。
        #    实测：输入有内容后按钮才变 enabled，此时点它最可靠；
        #    先发回车容易在某些站点被当成"换行"，反而把内容搞乱。
        clicked = await self._click_send_button(session, seconds=4.0)
        if clicked and await self._send_landed(
            session, baseline_count, baseline_text, seconds=6.0, sent_text=text
        ):
            return

        logger.info("点击发送按钮未生效，尝试回车")
        await self._press_enter(session)
        if await self._send_landed(
            session, baseline_count, baseline_text, seconds=4.0, sent_text=text
        ):
            return

        raise WebAgentError(
            f"消息没有发出去：{adapter.label} 的输入框里仍有内容"
            f"（已尝试{'点击发送按钮、' if clicked else ''}回车）。"
            "请在浏览器窗口里手动点一下输入框，再回来重试。",
            code="send_failed",
        )

    async def _press_enter(self, session: CDPSession) -> None:
        """发送原生 Enter 按键事件。

        不发 ``char``：带 ``text="\\r"`` 的 char 事件会被部分编辑器当成"插入换行"，
        而不是"提交"。
        """
        for key_type in ("keyDown", "keyUp"):
            try:
                await session.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": key_type,
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                        "nativeVirtualKeyCode": 13,
                    },
                    timeout=10,
                )
            except CDPError as exc:
                logger.debug("dispatchKeyEvent(%s) 失败：%s", key_type, exc)

    async def _click_send_button(self, session: CDPSession, *, seconds: float = 5.0) -> bool:
        """用原生鼠标事件点击"发送"按钮，返回是否点到了。

        必须**等按钮变为可用**再点：通义的按钮在输入框为空时是 disabled，
        对着禁用按钮发鼠标事件不会有任何效果（表现就是"点了没反应"）。
        """
        spot: dict[str, Any] | None = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            spot = await session.eval(self._js_send_spot(), timeout=10)
            if isinstance(spot, dict) and spot.get("enabled"):
                break
            await asyncio.sleep(0.4)
        else:
            spot = None

        if not isinstance(spot, dict) or not spot.get("enabled"):
            logger.info("没有找到可用的发送按钮")
            return False

        logger.debug("点击发送按钮：%s @ (%s, %s)", spot.get("label"), spot.get("x"), spot.get("y"))
        for event_type in ("mousePressed", "mouseReleased"):
            await session.call(
                "Input.dispatchMouseEvent",
                {
                    "type": event_type,
                    "x": float(spot["x"]),
                    "y": float(spot["y"]),
                    "button": "left",
                    "clickCount": 1,
                },
                timeout=10,
            )
        return True

    def _js_send_spot(self) -> str:
        """定位发送按钮并返回其坐标与可用状态。"""
        return """
(() => {
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  const norm = (s) => (s || '').replace(/\\s+/g, '').toLowerCase();
  const nodes = Array.from(document.querySelectorAll(
    'button, div[role="button"], [aria-label*="发送"], [aria-label*="send"], [class*="send"], [class*="Send"]'
  ));
  for (const el of nodes) {
    if (!visible(el)) continue;
    const label = norm(el.innerText) + ' ' + norm(el.getAttribute('aria-label')) + ' ' + norm(el.title);
    if (/取消|停止|清空|cancel|stop|clear/.test(label)) continue;
    if (!/发送|send|submit/.test(label)) continue;
    const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true'
      || el.classList.contains('disabled') || el.classList.contains('pointer-events-none');
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    return {
      x: r.x + r.width / 2,
      y: r.y + r.height / 2,
      enabled: !disabled,
      label: (el.innerText || el.getAttribute('aria-label') || '').slice(0, 12),
    };
  }
  return null;
})()
"""

    async def _send_landed(
        self,
        session: CDPSession,
        baseline_count: int,
        baseline_text: str,
        *,
        seconds: float,
        sent_text: str = "",
    ) -> bool:
        """判断消息是否真的发出去了。

        三个信号，按"出现速度"排序：
        1. 对话区出现了我们刚发的那段文字（最快，通常 1-2 秒）——网页版会把用户
           消息立刻渲染成气泡，这是最可靠的"已提交"信号；
        2. 回复条数增加；
        3. 最后一条回复文本变了。
        不能只看"输入框是否清空"：通义发完后 Slate 不清空节点，必然误判。
        """
        rounds = max(1, int(seconds / 0.4))
        for _ in range(rounds):
            await asyncio.sleep(0.4)

            if sent_text:
                try:
                    if await session.eval(self._js_text_in_page(sent_text), timeout=8):
                        return True
                except CDPError:
                    pass

            try:
                count = await self._reply_count(session)
                text = await self._last_reply(session)
            except CDPError:
                continue
            if count > baseline_count:
                return True
            if text and text != baseline_text:
                return True
        return False

    def _js_text_in_page(self, text: str) -> str:
        """对话区里是否已经出现这段文字（= 消息气泡已渲染）。"""
        needle = (text or "").strip()[:30]
        return f"""
(() => {{
  const NEEDLE = {json.dumps(needle, ensure_ascii=False)};
  if (!NEEDLE) return false;
  const nodes = document.querySelectorAll('div, p, span, li');
  for (const node of nodes) {{
    if (node.isContentEditable) continue;
    if (node.closest && node.closest('[contenteditable="true"]')) continue;
    if (node.children && node.children.length > 3) continue;
    const value = node.innerText || '';
    if (value.length < NEEDLE.length) continue;
    if (value.indexOf(NEEDLE) >= 0) return true;
  }}
  return false;
}})()
"""

    async def _wait_box_empty(self, session: CDPSession, *, seconds: float) -> bool:
        """等待输入框被清空（= 发送成功）。"""
        rounds = max(1, int(seconds / 0.4))
        for _ in range(rounds):
            await asyncio.sleep(0.4)
            state = await session.eval(self._js_box_state(), timeout=10)
            if isinstance(state, dict) and state.get("empty"):
                return True
        return False

    async def _clear_input(self, session: CDPSession) -> None:
        """用真实键盘清空输入框（全选 + 删除）。

        Slate / ProseMirror 这类编辑器只认真实事件流，直接改 DOM 会破坏其内部状态。
        """
        await self._key(session, "a", "KeyA", 65, modifiers=2)  # Ctrl+A
        await asyncio.sleep(0.12)
        await self._key(session, "Delete", "Delete", 46)
        await asyncio.sleep(0.12)

    async def _key(
        self,
        session: CDPSession,
        key: str,
        code: str,
        vk: int,
        *,
        modifiers: int = 0,
    ) -> None:
        """发送一次真实的按下/抬起按键。"""
        for event_type in ("keyDown", "keyUp"):
            try:
                await session.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": event_type,
                        "key": key,
                        "code": code,
                        "windowsVirtualKeyCode": vk,
                        "nativeVirtualKeyCode": vk,
                        "modifiers": modifiers,
                    },
                    timeout=10,
                )
            except CDPError as exc:
                logger.debug("按键 %s(%s) 失败：%s", key, event_type, exc)

    def _js_focus_input(self) -> str:
        """聚焦输入框（不动内容）。返回是否找到。"""
        adapter = self.adapter
        return f"""
(() => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  for (const sel of INPUTS) {{
    let nodes = [];
    try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
    for (const node of nodes) {{
      if (!visible(node)) continue;
      node.focus();
      return {{ ok: true, tag: node.tagName.toLowerCase(), url: location.href }};
    }}
  }}
  return {{ ok: false, url: location.href }};
}})()
"""

    def _js_focus_clear(self) -> str:
        """聚焦输入框并清空，返回是否找到。"""
        adapter = self.adapter
        return f"""
(() => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  for (const sel of INPUTS) {{
    let nodes = [];
    try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
    for (const node of nodes) {{
      if (!visible(node)) continue;
      node.focus();
      const tag = node.tagName.toLowerCase();
      if (tag === 'textarea' || tag === 'input') {{
        const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(node, '');
        node.dispatchEvent(new Event('input', {{ bubbles: true }}));
      }} else {{
        node.innerText = '';
        node.dispatchEvent(new InputEvent('input', {{ bubbles: true }}));
      }}
      return {{ ok: true, tag, url: location.href }};
    }}
  }}
  return {{ ok: false, url: location.href }};
}})()
"""

    def _js_fill(self, text: str) -> str:
        """JS 兜底填值（清空 → 写值 → 回读校验）。"""
        adapter = self.adapter
        return f"""
(async () => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const TEXT = {json.dumps(text, ensure_ascii=False)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  const find = () => {{
    for (const sel of INPUTS) {{
      let nodes = [];
      try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
      for (const node of nodes) {{ if (visible(node)) return node; }}
    }}
    return null;
  }};

  const box = find();
  if (!box) return {{ ok: false, reason: 'input_not_found', url: location.href }};

  box.focus();
  const tag = box.tagName.toLowerCase();
  const setValue = (value) => {{
    if (tag === 'textarea' || tag === 'input') {{
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(box, value);
      box.dispatchEvent(new Event('input', {{ bubbles: true }}));
      box.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }} else {{
      box.innerText = value;
      box.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: value, inputType: 'insertText' }}));
    }}
  }};

  setValue('');
  await new Promise((r) => setTimeout(r, 80));
  setValue(TEXT);
  await new Promise((r) => setTimeout(r, 200));

  const actual = (tag === 'textarea' || tag === 'input') ? box.value : box.innerText;
  const matched = (actual || '').indexOf(TEXT.slice(0, 12)) >= 0;
  return {{ ok: true, matched, length: (actual || '').length }};
}})()
"""

    def _js_exec_insert(self, text: str) -> str:
        """用 ``execCommand('insertText')`` 写入，兼容富文本编辑器。

        通义/豆包用的是 contenteditable + ProseMirror 类编辑器，只认完整的
        ``beforeinput`` 事件链；``Input.insertText`` 在这类编辑器上会被忽略。
        ``execCommand`` 是浏览器原生命令，会触发完整事件链，编辑器才肯接收。
        """
        adapter = self.adapter
        return f"""
(() => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const TEXT = {json.dumps(text, ensure_ascii=False)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);

  let box = null;
  for (const sel of INPUTS) {{
    let nodes = [];
    try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
    for (const node of nodes) {{ if (visible(node)) {{ box = node; break; }} }}
    if (box) break;
  }}
  if (!box) return false;

  box.focus();
  const tag = box.tagName.toLowerCase();
  if (tag === 'textarea' || tag === 'input') {{
    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(box, '');
    box.dispatchEvent(new Event('input', {{ bubbles: true }}));
    try {{ box.setSelectionRange(0, 0); }} catch (e) {{ }}
  }} else {{
    // contenteditable：全选后插入，execCommand 会替换选区
    const range = document.createRange();
    range.selectNodeContents(box);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }}
  try {{
    return document.execCommand('insertText', false, TEXT);
  }} catch (e) {{
    return false;
  }}
}})()
"""

    def _js_box_state(self) -> str:
        """读取输入框是否已空（判断发送是否真的生效）。

        优先读 ``document.activeElement``：``Input.insertText`` 写的是"当前焦点元素"，
        如果校验时去读"页面上第一个可见输入框"，两者可能不是同一个，就会误判成没写进去。
        """
        adapter = self.adapter
        return f"""
(() => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  const read = (el) => {{
    const tag = el.tagName.toLowerCase();
    const value = (tag === 'textarea' || tag === 'input') ? el.value : el.innerText;
    return {{ empty: !value || !value.trim(), length: (value || '').length }};
  }};
  const matches = (el) => {{
    for (const sel of INPUTS) {{
      try {{ if (el.matches(sel)) return true; }} catch (e) {{ }}
    }}
    return false;
  }};

  const active = document.activeElement;
  if (active && visible(active) && matches(active)) return read(active);

  for (const sel of INPUTS) {{
    let nodes = [];
    try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
    for (const node of nodes) {{
      if (!visible(node)) continue;
      return read(node);
    }}
  }}
  return {{ empty: true, gone: true }};
}})()
"""

    async def _reply_count(self, session: CDPSession) -> int:
        """当前页面上回复气泡的数量，用来判定"本轮新增的那条"。"""
        script = f"""
(() => {{
  const SELS = {_js_array(self.adapter.reply_selectors)};
  for (const sel of SELS) {{
    try {{
      const nodes = document.querySelectorAll(sel);
      if (nodes.length) return nodes.length;
    }} catch (e) {{ }}
  }}
  return 0;
}})()
"""
        try:
            value = await session.eval(script, timeout=10)
            return int(value) if isinstance(value, (int, float)) else 0
        except Exception:
            return 0

    async def _last_reply(self, session: CDPSession) -> str:
        """读取最后一条回复的正文。

        坑：直接取 ``innerText`` 会把代码块工具栏的「json 复制 下载」一起抓进来，
        导致模型输出里混着按钮文字、JSON 解析失败。所以优先只取 ``<pre>`` 里的代码，
        用 ``` 包起来（正好符合指令协议）；没有代码块时才退回清洗过的纯文本。
        """
        script = f"""
(() => {{
  const SELS = {_js_array(self.adapter.reply_selectors)};
  const clean = (text) => (text || '')
      .replace(/^\\s*(复制|下载|json|JSON|text|copy|download)\\s*$/gm, '')
      .trim();
  for (const sel of SELS) {{
    let nodes = [];
    try {{ nodes = document.querySelectorAll(sel); }} catch (e) {{ continue; }}
    if (!nodes.length) continue;
    const last = nodes[nodes.length - 1];

    const pres = last.querySelectorAll('pre');
    if (pres.length) {{
      // 只取最后一个代码块：容器可能把历史回答也包进来，全取会把旧 JSON 拼在前面
      const pre = pres[pres.length - 1];
      const code = pre.querySelector('code') || pre;
      const body = (code.textContent || '').trim();
      if (body) return '```\\n' + body + '\\n```';
    }}
    return clean(last.innerText || last.textContent || '');
  }}
  return '';
}})()
"""
        value = await session.eval(script, timeout=15)
        text = str(value or "")
        if text and self.adapter.strip_line_numbers:
            text = _strip_line_numbers(text)
        return text

    async def _is_generating(self, session: CDPSession) -> bool:
        """页面上是否还在生成（存在"停止生成"按钮）。

        网页版回答中途经常停顿超过 2 秒，只按"文本没变"判断会提前截断，
        拿到半个 JSON。有明确的停止按钮时，说明模型还在吐字，必须继续等。
        """
        script = """
(() => {
  const norm = (s) => (s || '').replace(/\\s+/g, '').toLowerCase();
  const nodes = document.querySelectorAll('button, div[role="button"], [class*="stop"], [class*="Stop"]');
  for (const el of nodes) {
    if (!(el.offsetWidth || el.offsetHeight)) continue;
    const label = norm(el.innerText) + norm(el.getAttribute('aria-label')) + norm(el.title);
    if (/停止|stop/.test(label)) return true;
  }
  return false;
})()
"""
        try:
            return bool(await session.eval(script, timeout=8))
        except Exception:
            return False

    async def _collect_reply(
        self,
        session: CDPSession,
        *,
        baseline_count: int,
        baseline_text: str,
    ) -> str:
        """轮询直到回复结束，返回**最终完整文本**。

        为什么不做字符级流式：网页版渲染 Markdown 时会整段重绘，同一段文字
        在不同帧里可能被重新排版（例如代码块从 "半句" 变成完整块），
        按"公共前缀"算增量会把重复内容拼进去，最终 JSON 直接损坏。
        所以这里只忠实取到最后一帧，一次性交给上层。

        结束判据三层：
        1. 条数增加 **或** 最后一条文本变化 → 有新内容；
        2. 文本连续 ``STABLE_ROUNDS`` 轮不变 → 疑似结束；
        3. 结束前确认 **代码块闭合** 且页面 **不在生成中**，否则继续等。
        """
        adapter = self.adapter
        deadline = time.monotonic() + self.timeout
        shown = ""
        stable = 0
        seen_any = False

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                count = await self._reply_count(session)
                current = await self._last_reply(session)
            except CDPError as exc:
                raise WebAgentError(f"读取回复失败：{exc.message}", code="cdp_failed") from exc

            if not current:
                continue
            if not (count > baseline_count or current != baseline_text):
                continue

            seen_any = True
            if current != shown:
                shown = current
                stable = 0
                continue

            stable += 1
            if stable < STABLE_ROUNDS:
                continue

            if not _reply_complete(current):
                logger.debug("回复疑似未完成（JSON 未闭合），继续等待")
                # 模型在长上下文下可能思考十几秒才继续吐字，给足 30 秒
                if stable > int(30 / POLL_INTERVAL):
                    logger.warning("等待完整回复超 30 秒，返回已拿到的部分")
                    return shown
                continue

            if await self._is_generating(session):
                logger.debug("页面仍在生成，继续等待")
                if stable > STABLE_ROUNDS * 6:
                    return shown
                continue

            return shown

        if not seen_any:
            raise WebAgentError(
                f"{adapter.label} 在 {int(self.timeout)} 秒内没有返回内容。"
                "请确认已登录、未触发人机验证，或在浏览器窗口里手动发一条消息看是否正常。",
                code="reply_timeout",
            )
        logger.warning("读取回复超时，返回已拿到的部分（%s 字符）", len(shown))
        return shown

    # ---------------- 对外接口 ----------------
    async def stream_chat(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        """在**全新的对话页**里发一条消息，抓取回复并返回。

        为什么不接着上一轮说：网页版会复用同一个消息节点渲染新回复（DOM 条数不变），
        新旧内容在页面上无法可靠区分，多轮必然抓错。每轮重新打开对话页后，
        页面上只有一个回复节点，判定变得确定——代价是每轮多发一次系统提示（网页版免费）。
        """
        async with self._lock:
            status = driver_status()
            if not status["available"]:
                reason = "缺少 websockets 依赖" if not status["websockets"] else "找不到 Microsoft Edge"
                raise WebAgentError(f"网页版驱动不可用：{reason}", code="driver_unavailable")

            # 发消息时不应该有窗口：如果还停着有头模式（用户登录后没点"已完成登录"），
            # 这里自动切到无头，避免每轮对话都把浏览器窗口弹出来。
            if not self.headless:
                logger.info("检测到有头模式，发消息前自动切换到无头")
                await self._set_headless(True)

            session = await self._ensure_session()
            await self._goto_new_chat(session)

            payload = self._compose(messages)
            baseline_count = await self._reply_count(session)
            baseline_text = await self._last_reply(session)

            await self._send_text(session, payload)
            self._started = True

            text = await self._collect_reply(
                session,
                baseline_count=baseline_count,
                baseline_text=baseline_text,
            )
            if text:
                # 网页版不做字符级流式（Markdown 整段重绘会拼坏 JSON），一次性交付
                yield text

    async def _goto_new_chat(self, session: CDPSession, *, wait_input: bool = True) -> None:
        """打开一个全新的对话页，保证页面上没有历史回复。

        这是多轮可靠性的关键：旧对话里节点会被复用，新回复和上一条挤在同一个
        元素里；新开对话后 ``.ds-markdown`` 数量从 0 开始，判定没有歧义。

        ``wait_input=False`` 用于"打开登录页"场景：只要求页面渲染出来，
        不要求输入框存在（未登录页或加载慢的站点可能暂时没有）。
        """
        adapter = self.adapter
        logger.info("打开新对话：%s", adapter.url)
        try:
            await session.call("Page.enable", timeout=10)
            await session.call("Page.navigate", {"url": adapter.url}, timeout=20)
        except CDPError as exc:
            # 导航期间连接可能抖动，重连一次
            logger.warning("导航失败，重连后重试：%s", exc)
            await session.close()
            self._session = None
            session = await self._ensure_session()
            await session.call("Page.navigate", {"url": adapter.url}, timeout=20)

        # 等待页面就绪
        deadline = time.monotonic() + 25
        last_len = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                if wait_input:
                    ready = bool(await session.eval(self._js_has_input(), timeout=8))
                else:
                    # 不要求输入框：地址对上 + 页面有内容就够了
                    probe = await session.eval(
                        """(() => ({
                          href: location.href,
                          len: ((document.body && document.body.innerText) || '').length,
                          state: document.readyState,
                        }))()""",
                        timeout=8,
                    )
                    if not isinstance(probe, dict):
                        continue
                    last_len = int(probe.get("len") or 0)
                    ready = last_len > 50 and probe.get("state") in {"interactive", "complete"}
            except Exception:
                continue  # 导航中执行上下文会短暂失效，重试即可
            if ready:
                await asyncio.sleep(0.8)
                return

        if not wait_input:
            logger.warning("%s 页面 25 秒内未渲染出内容（body 约 %s 字符），但浏览器已打开", adapter.label, last_len)
            return

        raise WebAgentError(
            f"打开 {adapter.label} 新对话超时（页面没有出现输入框）。"
            "可能是未登录，或浏览器被其他窗口遮挡导致加载缓慢。",
            code="new_chat_timeout",
        )

    def _js_has_input(self) -> str:
        """页面上是否存在可见输入框（= 对话页就绪）。"""
        adapter = self.adapter
        return f"""
(() => {{
  const INPUTS = {_js_array(adapter.input_selectors)};
  const visible = (el) => !!el && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
  for (const sel of INPUTS) {{
    let nodes = [];
    try {{ nodes = Array.from(document.querySelectorAll(sel)); }} catch (e) {{ continue; }}
    for (const node of nodes) {{ if (visible(node)) return true; }}
  }}
  return false;
}})()
"""

    def _compose(self, messages: list[dict[str, Any]]) -> str:
        """把系统规则、最近对话与当前任务压成一条消息。

        因为每轮都是新对话，这里始终给完整上下文，不依赖网页侧的记忆——
        这样单轮可复现，也不会因为上下文累积而变慢。
        """
        system_parts = [str(m.get("content", "")) for m in messages if m.get("role") == "system"]
        body = [m for m in messages if m.get("role") in {"user", "assistant"}]

        blocks: list[str] = []
        if system_parts:
            blocks.append("【系统规则】\n" + "\n\n".join(system_parts))

        if len(body) > 1:
            recent: list[str] = []
            for message in body[-6:-1]:  # 最近几轮（不含当前这条）
                role = "我" if message.get("role") == "user" else "你"
                text = str(message.get("content", "")).strip()
                if len(text) > 600:  # 执行结果可能很长，截断避免超出网页输入上限
                    text = text[:600] + " …（已截断）"
                recent.append(f"{role}：{text}")
            if recent:
                blocks.append("【此前对话】\n" + "\n".join(recent))

        if body:
            blocks.append("【当前任务】\n" + str(body[-1].get("content", "")).strip())

        blocks.append("请严格按系统规则回复：只输出一个 json 代码块，action 为 run_powershell / finish / ask_user。")
        return "\n\n".join(blocks)

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        async for chunk in self.stream_chat(messages):
            parts.append(chunk)
        return "".join(parts)
