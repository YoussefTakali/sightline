"""Windows' built-in OCR (Windows.Media.Ocr) - nothing to install, no admin rights, any
Python version. It reads the languages of the Windows user profile (French on a French PC).

Driven through Windows PowerShell 5.1 (present on every Windows 10/11; PowerShell 7 has no
WinRT projection). One long-lived PowerShell process answers one image per request, so an
OCR call costs no process start. The script is passed with -EncodedCommand: that is not a
script FILE, so execution policies that block .ps1 files do not apply.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading

import cv2
import numpy as np

from ..errors import SightlineError
from ..geometry import Rect
from .ocr import Word

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, [Type]$type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
  [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
  [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
  [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime] | Out-Null
  [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
  $lang = '__LANG__'
  if ($lang) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($lang)) }
  else { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
  if ($null -eq $engine) { throw "no OCR engine for language '$lang' (install the Windows language pack's OCR)" }
  [Console]::Out.WriteLine('READY ' + $engine.RecognizerLanguage.LanguageTag); [Console]::Out.Flush()
} catch { [Console]::Out.WriteLine('ERROR ' + $_.Exception.Message); [Console]::Out.Flush(); exit 1 }
while ($true) {
  $path = [Console]::In.ReadLine()
  if ($null -eq $path -or $path -eq '') { break }
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $words = New-Object System.Collections.ArrayList
    $n = 0
    foreach ($line in $result.Lines) {
      foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        [void]$words.Add([ordered]@{ t = $w.Text; x = $r.X; y = $r.Y; w = $r.Width; h = $r.Height; l = $n })
      }
      $n++
    }
    $stream.Dispose()
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject @($words) -Compress -Depth 3))
  } catch { [Console]::Out.WriteLine((ConvertTo-Json -InputObject @{ error = $_.Exception.Message } -Compress)) }
  [Console]::Out.Flush()
}
"""


class WindowsOcr:
    """OCR engine using Windows.Media.Ocr. language: None = the user profile's languages,
    or a tag like 'fr-FR' (its OCR component must be installed with the language pack)."""

    def __init__(self, language: str | None = None, upscale: float = 2.0, timeout: float = 30.0):
        if sys.platform != "win32":
            raise SightlineError("Windows OCR is only available on Windows")
        ps = shutil.which("powershell") or os.path.join(
            os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
            "powershell.exe")
        if not os.path.exists(ps):
            raise SightlineError("Windows PowerShell (powershell.exe) not found")
        script = _PS_SCRIPT.replace("__LANG__", (language or "").replace("'", ""))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        self.upscale, self.timeout = upscale, timeout
        self._proc = subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._lock = threading.Lock()
        self._dir = tempfile.mkdtemp(prefix="sightline-winocr-")
        first = self._read(timeout=60)
        if not first.startswith("READY"):
            self.close()
            raise SightlineError(f"Windows OCR could not start: {first.removeprefix('ERROR ').strip()}")
        self.language = first.split(" ", 1)[1].strip() if " " in first else "?"
        atexit.register(self.close)

    def _pump(self) -> None:
        for line in self._proc.stdout:
            self._lines.put(line.rstrip("\r\n"))
        self._lines.put(None)

    def _read(self, timeout: float) -> str:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise SightlineError(f"Windows OCR did not answer within {timeout:g}s") from None
        if line is None:
            raise SightlineError("Windows OCR process stopped")
        return line

    def _recognize(self, bgr: np.ndarray) -> list[dict]:
        f = self.upscale
        img = cv2.resize(bgr, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC) if f != 1 else bgr
        with self._lock:
            path = os.path.join(self._dir, "frame.png")
            cv2.imwrite(path, img)
            self._proc.stdin.write(path + "\n")
            self._proc.stdin.flush()
            out = self._read(self.timeout)
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            raise SightlineError(f"Windows OCR: {data.get('error', data)}")
        return data or []

    def words(self, bgr: np.ndarray) -> list[Word]:
        if bgr.size == 0:
            return []
        f = self.upscale
        return [Word(str(d["t"]), Rect(int(d["x"] / f), int(d["y"] / f),
                                       max(int(round(d["w"] / f)), 1), max(int(round(d["h"] / f)), 1)),
                     0.9)
                for d in self._recognize(bgr) if str(d.get("t", "")).strip()]

    def line(self, bgr: np.ndarray, cell: bool = True) -> tuple[str, float]:
        if bgr.size == 0:
            return "", 0.0
        ws = sorted(self._recognize(cv2.copyMakeBorder(bgr, 8, 8, 8, 8, cv2.BORDER_REPLICATE)),
                    key=lambda d: (d["l"], d["x"]))
        text = " ".join(str(d["t"]) for d in ws)
        return text, (0.9 if text else 0.0)

    def close(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        shutil.rmtree(getattr(self, "_dir", ""), ignore_errors=True)
