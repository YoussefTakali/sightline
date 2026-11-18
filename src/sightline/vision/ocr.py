"""OCR as a locator strategy and as a reader.

Screen text is small (x-height 5-8 px) and sits on every kind of background: white,
striped rows, blue selection, dark themes, coloured buttons. Tesseract wants dark text
on white, ~30 px capitals. So before OCR every image goes through `ink()`:

    ink = max over B,G,R of |pixel - local background|      (local bg = median 31x31)

which turns ANY text/background pair into dark-on-white (dark text on light, light text
on dark, red on grey...), then it is enlarged. The background is estimated locally, so
it also works on a full screenshot, not only on a single cell.

Engines are pluggable (`OcrEngine`): Tesseract here; PaddleOCR or a cloud OCR can be
added by implementing `words()` and `line()`.
"""
from __future__ import annotations

import difflib
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from ..errors import SightlineError
from ..geometry import Rect


@dataclass
class Word:
    text: str
    rect: Rect          # in the coordinates of the image given to the engine
    conf: float         # 0..1


@dataclass
class Phrase:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def rect(self) -> Rect:
        r = self.words[0].rect
        for w in self.words[1:]:
            r = r.union(w.rect)
        return r


class OcrEngine(Protocol):
    def words(self, bgr: np.ndarray) -> list[Word]: ...
    def line(self, bgr: np.ndarray) -> tuple[str, float]: ...


# ---------------------------------------------------------------- preprocessing
def ink(bgr: np.ndarray, bg_kernel: int = 31) -> np.ndarray:
    """Dark-on-white rendering of whatever text is in `bgr` (uint8, 1 channel)."""
    if bgr.ndim == 2:
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    k = max(3, bg_kernel | 1)
    diffs = [cv2.absdiff(ch, cv2.medianBlur(ch, k)) for ch in cv2.split(bgr)]
    d = np.maximum(np.maximum(diffs[0], diffs[1]), diffs[2]).astype(np.float32)
    hi = max(float(np.percentile(d, 99.7)), 40.0)
    return (255 - np.clip(d * (255.0 / hi), 0, 255)).astype(np.uint8)


def ink_cell(bgr: np.ndarray) -> np.ndarray:
    """For a small crop with ONE background (a cell, a field): distance to the crop's
    median colour, so a selected (blue) row reads like a white one."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    bg = np.median(lab.reshape(-1, 3), axis=0)
    d = np.sqrt(((lab - bg) ** 2).sum(axis=2))
    hi = max(float(np.percentile(d, 99.5)), 40.0)
    return (255 - np.clip(d * (255.0 / hi), 0, 255)).astype(np.uint8)


def text_height(gray_ink: np.ndarray) -> int:
    """Approximate height of the text rows in a dark-on-white image (px)."""
    rows = (gray_ink < 140).any(axis=1)
    runs, n = [], 0
    for v in rows:
        if v:
            n += 1
        elif n:
            runs.append(n)
            n = 0
    if n:
        runs.append(n)
    return int(np.median(runs)) if runs else 0


# ---------------------------------------------------------------- text comparison
def fold(s: str, accents: bool = False, case: bool = False) -> str:
    s = " ".join(s.split())
    if not case:
        s = s.casefold()
    if not accents:
        s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    # characters OCR confuses all the time on UI fonts
    return s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def phrase_similarity(text: str, query: str) -> float:
    """Fuzzy, but every WORD must match on its own. A plain string ratio says
    'commande cmd-0042 - martin' ~ 'commande cmd-0042 - lefevre' (0.8): on business
    screens the part that differs (a name, an amount) is short, so that is a false
    positive. Per word: an OCR typo ('valder') still matches, a different word does not."""
    tw, qw = text.split(), query.split()
    if len(tw) == len(qw):
        per = [similarity(a, b) for a, b in zip(tw, qw)]
        return min(sum(per) / len(per), min(per) + 0.25)
    # OCR glued/split words ("Email:" vs "Email :"): compare without spaces, small penalty
    return similarity(text.replace(" ", ""), query.replace(" ", "")) - 0.05


def group_phrases(words: list[Word], gap_factor: float = 1.2) -> list[Phrase]:
    """Words on the same line and close together form a phrase (a label, a button)."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.rect.center.y, w.rect.x)):
        for line in lines:
            if line[-1].rect.vertical_overlap(w.rect) >= 0.5:
                line.append(w)
                break
        else:
            lines.append([w])
    phrases: list[Phrase] = []
    for line in lines:
        line.sort(key=lambda w: w.rect.x)
        cur = [line[0]]
        for w in line[1:]:
            h = max(cur[-1].rect.h, w.rect.h)
            if w.rect.x - cur[-1].rect.right <= gap_factor * h:
                cur.append(w)
            else:
                phrases.append(Phrase(cur))
                cur = [w]
        phrases.append(Phrase(cur))
    return phrases


@dataclass
class TextHit:
    rect: Rect
    text: str
    score: float


def search_text(words: list[Word], query: str | re.Pattern, exact: bool = False,
                threshold: float = 0.8, accents: bool = False, case: bool = False) -> list[TextHit]:
    """Find `query` (string or compiled regex) among OCR words. A query of N words is
    compared to every run of N-1..N+1 consecutive words of each phrase, so it is found
    inside a longer label too; rects cover exactly the matched words."""
    hits: list[TextHit] = []
    for ph in group_phrases(words):
        ws = ph.words
        if isinstance(query, re.Pattern):
            joined, spans, pos = "", [], 0
            for w in ws:
                spans.append((pos, pos + len(w.text)))
                joined += w.text + " "
                pos += len(w.text) + 1
            for m in query.finditer(joined.rstrip()):
                idx = [i for i, (a, b) in enumerate(spans) if a < m.end() and b > m.start()]
                if idx:
                    sub = Phrase(ws[idx[0]:idx[-1] + 1])
                    hits.append(TextHit(sub.rect, m.group(0), 1.0))
            continue
        q = fold(query, accents, case)
        n = max(len(q.split()), 1)
        best: TextHit | None = None
        for size in sorted({max(1, n - 1), n, n + 1}):
            for i in range(max(len(ws) - size + 1, 0)):
                sub = Phrase(ws[i:i + size])
                t = fold(sub.text, accents, case)
                if exact:
                    score = 1.0 if t == q else 0.0
                else:
                    score = phrase_similarity(t, q)
                if score >= threshold and (best is None or score > best.score or
                                           (score == best.score and sub.rect.w < best.rect.w)):
                    best = TextHit(sub.rect, sub.text, score)
        if best:
            hits.append(best)
    return sorted(hits, key=lambda h: -h.score)


def _overlaps(a: Rect, b: Rect, frac: float = 0.3) -> bool:
    inter = a.intersect(b)
    return inter is not None and inter.area >= frac * min(a.area, b.area)


# ---------------------------------------------------------------- tesseract
class TesseractOcr:
    """Tesseract via pytesseract. lang like 'fra+eng' (needs those .traineddata)."""

    def __init__(self, lang: str = "eng", upscale: float = 2.0, min_conf: float = 0.3,
                 tesseract_cmd: str | None = None):
        try:
            import pytesseract
        except ImportError as exc:
            raise SightlineError("OCR needs: pip install 'sightline[ocr]' and the "
                                 "tesseract program") from exc
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self._pt = pytesseract
        self.lang, self.upscale, self.min_conf = lang, upscale, min_conf
        try:
            have = set(pytesseract.get_languages(config=""))
        except Exception as exc:
            raise SightlineError(f"tesseract program not found or not working: {exc}") from exc
        missing = [l for l in lang.split("+") if l not in have]
        if missing:
            raise SightlineError(
                f"Tesseract language data missing: {missing} (installed: {sorted(have)}). "
                f"Get them from https://github.com/tesseract-ocr/tessdata_best "
                f"(e.g. {missing[0]}.traineddata) into the tessdata folder.")

    def words(self, bgr: np.ndarray, band: int = 110, overlap: int = 40) -> list[Word]:
        """Words of a whole screen, read twice and merged (union of passes):
        - the full image, and
        - overlapping horizontal BANDS of ~110 px.
        Tesseract's page segmentation depends on the crop: on a full screen it can swallow
        a whole row (a menu bar under a title bar), in a band it can miss a button another
        crop reads fine - measured on real screens. Each reading is a separate tesseract
        process, so the passes run in parallel threads (about the time of the full pass).
        The full-screen reading is kept where it has words; bands add what it missed."""
        if bgr.size == 0:
            return []
        h = bgr.shape[0]
        if h <= band + overlap:
            return self._words(bgr)
        jobs = [(0, bgr)] + [(y, bgr[y:min(y + band + overlap, h)])
                             for y in range(0, max(h - overlap, 1), band)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, (os.cpu_count() or 2))) as pool:
            results = list(pool.map(lambda job: (job[0], self._words(job[1])), jobs))
        # the full-screen reading is the base; bands only FILL GAPS where it saw nothing
        # (replacing words would trade a clean "Mot de passe :" for a band's ":|****")
        out: list[Word] = list(results[0][1])
        for y, words in results[1:]:
            for w in words:
                w = Word(w.text, w.rect.offset(0, y), w.conf)
                if not any(_overlaps(w.rect, o.rect) for o in out):
                    out.append(w)
        return out

    def _words(self, bgr: np.ndarray) -> list[Word]:
        img = ink(bgr)
        f = self.upscale
        big = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        d = self._pt.image_to_data(big, lang=self.lang, config="--psm 11 --oem 1",
                                   output_type=self._pt.Output.DICT)
        out = []
        for i, txt in enumerate(d["text"]):
            txt = (txt or "").strip()
            conf = float(d["conf"][i]) / 100.0
            if not txt or conf < self.min_conf:
                continue
            x, y, w, h = (int(round(d[k][i] / f)) for k in ("left", "top", "width", "height"))
            out.append(Word(txt, Rect(x, y, max(w, 1), max(h, 1)), conf))
        return out

    def line(self, bgr: np.ndarray, cell: bool = True) -> tuple[str, float]:
        """Read ONE line of text (a field, a cell). Scales so capitals are ~30 px."""
        if bgr.size == 0:
            return "", 0.0
        img = ink_cell(bgr) if cell else ink(bgr)
        if float(img.std()) < 6:
            return "", 1.0                                   # blank
        h = text_height(img) or img.shape[0]
        f = min(max(30.0 / max(h, 1), 1.0), 6.0)
        big = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        big = cv2.copyMakeBorder(big, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
        d = self._pt.image_to_data(big, lang=self.lang, config="--psm 7 --oem 1",
                                   output_type=self._pt.Output.DICT)
        parts = [(t.strip(), float(c)) for t, c in zip(d["text"], d["conf"]) if (t or "").strip()]
        if not parts:
            return "", 0.0
        return " ".join(p[0] for p in parts), float(np.mean([p[1] for p in parts])) / 100.0


# ---------------------------------------------------------------- RapidOCR (pip only)
class RapidOcr:
    """PaddleOCR models through ONNX Runtime: `pip install rapidocr-onnxruntime` - no
    system program, no admin rights. Often better than Tesseract on small UI fonts.
    It returns text LINES; they are split into words with estimated positions so phrase
    search and label anchors work the same as with Tesseract."""

    def __init__(self, upscale: float = 1.5, min_conf: float = 0.5, **engine_kw):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise SightlineError("RapidOCR engine needs: pip install rapidocr-onnxruntime") from exc
        self._engine = RapidOCR(**engine_kw)
        self.upscale, self.min_conf = upscale, min_conf

    def _lines(self, bgr: np.ndarray):
        f = self.upscale
        img = cv2.resize(bgr, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC) if f != 1 else bgr
        result, _ = self._engine(img)
        out = []
        for box, text, score in result or []:
            if not text.strip() or float(score) < self.min_conf:
                continue
            xs = [p[0] / f for p in box]
            ys = [p[1] / f for p in box]
            out.append((text.strip(), Rect.from_corners(int(min(xs)), int(min(ys)),
                                                         int(max(xs)) + 1, int(max(ys)) + 1),
                        float(score)))
        return out

    def words(self, bgr: np.ndarray) -> list[Word]:
        if bgr.size == 0:
            return []
        words: list[Word] = []
        for text, r, score in self._lines(bgr):
            toks = text.split()
            total = sum(len(t) for t in toks) + max(len(toks) - 1, 0)
            pos = 0
            for t in toks:
                x0 = r.x + int(r.w * pos / max(total, 1))
                x1 = r.x + int(r.w * (pos + len(t)) / max(total, 1))
                words.append(Word(t, Rect(x0, r.y, max(x1 - x0, 1), r.h), score))
                pos += len(t) + 1
        return words

    def line(self, bgr: np.ndarray, cell: bool = True) -> tuple[str, float]:
        lines = sorted(self._lines(bgr), key=lambda t: t[1].x)
        if not lines:
            return "", 0.0
        return " ".join(t for t, _, _ in lines), float(np.mean([s for _, _, s in lines]))


def make_ocr(engine: str = "tesseract", lang: str = "eng"):
    """'tesseract' (needs the tesseract program) | 'rapidocr' (pip only) | 'auto'."""
    engine = (engine or "auto").lower()
    if engine in ("rapidocr", "rapid"):
        return RapidOcr()
    if engine == "tesseract":
        return TesseractOcr(lang)
    try:
        return TesseractOcr(lang)
    except SightlineError as first:
        try:
            return RapidOcr()
        except SightlineError:
            raise first from None
