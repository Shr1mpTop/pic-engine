# -*- coding: utf-8 -*-
"""假 worker：按引擎契约（multipart /render、/result、/view）秒回假图，
用于不占 GPU 验证协调者的切分/派发/收集/兜底逻辑。

用法：python tests/mock_worker.py [port]     （默认 8299）
环境：MOCK_FAIL=1 时 /render 直接 500（测协调者的本机兜底路径）
假图带 tEXt 元数据 style_index=N（子任务内的序号），供测试断言收集顺序。
"""
import io
import json
import os
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw, PngImagePlugin

FAIL = os.environ.get("MOCK_FAIL") == "1"
JOBS = {}          # rid -> images 清单
FILES = {}         # filename -> (png bytes, style_index)


def make_png(index):
    im = Image.new("RGB", (64, 64), (20 * index % 255, 80, 160))
    ImageDraw.Draw(im).text((8, 24), "S%d" % index, fill=(255, 255, 255))
    buf = io.BytesIO()
    meta = PngImagePlugin.PngInfo()
    meta.add_text("style_index", str(index))
    im.save(buf, format="PNG", pnginfo=meta)
    return buf.getvalue()


def parse_multipart(body, boundary):
    """极简 multipart 解析：返回 {name: bytes}。"""
    out = {}
    for part in body.split(b"--" + boundary.encode()):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        m = re.search(rb'name="([^"]+)"', head)
        if m:
            out[m.group(1).decode()] = content
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # 安静
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/cancel/"):
            return self._json({"rid": self.path.rsplit("/", 1)[-1], "status": "cancelled"})
        if FAIL:
            return self._json({"error": "mock fail mode"}, status=500)
        length = int(self.headers.get("Content-Length", 0))
        boundary = self.headers.get("Content-Type", "").split("boundary=")[-1]
        parts = parse_multipart(self.rfile.read(length), boundary)
        job = json.loads(parts.get("job", b"{}").decode("utf-8-sig"))
        rid = "m" + uuid.uuid4().hex[:8]
        files = []
        for i in range(len(job.get("styles") or [])):
            fn = "mock_%s_%02d.png" % (rid[1:5], i)
            FILES[fn] = (make_png(i), i)          # i = 子任务内风格序号
            files.append({"filename": fn, "subfolder": "", "type": "output"})
        JOBS[rid] = files
        self._json({"rid": rid})

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json({"ok": True})
        if self.path.startswith("/result/"):
            files = JOBS.get(self.path.rsplit("/", 1)[-1])
            if files is None:
                return self._json({"error": "未知任务"}, status=404)
            return self._json({"status": "done", "images": files, "expected": len(files),
                               "stage": "done", "error": None})
        if self.path.startswith("/view"):
            fn = re.search(r"filename=([^&]+)", self.path).group(1)
            data, _ = FILES[fn]
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, status=404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8299
    print("mock worker on :%d fail=%s" % (port, FAIL), flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
