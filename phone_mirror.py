# -*- coding: utf-8 -*-
"""
手机投屏助手  (Windows 电脑无线控制小米/安卓手机)

底层：scrcpy 5.0.1 + adb（随工具附带）
功能：无线连接、手机窗口、常用 App 独立窗口、电脑二维码发到手机扫、
      高清录屏保存到电脑。
"""

import ctypes
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, scrolledtext

from pm_tools import (ADB, APP_DIR, LOG_DIR, REC_DIR, REC_TMP, SHOT_DIR,
                      SCRCPY, Device, capture_region, capture_window,
                      close_window, ensure_bin, ensure_dpi_aware, find_window,
                      is_maximized, load_cfg, make_tool_window, place_window,
                      raise_window, run, save_cfg, scrcpy_args,
                      set_window_topmost, unwatch_window_move,
                      virtual_display_ids, watch_window_move, window_rect,
                      window_shown)
import ui_kit as U

FONT = U.FONT
BG = U.BG
CARD = U.CARD
INK = U.INK
SUB = U.SUB
ACCENT = U.BLUE
GREEN = U.GREEN
RED = U.RED

KNOWN_APPS = [
    ("微信", "com.tencent.mm"), ("抖音", "com.ss.android.ugc.aweme"),
    ("小红书", "com.xingin.xhs"), ("支付宝", "com.eg.android.AlipayGphone"),
    ("淘宝", "com.taobao.taobao"), ("QQ", "com.tencent.mobileqq"),
    ("企业微信", "com.tencent.wework"), ("哔哩哔哩", "tv.danmaku.bili"),
    ("微博", "com.sina.weibo"), ("京东", "com.jingdong.app.mall"),
    ("高德地图", "com.autonavi.minimap"), ("知乎", "com.zhihu.android"),
    ("快手", "com.smile.gifmaker"), ("闲鱼", "com.taobao.idlefish"),
    ("拼多多", "com.xunmeng.pinduoduo"), ("美团", "com.sankuai.meituan"),
    ("腾讯视频", "com.tencent.qqlive"), ("网易云音乐", "com.netease.cloudmusic"),
    ("百度网盘", "com.baidu.netdisk"), ("小米商城", "com.xiaomi.shop"),
    ("设置", "com.android.settings"), ("相册", "com.miui.gallery"),
]

WM_CLOSE = 0x0010
user32 = ctypes.windll.user32


def win_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


# ---------------------------------------------------------------- 屏幕框选

class Snipper:
    """全屏半透明遮罩，拖鼠标画框，返回屏幕坐标"""

    def __init__(self, master, on_done):
        self.on_done = on_done
        self.start = None
        self.scale = 1.0
        self.origin = (0, 0)

        self.top = tk.Toplevel(master)
        self.top.title("_snipper_")     # 不然会继承主窗口标题，按标题找窗口会抓错
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-alpha", 0.28)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="#000000")
        self.cv = tk.Canvas(self.top, bg="#000000", cursor="crosshair",
                            highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.top.update_idletasks()

        try:
            l, t, r, b = win_rect(self.top.winfo_id())
            w = float(self.cv.winfo_width() or 1)
            self.scale = (r - l) / w
            self.origin = (l, t)
        except Exception:
            self.scale, self.origin = 1.0, (0, 0)

        hint = tk.Label(self.top, text="按住左键拖一个框，把二维码框进去      按 Esc 取消",
                        bg="#111827", fg="#ffffff", font=(FONT, 12), padx=16, pady=8)
        hint.place(relx=0.5, rely=0.03, anchor="n")

        self.cv.bind("<Button-1>", self.down)
        self.cv.bind("<B1-Motion>", self.move)
        self.cv.bind("<ButtonRelease-1>", self.up)
        self.top.bind("<Escape>", lambda e: self.top.destroy())

    def phys(self, x, y):
        l, t = self.origin
        return int(l + x * self.scale), int(t + y * self.scale)

    def local(self, px, py):
        l, t = self.origin
        return (px - l) / self.scale, (py - t) / self.scale

    def down(self, e):
        self.start = self.phys(e.x, e.y)
        self.cv.delete("sel")

    def move(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = self.phys(e.x, e.y)
        ax, ay = self.local(x1, y1)
        self.cv.delete("sel")
        self.cv.create_rectangle(ax, ay, e.x, e.y, outline="#2563eb", width=2,
                                 fill="#2563eb", stipple="gray25", tags="sel")
        self.cv.create_text(e.x + 10, e.y + 10,
                            text="%d x %d" % (abs(x2 - x1), abs(y2 - y1)),
                            fill="#ffffff", font=(FONT, 10), anchor="nw", tags="sel")

    def up(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = self.phys(e.x, e.y)
        box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        self.top.destroy()
        if abs(box[2] - box[0]) > 10 and abs(box[3] - box[1]) > 10:
            self.on_done(box)


# ---------------------------------------------------------------- 单个窗口

class Session:
    """一个手机窗口，带自己的侧边工具栏：截图 / 录屏 / 置顶 / 关闭"""

    def __init__(self, app, name, title, kind, proc, args):
        self.app = app            # App 实例
        self.name = name
        self.title = title        # scrcpy 窗口标题（唯一）
        self.kind = kind          # main=手机主屏, virtual=独立虚拟屏
        self.proc = proc
        self.args = list(args)    # 原始启动参数，录完要用来把窗口恢复回来
        self.recording = False
        self.display_id = None
        self.bar = None
        self.rec_proc = None
        self.rec_tmp = ""
        self.rec_title = ""
        self.rec_start = 0
        self.top = False
        self.rec_btn = None
        self.top_btn = None
        self.win_rect = None      # 录屏重开窗口时用来把位置大小恢复回来
        self.maximized = False
        self.last_rect = None
        self.build_bar()

    def bind_hook(self, tries=6):
        """窗口刚建好时先跟一次，位置由 tick_sessions 持续校正"""
        if find_window(self.title):
            self.follow()
        elif tries > 0:
            self.app.root.after(1000, lambda: self.bind_hook(tries - 1))

    def unhook(self):
        try:
            unwatch_window_move(find_window(self.title))
        except Exception:
            pass

    # ---- 侧边栏
    def build_bar(self):
        w = tk.Toplevel(self.app.root)
        w.title("_sidebar_")            # 不然会继承主窗口标题，按标题找窗口会抓错
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg="#111827")
        self.bar = w
        tk.Label(w, text=self.name, bg="#111827", fg="#ffffff",
                 font=(FONT, 9, "bold"), wraplength=80, pady=5).pack()

        def mk(text, cmd, bg="#1f2937"):
            b = tk.Button(w, text=text, command=cmd, bg=bg, fg="#ffffff",
                          relief="flat", font=(FONT, 9), width=9, pady=3,
                          activebackground="#374151", activeforeground="#ffffff")
            b.pack(padx=6, pady=2)
            return b

        mk("截图", self.shot)
        self.rec_btn = mk("录屏", self.toggle_rec, "#2563eb")
        self.top_btn = mk("置顶", self.toggle_top)
        mk("关闭", self.close, "#7f1d1d")
        w.update_idletasks()
        self.follow()
        self.app.root.after(1200, self.bind_hook)   # 等 scrcpy 窗口真的建好

    def follow(self):
        """贴在窗口右边，跟着窗口走；窗口最小化/隐藏时侧边栏一起消失"""
        if not self.bar:
            return
        try:
            if not window_shown(self.title):
                if self.bar.state() != "withdrawn":
                    self.bar.withdraw()
                return
            if self.bar.state() == "withdrawn":
                self.bar.deiconify()
        except Exception:
            pass
        try:
            r = window_rect(self.title)
            if not r or r[2] <= 0:
                return
            w = self.bar.winfo_width() or 85
            sw = self.bar.winfo_screenwidth()
            x = r[2] + 8
            if x + w > sw - 4:          # 右边出界了，改贴左边
                x = max(0, r[0] - w - 8)
            y = max(0, min(r[1] + 4, self.bar.winfo_screenheight() - 200))
            self.bar.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def destroy_bar(self):
        if self.bar:
            try:
                self.bar.destroy()
            except Exception:
                pass
            self.bar = None

    def safe_name(self):
        return "".join(c for c in self.name if c not in '\\/:*?"<>|')

    # ---- 截图（截的就是这个窗口里看到的画面）
    def shot(self):
        self.app.bg("截图", self._shot)

    def _shot(self):
        p = os.path.join(SHOT_DIR, "%s_%s.png"
                         % (self.safe_name(), time.strftime("%m%d_%H%M%S")))
        try:
            capture_window(self.title, p)
        except Exception as e:
            self.app.log("截图失败：%s" % e)
            self.app.ui(lambda: messagebox.showerror("截图失败", str(e)))
            return
        kb = os.path.getsize(p) / 1024
        self.app.log("截图（%s）%.0f KB -> %s" % (self.name, kb, p))
        self.app.ui(lambda: messagebox.showinfo("截图好了", "存到：\n%s" % p))

    # ---- 录屏
    def toggle_rec(self):
        if self.rec_proc and self.rec_proc.poll() is None:
            self.stop_rec()
        else:
            self.start_rec()

    def start_rec(self):
        """
        录这个窗口里的内容：在右下角另开一个很小的录制窗口来录同一块屏。
        你现在这个窗口完全不动 —— 不关、不重开、App 也不会重新进入。
        （实测：两个投屏连接可以各连一块屏 / 同一块屏，互不干扰；
          录制窗口必须保持显示，藏起来或最小化就录不到画面，所以才做成小窗放角落）
        """
        serial = self.app.dev.target()
        if not serial:
            return
        did = self.display_id
        if self.kind == "virtual" and not did:
            ids = virtual_display_ids(serial)
            did = max(ids) if ids else None
        tmp = os.path.join(REC_TMP, "rec_%d.mp4" % int(time.time()))
        cfg = dict(self.app.cfg)
        cfg["recording"] = True
        cfg["window_width"] = 260                       # 小窗口，不挡视线
        sw = self.app.root.winfo_screenwidth()
        sh = self.app.root.winfo_screenheight()
        cfg["window_geometry"] = (max(0, sw - 285), max(0, sh - 620))
        extra = ["--display-id=%d" % did] if did else []
        self.rec_title = "● 录制中 - %s" % self.name
        args = scrcpy_args(serial, cfg, extra=extra, title=self.rec_title,
                           record=tmp)
        p = self.app.launch(args, "录屏(%s)" % self.name, watch=False)
        if not p:
            return
        self.rec_proc = p
        self.rec_tmp = tmp
        self.rec_start = time.time()
        self.recording = True
        if self.rec_btn:
            self.rec_btn.config(text="停止录制", bg="#dc2626")
        self.app.log("【%s】开始录制（右下角那个小窗口是录制用的，别关它）" % self.name)
        threading.Thread(target=self._wait_rec, args=(p,), daemon=True).start()
        threading.Thread(target=self._tidy_rec_win, daemon=True).start()

    def _tidy_rec_win(self):
        """录制窗口出来后：变成工具窗口（不占任务栏），挪到角落"""
        for _ in range(40):
            if find_window(self.rec_title):
                break
            time.sleep(0.3)
        make_tool_window(self.rec_title)
        sw = self.app.root.winfo_screenwidth()
        sh = self.app.root.winfo_screenheight()
        place_window(self.rec_title, (max(0, sw - 285), max(0, sh - 620), sw, sh))

    def close_proc(self):
        """正常关掉这个窗口（让 scrcpy 收尾）"""
        self.unhook()
        close_window(self.title)
        for _ in range(40):
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        time.sleep(0.6)

    def _wait_rec(self, p):
        p.wait()
        self.app.q.put(("sessrec", (self, self.rec_tmp)))

    def stop_rec(self):
        """关掉那个小的录制窗口，scrcpy 会把视频收尾（你自己这个窗口不受影响）"""
        if not self.rec_proc or self.rec_proc.poll() is not None:
            return
        close_window(self.rec_title)
        for _ in range(50):
            if self.rec_proc.poll() is not None:
                break
            time.sleep(0.1)
        if self.rec_proc.poll() is None:
            try:
                self.rec_proc.terminate()
            except Exception:
                pass

    # ---- 置顶 / 关闭
    def toggle_top(self):
        self.top = not self.top
        set_window_topmost(self.title, self.top)
        if self.top_btn:
            self.top_btn.config(bg="#2563eb" if self.top else "#1f2937")
        self.app.log("【%s】置顶：%s" % (self.name, "开" if self.top else "关"))

    def close(self):
        self.unhook()
        if self.rec_proc and self.rec_proc.poll() is None:
            self.stop_rec()
            time.sleep(1.2)
        close_window(self.title)
        for _ in range(40):
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.destroy_bar()


# ---------------------------------------------------------------- 主界面

class App:

    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.dev = Device(self.log)
        self.dev.serial = self.cfg.get("serial", "")
        self.q = queue.Queue()
        self.procs = []
        self.apps_cache = []
        self.rec_proc = None
        self.rec_start = 0
        self.rec_tmp = ""
        self.rec_label = ""
        self.rec_title = ""
        self.rec_float = None
        self.float_var = None
        self.sessions = []
        self.seq = 0

        root.title("手机投屏助手 · 让手机内容，更大更好看")
        root.geometry("1180x900")
        root.minsize(1020, 760)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        U.set_scale(root)
        self.style()
        self.build()
        self.poll()
        self.tick_sessions()

        if not ensure_bin():
            self.log("[错误] 缺少 scrcpy/adb，请确认 vendor 目录完整")
        else:
            self.log("组件就绪")
            if self.dev.serial:
                self.log("上次连的手机：%s，正在自动连接…" % self.dev.serial)
                self.bg("自动连接", self.auto_connect)
            else:
                self.log("第一次用？点右上角【更多】→【配对手机】")

    # ------------------------------------------------------------ 基础
    def log(self, msg):
        self.q.put(("log", "[%s] %s" % (time.strftime("%H:%M:%S"), msg)))

    def status(self, text, color):
        self.q.put(("status", (text, color)))

    def bg(self, name, fn):
        def wrap():
            try:
                fn()
            except Exception as e:
                self.log("[错误] %s: %s" % (name, e))
        threading.Thread(target=wrap, daemon=True).start()

    def ui(self, fn):
        self.root.after(0, fn)

    def poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log":
                    self.logbox.insert("end", v + "\n")
                    self.logbox.see("end")
                elif k == "status":
                    self.apply_status(v[0], v[1])
                elif k == "dev":
                    self.apply_dev(v)
                elif k == "devcard":
                    self.apply_devcard(v)
                elif k == "rec":
                    self.on_rec_done(v)
                elif k == "sessrec":
                    self.on_session_rec_done(v)
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    # ---- 状态刷新（设备卡片 + 底栏 + 按钮文字）
    def apply_status(self, text, color):
        self.status_var.set(text)
        self._connected = (text == "已连接")
        if hasattr(self, "devcard"):
            self.devcard.set_status(text, color)
        if hasattr(self, "connect_btn"):
            self.connect_btn.set_text("断开连接" if self._connected else "连接")
            self.connect_btn._cmd = self.disconnect if self._connected \
                else self.auto_connect
        if hasattr(self, "foot_dot"):
            self.foot_dot.config(fg=color)
            self._foot()

    def _foot(self):
        st = self.status_var.get()
        m = getattr(self, "_model", "")
        self.foot_var.set(("%s · %s" % (st, m)) if (self._connected and m)
                          else st)

    def apply_dev(self, text):
        self.dev_var.set(text)
        if "还没有" in text or not text:
            if hasattr(self, "devcard"):
                self.devcard.set_info("", "", "")
        else:
            parts = text.split()
            model = " ".join(parts[:2]) if len(parts) >= 2 else text
            ver = parts[-1] if len(parts) >= 3 else ""
            if hasattr(self, "devcard"):
                self.devcard.set_info(model, ver, "")
        self._model = model
        self._foot()

    def apply_devcard(self, d):
        self._model = (d.get("model") or "").strip()
        self.dev_var.set(self._model)
        self.devcard.set_info(d.get("model", ""), d.get("ver", ""),
                              d.get("res", ""), d.get("os", ""))
        self._foot()

    def style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", font=(FONT, 10), background=BG, foreground=INK)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=INK)
        s.configure("Card.TLabel", background=CARD, foreground=INK)
        s.configure("Sub.TLabel", background=CARD, foreground=SUB, font=(FONT, 9))
        s.configure("Head.TLabel", background=CARD, foreground=INK, font=(FONT, 12, "bold"))
        s.configure("TButton", font=(FONT, 10), padding=5)
        s.configure("Big.TButton", font=(FONT, 11, "bold"), padding=(10, 9))
        s.configure("Accent.TButton", font=(FONT, 11, "bold"), padding=(10, 9),
                    background=ACCENT, foreground="#ffffff")
        s.configure("TCheckbutton", background=CARD, foreground=INK)

    # ------------------------------------------------------------ 布局
    def build(self):
        r = self.root

        # ---- 顶部标题
        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=U.S(28), pady=(U.S(16), U.S(12)))
        logo = tk.Canvas(head, width=U.S(36), height=U.S(36), bg=BG,
                         highlightthickness=0)
        logo.pack(side="left")
        U.rr(logo, 1, 1, U.S(35), U.S(35), U.S(10), fill=U.BLUE, outline="")
        U.glyph_window(logo, U.S(18), U.S(18), U.S(8), "#ffffff")
        box = tk.Frame(head, bg=BG)
        box.pack(side="left", padx=(U.S(12), 0))
        tk.Label(box, text="手机投屏助手", font=(FONT, 16, "bold"),
                 bg=BG, fg=INK).pack(side="left")
        tk.Label(box, text="  ·  让手机内容，更大更好看", font=(FONT, 10),
                 bg=BG, fg=SUB).pack(side="left", pady=(U.S(6), 0))
        # 右上角：日志 / 设置 / 帮助（文字链接）
        bar = tk.Frame(head, bg=BG)
        bar.pack(side="right")
        for txt, cmd in (("日志", self.toggle_log),
                         ("设置", self.settings),
                         ("帮助", self.help_win)):
            U.LinkBtn(bar, txt, command=cmd).pack(side="left", padx=U.S(10))

        # ---- 固定区：设备状态卡（不随内容滚动）
        self.status_var = tk.StringVar(value="未连接")
        self.dev_var = tk.StringVar(value="还没有连接手机")
        self.devcard = U.DeviceCard(r, self.more_menu, self.connect_action)
        self.devcard.pack(fill="x", padx=U.S(24))
        self.apply_status("未连接", RED)

        # ---- 固定区：三张大功能卡（不随内容滚动）
        acts = tk.Frame(r, bg=BG)
        acts.pack(fill="x", padx=U.S(24), pady=(U.S(14), 0))
        U.ActionCard(acts, "打开手机窗口", "在电脑上操作手机，更方便",
                     U.glyph_window, style="primary",
                     command=self.open_mirror).pack(
            side="left", fill="x", expand=True, padx=(0, U.S(14)))
        U.ActionCard(acts, "录屏", "高清录制手机屏幕", U.glyph_record,
                     accent=U.BLUE, tile_bg=U.BLUE_L,
                     command=self.record_dialog).pack(
            side="left", fill="x", expand=True, padx=(0, U.S(14)))
        U.ActionCard(acts, "电脑上扫码", "快速打开手机页面", U.glyph_qr,
                     accent=U.PURPLE, tile_bg=U.PURPLE_L,
                     command=self.scan_dialog).pack(
            side="left", fill="x", expand=True)

        # ---- 录制条 / 窗口列表（内容多了这里自己滚，最多占 200px）
        self.page = U.ScrollPage(r, maxh=U.S(200))
        self.page.pack(fill="x", padx=U.S(24), pady=(U.S(12), 0))
        r = self.page.body

        # ---- 录制条容器（保证顺序：录制条在上，窗口列表在下）
        self.rec_holder = tk.Frame(r, bg=BG)
        self.rec_holder.pack(fill="x")
        self.recbar = U.Card(self.rec_holder, radius=U.S(14))
        rb = self.recbar.inner
        tk.Label(rb, text="●  录制中", fg=U.RED, bg=CARD,
                 font=(FONT, 11, "bold")).pack(
            side="left", padx=(U.S(8), U.S(12)), pady=U.S(4))
        self.rec_var = tk.StringVar(value="")
        tk.Label(rb, textvariable=self.rec_var, bg=CARD, fg=SUB,
                 font=(FONT, 9)).pack(side="left")
        U.Btn(rb, "结束并保存", kind="danger", command=self.stop_record,
              width=U.S(118), height=U.S(34), size=9).pack(
            side="right", padx=(U.S(12), U.S(4)))

        # ---- 已打开的窗口（有窗口时才显示）
        self.win_holder = tk.Frame(r, bg=BG)
        self.win_holder.pack(fill="x")
        self.win_card = U.Card(self.win_holder, radius=U.S(14))
        wb = self.win_card.inner
        tk.Label(wb, text="已打开的窗口（每个窗口右边有自己的工具栏）",
                 font=(FONT, 9), bg=CARD, fg=SUB).pack(
            anchor="w", padx=U.S(8), pady=(U.S(6), 0))
        self.win_box = tk.Frame(wb, bg=CARD)
        self.win_box.pack(fill="x", padx=U.S(8), pady=(U.S(2), U.S(6)))
        self.win_sig = ""
        self.render_wins()

        # 后面的模块固定在主窗口上（不随内容滚动）
        r = self.root

        # ---- 常用 App（模块固定，只有图标网格内部能滚）
        a = tk.Frame(r, bg=BG)
        a.pack(fill="both", expand=True, padx=U.S(24), pady=(U.S(14), 0))
        self.apps_frame = a
        apps_card = U.Card(a, radius=U.S(18), fit=False)
        apps_card.configure(height=U.S(200))     # 最小高度，有空间就撑开
        apps_card.pack(fill="both", expand=True)
        ac = apps_card.inner
        h2 = tk.Frame(ac, bg=CARD)
        h2.pack(fill="x", padx=U.S(10), pady=(U.S(8), 0))
        tk.Label(h2, text="常用 App", font=(FONT, 12, "bold"),
                 bg=CARD, fg=INK).pack(side="left")
        tk.Label(h2, text="    右键可移除", font=(FONT, 9),
                 bg=CARD, fg=SUB).pack(side="left", pady=(U.S(4), 0))
        U.Btn(h2, "＋  添加 / 管理", kind="outline", command=self.app_picker,
              height=U.S(34), size=9).pack(side="right")
        self.app_grid = U.ScrollPage(ac)
        self.app_grid.pack(fill="both", expand=True,
                           padx=U.S(8), pady=(U.S(8), U.S(8)))
        self.app_box = tk.Frame(self.app_grid.body, bg=CARD)
        self.app_box.pack(fill="both", expand=True)
        self.app_box.bind("<Configure>", self._apps_maybe_rerender)
        self._app_cols = 0
        self.render_apps()
        self.apps_card = apps_card

        # ---- 小提示条
        self.tip_card = U.Card(r, radius=U.S(14), bg=U.BLUE_L, border=U.BLUE_L2)
        tb = self.tip_card.inner
        ic = tk.Canvas(tb, width=U.S(24), height=U.S(24), bg=U.BLUE_L,
                       highlightthickness=0)
        ic.pack(side="left", padx=(U.S(8), U.S(4)), pady=U.S(4))
        U.glyph_bulb(ic, U.S(12), U.S(11), U.S(7), U.BLUE)
        tk.Label(tb, text="小提示：支持键盘鼠标在电脑上直接操作手机，投屏更稳定，画面更清晰。",
                 bg=U.BLUE_L, fg="#3a5fa8", font=(FONT, 9)).pack(
            side="left", pady=U.S(4))
        x = tk.Label(tb, text="✕", bg=U.BLUE_L, fg=SUB, font=(FONT, 10),
                     cursor="hand2")
        x.pack(side="right", padx=U.S(12))
        x.bind("<Button-1>", lambda e: self.tip_card.pack_forget())
        self.tip_card.pack(fill="x", padx=U.S(24), pady=(U.S(14), 0))

        # ---- 底部状态条
        self.bottom_bar = tk.Frame(self.root, bg=BG)
        self.bottom_bar.pack(fill="x", padx=U.S(28), pady=(U.S(6), U.S(12)))
        self.foot_var = tk.StringVar(value="未连接")
        self.foot_dot = tk.Label(self.bottom_bar, text="●", fg=U.RED, bg=BG,
                                 font=(FONT, 10))
        self.foot_dot.pack(side="left")
        tk.Label(self.bottom_bar, textvariable=self.foot_var, bg=BG, fg=SUB,
                 font=(FONT, 9)).pack(side="left", padx=(U.S(6), 0))
        self.log_shown = False
        self.page.bind_children()
        self.app_grid.bind_children()
        self._build_log_panel()

    # --------------------------------------------------------- 日志面板
    def _build_log_panel(self):
        """固定高度的小黑框日志，自己带滚动条，不撑开主界面"""
        p = tk.Frame(self.root, bg=BG)
        self.log_panel = p
        hdr = tk.Frame(p, bg=BG)
        hdr.pack(fill="x", padx=U.S(2), pady=(0, U.S(5)))
        tk.Label(hdr, text="运行日志", font=(FONT, 10, "bold"), bg=BG,
                 fg=INK).pack(side="left")
        tk.Label(hdr, text="  程序每一步做了什么，都记在这里",
                 font=(FONT, 9), bg=BG, fg=SUB).pack(side="left", pady=(U.S(2), 0))
        x = tk.Label(hdr, text="收起 ✕", font=(FONT, 9), bg=BG, fg=U.BLUE,
                     cursor="hand2")
        x.pack(side="right")
        x.bind("<Button-1>", lambda e: self.toggle_log())

        wrap = tk.Frame(p, bg="#0f172a", height=U.S(132))
        wrap.pack(fill="x")
        wrap.pack_propagate(False)
        self.logbox = scrolledtext.ScrolledText(
            wrap, font=(FONT, 9), bg="#0f172a", fg="#c9d4ea",
            insertbackground="#c9d4ea", relief="flat", bd=0,
            highlightthickness=0, padx=U.S(8), pady=U.S(6),
            wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=1, pady=1)
        try:
            self.logbox.vbar.configure(bg="#1e293b", troughcolor="#0f172a",
                                       activebackground="#334155",
                                       borderwidth=0, width=U.S(10),
                                       relief="flat")
        except Exception:
            pass

    def connect_action(self):
        if getattr(self, "_connected", False):
            self.disconnect()
        else:
            self.bg("连接", self.auto_connect)

    def _apps_maybe_rerender(self, e=None):
        w = self.app_box.winfo_width()
        if w < 50:
            return
        cols = max(2, min(6, w // max(1, U.S(160))))
        if cols != self._app_cols:
            self._app_cols = cols
            self.render_apps()

    def toggle_log(self):
        if self.log_shown:
            self.log_panel.pack_forget()
            self.log_shown = False
        else:
            self.log_panel.pack(fill="x", padx=U.S(28),
                                pady=(U.S(10), 0), before=self.bottom_bar)
            self.log_shown = True

    def more_menu(self):
        m = U.menu(self.root)
        m.add_command(label="配对手机（首次使用）", command=self.wizard)
        m.add_command(label="重新搜索 / 连接", command=lambda: self.bg("连接", self.auto_connect))
        m.add_command(label="断开连接", command=self.disconnect)
        m.add_separator()
        m.add_command(label="停止所有手机窗口", command=self.stop_all)
        m.add_command(label="截手机整屏（不看 App 窗口）", command=self.screenshot)
        m.add_command(label="手机输入法设置", command=self.ime_settings)
        m.add_separator()
        m.add_command(label="打开录屏文件夹", command=lambda: self.open_path(REC_DIR))
        m.add_command(label="打开截图文件夹", command=lambda: self.open_path(SHOT_DIR))
        b = self.devcard.more_btn
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height() + 6)

    # ---------------------------------------------------- 已打开窗口列表
    def render_wins(self, force=False):
        """列出打开的窗口，方便在多窗口里找到 / 提到前面"""
        sig = "|".join("%s:%s" % (s.name, s.recording) for s in self.sessions)
        if sig == self.win_sig and not force:
            return
        self.win_sig = sig
        for w in self.win_box.winfo_children():
            w.destroy()
        if not self.sessions:
            self.win_card.pack_forget()
            return
        if not self.win_card.winfo_ismapped():
            self.win_card.pack(fill="x", pady=(U.S(12), 0))
        for s in self.sessions:
            row = tk.Frame(self.win_box, bg=CARD)
            row.pack(fill="x", pady=U.S(3))
            tk.Label(row, text="●", fg=U.RED if s.recording else U.GREEN,
                     bg=CARD, font=(FONT, 10)).pack(side="left")
            tk.Label(row, text=s.name, bg=CARD, fg=INK,
                     font=(FONT, 10, "bold")).pack(side="left", padx=(U.S(8), 0))
            U.Btn(row, "提到前面", kind="ghost", width=U.S(84), height=U.S(30),
                  size=9, command=lambda x=s: raise_window(x.title)).pack(
                side="left", padx=(U.S(16), U.S(6)))
            U.Btn(row, "截图", kind="ghost", width=U.S(64), height=U.S(30),
                  size=9, command=lambda x=s: x.shot()).pack(
                side="left", padx=U.S(6))
            U.Btn(row, "停止录制" if s.recording else "录屏",
                  kind="danger" if s.recording else "tonal", width=U.S(84),
                  height=U.S(30), size=9,
                  command=lambda x=s: x.toggle_rec()).pack(
                side="left", padx=U.S(6))
            U.Btn(row, "关闭", kind="ghost", width=U.S(64), height=U.S(30),
                  size=9, command=lambda x=s: self.bg("关闭窗口", x.close)).pack(
                side="left", padx=U.S(6))
            if hasattr(self, "page"):
                self.page.bind_children()

    # ------------------------------------------------------------ 常用 App
    def render_apps(self):
        for w in self.app_box.winfo_children():
            w.destroy()
        apps = self.cfg.get("apps") or []
        cols = self._app_cols or 5
        if not apps:
            tk.Label(self.app_box, bg=CARD, fg=SUB, font=(FONT, 10),
                     text="还没有常用 App，点右上角【＋ 添加 / 管理】从手机里挑几个"
                     ).grid(row=0, column=0, columnspan=cols, pady=U.S(26))
            self.app_box.columnconfigure(0, weight=1)
            return
        for i, item in enumerate(apps):
            icon = U.app_icon(item["pkg"], item["name"], U.S(40))
            t = U.AppTile(self.app_box, name=item["name"], icon=icon,
                          w=U.S(150), h=U.S(88),
                          command=lambda p=item["pkg"], n=item["name"]:
                              self.open_app(p, n),
                          menu=lambda e, p=item["pkg"], n=item["name"]:
                              self.app_menu(e, p, n))
            t.grid(row=i // cols, column=i % cols, padx=U.S(7), pady=U.S(7),
                   sticky="nsew")
        add = U.AppTile(self.app_box, add_mode=True, cmd_add=self.app_picker,
                        w=U.S(150), h=U.S(88))
        add.grid(row=len(apps) // cols, column=len(apps) % cols,
                 padx=U.S(7), pady=U.S(7), sticky="nsew")
        for c in range(cols):
            self.app_box.columnconfigure(c, weight=1, uniform="apps")
        if hasattr(self, "page"):
            self.page.bind_children()
        if hasattr(self, "app_grid"):
            self.app_grid.bind_children()

    def app_menu(self, event, pkg, name):
        m = U.menu(self.root)
        m.add_command(label="打开 %s" % name, command=lambda: self.open_app(pkg, name))
        m.add_command(label="移除快捷入口", command=lambda: self.remove_app(pkg))
        m.tk_popup(event.x_root, event.y_root)

    def remove_app(self, pkg):
        self.cfg["apps"] = [a for a in self.cfg.get("apps", []) if a["pkg"] != pkg]
        save_cfg(self.cfg)
        self.render_apps()
        self.log("已移除快捷入口 %s" % pkg)

    def app_picker(self):
        if not self.dev.target():
            messagebox.showinfo("还没连上手机", "先连上手机，再来挑 App。")
            return
        if self.apps_cache:
            self.show_picker()
        else:
            messagebox.showinfo("正在读取", "正在读手机上的应用列表，读完会自动弹出选择窗口。\n"
                                            "万一没弹出来，再点一次【添加 / 管理】。")
            self.bg("读取应用", self.fetch_apps)

    def fetch_apps(self):
        pkgs = self.dev.packages()
        if not pkgs:
            self.log("没读到应用列表，看看手机是不是锁屏了、无线调试是不是还开着")
            return
        known = {p: n for n, p in KNOWN_APPS}
        res = [{"name": known.get(p, p), "pkg": p} for p in pkgs]
        self.apps_cache = sorted(res, key=lambda x: (x["name"] == x["pkg"], x["name"]))
        self.log("读到手机上 %d 个应用" % len(self.apps_cache))
        missing = [a["name"] for a in self.cfg.get("apps", [])
                   if a["pkg"] not in set(pkgs)]
        if missing:
            self.log("这几个常用 App 手机上没装，可以右键移除：%s" % "、".join(missing))
        self.ui(self.show_picker)

    def show_picker(self):
        win = tk.Toplevel(self.root)
        win.title("添加 / 管理常用 App")
        win.geometry("560x620")
        win.configure(bg=BG)
        top = ttk.Frame(win, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="选要放到首页的 App（最多 24 个）",
                  font=(FONT, 11, "bold")).pack(anchor="w")
        sv = tk.StringVar()
        ttk.Entry(top, textvariable=sv).pack(fill="x", pady=6)
        ttk.Label(top, text="输入名字或包名可以查找", background=BG,
                  foreground=SUB, font=(FONT, 9)).pack(anchor="w")

        box = ttk.Frame(win, style="Card.TFrame")
        box.pack(fill="both", expand=True, padx=12)
        cv = tk.Canvas(box, bg=CARD, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=cv.yview)
        inner = ttk.Frame(cv, style="Card.TFrame")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        cur = {a["pkg"] for a in self.cfg.get("apps", [])}
        rows = []

        def fill(kw=""):
            for w in inner.winfo_children():
                w.destroy()
            rows.clear()
            kw = kw.strip().lower()
            for a in self.apps_cache:
                if kw and kw not in a["name"].lower() and kw not in a["pkg"].lower():
                    continue
                v = tk.BooleanVar(value=(a["pkg"] in cur))
                rows.append((a["pkg"], a["name"], v))
                ttk.Checkbutton(inner, text="%s   (%s)" % (a["name"], a["pkg"]),
                                variable=v).pack(anchor="w", padx=10, pady=1)
            cv.yview_moveto(0)

        sv.trace_add("write", lambda *_: fill(sv.get()))
        fill()

        def save():
            picked, seen = [], set()
            for pkg, name, v in rows:
                if v.get() and pkg not in seen:
                    picked.append({"name": name, "pkg": pkg})
                    seen.add(pkg)
            # 保留这次没显示出来（被搜索过滤掉）但之前选过的
            for a in self.cfg.get("apps", []):
                if a["pkg"] not in seen and a["pkg"] in cur:
                    picked.append(a)
                    seen.add(a["pkg"])
            picked = picked[:24]
            self.cfg["apps"] = picked
            save_cfg(self.cfg)
            self.render_apps()
            win.destroy()
            self.log("常用 App 已保存（%d 个）" % len(picked))

        U.Btn(win, "保存", kind="primary", command=save).pack(pady=U.S(10))

    # ------------------------------------------------------------ 手机窗口
    def new_title(self, name):
        """窗口标题要唯一，不然多个同名窗口找不到对应那个"""
        self.seq += 1
        t = "%s ·%d" % (name, self.seq)
        # 上次异常退出可能留下同名旧窗口，自动往后让，避免抓错窗口
        while find_window(t):
            self.seq += 1
            t = "%s ·%d" % (name, self.seq)
        return t

    def stagger(self):
        """多开窗口时错开一点位置，别全叠在一处"""
        n = len(self.sessions) % 8
        return 80 + n * 48, 40 + n * 40

    def open_mirror(self):
        s = self.dev.target()
        if not s:
            messagebox.showinfo("还没连上手机",
                                "先点右上角【更多】→【配对手机】，或点【连接】。")
            return
        title = self.new_title("手机")
        x, y = self.stagger()
        args = scrcpy_args(s, self.cfg, title=title,
                           extra=["--window-x=%d" % x, "--window-y=%d" % y])
        p = self.launch(args, "手机窗口")
        if p:
            self.sessions.append(Session(self, "手机", title, "main", p, args))

    def open_app(self, pkg, name):
        s = self.dev.target()
        if not s:
            messagebox.showinfo("还没连上手机", "先连接手机。")
            return
        virtual = self.cfg.get("app_mode") == "virtual"
        extra = []
        if virtual:
            extra += ["--new-display", "--display-ime-policy=local"]
            self.log("用独立窗口打开 %s（手机屏幕不受影响）" % name)
        extra += ["--start-app=%s" % pkg]
        title = self.new_title(name)
        x, y = self.stagger()
        extra += ["--window-x=%d" % x, "--window-y=%d" % y]
        before = virtual_display_ids(s) if virtual else []
        args = scrcpy_args(s, self.cfg, extra, title=title)
        p = self.launch(args, "App 窗口")
        if not p:
            return
        sess = Session(self, name, title, "virtual" if virtual else "main", p, args)
        self.sessions.append(sess)
        if virtual:
            self.detect_display_id(sess, before)

    def detect_display_id(self, sess, before):
        """独立窗口开出来后，找出它对应的虚拟屏编号，之后录屏要用"""
        def job():
            serial = self.dev.target()
            if not serial:
                return
            for _ in range(6):
                time.sleep(2)
                try:
                    ids = virtual_display_ids(serial)
                except Exception:
                    continue
                new = [i for i in ids if i not in before]
                if new:
                    sess.display_id = max(new)
                    self.log("%s 的独立屏幕编号 = %d（录屏用）" % (sess.name, sess.display_id))
                    return
        threading.Thread(target=job, daemon=True).start()

    def tick_sessions(self):
        """侧边栏跟着窗口走；窗口关了就收掉。拖动时刷新更快，看着就是一体的"""
        moved = False
        for s in list(self.sessions):
            if s.proc is None or s.proc.poll() is not None:
                if s.recording:
                    continue      # 录制刚结束，等 on_session_rec_done 把窗口恢复回来
                s.destroy_bar()
                if s in self.sessions:
                    self.sessions.remove(s)
                self.log("窗口关了：%s" % s.name)
                continue
            try:
                r = window_rect(s.title)
                if r != s.last_rect:
                    s.last_rect = r
                    moved = True
            except Exception:
                pass
            s.follow()
            if s.recording and s.rec_start and s.rec_btn:
                d = int(time.time() - s.rec_start)
                s.rec_btn.config(text="停止 %02d:%02d" % (d // 60, d % 60))
        self.render_wins()
        # 窗口在动就 16ms 跟一次（约一帧，看不出延迟），不动就 250ms
        self.root.after(16 if moved else 250, self.tick_sessions)

    def launch(self, args, tag, watch=True):
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "scrcpy_%d.log" % int(time.time() * 1000))
        lf = open(path, "w", encoding="utf-8", errors="ignore")
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.Popen(args, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                             startupinfo=si, creationflags=0x08000000)
        self.procs.append(p)
        self.log("启动 %s" % tag)
        if watch:
            threading.Thread(target=self.watch, args=(p, path, tag, list(args)),
                             daemon=True).start()
        return p

    def watch(self, p, path, tag, args):
        time.sleep(5)
        if p.poll() is None:
            return
        try:
            tail = open(path, "r", encoding="utf-8", errors="ignore").read().strip()[-900:]
        except Exception:
            tail = ""
        self.log("%s 启动后立刻退出了：\n%s" % (tag, tail or "(没有输出)"))
        low = tail.lower()
        if "unauthorized" in low:
            self.ui(lambda: messagebox.showinfo(
                "手机上要确认一下",
                "手机上有【允许 USB 调试】的提示，勾上“始终允许”再确定，然后重开一次。"))
            return
        if "uhid" in low or "hid" in low:
            self.log("这台手机不支持物理键盘模式，自动改用兼容模式（中文输入会受影响）")
            self.cfg["keyboard"] = "sdk"
            save_cfg(self.cfg)
            self.ui(lambda: self.launch([a for a in args if a != "--keyboard=uhid"],
                                        tag + "（兼容模式）", watch=False))
            return
        if "display" in low and "--new-display" in args:
            self.log("这台手机不支持独立虚拟屏，改成在手机屏幕上打开")
            self.cfg["app_mode"] = "main"
            save_cfg(self.cfg)
            self.ui(lambda: self.launch([a for a in args if a not in
                                         ("--new-display", "--display-ime-policy=local")],
                                        tag + "（手机主屏）", watch=False))
            return
        if "already" in low or "in use" in low:
            return
        self.ui(lambda: messagebox.showinfo(
            "%s没起来" % tag,
            "常见原因：\n1. 手机上【USB 调试（安全设置）】没开（小米必开，开完要重启手机）\n"
            "2. 手机锁屏了，先解锁\n3. 详细原因看下方日志\n\n%s" % (tail or "")))

    def stop_all(self):
        for s in list(self.sessions):
            close_window(s.title)
            if s.rec_proc and s.rec_proc.poll() is None:
                close_window(s.rec_title)
        n = 0
        for p in list(self.procs):
            try:
                if p.poll() is None:
                    p.terminate()
                    n += 1
            except Exception:
                pass
        self.procs = [p for p in self.procs if p.poll() is None]
        self.log("关掉了全部手机窗口")

    def on_session_rec_done(self, ev):
        """某个窗口单独的录屏结束了"""
        sess, tmp = ev
        sess.rec_proc = None
        sess.recording = False
        if sess.rec_btn:
            sess.rec_btn.config(text="录屏", bg="#2563eb")
        time.sleep(0.8)
        if not os.path.exists(tmp) or os.path.getsize(tmp) < 10240:
            self.log("【%s】没录到有效视频" % sess.name)
            messagebox.showinfo(
                "没录上",
                "【%s】这次没录到有效视频。\n\n录单个 App 窗口要靠手机的虚拟屏能力，\n"
                "这台手机不行的话，用主界面的【录屏】录整个手机屏幕。" % sess.name)
            return
        os.makedirs(REC_DIR, exist_ok=True)
        base = "录屏_%s_%s" % (sess.safe_name(), time.strftime("%m%d_%H%M"))
        final = os.path.join(REC_DIR, base + ".mp4")
        n = 1
        while os.path.exists(final):
            n += 1
            final = os.path.join(REC_DIR, "%s_%d.mp4" % (base, n))
        for _ in range(12):
            try:
                shutil.move(tmp, final)
                break
            except Exception:
                time.sleep(0.8)
        if not os.path.exists(final):
            final = tmp
        mb = os.path.getsize(final) / 1024 / 1024
        dur = int(time.time() - sess.rec_start)
        self.log("录好了【%s】：%s（%.1f MB，约 %02d:%02d）"
                 % (sess.name, final, mb, dur // 60, dur % 60))
        messagebox.showinfo("录好了", "已保存到：\n%s\n\n大小 %.1f MB，时长约 %02d:%02d"
                            % (final, mb, dur // 60, dur % 60))

    # ------------------------------------------------------------ 录屏
    def record_dialog(self):
        s = self.dev.target()
        if not s:
            messagebox.showinfo("还没连上手机", "先连接手机。")
            return
        if self.rec_proc and self.rec_proc.poll() is None:
            messagebox.showinfo("正在录", "已经有一段在录了，先点【结束并保存】。")
            return

        win = tk.Toplevel(self.root)
        win.title("录屏")
        win.geometry("560x500")
        win.transient(self.root)
        f = U.dialog_body(win)
        tk.Label(f, text="录什么", bg=CARD, fg=INK,
                 font=(FONT, 12, "bold"), anchor="w").pack(anchor="w")
        scope = tk.StringVar(value="screen")
        ttk.Radiobutton(f, text="整个手机屏幕（横竖屏、通知都录进去）",
                        variable=scope, value="screen").pack(anchor="w", pady=2)
        rf = ttk.Frame(f, style="Card.TFrame")
        rf.pack(fill="x", anchor="w", pady=2)
        ttk.Radiobutton(rf, text="只录一个 App：", variable=scope,
                        value="app").pack(side="left")
        apps = self.cfg.get("apps") or []
        names = [a["name"] for a in apps] or ["（先去添加常用 App）"]
        app_v = tk.StringVar(value=names[0])
        ttk.Combobox(rf, values=names, textvariable=app_v, width=14,
                     state="readonly").pack(side="left", padx=6)

        ttk.Label(f, text="清晰度", style="Head.TLabel").pack(anchor="w", pady=(14, 0))
        ttk.Label(f, text="录的是手机发来的原始画面，跟电脑窗口大小无关，"
                          "窗口缩小也不会变糊", style="Sub.TLabel",
                  wraplength=480, justify="left").pack(anchor="w")
        g = ttk.Frame(f, style="Card.TFrame")
        g.pack(fill="x", pady=6)
        size = tk.StringVar(value=self.cfg.get("rec_size", "原始"))
        ttk.Label(g, text="分辨率", style="Card.TLabel").pack(side="left")
        ttk.Combobox(g, values=["原始", "1080", "720"], textvariable=size, width=7,
                     state="readonly").pack(side="left", padx=6)
        br = tk.StringVar(value=self.cfg.get("rec_bitrate", "16M"))
        ttk.Label(g, text="码率", style="Card.TLabel").pack(side="left", padx=(14, 0))
        ttk.Combobox(g, values=["8M", "16M", "24M"], textvariable=br, width=6,
                     state="readonly").pack(side="left", padx=6)

        prev = tk.BooleanVar(value=self.cfg.get("rec_preview", True))
        aud = tk.BooleanVar(value=self.cfg.get("rec_audio", False))
        ttk.Checkbutton(f, text="录制时显示预览窗口（不勾就自动最小化到任务栏，不占屏幕）",
                        variable=prev).pack(anchor="w", pady=(10, 2))
        ttk.Label(f, text="怎么停：开始后屏幕右上角会浮出一条【结束并保存】，"
                          "一直显示在最前面；直接关掉手机窗口也一样停。",
                  style="Sub.TLabel", wraplength=480, justify="left").pack(anchor="w", pady=(0, 4))
        ttk.Checkbutton(f, text="同时录声音（可能和画面对不上）",
                        variable=aud).pack(anchor="w")
        ttk.Label(f, text="文件存到本文件夹的【录屏】里，MP4 格式，剪映 / PR 直接导入",
                  style="Sub.TLabel", wraplength=480, justify="left").pack(anchor="w", pady=(10, 0))
        if self.cfg.get("codec") == "h265":
            tk.Label(f, text="注意：当前编码是 H.265，体积更小但部分剪辑软件导入会异常，"
                             "要最保险的话去【设置】里改回 H.264",
                     bg=CARD, fg="#b45309", font=(FONT, 9),
                     wraplength=480, justify="left").pack(anchor="w")

        def start():
            win.destroy()
            pkg, label = None, "手机屏幕"
            if scope.get() == "app":
                nm = app_v.get()
                pkg = next((a["pkg"] for a in apps if a["name"] == nm), None)
                label = nm
            self.start_record(size.get(), br.get(), prev.get(), aud.get(), pkg, label)

        U.Btn(win, "开始录制", kind="primary", command=start).pack(pady=U.S(10))

    def start_record(self, size, bitrate, preview, audio, pkg, label):
        s = self.dev.target()
        cfg = dict(self.cfg)
        # 录制时始终带窗口：没有窗口就只能强杀进程，视频会写坏
        # 不想看画面的话，启动后自动把它最小化到任务栏
        cfg.update({"recording": True, "rec_size": size, "bitrate": bitrate,
                    "rec_audio": audio, "no_window": False})
        if not preview:
            cfg.update(self.small_window_geometry())
        extra = []
        if pkg:
            if self.cfg.get("app_mode") == "virtual":
                extra += ["--new-display", "--display-ime-policy=local"]
            extra += ["--start-app=%s" % pkg]
        tmp = os.path.join(REC_TMP, "rec_%d.mp4" % int(time.time()))
        title = "录制中 - %s" % label
        args = scrcpy_args(s, cfg, extra=extra, title=title, record=tmp)
        self.cfg.update({"rec_size": size, "rec_bitrate": bitrate,
                         "rec_preview": preview, "rec_audio": audio})
        save_cfg(self.cfg)
        p = self.launch(args, "录屏", watch=False)
        if not p:
            return
        self.rec_proc, self.rec_start = p, time.time()
        self.rec_tmp, self.rec_label, self.rec_title = tmp, label, title
        # 插到常用 App 区前面，别用 winfo_children()[2]（那是 recbar 自己）
        self.recbar.pack(fill="x", pady=(0, U.S(12)))
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        self.show_rec_float()
        self.tick_rec()
        threading.Thread(target=self.rec_waiter, args=(p, tmp, label), daemon=True).start()

    def show_rec_float(self):
        """录制时浮在最前面的小条，免得被手机窗口挡住后找不到停止按钮"""
        w = tk.Toplevel(self.root)
        w.title("_recfloat_")           # 不然会继承主窗口标题
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg="#111827")
        f = tk.Frame(w, bg="#111827", padx=14, pady=9)
        f.pack()
        tk.Label(f, text="●", fg="#ef4444", bg="#111827", font=(FONT, 15)).pack(side="left")
        self.float_var = tk.StringVar(value="录制中 00:00")
        tk.Label(f, textvariable=self.float_var, fg="#ffffff", bg="#111827",
                 font=(FONT, 11, "bold")).pack(side="left", padx=10)
        tk.Button(f, text="结束并保存", bg="#ef4444", fg="#ffffff", relief="flat",
                  activebackground="#dc2626", activeforeground="#ffffff",
                  padx=12, pady=4, font=(FONT, 10, "bold"),
                  command=self.stop_record).pack(side="left")
        w.update_idletasks()
        w.geometry("+%d+36" % (self.root.winfo_screenwidth() - w.winfo_width() - 28))
        self.rec_float = w

    def tick_rec(self):
        if not (self.rec_proc and self.rec_proc.poll() is None):
            return
        t = int(time.time() - self.rec_start)
        self.rec_var.set("%s   %02d:%02d" % (self.rec_label, t // 60, t % 60))
        if self.float_var is not None:
            self.float_var.set("录制中 %02d:%02d" % (t // 60, t % 60))
        self.root.after(1000, self.tick_rec)

    def on_close(self):
        if self.rec_proc and self.rec_proc.poll() is None:
            messagebox.showinfo(
                "还在录屏",
                "录制还没结束。\n\n点主界面上、或者屏幕右上角小条里的【结束并保存】，\n"
                "也可以直接关掉手机窗口。视频存好后再退出工具。")
            return
        try:
            self.root.destroy()
        except Exception:
            pass

    def rec_waiter(self, p, tmp, label):
        p.wait()
        self.q.put(("rec", (tmp, label)))

    def small_window_geometry(self):
        """录制又不想看画面时：把窗口缩小挪到右下角（不能最小化，否则录不到）"""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return {"window_width": 320,
                "window_geometry": (max(0, sw - 345), max(0, sh - 700))}

    def stop_record(self):
        p = self.rec_proc
        if not p:
            return
        try:
            hwnd = user32.FindWindowW(None, self.rec_title)
            if hwnd:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                for _ in range(60):
                    if p.poll() is not None:
                        break
                    time.sleep(0.1)
        except Exception:
            pass
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            self.log("关不掉窗口，只能强制结束；万一视频打不开，下次直接关掉手机窗口结束录制")

    def on_rec_done(self, ev):
        tmp, label = ev
        self.recbar.pack_forget()
        self.rec_proc = None
        if self.rec_float is not None:
            try:
                self.rec_float.destroy()
            except Exception:
                pass
            self.rec_float = None
            self.float_var = None
        time.sleep(0.8)
        if not os.path.exists(tmp) or os.path.getsize(tmp) < 10240:
            self.log("没生成有效视频，看看手机是不是锁屏了、无线调试是不是断了")
            messagebox.showinfo("没录上", "没有生成有效的视频文件，详情看日志。")
            return
        os.makedirs(REC_DIR, exist_ok=True)
        safe = "".join(c for c in label if c not in '\\/:*?"<>|')
        base = "录屏_%s_%s" % (safe, time.strftime("%m%d_%H%M"))
        final = os.path.join(REC_DIR, base + ".mp4")
        n = 1
        while os.path.exists(final):
            n += 1
            final = os.path.join(REC_DIR, "%s_%d.mp4" % (base, n))
        moved, err = False, None
        for _ in range(12):      # 刚录完文件可能还被占用，多试几次
            try:
                shutil.move(tmp, final)
                moved = True
                break
            except Exception as e:
                err = e
                time.sleep(0.8)
        if not moved:
            self.log("移动文件失败：%s（文件还在 %s）" % (err, tmp))
            final = tmp
        mb = os.path.getsize(final) / 1024 / 1024
        dur = int(time.time() - self.rec_start)
        self.log("录好了：%s（%.1f MB，约 %02d:%02d）" % (final, mb, dur // 60, dur % 60))
        messagebox.showinfo("录好了",
                            "已保存到：\n%s\n\n大小 %.1f MB，时长约 %02d:%02d\n\n"
                            "点确定后打开文件夹。" % (final, mb, dur // 60, dur % 60))
        self.open_path(REC_DIR)

    # ------------------------------------------------------------ 电脑扫码
    def scan_dialog(self):
        if not self.dev.target():
            messagebox.showinfo("还没连上手机", "先连接手机。")
            return
        apps = self.cfg.get("apps") or [{"name": "微信", "pkg": "com.tencent.mm"}]
        names = [a["name"] for a in apps]
        win = tk.Toplevel(self.root)
        win.title("把电脑上的二维码发到手机扫")
        win.geometry("600x420")
        f = U.dialog_body(win)
        tk.Label(f, text="用哪个 App 扫码", bg=CARD, fg=INK,
                 font=(FONT, 12, "bold"), anchor="w").pack(anchor="w")
        v = tk.StringVar(value=names[0])
        ttk.Combobox(f, values=names, textvariable=v, width=16,
                     state="readonly").pack(anchor="w", pady=6)
        for s in ["做法：把电脑上这块二维码截成图，发到手机相册，",
                  "再让 App 从相册里选这张图识别，不用举手机对准屏幕。",
                  "",
                  "微信、支付宝、抖音、小红书、淘宝、京东这些带“扫一扫”的 App 都能这么用，",
                  "区别只在进了扫一扫以后【相册 / 图片】按钮的位置不一样。"]:
            ttk.Label(f, text=s, style="Sub.TLabel",
                      wraplength=520, justify="left").pack(anchor="w")

        def go():
            nm = v.get()
            item = next((a for a in apps if a["name"] == nm), None)
            win.destroy()
            Snipper(self.root, lambda box: self.after_snip(box, item))

        U.Btn(win, "下一步：在电脑上框选二维码", kind="primary",
                   command=go).pack(pady=14)

    def after_snip(self, box, item):
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, "qr_%d.png" % int(time.time()))
        try:
            capture_region(box, path)
        except Exception as e:
            messagebox.showerror("截图失败", str(e))
            return
        self.log("框选完成 %dx%d -> %s" % (box[2] - box[0], box[3] - box[1], path))
        self.show_qr_preview(path, item)

    def show_qr_preview(self, path, item):
        win = tk.Toplevel(self.root)
        win.title("确认框得对不对")
        win.geometry("380x460")
        win.configure(bg=BG)
        try:
            from PIL import Image, ImageTk
            im = Image.open(path)
            im.thumbnail((280, 280))
            ph = ImageTk.PhotoImage(im)
            lb = tk.Label(win, image=ph, bg=CARD)
            lb.image = ph
            lb.pack(pady=12)
        except Exception:
            ttk.Label(win, text=path).pack(pady=12)
        ttk.Label(win, text="框成这样发给手机？", style="Head.TLabel").pack()

        def send():
            win.destroy()
            self.bg("发送二维码", lambda: self.send_qr(path, item))

        bf = ttk.Frame(win)
        bf.pack(pady=14)
        U.Btn(bf, "发到手机", kind="primary", command=send).pack(side="left", padx=U.S(8))
        U.Btn(bf, "重新框", kind="ghost",
                   command=lambda: (win.destroy(),
                                    Snipper(self.root, lambda b: self.after_snip(b, item)))
                   ).pack(side="left", padx=6)

    def send_qr(self, path, item):
        name = item["name"] if item else "手机"
        pkg = item["pkg"] if item else ""
        try:
            remote = self.dev.push_scan_image(path)
        except Exception as e:
            self.log("发送失败：%s" % e)
            self.ui(lambda: messagebox.showerror("发送失败", str(e)))
            return
        self.log("图片已发到手机相册：%s" % remote)
        how = ""
        if pkg:
            _, how = self.dev.open_scan(pkg)
        tips = {
            "微信": "1. 微信右上角 ⊕ → 扫一扫\n"
                    "2. 点扫一扫界面里的【相册】（有的版本在 ⋯ 菜单里）\n"
                    "3. 选最新那张 qr_ 开头的图",
            "支付宝": "1. 支付宝首页【扫一扫】\n"
                      "2. 右上角【相册】\n"
                      "3. 选最新那张 qr_ 开头的图",
        }
        step = tips.get(name, "1. 在 %s 里找到【扫一扫】\n"
                              "2. 点里面的【相册 / 图片】按钮\n"
                              "3. 选最新那张 qr_ 开头的图" % name)
        self.ui(lambda: messagebox.showinfo(
            "已发到手机相册",
            "%s\n\n接下来在手机上（三步）：\n%s\n\n"
            "图片名是 qr_ 开头，在相册里排最前面。\n\n"
            "备选：直接在手机相册里打开这张图，很多手机相册自带“识别二维码”。\n"
            "如果扫一扫里找不到【相册】入口，说明这个 App 不支持从相册识别，"
            "换微信或支付宝试试。"
            % (how, step)))

    # ------------------------------------------------------------ 连接
    def auto_connect(self):
        self.status("连接中…", "#f59e0b")
        if self.dev.serial:
            _, out = self.dev.adb(["connect", self.dev.serial], 20)
            self.log("连接 %s -> %s" % (self.dev.serial, out.strip()))
            if "connected" in out or "already" in out:
                if self.check_ready():
                    return
        self.log("保存的地址连不上（手机重启后端口会变），改成在局域网里找…")
        self.discover()

    def discover(self):
        self.log("搜索同一 WiFi 下的手机…")
        _, out = self.dev.adb(["mdns", "services"], 25)
        self.log("搜索结果：\n" + (out.strip() or "(空)"))
        conns, pairs = [], []
        for line in out.splitlines():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", line)
            if not m:
                continue
            if "pairing" in line:
                pairs.append(m.group(0))
            elif "tls-connect" in line or "_adb._tcp" in line:
                conns.append(m.group(0))
        if not conns:
            self.status("未连接", RED)
            self.ui(lambda: messagebox.showinfo(
                "没搜到手机",
                "确认这几点：\n1. 手机上【无线调试】是开着的\n"
                "2. 手机和电脑在同一个 WiFi\n3. 手机屏幕是解锁的\n\n"
                "第一次用还得先配对：右上角【更多】→【配对手机】。"))
            return
        for addr in conns:
            _, out = self.dev.adb(["connect", addr], 20)
            self.log("连接 %s -> %s" % (addr, out.strip()))
            if "connected" in out or "already" in out:
                self.dev.serial = addr
                self.cfg["serial"] = addr
                save_cfg(self.cfg)
                if self.check_ready():
                    return
        self.status("未连接", RED)
        self.log("还是连不上，先用【配对手机】配对一次")

    def check_ready(self):
        s = self.dev.target()
        if not s:
            self.status("未连接", RED)
            return False
        self.dev.serial = s
        self.cfg["serial"] = s
        save_cfg(self.cfg)
        info = self.dev.info()
        self.q.put(("dev", info or s))
        self.status("已连接", GREEN)
        self.log("已连接：%s（%s）" % (info or "手机", s))
        self.dev.shell(["settings", "put", "secure", "show_ime_with_hard_keyboard", "0"])
        self.bg("读取设备信息", lambda: self.q.put(("devcard", self.dev.details())))
        self.bg("读取应用列表", self.fetch_apps)
        return True

    def pair(self, addr, code):
        self.status("配对中…", "#f59e0b")
        rc, out = self.dev.adb(["pair", addr, code], 40)
        self.log("配对 %s -> %s" % (addr, out.strip()))
        if rc != 0 or "failed" in out.lower():
            self.status("未连接", RED)
            self.log("配对失败，配对码可能过期了，在手机上重新点【使用配对码配对设备】")
            return
        self.log("配对成功，正在连接…")
        time.sleep(1)
        self.discover()

    def disconnect(self):
        self.stop_all()
        for d in self.dev.devices():
            self.dev.adb(["disconnect", d["serial"]], 10)
        self.dev.serial = ""
        self.cfg["serial"] = ""
        save_cfg(self.cfg)
        self.status("未连接", RED)
        self.q.put(("dev", "还没有连接手机"))
        self.q.put(("devcard", {"model": "", "ver": "", "res": "", "os": ""}))
        self.log("已断开")

    # ------------------------------------------------------------ 其它
    def screenshot(self):
        if not self.dev.target():
            messagebox.showinfo("还没连上手机", "先连接手机。")
            return
        p = self.dev.screenshot(SHOT_DIR)
        self.log("截图：%s" % p)
        messagebox.showinfo("截图好了", "存到：\n%s" % p)

    def ime_settings(self):
        if not self.dev.target():
            messagebox.showinfo("还没连上手机", "先连接手机。")
            return
        self.dev.shell(["am", "start", "-a", "android.settings.HARD_KEYBOARD_SETTINGS"])
        self.log("手机上已打开【物理键盘】设置：选好布局，把“屏幕键盘”关掉")

    def open_path(self, p):
        os.makedirs(p, exist_ok=True)
        try:
            os.startfile(p)
        except Exception as e:
            self.log("打开失败：%s" % e)

    # ------------------------------------------------------------ 设置 / 向导 / 帮助
    def settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("700x620")
        win.configure(bg=BG)
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=U.S(18), pady=U.S(16))

        def page(t):
            f = ttk.Frame(nb, padding=U.S(20))
            nb.add(f, text=t)
            return f

        def combo(par, label, vals, key, w=8):
            row = ttk.Frame(par, style="Card.TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, style="Card.TLabel").pack(side="left")
            v = tk.StringVar(value=str(self.cfg.get(key)))
            c = ttk.Combobox(row, values=vals, textvariable=v, width=w, state="readonly")
            c.pack(side="left", padx=8)
            c.bind("<<ComboboxSelected>>",
                   lambda e, k=key, vv=v: (self.cfg.__setitem__(k, vv.get()), save_cfg(self.cfg)))
            return v

        p1 = page("画面和流畅度")
        combo(p1, "流畅度", ["流畅优先", "均衡", "高清优先"], "perf", 10)
        ttk.Label(p1, text="流畅优先：延迟最低，拖动最跟手；高清优先：码率 16M，字更清楚",
                  style="Sub.TLabel", wraplength=560, justify="left").pack(anchor="w")
        combo(p1, "清晰度上限", ["原始", "1920", "1600", "1280", "1024", "800"], "max_size")
        combo(p1, "帧率", ["60", "30", "不限"], "fps", 6)
        combo(p1, "码率", ["4M", "8M", "12M", "16M"], "bitrate", 6)
        combo(p1, "编码", ["h264", "h265"], "codec", 7)
        combo(p1, "硬件解码", ["auto", "d3d11va", "disabled"], "hwdec", 10)
        for label, key in [("窗口置顶", "always_top"), ("投屏时常亮", "stay_awake"),
                           ("关手机屏幕省电", "screen_off"), ("传输声音", "audio"),
                           ("不发送鼠标悬停（更流畅）", "no_mouse_hover")]:
            v = tk.BooleanVar(value=bool(self.cfg.get(key)))
            ttk.Checkbutton(p1, text=label, variable=v,
                            command=lambda k=key, vv=v: (self.cfg.__setitem__(k, vv.get()),
                                                         save_cfg(self.cfg))).pack(anchor="w")

        p2 = page("键盘和 App 窗口")
        ttk.Label(p2, text="键盘模式", style="Head.TLabel").pack(anchor="w")
        kv = tk.StringVar(value=self.cfg.get("keyboard"))
        kc = ttk.Combobox(p2, values=["uhid", "sdk"], textvariable=kv, width=10,
                          state="readonly")
        kc.pack(anchor="w", pady=4)
        ttk.Label(p2, text="uhid（默认）：手机当成接了外接键盘，不弹手机键盘，可用手机输入法打中文\n"
                           "sdk：兼容模式，只能打英文数字",
                  style="Sub.TLabel", wraplength=560, justify="left").pack(anchor="w")
        kc.bind("<<ComboboxSelected>>",
                lambda e: (self.cfg.__setitem__("keyboard", kv.get()), save_cfg(self.cfg)))
        ttk.Label(p2, text="常用 App 怎么开", style="Head.TLabel").pack(anchor="w", pady=(16, 0))
        av = tk.StringVar(value=self.cfg.get("app_mode"))
        ac = ttk.Combobox(p2, values=["virtual", "main"], textvariable=av, width=10,
                          state="readonly")
        ac.pack(anchor="w", pady=4)
        ttk.Label(p2, text="virtual（默认）：单独虚拟屏，手机屏幕不受影响\n"
                           "main：在手机屏幕上打开，跟手机上的操作同步",
                  style="Sub.TLabel", wraplength=560, justify="left").pack(anchor="w")
        ac.bind("<<ComboboxSelected>>",
                lambda e: (self.cfg.__setitem__("app_mode", av.get()), save_cfg(self.cfg)))
        ttk.Button(p2, text="打开手机上的输入法设置",
                   command=self.ime_settings).pack(anchor="w", pady=12)

        p3 = page("录屏默认")
        combo(p3, "分辨率", ["原始", "1080", "720"], "rec_size")
        combo(p3, "码率", ["8M", "16M", "24M"], "rec_bitrate", 6)
        for label, key in [("录屏时显示预览窗口", "rec_preview"), ("录屏同时录声音", "rec_audio")]:
            v = tk.BooleanVar(value=bool(self.cfg.get(key)))
            ttk.Checkbutton(p3, text=label, variable=v,
                            command=lambda k=key, vv=v: (self.cfg.__setitem__(k, vv.get()),
                                                         save_cfg(self.cfg))).pack(anchor="w", pady=2)
        ttk.Label(p3, text="录屏不受电脑窗口大小影响，录的是手机原始画面。\n"
                           "关掉手机窗口就是结束录制，文件自动存到【录屏】文件夹。",
                  style="Sub.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=8)

    def wizard(self):
        w = tk.Toplevel(self.root)
        w.title("首次使用：配对手机")
        w.geometry("700x620")
        w.configure(bg=BG)
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        def page(t):
            f = ttk.Frame(nb, padding=14)
            nb.add(f, text=t)
            return f

        def txt(p, s, bold=False, size=10):
            tk.Label(p, text=s, justify="left", anchor="w", bg=BG, fg=INK, wraplength=620,
                     font=(FONT, size, "bold" if bold else "normal")).pack(fill="x", pady=2)

        p1 = page("① 开发者选项")
        txt(p1, "手机上操作（不用数据线）：", bold=True)
        txt(p1, "1. 【设置】→【我的设备】→【全部参数】")
        txt(p1, "2. 连点【MIUI 版本 / 系统版本】7 下，提示“已进入开发者模式”")
        txt(p1, "3. 回到【设置】→【更多设置】→【开发者选项】（找不到就在设置页顶部搜“开发者选项”）")

        p2 = page("② 三个开关")
        txt(p2, "在【开发者选项】里打开：", bold=True)
        txt(p2, "● USB 调试")
        txt(p2, "● 无线调试")
        txt(p2, "● USB 调试（安全设置） ← 小米必开，否则电脑鼠标点不动手机，开完重启一次手机")

        p3 = page("③ 配对")
        txt(p3, "1. 手机进【开发者选项】→【无线调试】", bold=True)
        txt(p3, "2. 点【使用配对码配对设备】，屏幕显示 IP:端口（如 192.168.1.8:37621）和 6 位配对码")
        txt(p3, "3. 填到下面点【配对并连接】（也可以先点【自动搜索】，只填 6 位码）")
        f = ttk.Frame(p3, padding=6)
        f.pack(fill="x", pady=8)
        ttk.Label(f, text="配对地址").grid(row=0, column=0, sticky="w", pady=3)
        av = tk.StringVar()
        ttk.Entry(f, textvariable=av, width=26).grid(row=0, column=1, padx=6)
        ttk.Button(f, text="自动搜索", command=lambda: self.fill_addr(av)).grid(row=0, column=2)
        ttk.Label(f, text="6 位配对码").grid(row=1, column=0, sticky="w", pady=3)
        cv = tk.StringVar()
        ttk.Entry(f, textvariable=cv, width=26).grid(row=1, column=1, padx=6)

        def do_pair():
            a, c = av.get().strip(), cv.get().strip()
            if not a or not c:
                messagebox.showwarning("还差信息", "配对地址和 6 位配对码都要填。")
                return
            self.bg("配对", lambda: self.pair(a, c))

        U.Btn(p3, "配对并连接", kind="primary", command=do_pair).pack(pady=U.S(8))

        p4 = page("④ 开始用")
        txt(p4, "连上以后：", bold=True)
        txt(p4, "● 【打开手机窗口】看手机画面；左键点击、按住拖动、滚轮滚动、右键返回、中键回桌面")
        txt(p4, "● 【录屏】选录整个屏幕还是单个 App，录完自动存到电脑")
        txt(p4, "● 【电脑上扫码】框住电脑上的二维码，发给手机相册识别")
        txt(p4, "● 常用 App 点一下单独开窗口，右键可移除，点【添加 / 管理】能加更多")
        txt(p4, "中文输入：", bold=True)
        txt(p4, "默认物理键盘模式，手机不会弹键盘挡住输入框。用电脑键盘敲拼音（nihao），"
                "候选字在手机屏幕底部，按空格或数字键选字。")
        U.Btn(p4, "连好了，打开手机窗口", kind="primary",
                   command=lambda: (w.destroy(), self.open_mirror())).pack(pady=12)

    def fill_addr(self, var):
        def job():
            _, out = self.dev.adb(["mdns", "services"], 25)
            self.log("搜索结果：\n" + (out.strip() or "(空)"))
            found = ""
            for line in out.splitlines():
                m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", line)
                if m and "pairing" in line:
                    found = m.group(0)
                    break
            if not found:
                for line in out.splitlines():
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", line)
                    if m:
                        found = m.group(0)
                        break
            if found:
                self.ui(lambda: var.set(found))
                self.log("填好了：%s" % found)
            else:
                self.log("没搜到，请在手机上点【使用配对码配对设备】后手动填")
        self.bg("搜索", job)

    def help_win(self):
        w = tk.Toplevel(self.root)
        w.title("帮助")
        w.geometry("720x600")
        w.configure(bg=BG)
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=U.S(18), pady=U.S(16))

        def page(t, lines):
            f = ttk.Frame(nb, padding=U.S(20))
            nb.add(f, text=t)
            for s in lines:
                tk.Label(f, text=s, justify="left", anchor="w", bg=CARD, fg=INK,
                         wraplength=620, font=(FONT, 10)).pack(fill="x", pady=3)

        page("中文输入", [
            "● 手机不会弹软键盘，不会挡住聊天框",
            "● 打中文：点手机上的输入框 → 电脑键盘敲拼音（nihao）",
            "  → 候选字出现在手机屏幕底部 → 空格或数字键选字",
            "● 英文数字直接敲",
            "● 复制粘贴：电脑 Ctrl+C，手机窗口里 Ctrl+V，反过来也行",
            "● 换输入法：手机【设置】→【语言和输入法】，搜狗 / 百度 / 讯飞 / 小米自带都支持外接键盘",
            "",
            "说明：电脑端输入法（比如搜狗 PC 版）的候选框没法搬到手机上，",
            "这是目前 scrcpy 体系下唯一能做到“电脑键盘打字 + 手机不弹键盘”的方案。",
        ])
        page("扫码", [
            "电脑上看到二维码（网页登录、收款码等）时：",
            "1. 点【电脑上扫码】，选一个 App（微信 / 支付宝 / 抖音等都行）",
            "2. 在屏幕上拖个框把二维码框住",
            "3. 看看截图对不对 → 【发到手机】",
            "4. 手机上会打开这个 App，你进【扫一扫】",
            "5. 点扫一扫里的【相册 / 图片】，选最新那张 qr_ 开头的图，就能识别",
            "",
            "原理：把二维码变成图片发到手机相册，让 App 从相册识别，不走相机，",
            "所以不用举手机对准屏幕。",
            "",
            "实测情况（在你的小米 Android 16 上跑过）：",
            "● 图片推送到手机相册、相册里能立刻看到：已验证通过",
            "● 微信、支付宝没对外暴露扫一扫入口，命令只能打开 App 首页，",
            "  所以进扫一扫这一步要你点一下（工具会提示具体点哪）",
            "● 能不能识别，取决于这个 App 的扫一扫有没有【相册】入口：",
            "  微信、支付宝是有的；抖音 / 小红书 / 淘宝 / 京东还没逐个验证，",
            "  你试完告诉我结果，我把结论写进工具",
            "",
            "注意：手机相机没法直接“看到”电脑屏幕，所以走相册这一步（多点一下）。",
        ])
        page("录屏", [
            "● 录之前选：录整个手机屏幕，还是只录某个 App",
            "● 清晰度可选原始（手机原生分辨率，最清楚）/ 1080 / 720，码率 8M~24M",
            "● 录的是手机发过来的原始画面，跟电脑窗口大小无关，窗口缩小不会变糊",
            "● 结束录制（三选一）：",
            "   1. 点屏幕右上角浮出来的【结束并保存】小条（一直在最前面，不会被挡）",
            "   2. 点主界面上出现的录制条里的【结束并保存】",
            "   3. 直接关掉手机窗口",
            "● 文件自动存到本文件夹的【录屏】里，MP4 格式，剪映 / PR 直接导入",
            "● 预览窗口建议保留；不勾的话窗口会最小化到任务栏，不影响录制内容",
            "● 录完之前别关这个工具，会提示你先停",
            "",
            "已在你的手机上实测：1080P / 16M 码率录 5 秒，文件正常、能播放。",
            "字不够清楚就把分辨率选“原始”、码率拉到 24M。",
        ])
        page("常见问题", [
            "连不上 / 搜不到：手机和电脑同一 WiFi、无线调试已开、手机已解锁",
            "能看画面但点不动：小米要开【USB 调试（安全设置）】，开完重启手机",
            "窗口打开就关：看底部日志，常见是上面这两条",
            "打字没反应：确认键盘模式是 uhid，并且先点一下手机上的输入框",
            "画面卡：设置里流畅度调到“流畅优先”，清晰度调到 1280 或更低",
            "App 独立窗口起不来：设置里把 App 窗口改成 main（在手机屏幕上打开）",
        ])


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    ensure_dpi_aware()          # 必须在创建窗口之前，截图/窗口坐标才一致
    root = tk.Tk()
    App(root)
    if os.environ.get("PM_SMOKE") == "1":
        root.after(3000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with open(os.path.join(APP_DIR, "启动错误.log"), "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise
