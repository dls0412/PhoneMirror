# -*- coding: utf-8 -*-
"""
轻量 UI 组件库：圆角卡片、按钮、磁贴、矢量小图标。
纯 tkinter Canvas 绘制，不引第三方 UI 框架，业务逻辑零依赖。
设计基调：浅色底 + 蓝色主色 + 圆角卡片 + 克制的描边阴影（8pt 网格）。
"""
import os
import tkinter as tk
from tkinter import font as tkfont

import pm_tools

FONT = "Microsoft YaHei UI"

# ---- 调色板（参考图取色 + Material 3 蓝）----
BG = "#eef1f8"          # 页面底色
CARD = "#ffffff"        # 卡片
BORDER = "#e6ebf5"      # 卡片描边
INK = "#1b2440"         # 主文字
SUB = "#8a94ab"         # 次要文字
BLUE = "#2e6bf0"        # 主色
BLUE_D = "#1f56d6"      # 主色 hover
BLUE_MID = "#4f86ff"    # 主色亮端（渐变）
BLUE_L = "#e8f0ff"      # 主色调底（tonal）
BLUE_L2 = "#dfe9ff"
PURPLE = "#7a5af5"
PURPLE_L = "#efeaff"
GREEN = "#22b573"
RED = "#e5484d"
RED_L = "#fdeeee"
AMBER = "#f5a623"
HOVER = "#f4f7fd"

_scale = 1.0


def set_scale(root):
    """按系统 DPI 缩放所有像素尺寸（进程已声明 DPI 感知）"""
    global _scale
    try:
        _scale = root.winfo_fpixels("1i") / 96.0
    except Exception:
        _scale = 1.0
    if _scale < 0.8:
        _scale = 1.0


def S(px):
    return int(round(px * _scale))


# ---------------------------------------------------------------- 基础绘制

def rr(cv, x1, y1, x2, y2, r=12, **kw):
    """圆角矩形（polygon smooth）"""
    r = min(r, (x2 - x1) // 2, (y2 - y1) // 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def gradient_rr(cv, x1, y1, x2, y2, r, c_top, c_bot, steps=36):
    """竖直渐变的圆角矩形：先铺底，再用一列列色带画内部"""
    rr(cv, x1, y1, x2, y2, r, fill=c_bot, outline="")
    def mix(a, b, t):
        return "#%02x%02x%02x" % tuple(
            int(round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t))
            for i in (1, 3, 5))
    for i in range(steps):
        t = i / steps
        y = y1 + (y2 - y1) * i / steps
        rr(cv, x1 + r * 0.25, y, x2 - r * 0.25, y + (y2 - y1) / steps + 1, 2,
           fill=mix(c_top, c_bot, t), outline="")


# ---------------------------------------------------------------- 卡片

class Card(tk.Canvas):
    """白底圆角卡片。往 self.inner 里 pack/grid 内容，高度自适应"""

    def __init__(self, master, radius=16, bg=CARD, border=BORDER, fit=True, **kw):
        super().__init__(master, highlightthickness=0, bg=master.cget("bg"), **kw)
        self.radius = radius
        self._bg, self._border = bg, border
        self._autofit = fit
        self.inner = tk.Frame(self, bg=bg)
        self.bind("<Configure>", self._redraw)
        self.inner.bind("<Configure>", lambda e: self.after(10, self.fit))
        self._fitting = False
        self._tick_on = True
        self.after(120, self._tick)

    def _redraw(self, e=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        rr(self, 1, 1, w - 2, h - 2, self.radius,
           fill=self._bg, outline=self._border)
        if not self._autofit:
            self.inner.place(x=1, y=1, width=max(1, w - 2),
                             height=max(1, h - 2))
        else:
            self.after(10, self.fit)

    def _tick(self):
        """内容变化后 inner 的尺寸不会自己触发 Configure，这里做一次兜底核对"""
        if not self._tick_on:
            return
        try:
            if not self.winfo_exists():
                self._tick_on = False
                return
        except Exception:
            return
        try:
            if self._autofit and self.winfo_ismapped():
                rh = self.inner.winfo_reqheight() + 2
                if rh > 3 and abs(self.winfo_height() - rh) > 1:
                    self.fit()
                elif self.inner.winfo_height() < max(1, rh - 3):
                    self.fit()
        except Exception:
            pass
        try:
            self.after(220, self._tick)
        except Exception:
            pass

    def refit(self):
        try:
            self.after(10, self.fit)
        except Exception:
            pass

    def fit(self, e=None):
        if self._fitting:
            return
        self._fitting = True
        try:
            self.inner.update_idletasks()
            if self._autofit:
                h = self.inner.winfo_reqheight() + 2
                if h < 3:
                    h = 3
                if abs(self.winfo_height() - h) > 1:
                    self.configure(height=h)
                self.inner.place(x=1, y=1,
                                 width=max(1, self.winfo_width() - 2),
                                 height=h - 2)
            else:
                self.inner.place(x=1, y=1, width=max(1, self.winfo_width() - 2),
                                 height=max(1, self.winfo_height() - 2))
        finally:
            self._fitting = False


# ---------------------------------------------------------------- 按钮

class Btn(tk.Canvas):
    """
    圆角按钮。kind: primary / tonal / outline / ghost / danger / soft
    width=None 时按文字自适应
    """

    STYLES = {
        "primary": dict(bg=BLUE, fg="#ffffff", hover=BLUE_D, border=""),
        "tonal":   dict(bg=BLUE_L, fg=BLUE, hover=BLUE_L2, border=""),
        "outline": dict(bg=CARD, fg=BLUE, hover=BLUE_L, border=BLUE),
        "ghost":   dict(bg=CARD, fg=INK, hover=HOVER, border=BORDER),
        "plain":   dict(bg=BG, fg=INK, hover=HOVER, border=""),
        "danger":  dict(bg=RED_L, fg=RED, hover="#fbdbdb", border=""),
        "green":   dict(bg="#e7f7ef", fg=GREEN, hover="#d6f1e3", border=""),
    }

    def __init__(self, master, text, command=None, kind="primary", width=None,
                 height=36, size=10, radius=10, bold=True, **kw):
        st = self.STYLES[kind]
        f = tkfont.Font(family=FONT, size=size, weight="bold" if bold else "normal")
        tw = f.measure(text)
        w = width if width else tw + S(34)
        h = max(height, f.metrics("linespace") + S(14))
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bg=master.cget("bg"), cursor="hand2", **kw)
        self._st, self._r = st, radius
        self._text, self._cmd = text, command
        self._f = f
        self._bw, self._bh = w, h
        self._draw(st["bg"], st["fg"], st["border"])
        self.bind("<Enter>", lambda e: self._draw(st["hover"], st["fg"], st["border"]))
        self.bind("<Leave>", lambda e: self._draw(st["bg"], st["fg"], st["border"]))
        self.bind("<Button-1>", self._click)

    def _click(self, e):
        if self._cmd:
            try:
                self._cmd()
            except Exception:
                pass

    def set_text(self, t):
        self._text = t
        self._draw(self._st["bg"], self._st["fg"], self._st["border"])

    def _draw(self, bg, fg, border):
        self.delete("all")
        rr(self, 1, 1, self._bw - 2, self._bh - 2, self._r,
           fill=bg, outline=border, width=1)
        self.create_text(self._bw / 2, self._bh / 2, text=self._text,
                         font=self._f, fill=fg)


# ---------------------------------------------------------------- 小图标（矢量手绘）

def glyph_window(cv, cx, cy, s, color, width=2):
    """显示器/手机窗口：圆角方框 + 底部小方块"""
    r = rr(cv, cx - s, cy - s * 0.82, cx + s, cy + s * 0.7, s * 0.28,
           outline=color, width=width)
    rr(cv, cx - s * 0.52, cy - s * 0.34, cx - s * 0.02, cy + 0.16 * s, s * 0.1,
       fill=color, outline="")
    return r


def glyph_record(cv, cx, cy, s, color, width=2):
    """显示器 + 录制圆点"""
    rr(cv, cx - s, cy - s * 0.82, cx + s, cy + s * 0.7, s * 0.28,
       outline=color, width=width)
    cv.create_oval(cx - s * 0.3, cy - s * 0.3, cx + s * 0.3, cy + s * 0.3,
                   fill=color, outline="")
    return None


def glyph_qr(cv, cx, cy, s, color):
    """二维码：三个回字框 + 点"""
    k = s * 0.42
    for dx, dy in ((-1, -1), (1, -1), (-1, 1)):
        x, y = cx + dx * s * 0.55, cy + dy * s * 0.55
        rr(cv, x - k, y - k, x + k, y + k, k * 0.4, outline=color, width=2)
        cv.create_oval(x - k * 0.32, y - k * 0.32, x + k * 0.32, y + k * 0.32,
                       fill=color, outline="")
    cv.create_oval(cx + s * 0.5, cy + s * 0.5, cx + s * 0.72, cy + s * 0.72,
                   fill=color, outline="")


def glyph_doc(cv, cx, cy, s, color, width=2):
    rr(cv, cx - s * 0.7, cy - s, cx + s * 0.7, cy + s, s * 0.22,
       outline=color, width=width)
    for t in (-0.45, 0, 0.45):
        cv.create_line(cx - s * 0.38, cy + s * t, cx + s * 0.38, cy + s * t,
                       fill=color, width=width)


def glyph_gear(cv, cx, cy, s, color, width=2):
    cv.create_oval(cx - s * 0.92, cy - s * 0.92, cx + s * 0.92, cy + s * 0.92,
                   outline=color, width=width)
    cv.create_oval(cx - s * 0.34, cy - s * 0.34, cx + s * 0.34, cy + s * 0.34,
                   outline=color, width=width)
    import math
    for i in range(8):
        a = i * math.pi / 4
        cv.create_line(cx + s * 0.92 * math.cos(a), cy + s * 0.92 * math.sin(a),
                       cx + s * 1.32 * math.cos(a), cy + s * 1.32 * math.sin(a),
                       fill=color, width=width + 1, capstyle="round")


def glyph_help(cv, cx, cy, s, color, width=2):
    cv.create_oval(cx - s, cy - s, cx + s, cy + s, outline=color, width=width)
    cv.create_text(cx, cy - s * 0.05, text="?", font=(FONT, int(s * 1.15), "bold"),
                   fill=color)


def glyph_bulb(cv, cx, cy, s, color):
    cv.create_oval(cx - s * 0.62, cy - s, cx + s * 0.62, cy + s * 0.28,
                   outline=color, width=2)
    rr(cv, cx - s * 0.3, cy + s * 0.34, cx + s * 0.3, cy + s * 0.66, 2,
       fill=color, outline="")
    cv.create_line(cx, cy + s * 0.8, cx, cy + s * 1.05, fill=color, width=2,
                   capstyle="round")


def draw_arrow_badge(cv, cx, cy, r, bg, fg):
    """右侧小圆 + 箭头"""
    cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=bg, outline="")
    a = r * 0.42
    cv.create_line(cx - a * 0.6, cy - a, cx + a * 0.7, cy, cx - a * 0.6, cy + a,
                   fill=fg, width=2, capstyle="round", joinstyle="round",
                   smooth=True)


def draw_phone(cv, x1, y1, x2, y2):
    """参考图里的手机示意图：深色圆角边框 + 蓝紫渐变屏"""
    rr(cv, x1, y1, x2, y2, (y2 - y1) * 0.16, fill="#141a2e", outline="")
    pad = (x2 - x1) * 0.07
    sx1, sy1, sx2, sy2 = x1 + pad, y1 + pad, x2 - pad, y2 - pad
    c1, c2 = (0x8f, 0xb7, 0xff), (0xcf, 0xa9, 0xff)
    n = 40
    for i in range(n):
        t = i / n
        col = "#%02x%02x%02x" % tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3))
        yy1 = sy1 + (sy2 - sy1) * i / n
        yy2 = sy1 + (sy2 - sy1) * (i + 1) / n + 1
        rr(cv, sx1, yy1, sx2, yy2, 2, fill=col, outline="")
    rr(cv, sx1, sy1, sx2, sy2, (y2 - y1) * 0.11, outline="#141a2e", width=2)
    cx = (x1 + x2) / 2
    cv.create_oval(cx - 3, sy1 + 5, cx + 3, sy1 + 11, fill="#141a2e", outline="")
    rr(cv, x2 + 1, y1 + (y2 - y1) * 0.3, x2 + 3, y1 + (y2 - y1) * 0.5, 1,
       fill="#2a3350", outline="")


# ---------------------------------------------------------------- 常用 App 磁贴

_ICON_DIR = os.path.join(pm_tools.APP_DIR, "icons")
_img_cache = {}
_fallback_font = None
BRAND_FALLBACK = {
    "小红书": "#ff2442", "支付宝": "#1677ff", "淘宝": "#ff5000",
    "知乎": "#0084ff", "高德地图": "#2354c8", "京东": "#e1251b",
    "企业微信": "#2e6bf0", "飞书": "#3370ff", "钉钉": "#1a7af8",
    "百度": "#2932e1", "网易云音乐": "#e60026", "快手": "#ff4906",
    "剪映": "#00d5c0", "WPS": "#e14d44",
}
_PALETTE = ["#2e6bf0", "#7a5af5", "#e5484d", "#0ea5a4", "#f5a623",
            "#22b573", "#e8590c", "#c2255c"]


def _fallback_icon(name, size):
    """拿不到真图标时：品牌色圆角块 + 名字首字（不伪造 Logo）"""
    global _fallback_font
    from PIL import Image, ImageDraw, ImageFont
    color = BRAND_FALLBACK.get(name)
    if not color:
        color = _PALETTE[sum(ord(c) for c in name) % len(_PALETTE)]
    im = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([3, 3, 93, 93], radius=28, fill=color)
    if _fallback_font is None:
        for f in ("msyhbd.ttc", "msyh.ttc"):
            try:
                _fallback_font = ImageFont.truetype(
                    "C:/Windows/Fonts/" + f, 46)
                break
            except Exception:
                pass
        if _fallback_font is None:
            _fallback_font = ImageFont.load_default()
    ch = name[0] if name else "?"
    d.text((48, 52), ch, font=_fallback_font, fill="#ffffff", anchor="mm")
    return im.resize((size, size), Image.LANCZOS)


def app_icon(pkg, name, size=44):
    """返回 PhotoImage；本地 icons/<包名>.png 优先，否则品牌色块兜底"""
    key = (pkg, size)
    if key in _img_cache:
        return _img_cache[key]
    ph = None
    from PIL import Image, ImageTk
    p = os.path.join(_ICON_DIR, "%s.png" % pkg)
    if os.path.exists(p):
        try:
            im = Image.open(p).convert("RGBA")
            im = im.resize((size, size), Image.LANCZOS)
            ph = ImageTk.PhotoImage(im)
        except Exception:
            ph = None
    if ph is None:
        ph = ImageTk.PhotoImage(_fallback_icon(name, size))
    _img_cache[key] = ph
    return ph


class AppTile(tk.Canvas):
    """常用 App 磁贴：图标 + 名字，支持点击打开 / 右键菜单 / 添加态"""

    def __init__(self, master, name="", icon=None, command=None, menu=None,
                 add_mode=False, w=150, h=88, cmd_add=None):
        self._tw, self._th = w, h
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bg=master.cget("bg"),
                         cursor="hand2" if not add_mode else "plus")
        self._name, self._icon = name, icon
        self._cmd, self._menu, self._add = command, menu, add_mode
        self._hover = False
        self._draw(CARD, BORDER, INK)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda e: self._draw(
            "#f8faff" if self._hover else CARD,
            "#c9d8f5" if self._hover else BORDER, INK))
        if add_mode:
            self.bind("<Button-1>", lambda e: cmd_add and cmd_add())
        else:
            self.bind("<Button-1>", lambda e: command and command())
            self.bind("<Button-3>", self._pop)
            self.bind("<Button-2>", self._pop)

    def _on_enter(self, e):
        self._hover = True
        self._draw("#f8faff", "#c9d8f5", INK)

    def _on_leave(self, e):
        self._hover = False
        self._draw(CARD, BORDER, INK)

    def _pop(self, e):
        if self._menu:
            self._menu(e)

    def _draw(self, bg, border, fg):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 30 or h < 30:          # 还没布局好就用预设尺寸
            w, h = self._tw, self._th
        if self._add:
            rr(self, 1, 1, w - 2, h - 2, S(14), fill=bg, outline="#b9c6e4",
               width=1, dash=(4, 3))
            cy = h / 2
            self.create_line(w / 2, cy - S(14), w / 2, cy + S(14),
                             fill=SUB, width=2, capstyle="round")
            self.create_line(w / 2 - S(14), cy, w / 2 + S(14), cy,
                             fill=SUB, width=2, capstyle="round")
            self.create_text(w / 2, cy + S(28), text="添加应用",
                             font=(FONT, 9), fill=SUB)
            return
        rr(self, 1, 1, w - 2, h - 2, S(14), fill=bg, outline=border)
        s = S(42)
        self.create_image(w / 2, h / 2 - S(12), image=self._icon)
        self.create_text(w / 2, h / 2 + S(20), text=self._name,
                         font=(FONT, 9), fill=fg)
        self.create_text(w - S(12), S(10), text="⋯", font=(FONT, 10, "bold"),
                         fill="#b6bfd2")


class IconBtnRow(tk.Canvas):
    """底部工具条小按钮：小图标 + 文字"""

    def __init__(self, master, text, glyph, command=None, w=None, h=34):
        f = tkfont.Font(family=FONT, size=9)
        w = w or f.measure(text) + S(46)
        super().__init__(master, width=w, height=h, highlightthickness=0,
                         bg=master.cget("bg"), cursor="hand2")
        self._t, self._g, self._cmd = text, glyph, command
        self._bw, self._bh, self._f = w, h, f
        self._draw(BG, INK)
        self.bind("<Enter>", lambda e: self._draw("#e2e9f5", BLUE))
        self.bind("<Leave>", lambda e: self._draw(BG, INK))
        self.bind("<Button-1>", lambda e: command and command())

    def _draw(self, bg, fg):
        self.delete("all")
        rr(self, 2, 1, self._bw - 2, self._bh - 2, S(9), fill=bg, outline="")
        self._g(self, S(15), self._bh / 2, S(7), fg)
        self.create_text(S(28), self._bh / 2, text=self._t, font=self._f, fill=fg)


# ---------------------------------------------------------------- 设备状态卡

class DeviceCard(tk.Canvas):
    """设备连接信息卡：手机示意图 + 状态 + 型号 + 右侧按钮"""

    def __init__(self, master, on_more, on_connect, h=None):
        self._h = h or S(128)
        super().__init__(master, width=S(200), height=self._h,
                         highlightthickness=0, bg=master.cget("bg"))
        self._on_more, self._on_connect = on_more, on_connect
        self._status, self._color = "未连接", RED
        self._model, self._ver, self._res, self._os = "", "", "", ""
        self.more_btn = Btn(self, "更多  ▾", kind="ghost", command=on_more,
                            width=S(96), height=S(40), size=10, bold=False)
        self.connect_btn = Btn(self, "连接", kind="primary", command=on_connect,
                               width=S(118), height=S(44), size=11)
        self.more_btn.place(relx=1.0, x=-S(26), y=self._h / 2, anchor="e")
        self.connect_btn.place(relx=1.0, x=-S(26) - S(108), y=self._h / 2,
                               anchor="e")
        self.bind("<Configure>", self._redraw)
        self._redraw()

    def set_status(self, text, color):
        self._status, self._color = text, color
        self._redraw()

    def set_info(self, model, ver, res, os_name=""):
        self._model, self._ver, self._res, self._os = model, ver, res, os_name
        self._redraw()

    def _redraw(self, e=None):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        rr(self, 1, 1, w - 2, h - 2, S(18), fill=CARD, outline=BORDER)
        ph = S(92)
        draw_phone(self, S(26), (h - ph) / 2, S(26) + ph * 0.5,
                   (h + ph) / 2)
        tx = S(26) + ph * 0.5 + S(28)
        connected = self._status == "已连接"
        dot = self._color
        self.create_oval(tx, h / 2 - S(26) - S(5), tx + S(10),
                         h / 2 - S(26) + S(5), fill=dot, outline="")
        self.create_text(tx + S(18), h / 2 - S(26), text=self._status,
                         anchor="w", font=(FONT, 13, "bold"), fill=dot)
        self.create_text(tx, h / 2 + S(4), anchor="w",
                         text=self._model or "尚未连接手机",
                         font=(FONT, 12, "bold"), fill=INK if self._model else SUB)
        sub = self._sub_line()
        self.create_text(tx, h / 2 + S(30), anchor="w", text=sub,
                         font=(FONT, 9), fill=SUB)

    def _sub_line(self):
        bits = []
        if self._os:
            bits.append(self._os)
        if self._ver:
            bits.append("Android " + self._ver)
        if self._res:
            bits.append(self._res)
        return "  ｜  ".join(bits) if bits else "从右上角【连接】，或【更多】里配对手机"


# ---------------------------------------------------------------- 大功能卡

class ActionCard(tk.Canvas):
    """首页三张大功能卡"""

    def __init__(self, master, title, sub, glyph, style="light",
                 accent=BLUE, tile_bg=BLUE_L, command=None, h=None):
        self._h = h or S(104)
        super().__init__(master, width=S(150), height=self._h,
                         highlightthickness=0, bg=master.cget("bg"),
                         cursor="hand2")
        self._title, self._sub, self._glyph = title, sub, glyph
        self._style, self._accent, self._tile_bg = style, accent, tile_bg
        self._cmd = command
        self.bind("<Configure>", lambda e: self._redraw(False))
        self.bind("<Enter>", lambda e: self._redraw(True))
        self.bind("<Leave>", lambda e: self._redraw(False))
        self.bind("<Button-1>", lambda e: command and command())
        self._redraw(False)

    def _redraw(self, hover):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        if self._style == "primary":
            top, bot = (BLUE_MID, BLUE) if not hover else ("#3f7bff", BLUE_D)
            gradient_rr(self, 1, 1, w - 2, h - 2, S(18), top, bot)
            fg, sub = "#ffffff", "#d8e4ff"
            tile_bg, glyph_c = "#3d76f2", "#ffffff"
        else:
            bg = "#fbfdff" if hover else CARD
            rr(self, 1, 1, w - 2, h - 2, S(18), fill=bg,
               outline=self._accent if hover else BORDER)
            fg, sub = INK, SUB
            tile_bg, glyph_c = self._tile_bg, self._accent
        s = S(54)
        rr(self, S(24), h / 2 - s / 2, S(24) + s, h / 2 + s / 2, S(15),
           fill=tile_bg, outline="")
        self._glyph(self, S(24) + s / 2, h / 2, s * 0.3, glyph_c)
        tx = S(24) + s + S(20)
        self.create_text(tx, h / 2 - S(13), anchor="w", text=self._title,
                         font=(FONT, 13, "bold"), fill=fg)
        self.create_text(tx, h / 2 + S(15), anchor="w", text=self._sub,
                         font=(FONT, 9), fill=sub)
        r = S(15)
        draw_arrow_badge(self, w - S(34), h / 2, r,
                         "#ffffff" if self._style == "primary" else BLUE_L,
                         BLUE)


# ---------------------------------------------------------------- 弹窗与菜单

def dialog(win, title=None, w=None, h=None):
    """统一弹窗外观：浅色底 + 标题"""
    if title:
        win.title(title)
    win.configure(bg=BG)
    if w and h:
        win.geometry("%dx%d" % (w, h))
    return win


def dialog_body(win, radius=16):
    """弹窗内容区：白卡片 + 内部留白，返回可以直接 pack/grid 的容器"""
    card = Card(win, radius=radius)
    card.pack(fill="both", expand=True, padx=S(18), pady=S(16))
    pad = tk.Frame(card.inner, bg=CARD)
    pad.pack(fill="both", expand=True, padx=S(20), pady=S(16))
    return pad


def menu(master):
    """统一右键 / 下拉菜单配色"""
    return tk.Menu(master, tearoff=0, font=(FONT, 10), bg=CARD, fg=INK,
                   activebackground=BLUE_L, activeforeground=BLUE,
                   bd=0, activeborderwidth=0, relief="flat")


def heading(parent, text):
    return tk.Label(parent, text=text, bg=CARD, fg=INK,
                    font=(FONT, 12, "bold"), anchor="w")


def note(parent, text, size=9):
    return tk.Label(parent, text=text, bg=CARD, fg=SUB, font=(FONT, size),
                    anchor="w", justify="left", wraplength=520)


# ---------------------------------------------------------------- 可滚动页面

class ScrollPage(tk.Frame):
    """内容高过窗口就自动出滚动条（鼠标滚轮可滚）"""

    def __init__(self, master, bg=BG, maxh=None):
        super().__init__(master, bg=bg)
        self._maxh = maxh
        self._busy = False
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, height=1,
                            yscrollcommand=self._on_scroll)
        self.sb = tk.Scrollbar(self, orient="vertical", command=self.cv.yview,
                               bg=bg, troughcolor=bg, activebackground="#c3cee4",
                               borderwidth=0, width=S(10), relief="flat")
        self.cv.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.cv, bg=bg)
        self._win = self.cv.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._update)
        self.cv.bind("<Configure>", self._update)
        self._shown = False
        self._bind(self.cv)
        self._bind(self.body)

    def _on_scroll(self, a, b):
        """滚动条回调里不能再改高度，否则和配置事件互相触发会死循环"""
        self.sb.set(a, b)
        if not self._busy:
            self._toggle_sb()

    def _toggle_sb(self):
        bb = self.cv.bbox("all")
        h = self.cv.winfo_height()
        need = bool(bb and bb[3] > h + 4) if h > 4 else False
        if need != self._shown:
            self._shown = need
            if need:
                self.sb.pack(side="right", fill="y")
            else:
                self.sb.pack_forget()

    def _update(self, e=None):
        if self._busy:
            return
        self._busy = True
        try:
            w = self.cv.winfo_width()
            if w > 4:
                self.cv.itemconfig(self._win, width=w)
            bb = self.cv.bbox("all")
            if self._maxh:
                h = max(1, min(bb[3], self._maxh)) if bb else 1
                if abs(self.cv.winfo_height() - h) > 1:
                    self.cv.configure(height=h)
                if abs(self.winfo_height() - h) > 1:
                    self.configure(height=h)
            self._toggle_sb()
        finally:
            self._busy = False

    def _bind(self, w):
        try:
            w.bind("<MouseWheel>", self._wheel, add="+")
            w.bind("<Button-4>", lambda e: self.cv.yview_scroll(-1, "units"))
            w.bind("<Button-5>", lambda e: self.cv.yview_scroll(1, "units"))
        except Exception:
            pass

    def bind_children(self):
        """给新增的内容补上滚轮事件"""
        def walk(w):
            self._bind(w)
            for c in w.winfo_children():
                walk(c)
        walk(self.body)

    def _wheel(self, e):
        self.cv.yview_scroll(int(-1 * (e.delta / 120)) or -1, "units")
        return "break"


class LinkBtn(tk.Label):
    """文字链接按钮：默认灰，鼠标移上去变蓝加下划线"""

    def __init__(self, master, text, command=None, bg=BG, size=10):
        f = tkfont.Font(family=FONT, size=size)
        fu = tkfont.Font(family=FONT, size=size, underline=True)
        super().__init__(master, text=text, bg=bg, fg=SUB, font=f,
                         cursor="hand2", padx=S(2), pady=S(2))
        self._f, self._fu = f, fu
        self._cmd = command
        self.bind("<Enter>", lambda e: self.config(fg=BLUE, font=self._fu))
        self.bind("<Leave>", lambda e: self.config(fg=SUB, font=self._f))
        self.bind("<Button-1>", lambda e: command and command())
