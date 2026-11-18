"""Evidence for every step: what was searched, what was found, where the click went.

mode: "off" | "failures" (only failure screenshots) | "actions" (default: one annotated
frame per action + failures) . A run folder gets steps.jsonl and report.html (a
timeline you can open after a headless run at 3 a.m.).
"""
from __future__ import annotations

import html
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import cv2

from .frame import Frame
from .geometry import Point, Rect

GREEN, ORANGE, RED, BLUE = (60, 180, 60), (0, 140, 255), (40, 40, 230), (230, 120, 30)


def annotate(frame: Frame, chosen: Rect | None = None, others: Iterable[Rect] = (),
             point: Point | None = None, caption: str = "", near: Rect | None = None):
    img = frame.pixels.copy()
    ox, oy = frame.origin_x, frame.origin_y

    def box(r: Rect, color, t=2):
        cv2.rectangle(img, (r.x - ox, r.y - oy), (r.right - ox - 1, r.bottom - oy - 1), color, t)

    for r in others:
        box(r, ORANGE, 1)
    if near is not None:
        box(near, BLUE, 1)
        cv2.putText(img, "best (below threshold)", (near.x - ox, max(near.y - oy - 4, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, BLUE, 1, cv2.LINE_AA)
    if chosen is not None:
        box(chosen, GREEN, 2)
    if point is not None:
        px, py = point.x - ox, point.y - oy
        cv2.drawMarker(img, (px, py), RED, cv2.MARKER_CROSS, 18, 2)
        cv2.circle(img, (px, py), 10, RED, 1, cv2.LINE_AA)
    if caption:
        (tw, th), _ = cv2.getTextSize(caption[:160], cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (0, 0), (tw + 10, th + 10), (30, 30, 30), -1)
        cv2.putText(img, caption[:160], (5, th + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return img


class Tracer:
    def __init__(self, out_dir: str | os.PathLike = "sightline-traces", mode: str = "actions",
                 on_artifact: Callable[[str, bool, str], None] | None = None):
        self.mode = mode
        self.on_artifact = on_artifact
        self.dir: Path | None = None
        self._n = 0
        self._steps: list[dict] = []
        if mode != "off":
            self.dir = Path(out_dir) / time.strftime("run-%Y%m%d-%H%M%S")
            self.dir.mkdir(parents=True, exist_ok=True)

    def _write(self, entry: dict) -> None:
        self._steps.append(entry)
        if self.dir:
            with open(self.dir / "steps.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def step(self, action: str, target: str, frame: Frame | None, chosen: Rect | None = None,
             point: Point | None = None, others: Iterable[Rect] = (), note: str = "") -> str | None:
        self._n += 1
        path = None
        if self.mode == "actions" and frame is not None and self.dir:
            img = annotate(frame, chosen, others, point, f"#{self._n} {action} {target}")
            path = str(self.dir / f"{self._n:04d}_{_safe(action)}.jpg")
            cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        self._write({"n": self._n, "t": time.time(), "action": action, "target": target,
                     "rect": _r(chosen), "point": [point.x, point.y] if point else None,
                     "note": note, "ok": True, "image": _rel(path, self.dir)})
        if path and self.on_artifact:
            self.on_artifact(path, True, f"{action} {target}")
        return path

    def failure(self, action: str, target: str, message: str, frame: Frame | None,
                others: Iterable[Rect] = (), near: Rect | None = None) -> str | None:
        self._n += 1
        path = None
        if self.mode != "off" and frame is not None and self.dir:
            img = annotate(frame, None, others, None, f"#{self._n} FAILED {action} {target}", near)
            path = str(self.dir / f"{self._n:04d}_FAILED_{_safe(action)}.png")
            cv2.imwrite(path, img)
        self._write({"n": self._n, "t": time.time(), "action": action, "target": target,
                     "note": message, "ok": False, "image": _rel(path, self.dir)})
        if path and self.on_artifact:
            self.on_artifact(path, False, message)
        return path

    def note(self, action: str, text: str) -> None:
        self._n += 1
        self._write({"n": self._n, "t": time.time(), "action": action, "target": "",
                     "note": text, "ok": True, "image": None})

    def close(self) -> str | None:
        if not self.dir or not self._steps:
            return None
        rows = []
        t0 = self._steps[0]["t"]
        for s in self._steps:
            img = (f'<a href="{s["image"]}"><img loading="lazy" src="{s["image"]}"></a>'
                   if s.get("image") else "")
            cls = "ok" if s["ok"] else "fail"
            rows.append(
                f'<tr class="{cls}"><td>{s["n"]}</td><td>+{s["t"] - t0:.2f}s</td>'
                f'<td>{html.escape(s["action"])}</td><td><code>{html.escape(s["target"])}</code>'
                f'<div class="note">{html.escape(s.get("note") or "")}</div></td><td>{img}</td></tr>')
        page = f"""<!doctype html><meta charset="utf-8"><title>sightline run</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--line:#ddd;--bad:#c62828;--badbg:#fdecea}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e6e6;--line:#333;--badbg:#3a1a1a;--bad:#ff8a80}}}}
body{{font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:16px}}
table{{border-collapse:collapse;width:100%}} td{{border-top:1px solid var(--line);padding:6px;vertical-align:top}}
img{{max-width:520px;width:100%;border:1px solid var(--line)}} tr.fail td{{background:var(--badbg)}}
tr.fail td:nth-child(3){{color:var(--bad);font-weight:600}} .note{{opacity:.75;white-space:pre-wrap;margin-top:4px}}
</style><h1>Run {html.escape(self.dir.name)}</h1>
<p>{len(self._steps)} steps, {sum(not s["ok"] for s in self._steps)} failed</p>
<table>{"".join(rows)}</table>"""
        out = self.dir / "report.html"
        out.write_text(page, encoding="utf-8")
        return str(out)


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)[:30]


def _r(r: Rect | None):
    return [r.x, r.y, r.w, r.h] if r else None


def _rel(path: str | None, base: Path | None):
    return os.path.relpath(path, base) if path and base else None
