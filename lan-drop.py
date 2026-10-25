#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish deliver · 肥鱼快递 —— 同一 Wi-Fi 下手机 ⇄ 电脑互传文件。

单文件、零第三方依赖、无广告、不走外网：电脑开一个本地网页服务，
手机浏览器扫码/输入地址就能上传下载，文件直接落在电脑硬盘上。
还带一个共享剪贴板，电脑和手机可以实时互传文字、链接。

用法:
    python lan-drop.py                     # 端口 8000，文件存到 D:\\LanDrop
    python lan-drop.py --port 9000
    python lan-drop.py --dir E:\\Share      # 自定义共享目录
    python lan-drop.py --no-browser        # 不自动打开浏览器

Windows 上如果敲 python 毫无反应（多半是应用商店的空壳别名），改用:
    py lan-drop.py
或者用真实解释器的完整路径，例如:
    D:\\python\\python.exe lan-drop.py

停止: Ctrl+C
"""

import argparse
import json
import mimetypes
import os
import socket
import stat
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

APP = "fish deliver"
APP_VERSION = "1.7"
MAX_NAME = 160
CHUNK = 256 * 1024
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ---------------------------------------------------------------- 工具函数

def human_size(n):
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
    return f"{n:.1f} PB"


def clean_name(raw):
    """只保留文件名本身，挡掉路径穿越和 Windows 非法字符。"""
    name = os.path.basename(str(raw).replace("\\", "/"))
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"/\\|?*')
    name = name.strip(" .")
    if not name:
        name = "file"
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    return name[:MAX_NAME]


def safe_join(base, name):
    """把 name 解析到 base 目录内，越界返回 None。"""
    name = os.path.basename(str(name).replace("\\", "/"))
    if not name or name in (".", ".."):
        return None
    base = base.resolve()
    p = (base / name).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    return p


def unique_path(folder, name):
    """同名文件自动加 (1)(2)，不覆盖已有文件。"""
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = os.path.splitext(name)
    for i in range(1, 10000):
        target = folder / f"{stem} ({i}){suffix}"
        if not target.exists():
            return target
    return folder / f"{stem}-{os.getpid()}{suffix}"


def content_disposition(name):
    fallback = name.encode("ascii", "ignore").decode("ascii").strip()
    fallback = fallback.replace("\\", "_").replace('"', "_")
    stem = fallback.rsplit(".", 1)[0] if "." in fallback else fallback
    if not any(ch.isalnum() for ch in stem):
        suffix = os.path.splitext(name)[1]
        suffix = suffix.encode("ascii", "ignore").decode("ascii").strip()
        suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")
        fallback = "file" + suffix
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def lan_ips():
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # 不会真的发包，只为拿到出口网卡地址
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            found.append(info[4][0])
    except OSError:
        pass

    out = []
    for ip in found:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip in out:
            continue
        out.append(ip)

    def rank(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            return 2
        return 3

    out.sort(key=rank)
    return out


def default_share_dir():
    # 优先放 D 盘，避免占 C 盘空间；没有 D 盘再退回下载目录
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/LanDrop")
    home = Path.home()
    downloads = home / "Downloads"
    return (downloads if downloads.is_dir() else home) / "LanDrop"


# ---------------------------------------------------------------- 请求处理

class Handler(BaseHTTPRequestHandler):
    server_version = "FishDeliver/1.7"
    protocol_version = "HTTP/1.1"
    share_dir = Path(".")
    lan_url = ""
    text_lock = threading.Lock()
    zip_lock = threading.Lock()
    zip_tokens = {}

    # 安静一点，不打印每个请求
    def log_message(self, fmt, *args):
        pass

    # ---- 基础响应

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if code >= 400:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _list_files(self):
        items = []
        try:
            entries = list(self.share_dir.iterdir())
        except OSError:
            return items
        for p in entries:
            try:
                if p.is_file() and not p.name.startswith("."):
                    st = p.stat()
                    items.append({"name": p.name, "size": st.st_size,
                                  "mtime": st.st_mtime})
            except OSError:
                continue
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    # ---- 共享剪贴板（存成一个隐藏小文件）

    def _text_path(self):
        return self.share_dir / ".shared-text.json"

    def _read_text(self):
        try:
            data = json.loads(self._text_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        try:
            updated = float(data.get("updated", 0) or 0)
        except (TypeError, ValueError):
            updated = 0.0
        return {"text": str(data.get("text", "")),
                "updated": updated,
                "by": str(data.get("by", ""))}

    def _write_text(self, text, by):
        payload = {"text": text, "updated": time.time(), "by": by}
        tmp = self.share_dir / ".shared-text.tmp"
        try:
            with self.text_lock:
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._text_path())
        except OSError:
            return None
        return payload

    # ---- GET

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            page = INDEX_HTML.replace("__LAN_URL__", self.lan_url)
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/qr.js":
            self._send(200, QR_JS, "application/javascript; charset=utf-8")
        elif path == "/api/list":
            self._json({"app": APP, "version": APP_VERSION,
                        "files": self._list_files(), "url": self.lan_url,
                        "dir": str(self.share_dir)})
        elif path in ("/bg.jpg", "/hero.jpg", "/favicon.ico"):
            self._send_asset(path)
        elif path == "/api/text":
            self._json(self._read_text())
        elif path == "/api/zip":
            query = parse_qs(urlsplit(self.path).query)
            token = (query.get("t") or [""])[0]
            if token:
                with self.zip_lock:
                    item = self.zip_tokens.pop(token, None)
                if not item:
                    self._send(404, "下载链接已过期，请回到页面重新点一次")
                    return
                self._zip_files(item[0])
            else:
                self._zip_files(None)
        elif path.startswith("/f/"):
            self._send_file(unquote(path[3:]))
        else:
            self._send(404, "not found")

    def _send_file(self, name):
        target = safe_join(self.share_dir, name)
        if not target or not target.is_file() or target.name.startswith("."):
            self._send(404, "file not found")
            return
        try:
            size = target.stat().st_size
        except OSError:
            self._send(404, "file not found")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", content_disposition(target.name))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with target.open("rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_asset(self, path):
        base = Path(__file__).resolve().parent
        if path == "/bg.jpg":
            file, ctype = base / "fish-deliver-bg.jpg", "image/jpeg"
        elif path == "/hero.jpg":
            file, ctype = base / "fish-deliver-hero.jpg", "image/jpeg"
        else:
            file, ctype = base / "fish-deliver-icon.ico", "image/x-icon"
        try:
            data = file.read_bytes()
        except OSError:
            self._send(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _zip_files(self, names):
        """names 为 None 时打包全部；否则只打包勾选的文件。"""
        files = []
        if names is not None:
            seen = set()
            for raw in names:
                p = safe_join(self.share_dir, raw)
                if not p or not p.is_file() or p.name.startswith("."):
                    continue
                if p.name in seen:
                    continue
                seen.add(p.name)
                files.append(p)
            files.sort(key=lambda q: q.name.lower())
        else:
            try:
                entries = sorted(self.share_dir.iterdir(), key=lambda q: q.name.lower())
            except OSError:
                entries = []
            for p in entries:
                try:
                    if p.is_file() and not p.name.startswith("."):
                        files.append(p)
                except OSError:
                    continue
        if not files:
            self._send(404, "没有可打包的文件")
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        tmp = self.share_dir / f".zip-{uuid.uuid4().hex}.part"
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=1, allowZip64=True) as zf:
                for p in files:
                    try:
                        # 大文件（视频/图片）直接存储，省 CPU；小文件压缩
                        big = p.stat().st_size > 50 * 1024 * 1024
                        zf.write(p, arcname=p.name,
                                 compress_type=zipfile.ZIP_STORED if big
                                 else zipfile.ZIP_DEFLATED)
                    except OSError:
                        continue
            size = tmp.stat().st_size
        except Exception as exc:
            print(f"  打包失败: {exc}", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass
            self._send(500, "打包失败")
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="fish-deliver-{stamp}.zip"')
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with tmp.open("rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    # ---- POST

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/api/upload":
            self._upload(parse_qs(parts.query))
        elif parts.path == "/api/delete":
            self._delete()
        elif parts.path == "/api/clear":
            self._clear()
        elif parts.path == "/api/text":
            self._post_text()
        elif parts.path == "/api/zip":
            self._post_zip()
        else:
            self._send(404, "not found")

    def _post_zip(self):
        ctype = self.headers.get("Content-Type", "")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > 2_000_000:
            self._send(413, "too large")
            return
        body = self.rfile.read(length) if length else b""
        names = []
        try:
            if "application/json" in ctype:
                data = json.loads(body or b"{}")
                if isinstance(data, dict):
                    names = [str(n) for n in (data.get("names") or [])]
            else:
                names = parse_qs(body.decode("utf-8", "replace")).get("name") or []
        except (ValueError, UnicodeDecodeError):
            names = []
        names = [n for n in names if n][:500]

        token = uuid.uuid4().hex
        now = time.time()
        with self.zip_lock:
            for key in [k for k, v in self.zip_tokens.items() if v[1] < now]:
                self.zip_tokens.pop(key, None)
            if len(self.zip_tokens) > 200:
                self.zip_tokens.clear()
            self.zip_tokens[token] = (names, now + 600)

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._json({"url": f"/api/zip?t={token}",
                    "name": f"fish-deliver-{stamp}.zip",
                    "count": len(names)})

    def _post_text(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 400_000:
            self._send(400, "bad request")
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, "bad json")
            return
        if not isinstance(data, dict):
            self._send(400, "bad json")
            return
        text = str(data.get("text", ""))[:200_000]
        by = str(data.get("by", ""))[:20]
        payload = self._write_text(text, by)
        if payload is None:
            self._json({"ok": False}, 500)
        else:
            self._json({"ok": True, "updated": payload["updated"], "by": by})

    def _clear(self):
        removed, failed = 0, []
        try:
            entries = list(self.share_dir.iterdir())
        except OSError as exc:
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        for p in entries:
            try:
                if not p.is_file() or p.name.startswith("."):
                    continue
            except OSError:
                continue
            try:
                p.unlink()
                removed += 1
                continue
            except OSError:
                pass
            # 只读文件先去掉只读属性再试一次
            try:
                os.chmod(p, stat.S_IWRITE)
                p.unlink()
                removed += 1
            except OSError:
                failed.append(p.name)
        print(f"  清空：删除 {removed} 个，失败 {len(failed)} 个", flush=True)
        self._json({"ok": True, "removed": removed, "failed": failed})

    def _upload(self, query):
        raw_name = (query.get("name") or [""])[0]
        name = clean_name(raw_name)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send(411, "length required")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send(400, "bad length")
            return
        if length < 0:
            self._send(400, "bad length")
            return

        target = unique_path(self.share_dir, name)
        tmp = self.share_dir / f".upload-{uuid.uuid4().hex}.part"
        got = 0
        try:
            with tmp.open("wb") as f:
                while got < length:
                    chunk = self.rfile.read(min(CHUNK, length - got))
                    if not chunk:
                        raise ConnectionError("client closed")
                    f.write(chunk)
                    got += len(chunk)
            os.replace(tmp, target)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            self.close_connection = True
            return

        print(f"  + {target.name}  ({human_size(got)})", flush=True)
        self._json({"ok": True, "name": target.name, "size": got})

    def _delete(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            self._send(400, "bad request")
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, "bad json")
            return
        target = safe_join(self.share_dir, str(data.get("name", "")))
        if not target or not target.is_file():
            self._send(404, "not found")
            return
        try:
            target.unlink()
        except OSError:
            try:
                os.chmod(target, stat.S_IWRITE)
                target.unlink()
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
                return
        self._json({"ok": True})


# ---------------------------------------------------------------- 页面

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0e13">
<title>fish deliver · 肥鱼快递</title>
<link rel="icon" href="/favicon.ico">
<style>
:root{
  --bg:#0b0e13; --card:rgba(20,26,34,.92); --card2:#1c2430; --line:#26313d;
  --text:#e7eef7; --muted:#8a99ab; --accent:#4da3ff; --green:#37d6a0;
  --danger:#ff6b6b; --radius:14px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;background:var(--bg)}
body{
  margin:0; padding:22px 16px 44px; min-height:100vh; color:var(--text);
  background:var(--bg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-tap-highlight-color:transparent;
}
.bg{position:fixed;inset:0;z-index:0;background:#0b0e13 url("/bg.jpg") center center / cover no-repeat}
.bg::after{content:"";position:absolute;inset:0;
  background:linear-gradient(180deg,rgba(7,10,15,.28) 0%,rgba(7,10,15,.44) 55%,rgba(7,10,15,.62) 100%)}
main{position:relative;z-index:1;max-width:680px;margin:0 auto}
.topbar{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:2px 2px 14px}
h1{margin:0;font-size:19px;letter-spacing:.3px;white-space:nowrap}
.logo{color:var(--accent)}
.cn{font-size:12.5px;color:var(--muted);margin-left:8px;letter-spacing:2px;font-weight:400}
.sub{margin:0;color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px}
main > * + *{margin-top:12px}
.qr{position:relative;display:flex;align-items:center;gap:18px;overflow:hidden;min-height:200px}
.qr-bg{position:absolute;inset:0;background:#0b0e13 url("/hero.jpg") center 42% / cover no-repeat}
.qr::after{content:"";position:absolute;inset:0;
  background:linear-gradient(90deg,rgba(7,10,15,.93) 0%,rgba(7,10,15,.70) 56%,rgba(7,10,15,.20) 100%)}
#qr,.qr-side{position:relative;z-index:1}
#qr{flex:0 0 132px;width:132px;height:132px}
#qr svg{display:block;width:100%;height:auto;background:#fff;border-radius:10px;padding:9px}
.qr-side{min-width:0}
.qr-label{color:var(--muted);font-size:12px;letter-spacing:.4px}
.url{font:13px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;margin:5px 0 10px;
     word-break:break-all;color:#cfe3ff}
.ghost{background:var(--card2);color:var(--text);border:1px solid var(--line);
       border-radius:9px;padding:7px 12px;font-size:13px;cursor:pointer;white-space:nowrap}
.ghost:hover{border-color:#33445a}
.ghost:active{transform:scale(.97)}
.ghost.sm{padding:5px 10px;font-size:12.5px;border-radius:8px}
.ghost.danger{color:#ff9a9a}
.ghost.danger:hover{border-color:#5a2c31;background:#241a1d}

/* 共享剪贴板 */
.text-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:9px}
.card-title{font-size:13px;color:var(--muted);letter-spacing:.5px}
.sync{font-size:12px;color:#5f8b74;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#sharedText{
  width:100%;min-height:78px;resize:vertical;outline:none;
  background:#0f151c;border:1px solid var(--line);border-radius:10px;
  color:var(--text);padding:10px 12px;font:14px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;
}
#sharedText:focus{border-color:#2f5d8a}
.text-actions{display:flex;align-items:center;gap:8px;margin-top:9px;flex-wrap:wrap}
.link{color:var(--accent);font-size:12.5px;text-decoration:none;margin-left:auto}
.link:hover{text-decoration:underline}

/* 上传区 */
.drop{cursor:pointer;text-align:center;border:1.5px dashed #33404f;transition:border-color .15s,background .15s}
.drop:hover{border-color:#41556b}
.drop.over{border-color:var(--accent);background:#182638}
.drop .icon{font-size:26px;line-height:1;color:var(--accent)}
.drop-title{margin-top:9px;font-size:15px}
.drop-hint{margin-top:5px;color:var(--muted);font-size:12.5px;line-height:1.6}
.queue{display:flex;flex-direction:column;gap:8px}
#batchInfo:not([hidden]) + .queue{margin-top:8px}
.queue:empty{display:none}
.batch{font-size:12.5px;color:#7f93a8;padding:2px 2px 0}
.up{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:10px 12px}
.up-top{display:flex;justify-content:space-between;gap:12px;font-size:13px;margin-bottom:7px}
.up-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up-pct{color:var(--muted);flex:none;font-variant-numeric:tabular-nums}
.bar{height:4px;background:#0d131a;border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--accent);border-radius:99px;
       transition:width .15s ease}
.up.done .bar>i{background:var(--green)}
.up.err .bar>i{background:var(--danger)}
.up.err .up-pct{color:var(--danger)}

/* 文件列表 */
.list-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.list-title{flex:1;min-width:110px}
.list-title b{color:var(--muted);font-weight:500;font-size:12px;margin-left:4px}
.list-actions{display:flex;gap:6px;flex-wrap:wrap}
.check-all{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);
           cursor:pointer;user-select:none;white-space:nowrap}
.check-all input,.pick{accent-color:var(--accent);cursor:pointer;margin:0}
.check-all input{width:15px;height:15px}
.pick{flex:none;width:17px;height:17px}
#zipBtn:disabled{opacity:.45;cursor:not-allowed}
#files{list-style:none;margin:0;padding:0}
#files li{display:flex;align-items:center;gap:11px;padding:11px 0;border-top:1px solid var(--line)}
#files li:first-child{border-top:0}
.ficon{flex:none;font-size:19px;width:24px;text-align:center}
.fmain{flex:1;min-width:0}
.fname{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fmeta{display:block;color:var(--muted);font-size:12px;margin-top:1px}
.dl{flex:none;color:var(--accent);text-decoration:none;font-size:13px;
    border:1px solid #264056;background:#16283a;border-radius:8px;padding:6px 11px}
.dl:active{transform:scale(.97)}
.del{flex:none;background:none;border:0;color:#5b6b7d;font-size:17px;line-height:1;
     padding:6px;cursor:pointer;border-radius:7px}
.del:hover{color:var(--danger);background:#241a1d}
.empty{color:var(--muted);text-align:center;font-size:13px;padding:18px 0 10px}
footer{margin:22px 4px 0;color:#93a1b3;font-size:12px;text-align:center;line-height:1.8;
       text-shadow:0 1px 10px rgba(0,0,0,.65)}
footer code{color:#7f93a8;font:12px ui-monospace,Consolas,monospace;word-break:break-all}
@media (max-width:560px){
  body{padding:16px 12px 34px}
  .qr{display:none}
  h1{font-size:18px}
  .text-actions .hint{display:none}
  .list-title{flex:1 1 auto;min-width:0}
  .list-actions{flex:0 0 100%;justify-content:flex-start}
}
</style>
</head>
<body>
<div class="bg"></div>
<main>
  <header class="topbar">
    <h1><span class="logo">fish deliver</span><span class="cn">肥鱼快递</span></h1>
    <p class="sub">同一 Wi-Fi · 手机 ⇄ 电脑 · 直传</p>
  </header>

  <section class="card qr">
    <div class="qr-bg"></div>
    <div id="qr"></div>
    <div class="qr-side">
      <div class="qr-label">手机扫码打开</div>
      <div class="url" id="urlText">__LAN_URL__</div>
      <button class="ghost" id="copyBtn">复制地址</button>
    </div>
  </section>

  <section class="card">
    <div class="text-head">
      <span class="card-title">共享剪贴板</span>
      <span class="sync" id="textStatus">读取中…</span>
    </div>
    <textarea id="sharedText" placeholder="在这里输入文字或粘贴链接，另一台设备会实时同步…"></textarea>
    <div class="text-actions">
      <button class="ghost sm" id="copyText">复制文字</button>
      <button class="ghost sm" id="clearText">清空</button>
      <a class="link" id="openLink" target="_blank" rel="noopener" hidden>打开链接 ↗</a>
    </div>
  </section>

  <section class="card drop" id="drop">
    <input type="file" id="fileInput" multiple hidden>
    <div class="icon">⇪</div>
    <div class="drop-title">点击选择文件，或拖到这里</div>
    <div class="drop-hint">可多选 · 单文件不限大小<br>多选最多 100 个，合计不超过 500 MB</div>
  </section>

  <div class="batch" id="batchInfo" hidden></div>
  <section class="queue" id="queue"></section>

  <section class="card">
    <div class="list-head">
      <label class="check-all" title="取消勾选的文件不会下载"><input type="checkbox" id="selectAll" checked><span>全选</span></label>
      <span class="list-title">共享文件 <b id="count">0</b></span>
      <div class="list-actions">
        <button class="ghost sm" id="dlBtn" disabled title="逐个下载勾选的文件">下载所选 (0)</button>
        <button class="ghost sm" id="zipBtn" disabled title="把勾选的文件打成一个 zip 下载">打包下载 (0)</button>
        <button class="ghost sm danger" id="clearBtn">清空</button>
        <button class="ghost sm" id="refreshBtn">刷新</button>
      </div>
    </div>
    <ul id="files"></ul>
    <div class="empty" id="empty">还没有文件，传一个试试</div>
  </section>

  <footer>文件只保存在这台电脑上 · 不经过互联网<br><code id="shareDir">…</code></footer>
</main>

<script src="/qr.js"></script>
<script>
(function () {
  "use strict";
  var LAN_URL = "__LAN_URL__";
  var MAX_FILES = 100;
  var MAX_BATCH = 500 * 1024 * 1024;   // 多选时合计上限；单文件不受限
  var $ = function (s) { return document.querySelector(s); };
  var listEl = $("#files"), emptyEl = $("#empty"), countEl = $("#count");
  var queueEl = $("#queue"), dropEl = $("#drop"), inputEl = $("#fileInput");
  var batchEl = $("#batchInfo");

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    var units = ["KB", "MB", "GB", "TB"], i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return (n < 10 ? n.toFixed(1) : Math.round(n)) + " " + units[i];
  }

  function fmtTime(ts) {
    var d = new Date(ts * 1000), now = new Date();
    var diff = (now - d) / 1000;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (d.getFullYear() === now.getFullYear())
      return (d.getMonth() + 1) + " 月 " + d.getDate() + " 日";
    return d.getFullYear() + "/" + (d.getMonth() + 1) + "/" + d.getDate();
  }

  function iconFor(name) {
    var e = (name.split(".").pop() || "").toLowerCase();
    if (/^(png|jpg|jpeg|gif|webp|bmp|svg|heic|avif)$/.test(e)) return "🖼️";
    if (/^(mp4|mov|mkv|avi|webm|m4v)$/.test(e)) return "🎬";
    if (/^(mp3|wav|flac|m4a|aac|ogg|opus)$/.test(e)) return "🎵";
    if (/^(zip|rar|7z|tar|gz|xz)$/.test(e)) return "📦";
    if (/^(pdf)$/.test(e)) return "📕";
    if (/^(doc|docx|rtf|txt|md)$/.test(e)) return "📄";
    if (/^(xls|xlsx|csv)$/.test(e)) return "📊";
    if (/^(ppt|pptx|key)$/.test(e)) return "📽️";
    if (/^(apk|exe|msi|dmg)$/.test(e)) return "⚙️";
    return "📎";
  }

  /* ---------------- 文件列表 ---------------- */

  var fileCount = 0, lastSig = "";
  var selected = {}, known = {};
  var selectAllEl = $("#selectAll");

  function updateSelUI() {
    var n = Object.keys(selected).length;
    var zip = $("#zipBtn"), dl = $("#dlBtn");
    zip.textContent = "打包下载 (" + n + ")";
    dl.textContent = "下载所选 (" + n + ")";
    zip.disabled = n === 0;
    dl.disabled = n === 0;
    if (n === 0 || fileCount === 0) {
      selectAllEl.checked = false;
      selectAllEl.indeterminate = false;
    } else if (n === fileCount) {
      selectAllEl.checked = true;
      selectAllEl.indeterminate = false;
    } else {
      selectAllEl.checked = false;
      selectAllEl.indeterminate = true;
    }
  }

  selectAllEl.addEventListener("change", function () {
    var on = selectAllEl.checked;
    var boxes = listEl.querySelectorAll(".pick");
    for (var i = 0; i < boxes.length; i++) {
      boxes[i].checked = on;
      var name = boxes[i].getAttribute("data-name");
      if (on) selected[name] = 1; else delete selected[name];
    }
    updateSelUI();
  });

  updateSelUI();

  function render(files) {
    var sig = files.map(function (f) {
      return f.name + "|" + f.size + "|" + Math.round(f.mtime);
    }).join("~");
    if (sig === lastSig) return;
    lastSig = sig;

    // 新文件默认勾选；用户取消过的保持取消；已删除的清掉
    var present = {};
    files.forEach(function (f) {
      present[f.name] = 1;
      if (!known[f.name]) selected[f.name] = 1;
    });
    Object.keys(selected).forEach(function (name) {
      if (!present[name]) delete selected[name];
    });
    known = present;

    fileCount = files.length;
    countEl.textContent = files.length;
    emptyEl.hidden = files.length > 0;
    listEl.textContent = "";
    files.forEach(function (f) {
      var li = document.createElement("li");

      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "pick";
      cb.checked = !!selected[f.name];
      cb.setAttribute("data-name", f.name);
      cb.title = "勾选后一起打包下载";
      cb.onchange = function () {
        if (cb.checked) selected[f.name] = 1; else delete selected[f.name];
        updateSelUI();
      };

      var icon = document.createElement("span");
      icon.className = "ficon";
      icon.textContent = iconFor(f.name);

      var main = document.createElement("div");
      main.className = "fmain";
      var nm = document.createElement("span");
      nm.className = "fname";
      nm.textContent = f.name;
      nm.title = f.name;
      var meta = document.createElement("span");
      meta.className = "fmeta";
      meta.textContent = fmtSize(f.size) + " · " + fmtTime(f.mtime);
      main.appendChild(nm);
      main.appendChild(meta);

      var a = document.createElement("a");
      a.className = "dl";
      a.textContent = "下载";
      a.href = "/f/" + encodeURIComponent(f.name);

      var del = document.createElement("button");
      del.className = "del";
      del.textContent = "✕";
      del.title = "删除";
      del.onclick = function () {
        if (!window.confirm("删除「" + f.name + "」？")) return;
        fetch("/api/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: f.name })
        }).then(function (r) { return r.json(); })
          .then(function (d) {
            refresh();
            if (d && d.ok === false) {
              window.alert("删除失败，文件可能正被其他程序打开。");
            }
          })
          .catch(function () { window.alert("删除失败，请重试。"); });
      };

      li.appendChild(cb);
      li.appendChild(icon);
      li.appendChild(main);
      li.appendChild(a);
      li.appendChild(del);
      listEl.appendChild(li);
    });
    updateSelUI();
  }

  var refreshing = false;
  function refresh() {
    if (refreshing) return;
    refreshing = true;
    fetch("/api/list", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        render(d.files || []);
        if (d.dir) $("#shareDir").textContent = d.dir;
      })
      .catch(function () {})
      .then(function () { refreshing = false; });
  }

  /* ---------------- 上传队列 ---------------- */

  var queue = [], pumping = false, batchTotal = 0;

  function enqueue(fileList) {
    var files = [];
    for (var i = 0; i < fileList.length; i++) files.push(fileList[i]);
    if (!files.length) return;

    var total = 0;
    files.forEach(function (f) { total += f.size; });

    if (files.length > 1) {
      if (files.length > MAX_FILES) {
        window.alert("一次最多选 " + MAX_FILES + " 个文件（这次选了 " +
          files.length + " 个）。\n这是为了防止误选整个文件夹。");
        return;
      }
      if (total > MAX_BATCH) {
        window.alert("多文件上传时，合计不能超过 500 MB。\n这次合计 " +
          fmtSize(total) + "。\n\n单个文件不受此限制，可以单独传。");
        return;
      }
    }

    batchTotal += total;
    batchEl.hidden = false;
    batchEl.textContent = "待上传 " + (queue.length + files.length) +
      " 个文件 · 合计 " + fmtSize(batchTotal) + " · 正在上传…";

    files.forEach(function (f) { queue.push(f); });
    pump();
  }

  function pump() {
    if (pumping) return;
    pumping = true;
    (function next() {
      if (!queue.length) {
        pumping = false;
        batchEl.textContent = "上传完成 · 本次 " + fmtSize(batchTotal);
        setTimeout(function () { batchEl.hidden = true; batchTotal = 0; }, 2500);
        refresh();
        return;
      }
      uploadOne(queue.shift()).then(next);
    })();
  }

  function uploadOne(file) {
    return new Promise(function (resolve) {
      var row = document.createElement("div");
      row.className = "up";
      row.innerHTML =
        '<div class="up-top"><span class="up-name"></span>' +
        '<span class="up-pct">0%</span></div><div class="bar"><i></i></div>';
      row.querySelector(".up-name").textContent = file.name;
      var bar = row.querySelector(".bar > i");
      var pct = row.querySelector(".up-pct");
      queueEl.appendChild(row);

      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload?name=" + encodeURIComponent(file.name));
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        var p = Math.round((e.loaded / e.total) * 100);
        bar.style.width = p + "%";
        pct.textContent = p + "%";
      };
      function fail() {
        row.classList.add("err");
        pct.textContent = "失败";
        setTimeout(function () { row.remove(); }, 5000);
        resolve();
      }
      xhr.onload = function () {
        if (xhr.status === 200) {
          row.classList.add("done");
          bar.style.width = "100%";
          pct.textContent = "完成";
          setTimeout(function () { row.remove(); }, 900);
          resolve();
        } else {
          fail();
        }
      };
      xhr.onerror = fail;
      xhr.onabort = fail;
      xhr.send(file);
    });
  }

  /* ---------------- 共享剪贴板 ---------------- */

  var textEl = $("#sharedText"), textStatus = $("#textStatus"), openLink = $("#openLink");
  var textDirty = false, textTimer = null, appliedAt = 0;
  var DEVICE = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) ? "手机" : "电脑";

  function setStatus(d) {
    if (!d || !d.updated) { textStatus.textContent = "还没有内容"; return; }
    var t = new Date(d.updated * 1000);
    var hh = ("0" + t.getHours()).slice(-2) + ":" + ("0" + t.getMinutes()).slice(-2);
    textStatus.textContent = (d.by ? "来自" + d.by + " · " : "") + hh;
  }

  function updateLink() {
    var v = textEl.value.trim();
    if (/^https?:\/\/\S+$/i.test(v)) {
      openLink.href = v;
      openLink.hidden = false;
    } else {
      openLink.hidden = true;
    }
  }

  function pullText() {
    fetch("/api/text", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.updated > appliedAt && !textDirty && textEl.value !== d.text) {
          textEl.value = d.text || "";
          appliedAt = d.updated;
          updateLink();
        }
        setStatus(d);
      })
      .catch(function () {});
  }

  function pushText() {
    var val = textEl.value;
    textDirty = false;
    return fetch("/api/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: val, by: DEVICE })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.updated) { appliedAt = d.updated; setStatus({ updated: d.updated, by: DEVICE }); }
      })
      .catch(function () { textDirty = true; });
  }

  textEl.addEventListener("input", function () {
    textDirty = true;
    textStatus.textContent = "输入中…";
    updateLink();
    clearTimeout(textTimer);
    textTimer = setTimeout(pushText, 450);
  });
  textEl.addEventListener("blur", function () {
    if (textDirty) pushText();
  });

  $("#copyText").addEventListener("click", function () {
    var btn = this, val = textEl.value;
    function done() {
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = "复制文字"; }, 1400);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(val).then(done, function () {});
    } else {
      textEl.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
    }
  });

  $("#clearText").addEventListener("click", function () {
    if (textEl.value && !window.confirm("清空共享剪贴板内容？")) return;
    textEl.value = "";
    updateLink();
    pushText();
  });

  /* ---------------- 二维码 / 复制地址 ---------------- */

  function drawQR() {
    if (typeof qrcode !== "function") return;
    try {
      var qr = qrcode(0, "M");
      qr.addData(LAN_URL);
      qr.make();
      $("#qr").innerHTML = qr.createSvgTag({ cellSize: 6, margin: 0, scalable: true });
    } catch (err) {}
  }

  $("#copyBtn").addEventListener("click", function () {
    var btn = this;
    function done() {
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = "复制地址"; }, 1500);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(LAN_URL).then(done, function () {});
    } else {
      var ta = document.createElement("textarea");
      ta.value = LAN_URL;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      ta.remove();
    }
  });

  /* ---------------- 选择 / 拖拽 / 粘贴 ---------------- */

  dropEl.addEventListener("click", function () { inputEl.click(); });
  inputEl.addEventListener("change", function () {
    if (inputEl.files.length) enqueue(inputEl.files);
    inputEl.value = "";
  });
  ["dragenter", "dragover"].forEach(function (ev) {
    dropEl.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropEl.classList.add("over");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dropEl.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropEl.classList.remove("over");
    });
  });
  dropEl.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) enqueue(e.dataTransfer.files);
  });
  document.addEventListener("paste", function (e) {
    if (e.target === textEl) return;
    if (e.clipboardData && e.clipboardData.files && e.clipboardData.files.length) {
      e.preventDefault();
      enqueue(e.clipboardData.files);
    }
  });

  /* ---------------- 下载所选 / 打包下载 / 清空 / 刷新 ---------------- */

  $("#dlBtn").addEventListener("click", function () {
    var names = Object.keys(selected);
    if (!names.length) return;
    if (names.length > 10 &&
        !window.confirm("要逐个下载 " + names.length + " 个文件吗？\n" +
                        "文件多时建议用「打包下载」。")) return;
    names.forEach(function (name, i) {
      setTimeout(function () {
        var a = document.createElement("a");
        a.href = "/f/" + encodeURIComponent(name);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
      }, i * 400);
    });
  });

  $("#zipBtn").addEventListener("click", function () {
    var names = Object.keys(selected);
    if (!names.length) { window.alert("没有勾选任何文件。"); return; }
    var btn = this;
    btn.disabled = true;
    btn.textContent = "打包中…";
    fetch("/api/zip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names: names })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.url) throw new Error("no url");
        var a = document.createElement("a");
        a.href = d.url;
        a.download = d.name || "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      })
      .catch(function () { window.alert("打包失败，请重试。"); })
      .then(function () {
        setTimeout(function () { updateSelUI(); }, 1500);
      });
  });

  $("#clearBtn").addEventListener("click", function () {
    if (!fileCount) { window.alert("共享目录已经是空的。"); return; }
    if (!window.confirm("确定清空共享目录里的 " + fileCount + " 个文件？\n此操作不可恢复。")) return;
    fetch("/api/clear", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        refresh();
        if (d && d.failed && d.failed.length) {
          var names = d.failed.slice(0, 5).join("\n");
          if (d.failed.length > 5) names += "\n…";
          window.alert("已删除 " + d.removed + " 个，还有 " + d.failed.length +
                       " 个没删掉（可能正被其他程序打开）：\n" + names);
        }
      })
      .catch(function () { window.alert("清空失败，请重试。"); });
  });

  $("#refreshBtn").addEventListener("click", refresh);

  setInterval(function () { if (!document.hidden) refresh(); }, 3000);
  setInterval(function () { if (!document.hidden) pullText(); }, 1500);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { refresh(); pullText(); }
  });

  drawQR();
  refresh();
  pullText();
  updateLink();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 入口

def open_browser(url):
    try:
        ok = webbrowser.open(url)
    except Exception:
        ok = False
    if not ok:
        print(f"  浏览器没有自动打开，请手动访问 {url}", flush=True)


def probe_running(port):
    """探测端口上是不是已经有一个肥鱼快递在跑。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/list",
                                    timeout=0.6) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    if isinstance(data, dict) and "files" in data and "url" in data:
        return data
    return None


def main():
    # 有些终端/IDE 把输出接到管道，默认会整块缓冲，看起来像“没反应”
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="fish deliver · 肥鱼快递：同一 Wi-Fi 下手机与电脑互传文件")
    parser.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    parser.add_argument("--dir", default=None, help="共享目录，默认 D:\\LanDrop")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    running = probe_running(args.port)
    if running is not None:
        url = f"http://127.0.0.1:{args.port}"
        if running.get("version") == APP_VERSION:
            print(f"  肥鱼快递 已经在运行：{url}", flush=True)
            print("  想再开一个实例请加 --port 8001", flush=True)
            if not args.no_browser:
                open_browser(url)
        else:
            print(f"  端口 {args.port} 上有一个旧版在运行。", flush=True)
            print("  请先关闭那个命令行窗口，再重新启动；", flush=True)
            print(f"  或者用 --port 8001 另开一个新实例。", flush=True)
        return

    share = Path(args.dir).expanduser() if args.dir else default_share_dir()
    try:
        share.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if args.dir:
            print(f"无法创建共享目录 {share}: {exc}", flush=True)
            sys.exit(1)
        # 默认目录不可写（权限/沙箱/被占用）时，退回到脚本旁边的目录
        fallback = Path(__file__).resolve().parent / "LanDrop"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            print(f"无法创建共享目录 {share}: {exc}", flush=True)
            print("可以换一个目录：python lan-drop.py --dir D:\\Share", flush=True)
            sys.exit(1)
        print(f"  提示：{share} 不可写，改用 {fallback}", flush=True)
        share = fallback

    # 清掉上次异常退出留下的临时压缩包 / 上传碎片
    for pattern in (".zip-*.part", ".upload-*.part"):
        for stale in share.glob(pattern):
            try:
                if time.time() - stale.stat().st_mtime > 3600:
                    stale.unlink()
            except OSError:
                pass

    httpd = None
    port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print(f"端口 {args.port}-{args.port + 19} 都被占用了，换个 --port 再试")
        sys.exit(1)

    ips = lan_ips()
    lan_url = f"http://{ips[0]}:{port}" if ips else f"http://127.0.0.1:{port}"

    Handler.share_dir = share
    Handler.lan_url = lan_url

    print()
    print("  fish deliver · 肥鱼快递 已启动")
    print(f"  共享目录  {share}")
    print()
    if ips:
        print("  手机浏览器打开下面地址（或扫电脑屏幕上的二维码）：")
        for ip in ips:
            print(f"    http://{ip}:{port}")
    else:
        print("  没检测到局域网地址，请确认电脑已连上 Wi-Fi 或网线")
    print()
    print("  手机和电脑要连同一个 Wi-Fi。打不开时依次检查：")
    print("  1) Windows 防火墙弹窗是否点了「允许访问」")
    print("  2) 路由器是否开了「AP 隔离 / 访客网络」")
    print()
    print("  按 Ctrl+C 停止", flush=True)

    if not args.no_browser:
        threading.Timer(0.6, open_browser, args=(f"http://127.0.0.1:{port}",)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()


QR_JS = r"""//---------------------------------------------------------------------
//
// QR Code Generator for JavaScript
//
// Copyright (c) 2009 Kazuhiko Arase
//
// URL: http://www.d-project.com/
//
// Licensed under the MIT license:
//  http://www.opensource.org/licenses/mit-license.php
//
// The word 'QR Code' is registered trademark of
// DENSO WAVE INCORPORATED
//  http://www.denso-wave.com/qrcode/faqpatent-e.html
//
//---------------------------------------------------------------------

var qrcode = function() {

  //---------------------------------------------------------------------
  // qrcode
  //---------------------------------------------------------------------

  /**
   * qrcode
   * @param typeNumber 1 to 40
   * @param errorCorrectionLevel 'L','M','Q','H'
   */
  var qrcode = function(typeNumber, errorCorrectionLevel) {

    var PAD0 = 0xEC;
    var PAD1 = 0x11;

    var _typeNumber = typeNumber;
    var _errorCorrectionLevel = QRErrorCorrectionLevel[errorCorrectionLevel];
    var _modules = null;
    var _moduleCount = 0;
    var _dataCache = null;
    var _dataList = [];

    var _this = {};

    var makeImpl = function(test, maskPattern) {

      _moduleCount = _typeNumber * 4 + 17;
      _modules = function(moduleCount) {
        var modules = new Array(moduleCount);
        for (var row = 0; row < moduleCount; row += 1) {
          modules[row] = new Array(moduleCount);
          for (var col = 0; col < moduleCount; col += 1) {
            modules[row][col] = null;
          }
        }
        return modules;
      }(_moduleCount);

      setupPositionProbePattern(0, 0);
      setupPositionProbePattern(_moduleCount - 7, 0);
      setupPositionProbePattern(0, _moduleCount - 7);
      setupPositionAdjustPattern();
      setupTimingPattern();
      setupTypeInfo(test, maskPattern);

      if (_typeNumber >= 7) {
        setupTypeNumber(test);
      }

      if (_dataCache == null) {
        _dataCache = createData(_typeNumber, _errorCorrectionLevel, _dataList);
      }

      mapData(_dataCache, maskPattern);
    };

    var setupPositionProbePattern = function(row, col) {

      for (var r = -1; r <= 7; r += 1) {

        if (row + r <= -1 || _moduleCount <= row + r) continue;

        for (var c = -1; c <= 7; c += 1) {

          if (col + c <= -1 || _moduleCount <= col + c) continue;

          if ( (0 <= r && r <= 6 && (c == 0 || c == 6) )
              || (0 <= c && c <= 6 && (r == 0 || r == 6) )
              || (2 <= r && r <= 4 && 2 <= c && c <= 4) ) {
            _modules[row + r][col + c] = true;
          } else {
            _modules[row + r][col + c] = false;
          }
        }
      }
    };

    var getBestMaskPattern = function() {

      var minLostPoint = 0;
      var pattern = 0;

      for (var i = 0; i < 8; i += 1) {

        makeImpl(true, i);

        var lostPoint = QRUtil.getLostPoint(_this);

        if (i == 0 || minLostPoint > lostPoint) {
          minLostPoint = lostPoint;
          pattern = i;
        }
      }

      return pattern;
    };

    var setupTimingPattern = function() {

      for (var r = 8; r < _moduleCount - 8; r += 1) {
        if (_modules[r][6] != null) {
          continue;
        }
        _modules[r][6] = (r % 2 == 0);
      }

      for (var c = 8; c < _moduleCount - 8; c += 1) {
        if (_modules[6][c] != null) {
          continue;
        }
        _modules[6][c] = (c % 2 == 0);
      }
    };

    var setupPositionAdjustPattern = function() {

      var pos = QRUtil.getPatternPosition(_typeNumber);

      for (var i = 0; i < pos.length; i += 1) {

        for (var j = 0; j < pos.length; j += 1) {

          var row = pos[i];
          var col = pos[j];

          if (_modules[row][col] != null) {
            continue;
          }

          for (var r = -2; r <= 2; r += 1) {

            for (var c = -2; c <= 2; c += 1) {

              if (r == -2 || r == 2 || c == -2 || c == 2
                  || (r == 0 && c == 0) ) {
                _modules[row + r][col + c] = true;
              } else {
                _modules[row + r][col + c] = false;
              }
            }
          }
        }
      }
    };

    var setupTypeNumber = function(test) {

      var bits = QRUtil.getBCHTypeNumber(_typeNumber);

      for (var i = 0; i < 18; i += 1) {
        var mod = (!test && ( (bits >> i) & 1) == 1);
        _modules[Math.floor(i / 3)][i % 3 + _moduleCount - 8 - 3] = mod;
      }

      for (var i = 0; i < 18; i += 1) {
        var mod = (!test && ( (bits >> i) & 1) == 1);
        _modules[i % 3 + _moduleCount - 8 - 3][Math.floor(i / 3)] = mod;
      }
    };

    var setupTypeInfo = function(test, maskPattern) {

      var data = (_errorCorrectionLevel << 3) | maskPattern;
      var bits = QRUtil.getBCHTypeInfo(data);

      // vertical
      for (var i = 0; i < 15; i += 1) {

        var mod = (!test && ( (bits >> i) & 1) == 1);

        if (i < 6) {
          _modules[i][8] = mod;
        } else if (i < 8) {
          _modules[i + 1][8] = mod;
        } else {
          _modules[_moduleCount - 15 + i][8] = mod;
        }
      }

      // horizontal
      for (var i = 0; i < 15; i += 1) {

        var mod = (!test && ( (bits >> i) & 1) == 1);

        if (i < 8) {
          _modules[8][_moduleCount - i - 1] = mod;
        } else if (i < 9) {
          _modules[8][15 - i - 1 + 1] = mod;
        } else {
          _modules[8][15 - i - 1] = mod;
        }
      }

      // fixed module
      _modules[_moduleCount - 8][8] = (!test);
    };

    var mapData = function(data, maskPattern) {

      var inc = -1;
      var row = _moduleCount - 1;
      var bitIndex = 7;
      var byteIndex = 0;
      var maskFunc = QRUtil.getMaskFunction(maskPattern);

      for (var col = _moduleCount - 1; col > 0; col -= 2) {

        if (col == 6) col -= 1;

        while (true) {

          for (var c = 0; c < 2; c += 1) {

            if (_modules[row][col - c] == null) {

              var dark = false;

              if (byteIndex < data.length) {
                dark = ( ( (data[byteIndex] >>> bitIndex) & 1) == 1);
              }

              var mask = maskFunc(row, col - c);

              if (mask) {
                dark = !dark;
              }

              _modules[row][col - c] = dark;
              bitIndex -= 1;

              if (bitIndex == -1) {
                byteIndex += 1;
                bitIndex = 7;
              }
            }
          }

          row += inc;

          if (row < 0 || _moduleCount <= row) {
            row -= inc;
            inc = -inc;
            break;
          }
        }
      }
    };

    var createBytes = function(buffer, rsBlocks) {

      var offset = 0;

      var maxDcCount = 0;
      var maxEcCount = 0;

      var dcdata = new Array(rsBlocks.length);
      var ecdata = new Array(rsBlocks.length);

      for (var r = 0; r < rsBlocks.length; r += 1) {

        var dcCount = rsBlocks[r].dataCount;
        var ecCount = rsBlocks[r].totalCount - dcCount;

        maxDcCount = Math.max(maxDcCount, dcCount);
        maxEcCount = Math.max(maxEcCount, ecCount);

        dcdata[r] = new Array(dcCount);

        for (var i = 0; i < dcdata[r].length; i += 1) {
          dcdata[r][i] = 0xff & buffer.getBuffer()[i + offset];
        }
        offset += dcCount;

        var rsPoly = QRUtil.getErrorCorrectPolynomial(ecCount);
        var rawPoly = qrPolynomial(dcdata[r], rsPoly.getLength() - 1);

        var modPoly = rawPoly.mod(rsPoly);
        ecdata[r] = new Array(rsPoly.getLength() - 1);
        for (var i = 0; i < ecdata[r].length; i += 1) {
          var modIndex = i + modPoly.getLength() - ecdata[r].length;
          ecdata[r][i] = (modIndex >= 0)? modPoly.getAt(modIndex) : 0;
        }
      }

      var totalCodeCount = 0;
      for (var i = 0; i < rsBlocks.length; i += 1) {
        totalCodeCount += rsBlocks[i].totalCount;
      }

      var data = new Array(totalCodeCount);
      var index = 0;

      for (var i = 0; i < maxDcCount; i += 1) {
        for (var r = 0; r < rsBlocks.length; r += 1) {
          if (i < dcdata[r].length) {
            data[index] = dcdata[r][i];
            index += 1;
          }
        }
      }

      for (var i = 0; i < maxEcCount; i += 1) {
        for (var r = 0; r < rsBlocks.length; r += 1) {
          if (i < ecdata[r].length) {
            data[index] = ecdata[r][i];
            index += 1;
          }
        }
      }

      return data;
    };

    var createData = function(typeNumber, errorCorrectionLevel, dataList) {

      var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, errorCorrectionLevel);

      var buffer = qrBitBuffer();

      for (var i = 0; i < dataList.length; i += 1) {
        var data = dataList[i];
        buffer.put(data.getMode(), 4);
        buffer.put(data.getLength(), QRUtil.getLengthInBits(data.getMode(), typeNumber) );
        data.write(buffer);
      }

      // calc num max data.
      var totalDataCount = 0;
      for (var i = 0; i < rsBlocks.length; i += 1) {
        totalDataCount += rsBlocks[i].dataCount;
      }

      if (buffer.getLengthInBits() > totalDataCount * 8) {
        throw 'code length overflow. ('
          + buffer.getLengthInBits()
          + '>'
          + totalDataCount * 8
          + ')';
      }

      // end code
      if (buffer.getLengthInBits() + 4 <= totalDataCount * 8) {
        buffer.put(0, 4);
      }

      // padding
      while (buffer.getLengthInBits() % 8 != 0) {
        buffer.putBit(false);
      }

      // padding
      while (true) {

        if (buffer.getLengthInBits() >= totalDataCount * 8) {
          break;
        }
        buffer.put(PAD0, 8);

        if (buffer.getLengthInBits() >= totalDataCount * 8) {
          break;
        }
        buffer.put(PAD1, 8);
      }

      return createBytes(buffer, rsBlocks);
    };

    _this.addData = function(data, mode) {

      mode = mode || 'Byte';

      var newData = null;

      switch(mode) {
      case 'Numeric' :
        newData = qrNumber(data);
        break;
      case 'Alphanumeric' :
        newData = qrAlphaNum(data);
        break;
      case 'Byte' :
        newData = qr8BitByte(data);
        break;
      case 'Kanji' :
        newData = qrKanji(data);
        break;
      default :
        throw 'mode:' + mode;
      }

      _dataList.push(newData);
      _dataCache = null;
    };

    _this.isDark = function(row, col) {
      if (row < 0 || _moduleCount <= row || col < 0 || _moduleCount <= col) {
        throw row + ',' + col;
      }
      return _modules[row][col];
    };

    _this.getModuleCount = function() {
      return _moduleCount;
    };

    _this.make = function() {
      if (_typeNumber < 1) {
        var typeNumber = 1;

        for (; typeNumber < 40; typeNumber++) {
          var rsBlocks = QRRSBlock.getRSBlocks(typeNumber, _errorCorrectionLevel);
          var buffer = qrBitBuffer();

          for (var i = 0; i < _dataList.length; i++) {
            var data = _dataList[i];
            buffer.put(data.getMode(), 4);
            buffer.put(data.getLength(), QRUtil.getLengthInBits(data.getMode(), typeNumber) );
            data.write(buffer);
          }

          var totalDataCount = 0;
          for (var i = 0; i < rsBlocks.length; i++) {
            totalDataCount += rsBlocks[i].dataCount;
          }

          if (buffer.getLengthInBits() <= totalDataCount * 8) {
            break;
          }
        }

        _typeNumber = typeNumber;
      }

      makeImpl(false, getBestMaskPattern() );
    };

    _this.createTableTag = function(cellSize, margin) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var qrHtml = '';

      qrHtml += '<table style="';
      qrHtml += ' border-width: 0px; border-style: none;';
      qrHtml += ' border-collapse: collapse;';
      qrHtml += ' padding: 0px; margin: ' + margin + 'px;';
      qrHtml += '">';
      qrHtml += '<tbody>';

      for (var r = 0; r < _this.getModuleCount(); r += 1) {

        qrHtml += '<tr>';

        for (var c = 0; c < _this.getModuleCount(); c += 1) {
          qrHtml += '<td style="';
          qrHtml += ' border-width: 0px; border-style: none;';
          qrHtml += ' border-collapse: collapse;';
          qrHtml += ' padding: 0px; margin: 0px;';
          qrHtml += ' width: ' + cellSize + 'px;';
          qrHtml += ' height: ' + cellSize + 'px;';
          qrHtml += ' background-color: ';
          qrHtml += _this.isDark(r, c)? '#000000' : '#ffffff';
          qrHtml += ';';
          qrHtml += '"/>';
        }

        qrHtml += '</tr>';
      }

      qrHtml += '</tbody>';
      qrHtml += '</table>';

      return qrHtml;
    };

    _this.createSvgTag = function(cellSize, margin, alt, title) {

      var opts = {};
      if (typeof arguments[0] == 'object') {
        // Called by options.
        opts = arguments[0];
        // overwrite cellSize and margin.
        cellSize = opts.cellSize;
        margin = opts.margin;
        alt = opts.alt;
        title = opts.title;
      }

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      // Compose alt property surrogate
      alt = (typeof alt === 'string') ? {text: alt} : alt || {};
      alt.text = alt.text || null;
      alt.id = (alt.text) ? alt.id || 'qrcode-description' : null;

      // Compose title property surrogate
      title = (typeof title === 'string') ? {text: title} : title || {};
      title.text = title.text || null;
      title.id = (title.text) ? title.id || 'qrcode-title' : null;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var c, mc, r, mr, qrSvg='', rect;

      rect = 'l' + cellSize + ',0 0,' + cellSize +
        ' -' + cellSize + ',0 0,-' + cellSize + 'z ';

      qrSvg += '<svg version="1.1" xmlns="http://www.w3.org/2000/svg"';
      qrSvg += !opts.scalable ? ' width="' + size + 'px" height="' + size + 'px"' : '';
      qrSvg += ' viewBox="0 0 ' + size + ' ' + size + '" ';
      qrSvg += ' preserveAspectRatio="xMinYMin meet"';
      qrSvg += (title.text || alt.text) ? ' role="img" aria-labelledby="' +
          escapeXml([title.id, alt.id].join(' ').trim() ) + '"' : '';
      qrSvg += '>';
      qrSvg += (title.text) ? '<title id="' + escapeXml(title.id) + '">' +
          escapeXml(title.text) + '</title>' : '';
      qrSvg += (alt.text) ? '<description id="' + escapeXml(alt.id) + '">' +
          escapeXml(alt.text) + '</description>' : '';
      qrSvg += '<rect width="100%" height="100%" fill="white" cx="0" cy="0"/>';
      qrSvg += '<path d="';

      for (r = 0; r < _this.getModuleCount(); r += 1) {
        mr = r * cellSize + margin;
        for (c = 0; c < _this.getModuleCount(); c += 1) {
          if (_this.isDark(r, c) ) {
            mc = c*cellSize+margin;
            qrSvg += 'M' + mc + ',' + mr + rect;
          }
        }
      }

      qrSvg += '" stroke="transparent" fill="black"/>';
      qrSvg += '</svg>';

      return qrSvg;
    };

    _this.createDataURL = function(cellSize, margin) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      return createDataURL(size, size, function(x, y) {
        if (min <= x && x < max && min <= y && y < max) {
          var c = Math.floor( (x - min) / cellSize);
          var r = Math.floor( (y - min) / cellSize);
          return _this.isDark(r, c)? 0 : 1;
        } else {
          return 1;
        }
      } );
    };

    _this.createImgTag = function(cellSize, margin, alt) {

      cellSize = cellSize || 2;
      margin = (typeof margin == 'undefined')? cellSize * 4 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;

      var img = '';
      img += '<img';
      img += '\u0020src="';
      img += _this.createDataURL(cellSize, margin);
      img += '"';
      img += '\u0020width="';
      img += size;
      img += '"';
      img += '\u0020height="';
      img += size;
      img += '"';
      if (alt) {
        img += '\u0020alt="';
        img += escapeXml(alt);
        img += '"';
      }
      img += '/>';

      return img;
    };

    var escapeXml = function(s) {
      var escaped = '';
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charAt(i);
        switch(c) {
        case '<': escaped += '&lt;'; break;
        case '>': escaped += '&gt;'; break;
        case '&': escaped += '&amp;'; break;
        case '"': escaped += '&quot;'; break;
        default : escaped += c; break;
        }
      }
      return escaped;
    };

    var _createHalfASCII = function(margin) {
      var cellSize = 1;
      margin = (typeof margin == 'undefined')? cellSize * 2 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      var y, x, r1, r2, p;

      var blocks = {
        '██': '█',
        '█ ': '▀',
        ' █': '▄',
        '  ': ' '
      };

      var blocksLastLineNoMargin = {
        '██': '▀',
        '█ ': '▀',
        ' █': ' ',
        '  ': ' '
      };

      var ascii = '';
      for (y = 0; y < size; y += 2) {
        r1 = Math.floor((y - min) / cellSize);
        r2 = Math.floor((y + 1 - min) / cellSize);
        for (x = 0; x < size; x += 1) {
          p = '█';

          if (min <= x && x < max && min <= y && y < max && _this.isDark(r1, Math.floor((x - min) / cellSize))) {
            p = ' ';
          }

          if (min <= x && x < max && min <= y+1 && y+1 < max && _this.isDark(r2, Math.floor((x - min) / cellSize))) {
            p += ' ';
          }
          else {
            p += '█';
          }

          // Output 2 characters per pixel, to create full square. 1 character per pixels gives only half width of square.
          ascii += (margin < 1 && y+1 >= max) ? blocksLastLineNoMargin[p] : blocks[p];
        }

        ascii += '\n';
      }

      if (size % 2 && margin > 0) {
        return ascii.substring(0, ascii.length - size - 1) + Array(size+1).join('▀');
      }

      return ascii.substring(0, ascii.length-1);
    };

    _this.createASCII = function(cellSize, margin) {
      cellSize = cellSize || 1;

      if (cellSize < 2) {
        return _createHalfASCII(margin);
      }

      cellSize -= 1;
      margin = (typeof margin == 'undefined')? cellSize * 2 : margin;

      var size = _this.getModuleCount() * cellSize + margin * 2;
      var min = margin;
      var max = size - margin;

      var y, x, r, p;

      var white = Array(cellSize+1).join('██');
      var black = Array(cellSize+1).join('  ');

      var ascii = '';
      var line = '';
      for (y = 0; y < size; y += 1) {
        r = Math.floor( (y - min) / cellSize);
        line = '';
        for (x = 0; x < size; x += 1) {
          p = 1;

          if (min <= x && x < max && min <= y && y < max && _this.isDark(r, Math.floor((x - min) / cellSize))) {
            p = 0;
          }

          // Output 2 characters per pixel, to create full square. 1 character per pixels gives only half width of square.
          line += p ? white : black;
        }

        for (r = 0; r < cellSize; r += 1) {
          ascii += line + '\n';
        }
      }

      return ascii.substring(0, ascii.length-1);
    };

    _this.renderTo2dContext = function(context, cellSize) {
      cellSize = cellSize || 2;
      var length = _this.getModuleCount();
      for (var row = 0; row < length; row++) {
        for (var col = 0; col < length; col++) {
          context.fillStyle = _this.isDark(row, col) ? 'black' : 'white';
          context.fillRect(row * cellSize, col * cellSize, cellSize, cellSize);
        }
      }
    }

    return _this;
  };

  //---------------------------------------------------------------------
  // qrcode.stringToBytes
  //---------------------------------------------------------------------

  qrcode.stringToBytesFuncs = {
    'default' : function(s) {
      var bytes = [];
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charCodeAt(i);
        bytes.push(c & 0xff);
      }
      return bytes;
    }
  };

  qrcode.stringToBytes = qrcode.stringToBytesFuncs['default'];

  //---------------------------------------------------------------------
  // qrcode.createStringToBytes
  //---------------------------------------------------------------------

  /**
   * @param unicodeData base64 string of byte array.
   * [16bit Unicode],[16bit Bytes], ...
   * @param numChars
   */
  qrcode.createStringToBytes = function(unicodeData, numChars) {

    // create conversion map.

    var unicodeMap = function() {

      var bin = base64DecodeInputStream(unicodeData);
      var read = function() {
        var b = bin.read();
        if (b == -1) throw 'eof';
        return b;
      };

      var count = 0;
      var unicodeMap = {};
      while (true) {
        var b0 = bin.read();
        if (b0 == -1) break;
        var b1 = read();
        var b2 = read();
        var b3 = read();
        var k = String.fromCharCode( (b0 << 8) | b1);
        var v = (b2 << 8) | b3;
        unicodeMap[k] = v;
        count += 1;
      }
      if (count != numChars) {
        throw count + ' != ' + numChars;
      }

      return unicodeMap;
    }();

    var unknownChar = '?'.charCodeAt(0);

    return function(s) {
      var bytes = [];
      for (var i = 0; i < s.length; i += 1) {
        var c = s.charCodeAt(i);
        if (c < 128) {
          bytes.push(c);
        } else {
          var b = unicodeMap[s.charAt(i)];
          if (typeof b == 'number') {
            if ( (b & 0xff) == b) {
              // 1byte
              bytes.push(b);
            } else {
              // 2bytes
              bytes.push(b >>> 8);
              bytes.push(b & 0xff);
            }
          } else {
            bytes.push(unknownChar);
          }
        }
      }
      return bytes;
    };
  };

  //---------------------------------------------------------------------
  // QRMode
  //---------------------------------------------------------------------

  var QRMode = {
    MODE_NUMBER :    1 << 0,
    MODE_ALPHA_NUM : 1 << 1,
    MODE_8BIT_BYTE : 1 << 2,
    MODE_KANJI :     1 << 3
  };

  //---------------------------------------------------------------------
  // QRErrorCorrectionLevel
  //---------------------------------------------------------------------

  var QRErrorCorrectionLevel = {
    L : 1,
    M : 0,
    Q : 3,
    H : 2
  };

  //---------------------------------------------------------------------
  // QRMaskPattern
  //---------------------------------------------------------------------

  var QRMaskPattern = {
    PATTERN000 : 0,
    PATTERN001 : 1,
    PATTERN010 : 2,
    PATTERN011 : 3,
    PATTERN100 : 4,
    PATTERN101 : 5,
    PATTERN110 : 6,
    PATTERN111 : 7
  };

  //---------------------------------------------------------------------
  // QRUtil
  //---------------------------------------------------------------------

  var QRUtil = function() {

    var PATTERN_POSITION_TABLE = [
      [],
      [6, 18],
      [6, 22],
      [6, 26],
      [6, 30],
      [6, 34],
      [6, 22, 38],
      [6, 24, 42],
      [6, 26, 46],
      [6, 28, 50],
      [6, 30, 54],
      [6, 32, 58],
      [6, 34, 62],
      [6, 26, 46, 66],
      [6, 26, 48, 70],
      [6, 26, 50, 74],
      [6, 30, 54, 78],
      [6, 30, 56, 82],
      [6, 30, 58, 86],
      [6, 34, 62, 90],
      [6, 28, 50, 72, 94],
      [6, 26, 50, 74, 98],
      [6, 30, 54, 78, 102],
      [6, 28, 54, 80, 106],
      [6, 32, 58, 84, 110],
      [6, 30, 58, 86, 114],
      [6, 34, 62, 90, 118],
      [6, 26, 50, 74, 98, 122],
      [6, 30, 54, 78, 102, 126],
      [6, 26, 52, 78, 104, 130],
      [6, 30, 56, 82, 108, 134],
      [6, 34, 60, 86, 112, 138],
      [6, 30, 58, 86, 114, 142],
      [6, 34, 62, 90, 118, 146],
      [6, 30, 54, 78, 102, 126, 150],
      [6, 24, 50, 76, 102, 128, 154],
      [6, 28, 54, 80, 106, 132, 158],
      [6, 32, 58, 84, 110, 136, 162],
      [6, 26, 54, 82, 110, 138, 166],
      [6, 30, 58, 86, 114, 142, 170]
    ];
    var G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0);
    var G18 = (1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8) | (1 << 5) | (1 << 2) | (1 << 0);
    var G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1);

    var _this = {};

    var getBCHDigit = function(data) {
      var digit = 0;
      while (data != 0) {
        digit += 1;
        data >>>= 1;
      }
      return digit;
    };

    _this.getBCHTypeInfo = function(data) {
      var d = data << 10;
      while (getBCHDigit(d) - getBCHDigit(G15) >= 0) {
        d ^= (G15 << (getBCHDigit(d) - getBCHDigit(G15) ) );
      }
      return ( (data << 10) | d) ^ G15_MASK;
    };

    _this.getBCHTypeNumber = function(data) {
      var d = data << 12;
      while (getBCHDigit(d) - getBCHDigit(G18) >= 0) {
        d ^= (G18 << (getBCHDigit(d) - getBCHDigit(G18) ) );
      }
      return (data << 12) | d;
    };

    _this.getPatternPosition = function(typeNumber) {
      return PATTERN_POSITION_TABLE[typeNumber - 1];
    };

    _this.getMaskFunction = function(maskPattern) {

      switch (maskPattern) {

      case QRMaskPattern.PATTERN000 :
        return function(i, j) { return (i + j) % 2 == 0; };
      case QRMaskPattern.PATTERN001 :
        return function(i, j) { return i % 2 == 0; };
      case QRMaskPattern.PATTERN010 :
        return function(i, j) { return j % 3 == 0; };
      case QRMaskPattern.PATTERN011 :
        return function(i, j) { return (i + j) % 3 == 0; };
      case QRMaskPattern.PATTERN100 :
        return function(i, j) { return (Math.floor(i / 2) + Math.floor(j / 3) ) % 2 == 0; };
      case QRMaskPattern.PATTERN101 :
        return function(i, j) { return (i * j) % 2 + (i * j) % 3 == 0; };
      case QRMaskPattern.PATTERN110 :
        return function(i, j) { return ( (i * j) % 2 + (i * j) % 3) % 2 == 0; };
      case QRMaskPattern.PATTERN111 :
        return function(i, j) { return ( (i * j) % 3 + (i + j) % 2) % 2 == 0; };

      default :
        throw 'bad maskPattern:' + maskPattern;
      }
    };

    _this.getErrorCorrectPolynomial = function(errorCorrectLength) {
      var a = qrPolynomial([1], 0);
      for (var i = 0; i < errorCorrectLength; i += 1) {
        a = a.multiply(qrPolynomial([1, QRMath.gexp(i)], 0) );
      }
      return a;
    };

    _this.getLengthInBits = function(mode, type) {

      if (1 <= type && type < 10) {

        // 1 - 9

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 10;
        case QRMode.MODE_ALPHA_NUM : return 9;
        case QRMode.MODE_8BIT_BYTE : return 8;
        case QRMode.MODE_KANJI     : return 8;
        default :
          throw 'mode:' + mode;
        }

      } else if (type < 27) {

        // 10 - 26

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 12;
        case QRMode.MODE_ALPHA_NUM : return 11;
        case QRMode.MODE_8BIT_BYTE : return 16;
        case QRMode.MODE_KANJI     : return 10;
        default :
          throw 'mode:' + mode;
        }

      } else if (type < 41) {

        // 27 - 40

        switch(mode) {
        case QRMode.MODE_NUMBER    : return 14;
        case QRMode.MODE_ALPHA_NUM : return 13;
        case QRMode.MODE_8BIT_BYTE : return 16;
        case QRMode.MODE_KANJI     : return 12;
        default :
          throw 'mode:' + mode;
        }

      } else {
        throw 'type:' + type;
      }
    };

    _this.getLostPoint = function(qrcode) {

      var moduleCount = qrcode.getModuleCount();

      var lostPoint = 0;

      // LEVEL1

      for (var row = 0; row < moduleCount; row += 1) {
        for (var col = 0; col < moduleCount; col += 1) {

          var sameCount = 0;
          var dark = qrcode.isDark(row, col);

          for (var r = -1; r <= 1; r += 1) {

            if (row + r < 0 || moduleCount <= row + r) {
              continue;
            }

            for (var c = -1; c <= 1; c += 1) {

              if (col + c < 0 || moduleCount <= col + c) {
                continue;
              }

              if (r == 0 && c == 0) {
                continue;
              }

              if (dark == qrcode.isDark(row + r, col + c) ) {
                sameCount += 1;
              }
            }
          }

          if (sameCount > 5) {
            lostPoint += (3 + sameCount - 5);
          }
        }
      };

      // LEVEL2

      for (var row = 0; row < moduleCount - 1; row += 1) {
        for (var col = 0; col < moduleCount - 1; col += 1) {
          var count = 0;
          if (qrcode.isDark(row, col) ) count += 1;
          if (qrcode.isDark(row + 1, col) ) count += 1;
          if (qrcode.isDark(row, col + 1) ) count += 1;
          if (qrcode.isDark(row + 1, col + 1) ) count += 1;
          if (count == 0 || count == 4) {
            lostPoint += 3;
          }
        }
      }

      // LEVEL3

      for (var row = 0; row < moduleCount; row += 1) {
        for (var col = 0; col < moduleCount - 6; col += 1) {
          if (qrcode.isDark(row, col)
              && !qrcode.isDark(row, col + 1)
              &&  qrcode.isDark(row, col + 2)
              &&  qrcode.isDark(row, col + 3)
              &&  qrcode.isDark(row, col + 4)
              && !qrcode.isDark(row, col + 5)
              &&  qrcode.isDark(row, col + 6) ) {
            lostPoint += 40;
          }
        }
      }

      for (var col = 0; col < moduleCount; col += 1) {
        for (var row = 0; row < moduleCount - 6; row += 1) {
          if (qrcode.isDark(row, col)
              && !qrcode.isDark(row + 1, col)
              &&  qrcode.isDark(row + 2, col)
              &&  qrcode.isDark(row + 3, col)
              &&  qrcode.isDark(row + 4, col)
              && !qrcode.isDark(row + 5, col)
              &&  qrcode.isDark(row + 6, col) ) {
            lostPoint += 40;
          }
        }
      }

      // LEVEL4

      var darkCount = 0;

      for (var col = 0; col < moduleCount; col += 1) {
        for (var row = 0; row < moduleCount; row += 1) {
          if (qrcode.isDark(row, col) ) {
            darkCount += 1;
          }
        }
      }

      var ratio = Math.abs(100 * darkCount / moduleCount / moduleCount - 50) / 5;
      lostPoint += ratio * 10;

      return lostPoint;
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // QRMath
  //---------------------------------------------------------------------

  var QRMath = function() {

    var EXP_TABLE = new Array(256);
    var LOG_TABLE = new Array(256);

    // initialize tables
    for (var i = 0; i < 8; i += 1) {
      EXP_TABLE[i] = 1 << i;
    }
    for (var i = 8; i < 256; i += 1) {
      EXP_TABLE[i] = EXP_TABLE[i - 4]
        ^ EXP_TABLE[i - 5]
        ^ EXP_TABLE[i - 6]
        ^ EXP_TABLE[i - 8];
    }
    for (var i = 0; i < 255; i += 1) {
      LOG_TABLE[EXP_TABLE[i] ] = i;
    }

    var _this = {};

    _this.glog = function(n) {

      if (n < 1) {
        throw 'glog(' + n + ')';
      }

      return LOG_TABLE[n];
    };

    _this.gexp = function(n) {

      while (n < 0) {
        n += 255;
      }

      while (n >= 256) {
        n -= 255;
      }

      return EXP_TABLE[n];
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // qrPolynomial
  //---------------------------------------------------------------------

  function qrPolynomial(num, shift) {

    if (typeof num.length == 'undefined') {
      throw num.length + '/' + shift;
    }

    var _num = function() {
      var offset = 0;
      while (offset < num.length && num[offset] == 0) {
        offset += 1;
      }
      var _num = new Array(num.length - offset + shift);
      for (var i = 0; i < num.length - offset; i += 1) {
        _num[i] = num[i + offset];
      }
      return _num;
    }();

    var _this = {};

    _this.getAt = function(index) {
      return _num[index];
    };

    _this.getLength = function() {
      return _num.length;
    };

    _this.multiply = function(e) {

      var num = new Array(_this.getLength() + e.getLength() - 1);

      for (var i = 0; i < _this.getLength(); i += 1) {
        for (var j = 0; j < e.getLength(); j += 1) {
          num[i + j] ^= QRMath.gexp(QRMath.glog(_this.getAt(i) ) + QRMath.glog(e.getAt(j) ) );
        }
      }

      return qrPolynomial(num, 0);
    };

    _this.mod = function(e) {

      if (_this.getLength() - e.getLength() < 0) {
        return _this;
      }

      var ratio = QRMath.glog(_this.getAt(0) ) - QRMath.glog(e.getAt(0) );

      var num = new Array(_this.getLength() );
      for (var i = 0; i < _this.getLength(); i += 1) {
        num[i] = _this.getAt(i);
      }

      for (var i = 0; i < e.getLength(); i += 1) {
        num[i] ^= QRMath.gexp(QRMath.glog(e.getAt(i) ) + ratio);
      }

      // recursive call
      return qrPolynomial(num, 0).mod(e);
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // QRRSBlock
  //---------------------------------------------------------------------

  var QRRSBlock = function() {

    var RS_BLOCK_TABLE = [

      // L
      // M
      // Q
      // H

      // 1
      [1, 26, 19],
      [1, 26, 16],
      [1, 26, 13],
      [1, 26, 9],

      // 2
      [1, 44, 34],
      [1, 44, 28],
      [1, 44, 22],
      [1, 44, 16],

      // 3
      [1, 70, 55],
      [1, 70, 44],
      [2, 35, 17],
      [2, 35, 13],

      // 4
      [1, 100, 80],
      [2, 50, 32],
      [2, 50, 24],
      [4, 25, 9],

      // 5
      [1, 134, 108],
      [2, 67, 43],
      [2, 33, 15, 2, 34, 16],
      [2, 33, 11, 2, 34, 12],

      // 6
      [2, 86, 68],
      [4, 43, 27],
      [4, 43, 19],
      [4, 43, 15],

      // 7
      [2, 98, 78],
      [4, 49, 31],
      [2, 32, 14, 4, 33, 15],
      [4, 39, 13, 1, 40, 14],

      // 8
      [2, 121, 97],
      [2, 60, 38, 2, 61, 39],
      [4, 40, 18, 2, 41, 19],
      [4, 40, 14, 2, 41, 15],

      // 9
      [2, 146, 116],
      [3, 58, 36, 2, 59, 37],
      [4, 36, 16, 4, 37, 17],
      [4, 36, 12, 4, 37, 13],

      // 10
      [2, 86, 68, 2, 87, 69],
      [4, 69, 43, 1, 70, 44],
      [6, 43, 19, 2, 44, 20],
      [6, 43, 15, 2, 44, 16],

      // 11
      [4, 101, 81],
      [1, 80, 50, 4, 81, 51],
      [4, 50, 22, 4, 51, 23],
      [3, 36, 12, 8, 37, 13],

      // 12
      [2, 116, 92, 2, 117, 93],
      [6, 58, 36, 2, 59, 37],
      [4, 46, 20, 6, 47, 21],
      [7, 42, 14, 4, 43, 15],

      // 13
      [4, 133, 107],
      [8, 59, 37, 1, 60, 38],
      [8, 44, 20, 4, 45, 21],
      [12, 33, 11, 4, 34, 12],

      // 14
      [3, 145, 115, 1, 146, 116],
      [4, 64, 40, 5, 65, 41],
      [11, 36, 16, 5, 37, 17],
      [11, 36, 12, 5, 37, 13],

      // 15
      [5, 109, 87, 1, 110, 88],
      [5, 65, 41, 5, 66, 42],
      [5, 54, 24, 7, 55, 25],
      [11, 36, 12, 7, 37, 13],

      // 16
      [5, 122, 98, 1, 123, 99],
      [7, 73, 45, 3, 74, 46],
      [15, 43, 19, 2, 44, 20],
      [3, 45, 15, 13, 46, 16],

      // 17
      [1, 135, 107, 5, 136, 108],
      [10, 74, 46, 1, 75, 47],
      [1, 50, 22, 15, 51, 23],
      [2, 42, 14, 17, 43, 15],

      // 18
      [5, 150, 120, 1, 151, 121],
      [9, 69, 43, 4, 70, 44],
      [17, 50, 22, 1, 51, 23],
      [2, 42, 14, 19, 43, 15],

      // 19
      [3, 141, 113, 4, 142, 114],
      [3, 70, 44, 11, 71, 45],
      [17, 47, 21, 4, 48, 22],
      [9, 39, 13, 16, 40, 14],

      // 20
      [3, 135, 107, 5, 136, 108],
      [3, 67, 41, 13, 68, 42],
      [15, 54, 24, 5, 55, 25],
      [15, 43, 15, 10, 44, 16],

      // 21
      [4, 144, 116, 4, 145, 117],
      [17, 68, 42],
      [17, 50, 22, 6, 51, 23],
      [19, 46, 16, 6, 47, 17],

      // 22
      [2, 139, 111, 7, 140, 112],
      [17, 74, 46],
      [7, 54, 24, 16, 55, 25],
      [34, 37, 13],

      // 23
      [4, 151, 121, 5, 152, 122],
      [4, 75, 47, 14, 76, 48],
      [11, 54, 24, 14, 55, 25],
      [16, 45, 15, 14, 46, 16],

      // 24
      [6, 147, 117, 4, 148, 118],
      [6, 73, 45, 14, 74, 46],
      [11, 54, 24, 16, 55, 25],
      [30, 46, 16, 2, 47, 17],

      // 25
      [8, 132, 106, 4, 133, 107],
      [8, 75, 47, 13, 76, 48],
      [7, 54, 24, 22, 55, 25],
      [22, 45, 15, 13, 46, 16],

      // 26
      [10, 142, 114, 2, 143, 115],
      [19, 74, 46, 4, 75, 47],
      [28, 50, 22, 6, 51, 23],
      [33, 46, 16, 4, 47, 17],

      // 27
      [8, 152, 122, 4, 153, 123],
      [22, 73, 45, 3, 74, 46],
      [8, 53, 23, 26, 54, 24],
      [12, 45, 15, 28, 46, 16],

      // 28
      [3, 147, 117, 10, 148, 118],
      [3, 73, 45, 23, 74, 46],
      [4, 54, 24, 31, 55, 25],
      [11, 45, 15, 31, 46, 16],

      // 29
      [7, 146, 116, 7, 147, 117],
      [21, 73, 45, 7, 74, 46],
      [1, 53, 23, 37, 54, 24],
      [19, 45, 15, 26, 46, 16],

      // 30
      [5, 145, 115, 10, 146, 116],
      [19, 75, 47, 10, 76, 48],
      [15, 54, 24, 25, 55, 25],
      [23, 45, 15, 25, 46, 16],

      // 31
      [13, 145, 115, 3, 146, 116],
      [2, 74, 46, 29, 75, 47],
      [42, 54, 24, 1, 55, 25],
      [23, 45, 15, 28, 46, 16],

      // 32
      [17, 145, 115],
      [10, 74, 46, 23, 75, 47],
      [10, 54, 24, 35, 55, 25],
      [19, 45, 15, 35, 46, 16],

      // 33
      [17, 145, 115, 1, 146, 116],
      [14, 74, 46, 21, 75, 47],
      [29, 54, 24, 19, 55, 25],
      [11, 45, 15, 46, 46, 16],

      // 34
      [13, 145, 115, 6, 146, 116],
      [14, 74, 46, 23, 75, 47],
      [44, 54, 24, 7, 55, 25],
      [59, 46, 16, 1, 47, 17],

      // 35
      [12, 151, 121, 7, 152, 122],
      [12, 75, 47, 26, 76, 48],
      [39, 54, 24, 14, 55, 25],
      [22, 45, 15, 41, 46, 16],

      // 36
      [6, 151, 121, 14, 152, 122],
      [6, 75, 47, 34, 76, 48],
      [46, 54, 24, 10, 55, 25],
      [2, 45, 15, 64, 46, 16],

      // 37
      [17, 152, 122, 4, 153, 123],
      [29, 74, 46, 14, 75, 47],
      [49, 54, 24, 10, 55, 25],
      [24, 45, 15, 46, 46, 16],

      // 38
      [4, 152, 122, 18, 153, 123],
      [13, 74, 46, 32, 75, 47],
      [48, 54, 24, 14, 55, 25],
      [42, 45, 15, 32, 46, 16],

      // 39
      [20, 147, 117, 4, 148, 118],
      [40, 75, 47, 7, 76, 48],
      [43, 54, 24, 22, 55, 25],
      [10, 45, 15, 67, 46, 16],

      // 40
      [19, 148, 118, 6, 149, 119],
      [18, 75, 47, 31, 76, 48],
      [34, 54, 24, 34, 55, 25],
      [20, 45, 15, 61, 46, 16]
    ];

    var qrRSBlock = function(totalCount, dataCount) {
      var _this = {};
      _this.totalCount = totalCount;
      _this.dataCount = dataCount;
      return _this;
    };

    var _this = {};

    var getRsBlockTable = function(typeNumber, errorCorrectionLevel) {

      switch(errorCorrectionLevel) {
      case QRErrorCorrectionLevel.L :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 0];
      case QRErrorCorrectionLevel.M :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 1];
      case QRErrorCorrectionLevel.Q :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 2];
      case QRErrorCorrectionLevel.H :
        return RS_BLOCK_TABLE[(typeNumber - 1) * 4 + 3];
      default :
        return undefined;
      }
    };

    _this.getRSBlocks = function(typeNumber, errorCorrectionLevel) {

      var rsBlock = getRsBlockTable(typeNumber, errorCorrectionLevel);

      if (typeof rsBlock == 'undefined') {
        throw 'bad rs block @ typeNumber:' + typeNumber +
            '/errorCorrectionLevel:' + errorCorrectionLevel;
      }

      var length = rsBlock.length / 3;

      var list = [];

      for (var i = 0; i < length; i += 1) {

        var count = rsBlock[i * 3 + 0];
        var totalCount = rsBlock[i * 3 + 1];
        var dataCount = rsBlock[i * 3 + 2];

        for (var j = 0; j < count; j += 1) {
          list.push(qrRSBlock(totalCount, dataCount) );
        }
      }

      return list;
    };

    return _this;
  }();

  //---------------------------------------------------------------------
  // qrBitBuffer
  //---------------------------------------------------------------------

  var qrBitBuffer = function() {

    var _buffer = [];
    var _length = 0;

    var _this = {};

    _this.getBuffer = function() {
      return _buffer;
    };

    _this.getAt = function(index) {
      var bufIndex = Math.floor(index / 8);
      return ( (_buffer[bufIndex] >>> (7 - index % 8) ) & 1) == 1;
    };

    _this.put = function(num, length) {
      for (var i = 0; i < length; i += 1) {
        _this.putBit( ( (num >>> (length - i - 1) ) & 1) == 1);
      }
    };

    _this.getLengthInBits = function() {
      return _length;
    };

    _this.putBit = function(bit) {

      var bufIndex = Math.floor(_length / 8);
      if (_buffer.length <= bufIndex) {
        _buffer.push(0);
      }

      if (bit) {
        _buffer[bufIndex] |= (0x80 >>> (_length % 8) );
      }

      _length += 1;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrNumber
  //---------------------------------------------------------------------

  var qrNumber = function(data) {

    var _mode = QRMode.MODE_NUMBER;
    var _data = data;

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _data.length;
    };

    _this.write = function(buffer) {

      var data = _data;

      var i = 0;

      while (i + 2 < data.length) {
        buffer.put(strToNum(data.substring(i, i + 3) ), 10);
        i += 3;
      }

      if (i < data.length) {
        if (data.length - i == 1) {
          buffer.put(strToNum(data.substring(i, i + 1) ), 4);
        } else if (data.length - i == 2) {
          buffer.put(strToNum(data.substring(i, i + 2) ), 7);
        }
      }
    };

    var strToNum = function(s) {
      var num = 0;
      for (var i = 0; i < s.length; i += 1) {
        num = num * 10 + chatToNum(s.charAt(i) );
      }
      return num;
    };

    var chatToNum = function(c) {
      if ('0' <= c && c <= '9') {
        return c.charCodeAt(0) - '0'.charCodeAt(0);
      }
      throw 'illegal char :' + c;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrAlphaNum
  //---------------------------------------------------------------------

  var qrAlphaNum = function(data) {

    var _mode = QRMode.MODE_ALPHA_NUM;
    var _data = data;

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _data.length;
    };

    _this.write = function(buffer) {

      var s = _data;

      var i = 0;

      while (i + 1 < s.length) {
        buffer.put(
          getCode(s.charAt(i) ) * 45 +
          getCode(s.charAt(i + 1) ), 11);
        i += 2;
      }

      if (i < s.length) {
        buffer.put(getCode(s.charAt(i) ), 6);
      }
    };

    var getCode = function(c) {

      if ('0' <= c && c <= '9') {
        return c.charCodeAt(0) - '0'.charCodeAt(0);
      } else if ('A' <= c && c <= 'Z') {
        return c.charCodeAt(0) - 'A'.charCodeAt(0) + 10;
      } else {
        switch (c) {
        case ' ' : return 36;
        case '$' : return 37;
        case '%' : return 38;
        case '*' : return 39;
        case '+' : return 40;
        case '-' : return 41;
        case '.' : return 42;
        case '/' : return 43;
        case ':' : return 44;
        default :
          throw 'illegal char :' + c;
        }
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qr8BitByte
  //---------------------------------------------------------------------

  var qr8BitByte = function(data) {

    var _mode = QRMode.MODE_8BIT_BYTE;
    var _data = data;
    var _bytes = qrcode.stringToBytes(data);

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return _bytes.length;
    };

    _this.write = function(buffer) {
      for (var i = 0; i < _bytes.length; i += 1) {
        buffer.put(_bytes[i], 8);
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // qrKanji
  //---------------------------------------------------------------------

  var qrKanji = function(data) {

    var _mode = QRMode.MODE_KANJI;
    var _data = data;

    var stringToBytes = qrcode.stringToBytesFuncs['SJIS'];
    if (!stringToBytes) {
      throw 'sjis not supported.';
    }
    !function(c, code) {
      // self test for sjis support.
      var test = stringToBytes(c);
      if (test.length != 2 || ( (test[0] << 8) | test[1]) != code) {
        throw 'sjis not supported.';
      }
    }('\u53cb', 0x9746);

    var _bytes = stringToBytes(data);

    var _this = {};

    _this.getMode = function() {
      return _mode;
    };

    _this.getLength = function(buffer) {
      return ~~(_bytes.length / 2);
    };

    _this.write = function(buffer) {

      var data = _bytes;

      var i = 0;

      while (i + 1 < data.length) {

        var c = ( (0xff & data[i]) << 8) | (0xff & data[i + 1]);

        if (0x8140 <= c && c <= 0x9FFC) {
          c -= 0x8140;
        } else if (0xE040 <= c && c <= 0xEBBF) {
          c -= 0xC140;
        } else {
          throw 'illegal char at ' + (i + 1) + '/' + c;
        }

        c = ( (c >>> 8) & 0xff) * 0xC0 + (c & 0xff);

        buffer.put(c, 13);

        i += 2;
      }

      if (i < data.length) {
        throw 'illegal char at ' + (i + 1);
      }
    };

    return _this;
  };

  //=====================================================================
  // GIF Support etc.
  //

  //---------------------------------------------------------------------
  // byteArrayOutputStream
  //---------------------------------------------------------------------

  var byteArrayOutputStream = function() {

    var _bytes = [];

    var _this = {};

    _this.writeByte = function(b) {
      _bytes.push(b & 0xff);
    };

    _this.writeShort = function(i) {
      _this.writeByte(i);
      _this.writeByte(i >>> 8);
    };

    _this.writeBytes = function(b, off, len) {
      off = off || 0;
      len = len || b.length;
      for (var i = 0; i < len; i += 1) {
        _this.writeByte(b[i + off]);
      }
    };

    _this.writeString = function(s) {
      for (var i = 0; i < s.length; i += 1) {
        _this.writeByte(s.charCodeAt(i) );
      }
    };

    _this.toByteArray = function() {
      return _bytes;
    };

    _this.toString = function() {
      var s = '';
      s += '[';
      for (var i = 0; i < _bytes.length; i += 1) {
        if (i > 0) {
          s += ',';
        }
        s += _bytes[i];
      }
      s += ']';
      return s;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // base64EncodeOutputStream
  //---------------------------------------------------------------------

  var base64EncodeOutputStream = function() {

    var _buffer = 0;
    var _buflen = 0;
    var _length = 0;
    var _base64 = '';

    var _this = {};

    var writeEncoded = function(b) {
      _base64 += String.fromCharCode(encode(b & 0x3f) );
    };

    var encode = function(n) {
      if (n < 0) {
        // error.
      } else if (n < 26) {
        return 0x41 + n;
      } else if (n < 52) {
        return 0x61 + (n - 26);
      } else if (n < 62) {
        return 0x30 + (n - 52);
      } else if (n == 62) {
        return 0x2b;
      } else if (n == 63) {
        return 0x2f;
      }
      throw 'n:' + n;
    };

    _this.writeByte = function(n) {

      _buffer = (_buffer << 8) | (n & 0xff);
      _buflen += 8;
      _length += 1;

      while (_buflen >= 6) {
        writeEncoded(_buffer >>> (_buflen - 6) );
        _buflen -= 6;
      }
    };

    _this.flush = function() {

      if (_buflen > 0) {
        writeEncoded(_buffer << (6 - _buflen) );
        _buffer = 0;
        _buflen = 0;
      }

      if (_length % 3 != 0) {
        // padding
        var padlen = 3 - _length % 3;
        for (var i = 0; i < padlen; i += 1) {
          _base64 += '=';
        }
      }
    };

    _this.toString = function() {
      return _base64;
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // base64DecodeInputStream
  //---------------------------------------------------------------------

  var base64DecodeInputStream = function(str) {

    var _str = str;
    var _pos = 0;
    var _buffer = 0;
    var _buflen = 0;

    var _this = {};

    _this.read = function() {

      while (_buflen < 8) {

        if (_pos >= _str.length) {
          if (_buflen == 0) {
            return -1;
          }
          throw 'unexpected end of file./' + _buflen;
        }

        var c = _str.charAt(_pos);
        _pos += 1;

        if (c == '=') {
          _buflen = 0;
          return -1;
        } else if (c.match(/^\s$/) ) {
          // ignore if whitespace.
          continue;
        }

        _buffer = (_buffer << 6) | decode(c.charCodeAt(0) );
        _buflen += 6;
      }

      var n = (_buffer >>> (_buflen - 8) ) & 0xff;
      _buflen -= 8;
      return n;
    };

    var decode = function(c) {
      if (0x41 <= c && c <= 0x5a) {
        return c - 0x41;
      } else if (0x61 <= c && c <= 0x7a) {
        return c - 0x61 + 26;
      } else if (0x30 <= c && c <= 0x39) {
        return c - 0x30 + 52;
      } else if (c == 0x2b) {
        return 62;
      } else if (c == 0x2f) {
        return 63;
      } else {
        throw 'c:' + c;
      }
    };

    return _this;
  };

  //---------------------------------------------------------------------
  // gifImage (B/W)
  //---------------------------------------------------------------------

  var gifImage = function(width, height) {

    var _width = width;
    var _height = height;
    var _data = new Array(width * height);

    var _this = {};

    _this.setPixel = function(x, y, pixel) {
      _data[y * _width + x] = pixel;
    };

    _this.write = function(out) {

      //---------------------------------
      // GIF Signature

      out.writeString('GIF87a');

      //---------------------------------
      // Screen Descriptor

      out.writeShort(_width);
      out.writeShort(_height);

      out.writeByte(0x80); // 2bit
      out.writeByte(0);
      out.writeByte(0);

      //---------------------------------
      // Global Color Map

      // black
      out.writeByte(0x00);
      out.writeByte(0x00);
      out.writeByte(0x00);

      // white
      out.writeByte(0xff);
      out.writeByte(0xff);
      out.writeByte(0xff);

      //---------------------------------
      // Image Descriptor

      out.writeString(',');
      out.writeShort(0);
      out.writeShort(0);
      out.writeShort(_width);
      out.writeShort(_height);
      out.writeByte(0);

      //---------------------------------
      // Local Color Map

      //---------------------------------
      // Raster Data

      var lzwMinCodeSize = 2;
      var raster = getLZWRaster(lzwMinCodeSize);

      out.writeByte(lzwMinCodeSize);

      var offset = 0;

      while (raster.length - offset > 255) {
        out.writeByte(255);
        out.writeBytes(raster, offset, 255);
        offset += 255;
      }

      out.writeByte(raster.length - offset);
      out.writeBytes(raster, offset, raster.length - offset);
      out.writeByte(0x00);

      //---------------------------------
      // GIF Terminator
      out.writeString(';');
    };

    var bitOutputStream = function(out) {

      var _out = out;
      var _bitLength = 0;
      var _bitBuffer = 0;

      var _this = {};

      _this.write = function(data, length) {

        if ( (data >>> length) != 0) {
          throw 'length over';
        }

        while (_bitLength + length >= 8) {
          _out.writeByte(0xff & ( (data << _bitLength) | _bitBuffer) );
          length -= (8 - _bitLength);
          data >>>= (8 - _bitLength);
          _bitBuffer = 0;
          _bitLength = 0;
        }

        _bitBuffer = (data << _bitLength) | _bitBuffer;
        _bitLength = _bitLength + length;
      };

      _this.flush = function() {
        if (_bitLength > 0) {
          _out.writeByte(_bitBuffer);
        }
      };

      return _this;
    };

    var getLZWRaster = function(lzwMinCodeSize) {

      var clearCode = 1 << lzwMinCodeSize;
      var endCode = (1 << lzwMinCodeSize) + 1;
      var bitLength = lzwMinCodeSize + 1;

      // Setup LZWTable
      var table = lzwTable();

      for (var i = 0; i < clearCode; i += 1) {
        table.add(String.fromCharCode(i) );
      }
      table.add(String.fromCharCode(clearCode) );
      table.add(String.fromCharCode(endCode) );

      var byteOut = byteArrayOutputStream();
      var bitOut = bitOutputStream(byteOut);

      // clear code
      bitOut.write(clearCode, bitLength);

      var dataIndex = 0;

      var s = String.fromCharCode(_data[dataIndex]);
      dataIndex += 1;

      while (dataIndex < _data.length) {

        var c = String.fromCharCode(_data[dataIndex]);
        dataIndex += 1;

        if (table.contains(s + c) ) {

          s = s + c;

        } else {

          bitOut.write(table.indexOf(s), bitLength);

          if (table.size() < 0xfff) {

            if (table.size() == (1 << bitLength) ) {
              bitLength += 1;
            }

            table.add(s + c);
          }

          s = c;
        }
      }

      bitOut.write(table.indexOf(s), bitLength);

      // end code
      bitOut.write(endCode, bitLength);

      bitOut.flush();

      return byteOut.toByteArray();
    };

    var lzwTable = function() {

      var _map = {};
      var _size = 0;

      var _this = {};

      _this.add = function(key) {
        if (_this.contains(key) ) {
          throw 'dup key:' + key;
        }
        _map[key] = _size;
        _size += 1;
      };

      _this.size = function() {
        return _size;
      };

      _this.indexOf = function(key) {
        return _map[key];
      };

      _this.contains = function(key) {
        return typeof _map[key] != 'undefined';
      };

      return _this;
    };

    return _this;
  };

  var createDataURL = function(width, height, getPixel) {
    var gif = gifImage(width, height);
    for (var y = 0; y < height; y += 1) {
      for (var x = 0; x < width; x += 1) {
        gif.setPixel(x, y, getPixel(x, y) );
      }
    }

    var b = byteArrayOutputStream();
    gif.write(b);

    var base64 = base64EncodeOutputStream();
    var bytes = b.toByteArray();
    for (var i = 0; i < bytes.length; i += 1) {
      base64.writeByte(bytes[i]);
    }
    base64.flush();

    return 'data:image/gif;base64,' + base64;
  };

  //---------------------------------------------------------------------
  // returns qrcode function.

  return qrcode;
}();

// multibyte support
!function() {

  qrcode.stringToBytesFuncs['UTF-8'] = function(s) {
    // http://stackoverflow.com/questions/18729405/how-to-convert-utf8-string-to-byte-array
    function toUTF8Array(str) {
      var utf8 = [];
      for (var i=0; i < str.length; i++) {
        var charcode = str.charCodeAt(i);
        if (charcode < 0x80) utf8.push(charcode);
        else if (charcode < 0x800) {
          utf8.push(0xc0 | (charcode >> 6),
              0x80 | (charcode & 0x3f));
        }
        else if (charcode < 0xd800 || charcode >= 0xe000) {
          utf8.push(0xe0 | (charcode >> 12),
              0x80 | ((charcode>>6) & 0x3f),
              0x80 | (charcode & 0x3f));
        }
        // surrogate pair
        else {
          i++;
          // UTF-16 encodes 0x10000-0x10FFFF by
          // subtracting 0x10000 and splitting the
          // 20 bits of 0x0-0xFFFFF into two halves
          charcode = 0x10000 + (((charcode & 0x3ff)<<10)
            | (str.charCodeAt(i) & 0x3ff));
          utf8.push(0xf0 | (charcode >>18),
              0x80 | ((charcode>>12) & 0x3f),
              0x80 | ((charcode>>6) & 0x3f),
              0x80 | (charcode & 0x3f));
        }
      }
      return utf8;
    }
    return toUTF8Array(s);
  };

}();

(function (factory) {
  if (typeof define === 'function' && define.amd) {
      define([], factory);
  } else if (typeof exports === 'object') {
      module.exports = factory();
  }
}(function () {
    return qrcode;
}));
"""

if __name__ == "__main__":
    main()
