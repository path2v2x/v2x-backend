#!/usr/bin/env python3
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAMERAS = ["ch1", "ch2", "ch3", "ch4"]


def svg_frame(camera_id):
    now = time.strftime("%H:%M:%S")
    color = {
        "ch1": "#22c55e",
        "ch2": "#38bdf8",
        "ch3": "#f59e0b",
        "ch4": "#f472b6",
    }.get(camera_id, "#22c55e")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 480">
  <rect width="640" height="480" fill="#050505"/>
  <rect x="24" y="24" width="592" height="432" fill="#111827" stroke="#334155"/>
  <path d="M0 390 C160 330 260 420 410 365 S560 320 640 350" fill="none" stroke="#475569" stroke-width="18"/>
  <rect x="182" y="148" width="150" height="92" fill="none" stroke="{color}" stroke-width="5"/>
  <circle cx="257" cy="240" r="6" fill="{color}"/>
  <rect x="182" y="112" width="250" height="30" fill="{color}"/>
  <text x="194" y="133" font-family="Arial, sans-serif" font-size="16" fill="#fff">mock vehicle 0.91 GPS OK</text>
  <text x="28" y="54" font-family="Arial, sans-serif" font-size="26" fill="#e5e7eb">{camera_id} perception stream</text>
  <text x="28" y="84" font-family="Arial, sans-serif" font-size="16" fill="#94a3b8">bounding boxes from local mock at {now}</text>
  <text x="28" y="438" font-family="Arial, sans-serif" font-size="18" fill="#22c55e">Detections: 1</text>
</svg>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_HEAD(self):
        if self.path == "/health":
            self.send_response(200)
            self.cors()
            self.send_header("content-type", "application/json")
            self.end_headers()
            return

        for camera_id in CAMERAS:
            if self.path == f"/streams/{camera_id}.svg":
                self.send_response(200)
                self.cors()
                self.send_header("cache-control", "no-store")
                self.send_header("content-type", "image/svg+xml")
                self.end_headers()
                return

        self.send_response(404)
        self.cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "cameras": CAMERAS}).encode("utf-8")
            self.send_response(200)
            self.cors()
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        for camera_id in CAMERAS:
            if self.path == f"/streams/{camera_id}.svg":
                body = svg_frame(camera_id)
                self.send_response(200)
                self.cors()
                self.send_header("cache-control", "no-store")
                self.send_header("content-type", "image/svg+xml")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        self.send_response(404)
        self.cors()
        self.end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock perception stream listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
