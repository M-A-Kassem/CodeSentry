"""Best-effort screen detection for editors without a dedicated integration.

Opt-in (``screen_detection_enabled``). When on, and only while a *known
coding application* without an API integration (JetBrains IDEs, Sublime,
Notepad++, Eclipse, ...) is the foreground window, it captures **that
window only** every few seconds and analyses the image in memory:

- **Red squiggles** -- thin, wavy, red pixel runs, told apart from red
  text (too tall) and plain red underlines (not wavy). Squiggles carry no
  message, so they are only ever *possible* issues (low confidence): shown
  as ``?n`` on the overlay, never counted as errors, never recorded.
- **Windows OCR** (built into Windows 10/11, runs locally) of visible text,
  matched against compiler-style error lines such as
  ``Program.cs(12,17): error CS0103: ...``. A full compiler-format line
  seen in two consecutive frames is high confidence; exception/traceback
  text alone is medium (possible).

Privacy: images never leave the process and are never written to disk;
OCR text is discarded except for lines that match an error pattern.
Browsers, terminals, chat apps etc. are never captured because they are
not in the known-editor list.

Limitations: anything not visible on screen is invisible to this; themes
with non-red error colours defeat squiggle detection; OCR misreads small
or low-contrast text; a build error still shown in an old output panel
looks current. Prefer an API integration wherever one exists.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import logging
import re
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from ..app_detector import ForegroundApp
from ..diagnostic import ERROR, WARNING, Diagnostic
from .base import STATE_ACTIVE, STATE_ERROR, STATE_IDLE, Provider

try:
    from PIL import Image, ImageChops, ImageGrab
except ImportError:  # pragma: no cover - Pillow is a declared dependency
    Image = ImageChops = ImageGrab = None  # type: ignore[assignment]

_logger = logging.getLogger("screenshot_logger")

SQUIGGLE_CONFIDENCE = 0.4
EXCEPTION_TEXT_CONFIDENCE = 0.6
CODE_ONLY_CONFIDENCE = 0.7
COMPILER_LINE_CONFIDENCE = 0.75
CONFIRMED_BY_REPEAT_BONUS = 0.15  # same line in two consecutive frames
MAX_SQUIGGLES_REPORTED = 50


# -- squiggle detection ---------------------------------------------------------


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    width: int
    height: int


def red_mask(image: "Image.Image") -> "Image.Image":
    """1-byte mask of strongly red pixels. Covers the error squiggle
    colours of VS Code (#F14C4C / #E51400), Visual Studio (#FF0000) and
    JetBrains Darcula (#BC3F3C)."""
    r, g, b = image.convert("RGB").split()
    high_r = r.point(lambda v: 255 if v >= 170 else 0)
    low_g = g.point(lambda v: 255 if v <= 110 else 0)
    low_b = b.point(lambda v: 255 if v <= 110 else 0)
    return ImageChops.multiply(ImageChops.multiply(high_r, low_g), low_b)


def _red_runs(mask: "Image.Image") -> dict[int, list[tuple[int, int]]]:
    """Horizontal runs ``(x_start, x_end)`` of red pixels, per row.

    ``bytes.find`` skips the (usually overwhelming) non-red pixels in C,
    so a full-window scan stays cheap without numpy.
    """
    width, height = mask.size
    data = mask.tobytes()
    runs: dict[int, list[tuple[int, int]]] = {}
    position = data.find(b"\xff")
    while position != -1:
        y, x = divmod(position, width)
        row_end = (y + 1) * width
        end = position
        while end < row_end and data[end] == 255:
            end += 1
        runs.setdefault(y, []).append((x, x + (end - position) - 1))
        position = data.find(b"\xff", end)
    return runs


def find_squiggles(image: "Image.Image") -> list[Box]:
    """Return bounding boxes of shapes that look like error squiggles."""
    runs = _red_runs(red_mask(image))
    # Union touching runs on adjacent rows into connected components.
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for y, row in runs.items():
        for x0, x1 in row:
            parent[(y, x0)] = (y, x0)
            for px0, px1 in runs.get(y - 1, ()):
                if px0 <= x1 + 1 and x0 <= px1 + 1:
                    parent[find((y, x0))] = find((y - 1, px0))

    components: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for y, row in runs.items():
        for x0, x1 in row:
            components.setdefault(find((y, x0)), []).append((y, x0, x1))

    boxes: list[Box] = []
    for segments in components.values():
        xs0 = min(s[1] for s in segments)
        xs1 = max(s[2] for s in segments)
        ys0 = min(s[0] for s in segments)
        ys1 = max(s[0] for s in segments)
        width, height = xs1 - xs0 + 1, ys1 - ys0 + 1
        if width < 12 or not 2 <= height <= 6 or width < height * 4:
            continue  # too short, flat underline, or a glyph/icon
        pixels = sum(x1 - x0 + 1 for _, x0, x1 in segments)
        if not 0.2 <= pixels / (width * height) <= 0.8:
            continue  # solid bar (not wavy) or noise
        if _zigzag_turns(segments, xs0, xs1) < max(2, width // 10):
            continue
        boxes.append(Box(xs0, ys0, width, height))
    return boxes


def _zigzag_turns(segments: list[tuple[int, int, int]], x_start: int, x_end: int) -> int:
    """Count up/down direction changes of the shape's mean y, column by
    column. A wavy squiggle turns every few pixels; a straight line never."""
    column_rows: dict[int, list[int]] = {}
    for y, x0, x1 in segments:
        for x in range(x0, x1 + 1):
            column_rows.setdefault(x, []).append(y)
    means = [sum(column_rows[x]) / len(column_rows[x]) for x in range(x_start, x_end + 1) if x in column_rows]
    turns, direction = 0, 0
    for previous, current in zip(means, means[1:]):
        step = (current > previous) - (current < previous)
        if step and direction and step != direction:
            turns += 1
        if step:
            direction = step
    return turns


# -- OCR text classification -------------------------------------------------------

_DIGITISH = r"[0-9OoQDIl|e\u00f8\ufffd]"  # OCR digit confusions seen in practice
_COMPILER_LINE_RE = re.compile(
    r"(?P<file>[\w.\-\\/: ]+?\.\s?[A-Za-z]{1,6})\s*(?:\(\s*(?P<line>" + _DIGITISH + r"+)[,\s]+" + _DIGITISH + r"+\s*\)|:(?P<line2>"
    + _DIGITISH + r"+)(?::" + _DIGITISH + r"+)?)\s*:?\s*(?P<sev>fatal error|error|warning)\b\s*:?\s*(?P<code>[A-Z]{1,4}" + _DIGITISH
    + r"{2,5})?\s*:?\s*(?P<msg>.*)",
    re.IGNORECASE,
)
_CODE_ONLY_RE = re.compile(
    r"\b(?P<sev>error|warning)\s+(?P<code>(?:CS|TS|BC|FS|LNK|MSB|NU|C)" + _DIGITISH + r"{3,5})\b\s*:?\s*(?P<msg>.*)",
    re.IGNORECASE,
)
_EXCEPTION_RE = re.compile(
    r"(Traceback \(most recent call last\)|Unhandled exception|Exception (?:Unhandled|thrown)"
    r"|\b[A-Z]\w*(?:Error|Exception)\b:\s+\S)"
)


@dataclass(frozen=True)
class TextFinding:
    severity: str
    message: str
    confidence: float
    code: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    detail: str = ""  # message text after the code, used for identity

    @property
    def identity(self) -> str:
        # File name + message words, letters only. The code and position
        # are left out: OCR misreads digits there ("CS01O3", "(l2,17)").
        return re.sub(r"[^a-z]", "", ((self.file or "") + " " + (self.detail or self.message)).lower())[:200]


def _ocr_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = text.translate(str.maketrans("OoQDIl|eø", "000011100"))
    return int(digits) if digits.isdigit() else None


def classify_text_lines(lines: list[str]) -> list[TextFinding]:
    """Turn OCR'd lines into error findings with a confidence score."""
    findings: list[TextFinding] = []
    for raw in lines:
        line = raw.strip()
        if len(line) < 8:
            continue
        match = _COMPILER_LINE_RE.search(line)
        if match:
            findings.append(
                TextFinding(
                    severity=WARNING if match.group("sev").lower() == "warning" else ERROR,
                    message=line[:500],
                    confidence=COMPILER_LINE_CONFIDENCE,
                    code=match.group("code"),
                    file=match.group("file").strip(),
                    line=_ocr_int(match.group("line") or match.group("line2")),
                    detail=match.group("msg"),
                )
            )
            continue
        match = _CODE_ONLY_RE.search(line)
        if match:
            findings.append(
                TextFinding(
                    severity=WARNING if match.group("sev").lower() == "warning" else ERROR,
                    message=line[:500],
                    confidence=CODE_ONLY_CONFIDENCE,
                    code=match.group("code"),
                    detail=match.group("msg"),
                )
            )
            continue
        if _EXCEPTION_RE.search(line):
            findings.append(TextFinding(severity=ERROR, message=line[:500], confidence=EXCEPTION_TEXT_CONFIDENCE))
    return findings


# -- capture and OCR backends --------------------------------------------------------


def window_bounds(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """Visible window rectangle in physical pixels (DWM extended frame
    bounds, which exclude the invisible resize border)."""
    rect = ctypes.wintypes.RECT()
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)
        )  # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        if result != 0 and not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    except Exception:
        return None
    if rect.right - rect.left < 50 or rect.bottom - rect.top < 50:
        return None  # minimised or tiny
    return rect.left, rect.top, rect.right, rect.bottom


def capture_window(hwnd: int) -> Optional["Image.Image"]:
    """Grab only the given window's area (works across monitors, including
    ones left of / above the primary). Kept in memory only."""
    bounds = window_bounds(hwnd)
    if bounds is None or ImageGrab is None:
        return None
    try:
        return ImageGrab.grab(bbox=bounds, all_screens=True)
    except Exception:
        _logger.debug("Window capture failed.", exc_info=True)
        return None


def is_window(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


class WindowsOcr:
    """Windows.Media.Ocr via the optional ``winrt`` packages. Local only."""

    def __init__(self) -> None:
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        self._bitmap_type = SoftwareBitmap
        self._pixel_format = BitmapPixelFormat
        self._writer_type = DataWriter
        self._engine = OcrEngine.try_create_from_user_profile_languages()
        if self._engine is None:
            raise RuntimeError("no OCR language pack is installed for the user's languages")
        self.max_dimension = int(OcrEngine.max_image_dimension)

    def recognize_lines(self, image: "Image.Image") -> list[str]:
        # Code fonts are small; Windows OCR is far more accurate on them at
        # 2x (measured: "CS0103" vs "cse1?3" at 1x), so upscale when it fits.
        scale = 2 if max(image.size) * 2 <= min(self.max_dimension, 5120) else 1
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.LANCZOS)
        elif max(image.size) > self.max_dimension:
            image = image.copy()
            image.thumbnail((self.max_dimension, self.max_dimension))
        rgba = image.convert("RGBA")
        writer = self._writer_type()
        writer.write_bytes(rgba.tobytes("raw", "BGRA"))
        bitmap = self._bitmap_type.create_copy_from_buffer(
            writer.detach_buffer(), self._pixel_format.BGRA8, rgba.width, rgba.height
        )

        async def run():
            return await self._engine.recognize_async(bitmap)

        result = asyncio.run(run())
        words = [
            OcrWord(word.text, word.bounding_rect.x, word.bounding_rect.y, word.bounding_rect.width, word.bounding_rect.height)
            for line in result.lines
            for word in line.words
        ]
        return group_words_into_lines(words)


@dataclass(frozen=True)
class OcrWord:
    text: str
    x: float
    y: float
    width: float
    height: float


def group_words_into_lines(words: list[OcrWord]) -> list[str]:
    """Rebuild visual text lines from OCR words.

    Windows OCR splits one visual line into several "lines" at punctuation
    and gaps (measured: "Program" / "error CS0103:" / "The name ..."), which
    defeats line-based pattern matching. Words are regrouped by vertical
    centre, then split again at wide horizontal gaps so that two
    side-by-side panels do not merge into one line.
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda w: w.y + w.height / 2)
    rows: list[list[OcrWord]] = []
    for word in ordered:
        centre = word.y + word.height / 2
        if rows:
            row = rows[-1]
            row_centre = sum(w.y + w.height / 2 for w in row) / len(row)
            row_height = max(w.height for w in row)
            if abs(centre - row_centre) <= max(row_height, word.height) * 0.6:
                row.append(word)
                continue
        rows.append([word])

    lines: list[str] = []
    for row in rows:
        row.sort(key=lambda w: w.x)
        height = max(w.height for w in row)
        segment = [row[0]]
        for previous, word in zip(row, row[1:]):
            if word.x - (previous.x + previous.width) > height * 4:
                lines.append(" ".join(w.text for w in segment))
                segment = []
            segment.append(word)
        lines.append(" ".join(w.text for w in segment))
    return lines


def load_ocr_backend() -> tuple[Optional[WindowsOcr], str]:
    try:
        return WindowsOcr(), "Windows OCR"
    except ImportError:
        return None, "OCR unavailable (install the optional winrt packages, see README)"
    except Exception as exc:
        return None, f"OCR unavailable ({exc})"


def _frame_signature(image: "Image.Image") -> bytes:
    return image.convert("L").resize((96, 54)).tobytes()


def _frames_differ(a: Optional[bytes], b: bytes) -> bool:
    if a is None or len(a) != len(b):
        return True
    changed = sum(1 for x, y in zip(a, b) if abs(x - y) > 12)
    return changed > len(b) // 400  # ~13 cells of 5184


class ScreenDetectionProvider(Provider):
    name = "screen"
    display_name = "Screen detection (best effort)"

    def __init__(
        self,
        poll_seconds: float = 5.0,
        use_ocr: bool = True,
        capture: Callable[[int], Optional["Image.Image"]] = capture_window,
        ocr=None,
        window_exists: Callable[[int], bool] = is_window,
    ) -> None:
        super().__init__()
        self.poll_seconds = max(1.0, poll_seconds)
        self.use_ocr = use_ocr
        self._capture = capture
        self._ocr = ocr
        self._ocr_note = "OCR off"
        self._window_exists = window_exists
        self._target: Optional[ForegroundApp] = None
        self._target_lock = threading.Lock()
        self._last_signature: dict[int, bytes] = {}
        self._previous_identities: dict[int, set[str]] = {}
        self._last_findings: dict[int, list[Diagnostic]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, tracker) -> None:
        super().start(tracker)
        if self.use_ocr and self._ocr is None:
            self._ocr, self._ocr_note = load_ocr_backend()
        elif self._ocr is not None:
            self._ocr_note = "Windows OCR"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="screen-provider", daemon=True)
        self._thread.start()
        self.set_status(STATE_IDLE, f"waiting for an editor without integration; {self._ocr_note}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        super().stop()

    def on_context(self, foreground, running) -> None:
        eligible = (
            foreground is not None
            and foreground.editor is not None
            and foreground.editor.integration is None
        )
        with self._target_lock:
            self._target = foreground if eligible else None
        # Forget windows that have been closed.
        if self.tracker is not None:
            for scope in self.tracker.scopes(self.name):
                hwnd = int(scope.rsplit(":", 1)[-1])
                if not self._window_exists(hwnd):
                    self.tracker.clear_scope(self.name, scope)
                    self._last_signature.pop(hwnd, None)
                    self._previous_identities.pop(hwnd, None)
                    self._last_findings.pop(hwnd, None)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            with self._target_lock:
                target = self._target
            if target is None:
                continue  # no eligible editor in front: no capture at all
            try:
                self.scan(target)
            except Exception as exc:
                _logger.warning("Screen detection failed.", exc_info=True)
                self.set_status(STATE_ERROR, f"screen analysis failed: {exc}")

    def scan(self, target: ForegroundApp) -> Optional[list[Diagnostic]]:
        """Capture and analyse one frame of ``target``'s window. Returns the
        reported diagnostics, or ``None`` if the frame was skipped."""
        image = self._capture(target.hwnd)
        if image is None:
            return None
        scope = f"{target.process_name}:{target.hwnd}"
        signature = _frame_signature(image)
        if not _frames_differ(self._last_signature.get(target.hwnd), signature):
            # Nothing changed on screen: skip the analysis. Text still
            # visible a frame later counts as seen twice, though.
            last = self._last_findings.get(target.hwnd)
            if not last or self.tracker is None:
                return None
            confirmed = [
                d.with_changes(confidence=min(0.95, d.confidence + CONFIRMED_BY_REPEAT_BONUS))
                if d.source == "screen: OCR" and d.confidence < COMPILER_LINE_CONFIDENCE + CONFIRMED_BY_REPEAT_BONUS
                else d
                for d in last
            ]
            self._last_findings[target.hwnd] = confirmed
            self.tracker.update(self.name, scope, confirmed)
            return confirmed
        self._last_signature[target.hwnd] = signature

        app_key = target.editor.key if target.editor else target.process_name
        diagnostics: list[Diagnostic] = []
        squiggles = find_squiggles(image)
        for box in squiggles[:MAX_SQUIGGLES_REPORTED]:
            diagnostics.append(
                Diagnostic(
                    severity=ERROR,
                    message=f"Possible error underline (red squiggle) at {box.x},{box.y} in the window",
                    source="screen: squiggle",
                    application=app_key,
                    language=target.language,
                    confidence=SQUIGGLE_CONFIDENCE,
                    identity=f"squiggle {box.x // 40} {box.y // 20}",
                )
            )

        identities: set[str] = set()
        if self._ocr is not None:
            previous = self._previous_identities.get(target.hwnd, set())
            for finding in classify_text_lines(self._ocr.recognize_lines(image)):
                identities.add(finding.identity)
                confidence = finding.confidence
                if finding.identity in previous:
                    confidence = min(0.95, confidence + CONFIRMED_BY_REPEAT_BONUS)
                diagnostics.append(
                    Diagnostic(
                        severity=finding.severity,
                        message=finding.message,
                        file=None,  # OCR'd paths are too unreliable to key on
                        line=finding.line,
                        code=finding.code,
                        source="screen: OCR",
                        application=app_key,
                        language=target.language,
                        confidence=confidence,
                        identity=finding.identity,
                    )
                )
            self._previous_identities[target.hwnd] = identities

        self._last_findings[target.hwnd] = diagnostics
        if self.tracker is not None:
            self.tracker.update(self.name, scope, diagnostics)
        name = target.editor.display_name if target.editor else target.process_name
        self.set_status(
            STATE_ACTIVE,
            f"watching {name}: {len(squiggles)} squiggle(s), {len(identities)} error text line(s); {self._ocr_note}",
        )
        return diagnostics
