# -*- coding: utf-8 -*-
"""
底层能力：adb 控制、屏幕区域截图、录屏管理、scrcpy 参数拼装
（不含界面，界面在 phone_mirror.py）
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(APP_DIR, "vendor", "scrcpy", "scrcpy-win64-v5.0.1")
LOG_DIR = os.path.join(APP_DIR, "logs")
SHOT_DIR = os.path.join(APP_DIR, "截图")
REC_DIR = os.path.join(APP_DIR, "录屏")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
BIN_DIR = os.path.join(LOCAL, "PhoneMirror", "bin")
REC_TMP = os.path.join(LOCAL, "PhoneMirror", "rec")
ADB = os.path.join(BIN_DIR, "adb.exe")
SCRCPY = os.path.join(BIN_DIR, "scrcpy.exe")

# 手机上放扫码图片的位置（英文路径，避免编码问题）
PHONE_SCAN_DIR = "/sdcard/DCIM/ScanFromPC"

NEED_FILES = ["adb.exe", "scrcpy.exe", "scrcpy-server", "AdbWinApi.dll",
              "AdbWinUsbApi.dll", "SDL3.dll", "libusb-1.0.dll",
              "avcodec-63.dll", "avformat-63.dll", "avutil-61.dll",
              "swresample-7.dll"]


def ensure_dpi_aware():
    """
    声明进程感知 DPI。系统缩放不是 100% 时（比如 125%），
    不声明的话 GetWindowRect 给的是被 Windows 缩小的“虚拟坐标”，
    而 PIL 截屏抓的是物理像素，两边对不上，窗口截图就会偏。
    必须在创建任何窗口（tk.Tk）之前调用。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 按显示器感知
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def ensure_bin():
    os.makedirs(BIN_DIR, exist_ok=True)
    for f in NEED_FILES:
        src, dst = os.path.join(VENDOR_DIR, f), os.path.join(BIN_DIR, f)
        if os.path.exists(src) and (not os.path.exists(dst)
                                    or os.path.getsize(src) != os.path.getsize(dst)):
            try:
                shutil.copyfile(src, dst)
            except Exception:
                pass
    os.makedirs(REC_TMP, exist_ok=True)
    return os.path.exists(ADB) and os.path.exists(SCRCPY)


def run(cmd, timeout=20):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="ignore", startupinfo=si,
                           creationflags=0x08000000)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "执行超时"
    except Exception as e:
        return -2, "执行失败: %s" % e


# ------------------------------------------------------------------ 窗口操作

_u = ctypes.windll.user32


def find_window(title):
    return _u.FindWindowW(None, title) or 0


def window_rect(title):
    """窗口在屏幕上的位置 (left, top, right, bottom)，找不到返回 None"""
    h = find_window(title)
    if not h:
        return None
    r = wintypes.RECT()
    _u.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def raise_window(title):
    h = find_window(title)
    if h:
        _u.SetForegroundWindow(h)
        return True
    return False


def set_window_topmost(title, on):
    h = find_window(title)
    if not h:
        return False
    # -1 = HWND_TOPMOST, -2 = HWND_NOTOPMOST
    _u.SetWindowPos(h, -1 if on else -2, 0, 0, 0, 0, 0x0001 | 0x0002)
    return True


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPos", wintypes.POINT),
                ("ptMaxPos", wintypes.POINT), ("rcNormalPos", wintypes.RECT)]


def window_shown(title):
    """窗口是真的显示在屏幕上吗（最小化 / 隐藏都算不显示）"""
    h = find_window(title)
    if not h:
        return False
    if not _u.IsWindowVisible(h):
        return False
    return not _u.IsIconic(h)


def make_tool_window(title):
    """把窗口变成工具窗口：不占任务栏、不进 Alt+Tab"""
    h = find_window(title)
    if not h:
        return False
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    try:
        get = getattr(_u, "GetWindowLongPtrW", None) or _u.GetWindowLongW
        set_ = getattr(_u, "SetWindowLongPtrW", None) or _u.SetWindowLongW
        st = get(h, GWL_EXSTYLE)
        set_(h, GWL_EXSTYLE, (st | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        return True
    except Exception:
        return False


def is_maximized(title):
    h = find_window(title)
    if not h:
        return False
    wp = _WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
    _u.GetWindowPlacement(h, ctypes.byref(wp))
    return wp.showCmd == 3          # SW_SHOWMAXIMIZED


def place_window(title, rect, maximized=False, top=False, timeout=10.0):
    """
    把窗口摆成指定样子：先确保它真的显示出来（有时会被创建成隐藏或最小化），
    再恢复原来的位置和大小（或最大化状态）。
    """
    h = 0
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = find_window(title)
        if h:
            break
        time.sleep(0.15)
    if not h:
        return False
    if _u.IsIconic(h):                       # 被最小化就还原
        _u.ShowWindow(h, 9)                  # SW_RESTORE
    if not _u.IsWindowVisible(h):            # 被藏起来就显示
        _u.ShowWindow(h, 5)                  # SW_SHOW
    if maximized:
        _u.ShowWindow(h, 3)                  # SW_MAXIMIZE
    elif rect and rect[2] > rect[0]:
        _u.MoveWindow(h, rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1], True)
    if top:
        set_window_topmost(title, True)
    return True


# ---- 窗口一动就通知（比定时轮询跟手得多）
EVENT_LOCATIONCHANGE = 0x800B
_WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD,
                                   wintypes.HWND, wintypes.LONG, wintypes.LONG,
                                   wintypes.DWORD, wintypes.DWORD)
_hooks = {}


def watch_window_move(hwnd, callback):
    """窗口位置或大小一变就调用 callback，返回 True 表示挂上了"""
    if not hwnd:
        return False
    key = int(hwnd)
    unwatch_window_move(hwnd)

    def _cb(hHook, event, h, idObject, idChild, thread, ts):
        if int(h or 0) == key and idObject == 0:
            try:
                callback()
            except Exception:
                pass

    proc = _WINEVENTPROC(_cb)
    try:
        hk = _u.SetWinEventHook(EVENT_LOCATIONCHANGE, EVENT_LOCATIONCHANGE,
                                0, proc, 0, 0, 0)
    except Exception:
        return False
    if hk:
        _hooks[key] = (hk, proc, _cb)
        return True
    return False


def unwatch_window_move(hwnd):
    if not hwnd:
        return
    old = _hooks.pop(int(hwnd), None)
    if old:
        try:
            _u.UnhookWinEvent(old[0])
        except Exception:
            pass


def close_window(title):
    """正常关闭（让 scrcpy 自己收尾，视频才不会写坏）"""
    h = find_window(title)
    if not h:
        return False
    _u.PostMessageW(h, 0x0010, 0, 0)
    return True


def virtual_display_ids(serial):
    """列出 scrcpy 建的虚拟屏 displayId（新开的号更大）"""
    rc, out = run([ADB, "-s", serial, "shell", "dumpsys", "display"], timeout=25)
    ids = set()
    for m in re.finditer(r"displayId=(\d+),\s*uniqueId='virtual:com\.android\.shell,\d+,scrcpy",
                         out):
        ids.add(int(m.group(1)))
    return sorted(ids)


class _BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def capture_window(title, out_path):
    """
    截电脑上某个窗口的画面。
    优先用 PrintWindow 直接抓窗口自己的内容：不用把窗口顶到最前面，
    被别的窗口挡住也能截到，多开几个窗口互不影响。
    失败时退回老办法：把窗口提到前面再抓屏幕那一块。
    """
    hwnd = find_window(title)
    if not hwnd:
        raise ValueError("找不到这个窗口，可能已经关了")
    rect = window_rect(title)
    if not rect:
        raise ValueError("拿不到窗口位置")
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        raise ValueError("窗口尺寸不对")

    im = None
    try:
        u32, g32 = _u, ctypes.windll.gdi32
        hdc = u32.GetWindowDC(hwnd)
        mem = g32.CreateCompatibleDC(hdc)
        bmp = g32.CreateCompatibleBitmap(hdc, w, h)
        old = g32.SelectObject(mem, bmp)
        try:
            ok = u32.PrintWindow(hwnd, mem, 2)      # 2 = PW_RENDERFULLCONTENT
            bi = _BMIH()
            bi.biSize = ctypes.sizeof(_BMIH)
            bi.biWidth, bi.biHeight, bi.biPlanes = w, -h, 1
            bi.biBitCount = 32
            buf = ctypes.create_string_buffer(w * h * 4)
            if g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0) and ok:
                from PIL import Image
                im = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
                im = im.convert("RGB")
        finally:
            g32.SelectObject(mem, old)
            g32.DeleteObject(bmp)
            g32.DeleteDC(mem)
            u32.ReleaseDC(hwnd, hdc)
    except Exception:
        im = None

    if im is None:                                   # 退回抓屏幕区域
        raise_window(title)
        time.sleep(0.45)
        from PIL import ImageGrab
        im = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")

    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    im.save(out_path, "PNG")
    return out_path


# ------------------------------------------------------------------ 屏幕截图

def capture_region(box, out_path, min_side=520, border=36):
    """截取电脑屏幕上一块区域，放大加白边后存成 PNG（给手机相册识别二维码用）"""
    from PIL import Image, ImageGrab
    x1, y1, x2, y2 = [int(v) for v in box]
    if x2 - x1 < 8 or y2 - y1 < 8:
        raise ValueError("框太小了，重新框一次")
    im = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True).convert("RGB")
    w, h = im.size
    if max(w, h) < min_side:
        scale = max(1, int(round(min_side / float(max(w, h)))))
        im = im.resize((w * scale, h * scale), Image.NEAREST)  # 整数倍放大，边缘更锐利
    canvas = Image.new("RGB", (im.width + border * 2, im.height + border * 2), "white")
    canvas.paste(im, (border, border))
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    canvas.save(out_path, "PNG")
    return out_path


# ------------------------------------------------------------------ ADB

class Device:
    """对当前手机的操作封装"""

    def __init__(self, log=print):
        self.log = log
        self.serial = ""

    # 基础
    def adb(self, args, timeout=20):
        return run([ADB] + args, timeout=timeout)

    def devices(self):
        _, out = self.adb(["devices", "-l"], 15)
        res = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("List") or line.startswith("*"):
                continue
            p = line.split()
            if len(p) >= 2:
                res.append({"serial": p[0], "state": p[1]})
        return res

    def target(self):
        ds = [d for d in self.devices() if d["state"] == "device"]
        if not ds:
            return ""
        if self.serial and any(d["serial"] == self.serial for d in ds):
            return self.serial
        return ds[0]["serial"]

    def shell(self, cmd, timeout=15):
        s = self.target()
        base = [ADB] + (["-s", s] if s else [])
        return run(base + ["shell"] + cmd, timeout=timeout)

    # 信息
    def info(self):
        vals = {}
        for k, prop in [("brand", "ro.product.brand"),
                        ("model", "ro.product.model"),
                        ("ver", "ro.build.version.release")]:
            _, out = self.shell(["getprop", prop], 12)
            vals[k] = out.strip()
        return " ".join(v for v in vals.values() if v)

    def details(self):
        """一次拿齐设备卡片要用的信息：品牌型号 / 安卓版本 / 系统 / 分辨率"""
        rc, out = self.shell(["sh", "-c",
                              "echo brand=$(getprop ro.product.brand);"
                              "echo model=$(getprop ro.product.model);"
                              "echo ver=$(getprop ro.build.version.release);"
                              "echo os=$(getprop ro.mi.os.version.name);"
                              "echo miui=$(getprop ro.miui.ui.version.name);"
                              "wm size"], 15)
        d = {"brand": "", "model": "", "ver": "", "os": "", "res": ""}
        for line in out.splitlines():
            line = line.strip()
            if "=" in line:
                k, _, v = line.partition("=")
                if k in d:
                    d[k] = v.strip()
            elif "size:" in line.lower() and not d["res"]:
                d["res"] = line.split(":")[-1].strip().replace("x", " × ")
        d["model"] = " ".join(x for x in (d["brand"], d["model"]) if x)
        if d["os"] and d["os"].lower() in ("none", "unknown"):
            d["os"] = ""
        return d

    # 应用列表
    def packages(self, timeout=45):
        rc, out = self.shell(["cmd", "package", "list", "packages", "-3"], timeout)
        if not out.strip():
            _, out = self.adb(["shell", "pm", "list", "packages", "-3"], timeout)
        return [l.replace("package:", "").strip()
                for l in out.splitlines() if l.startswith("package:")]

    # 扫码：把图片推到手机相册
    def push_scan_image(self, local_path):
        name = os.path.basename(local_path)
        remote = PHONE_SCAN_DIR + "/" + name
        self.shell(["mkdir", "-p", PHONE_SCAN_DIR])
        rc, out = run([ADB] + (["-s", self.target()] if self.target() else [])
                      + ["push", local_path, remote], timeout=60)
        self.log("推送图片：%s" % out.strip())
        if rc != 0 or "error" in out.lower():
            # 有些机型不允许写 DCIM，退到 Pictures
            remote = "/sdcard/Pictures/" + name
            rc, out = run([ADB] + (["-s", self.target()] if self.target() else [])
                          + ["push", local_path, remote], timeout=60)
            self.log("改存到 Pictures：%s" % out.strip())
        # 让相册立刻能看到
        self.shell(["am", "broadcast", "-a",
                    "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                    "-d", "file://" + remote])
        self.shell(["content", "scan", "--_uri", "file://" + remote])
        return remote

    # 打开某个 App 的扫一扫
    SCAN_SCHEME = {
        "com.tencent.mm": "weixin://scanqrcode",
        "com.eg.android.AlipayGphone": "alipayqr://platformapi/startapp?saId=10000007",
        "com.taobao.taobao": "taobao://",
        "com.jingdong.app.mall": "openapp.jdmobile://",
    }

    def open_scan(self, pkg):
        """
        尽力把 App 拉到前台。
        实测：微信/支付宝都没对外暴露扫一扫入口（weixin://scanqrcode、
        alipayqr://... 都只打开 App 首页），所以只能帮你打开 App，
        扫一扫的那一步要自己点。
        """
        self.shell(["monkey", "-p", pkg, "-c",
                    "android.intent.category.LAUNCHER", "1"])
        scheme = self.SCAN_SCHEME.get(pkg)
        if scheme:
            rc, out = self.shell(["am", "start", "-a", "android.intent.action.VIEW",
                                  "-d", "'%s'" % scheme])
            self.log("尝试 %s 的扫一扫入口 -> %s" % (pkg, out.strip()[:100]))
        return True, "已经帮你把这个 App 打开到前台了"

    def screenshot(self, save_dir):
        s = self.target()
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, "截图_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        with open(path, "wb") as f:
            subprocess.run([ADB, "-s", s, "exec-out", "screencap", "-p"],
                           stdout=f, stderr=subprocess.DEVNULL, startupinfo=si,
                           creationflags=0x08000000, timeout=30)
        return path


# ------------------------------------------------------------------ scrcpy 参数

def scrcpy_args(serial, cfg, extra=None, title=None, record=None):
    """cfg: 界面上的设置项"""
    a = [SCRCPY]
    if serial:
        a += ["-s", serial]

    if cfg.get("keyboard") == "uhid":
        a += ["--keyboard=uhid"]

    mode = cfg.get("perf", "均衡")
    if mode == "流畅优先":
        # 录制时不要零缓冲，免得录出来丢帧
        if not cfg.get("recording"):
            a += ["--video-buffer=0"]
            a += ["--max-size=1280", "--max-fps=60"]
    elif mode == "高清优先":
        a += ["--video-bit-rate=16M"]
    if cfg.get("bitrate") and mode != "高清优先":
        a += ["--video-bit-rate=%s" % cfg["bitrate"]]

    # 录制时清晰度由录制设置决定，不要被画面缩放影响
    if cfg.get("recording"):
        rsize = cfg.get("rec_size", "原始")
        if rsize != "原始":
            a += ["--max-size=%s" % rsize]
    else:
        ms = int(cfg.get("max_size") or 0)
        if ms:
            a += ["--max-size=%d" % ms]
    fps = int(cfg.get("fps") or 0)
    if fps:
        a += ["--max-fps=%d" % fps]
    if cfg.get("codec") == "h265":
        a += ["--video-codec=h265"]
    if not cfg.get("audio"):
        a += ["--no-audio"]
    if cfg.get("record_audio"):
        a = [x for x in a if x != "--no-audio"]
    if cfg.get("stay_awake"):
        a += ["--stay-awake"]
    if cfg.get("screen_off") and not cfg.get("recording"):
        a += ["--turn-screen-off"]
    if cfg.get("always_top"):
        a += ["--always-on-top"]
    if cfg.get("no_mouse_hover"):
        a += ["--no-mouse-hover"]
    if cfg.get("render_driver"):
        a += ["--render-driver=%s" % cfg["render_driver"]]
    if cfg.get("hwdec") and cfg["hwdec"] != "auto":
        a += ["--hwdec=%s" % cfg["hwdec"]]
    if record:
        a += ["--record=%s" % record, "--record-format=mp4"]
    # 录制时不能最小化窗口（最小化会停止渲染，录出来是空的），
    # 不想看画面就把它缩小挪到角落，保持渲染
    if cfg.get("window_width"):
        a += ["--window-width=%d" % cfg["window_width"]]
    if cfg.get("window_geometry"):
        x, y = cfg["window_geometry"]
        a += ["--window-x=%d" % x, "--window-y=%d" % y]
    if cfg.get("no_window"):
        a += ["--no-window"]
    if title:
        a += ["--window-title=%s" % title]
    if extra:
        a += extra
    return a


# ------------------------------------------------------------------ 配置

DEFAULT_CFG = {
    "serial": "",
    "max_size": 1280,
    "fps": 60,
    "bitrate": "8M",
    "keyboard": "uhid",
    "app_mode": "virtual",
    "audio": False,
    "screen_off": False,
    "stay_awake": True,
    "always_top": False,
    "codec": "h264",
    "perf": "均衡",
    "rec_size": "原始",
    "rec_bitrate": "16M",
    "rec_audio": False,
    "rec_preview": True,
    "no_mouse_hover": False,
    "hwdec": "auto",
    "render_driver": "",
    "apps": [],
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
