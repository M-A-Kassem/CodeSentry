"""Screen detection: squiggle recognition on synthetic images, OCR text
classification and line regrouping, and provider behaviour (confidence,
confirmation over two frames, skipping unchanged frames, eligibility)."""
from __future__ import annotations

from PIL import Image, ImageDraw

from screenshot_logger.coding.app_detector import KNOWN_EDITORS, ForegroundApp
from screenshot_logger.coding.issue_tracker import IssueTracker
from screenshot_logger.coding.providers.screen_detector import (
    OcrWord,
    ScreenDetectionProvider,
    classify_text_lines,
    find_squiggles,
    group_words_into_lines,
)

SQUIGGLE_RED = (241, 76, 76)  # VS Code dark theme editorError.foreground


def canvas(width=600, height=200, bg=(30, 30, 30)):
    image = Image.new("RGB", (width, height), bg)
    return image, ImageDraw.Draw(image)


def draw_squiggle(draw, x0, y0, width, colour=SQUIGGLE_RED):
    points = [(x, y0 + (0 if (x // 2) % 2 == 0 else 2)) for x in range(x0, x0 + width)]
    draw.line(points, fill=colour, width=1)


# -- squiggles ---------------------------------------------------------------------


def test_wavy_red_line_is_detected():
    image, draw = canvas()
    draw_squiggle(draw, 50, 60, 120)
    [box] = find_squiggles(image)
    assert (box.x, box.y, box.width) == (50, 60, 120)


def test_light_theme_and_jetbrains_colours_are_detected():
    for colour, bg in (((229, 20, 0), (255, 255, 255)), ((188, 63, 60), (43, 43, 43))):
        image, draw = canvas(bg=bg)
        draw_squiggle(draw, 10, 10, 60, colour)
        assert len(find_squiggles(image)) == 1


def test_red_text_and_straight_underlines_are_not_squiggles():
    image, draw = canvas()
    draw.rectangle((20, 20, 200, 34), fill=SQUIGGLE_RED)  # red block / tall red glyphs
    draw.line([(20, 80), (300, 80)], fill=SQUIGGLE_RED, width=2)  # hyperlink-style underline
    draw.line([(20, 120), (28, 122)], fill=SQUIGGLE_RED)  # too short
    assert find_squiggles(image) == []


def test_non_red_squiggles_are_ignored():
    image, draw = canvas()
    draw_squiggle(draw, 10, 10, 80, colour=(205, 173, 0))  # warning squiggle (yellow)
    assert find_squiggles(image) == []


# -- OCR text ------------------------------------------------------------------------


def test_compiler_lines_are_recognised_with_ocr_noise():
    findings = classify_text_lines([
        "Program. CS (12, 17): error CS0103: The name 'foo' does not exist",  # real Windows OCR output
        "src/app.ts(3,5): error TS2322: Type 'string' is not assignable",
        "main.c:5:10: warning: unused variable 'x'",
        "error cse1�3: something",  # OCR misread of CS0103 at 1x scale
    ])
    assert [f.severity for f in findings] == ["error", "error", "warning", "error"]
    assert findings[0].line == 12 and findings[0].code == "CS0103"
    assert findings[3].code is not None


def test_exception_text_is_lower_confidence_and_plain_words_are_ignored():
    findings = classify_text_lines([
        "Traceback (most recent call last):",
        "ValueError: invalid literal for int()",
        "we should handle the error case later",  # just the word 'error'
        "errors = []",
    ])
    assert len(findings) == 2
    assert all(f.confidence < 0.7 for f in findings)


def test_identity_survives_ocr_digit_noise():
    a, b = classify_text_lines(["a.cs(1,2): error CS0103: name 'foo'", "a.cs(1,2): error CS01O3: name 'foo'"])
    assert a.identity == b.identity


def test_ocr_words_are_regrouped_into_visual_lines():
    # Windows OCR returned this one visual line as three separate "lines".
    words = [
        OcrWord("Program.cs(12,17):", 20, 100, 150, 12),
        OcrWord("error", 180, 103, 40, 9),
        OcrWord("CS0103:", 230, 100, 60, 12),
        OcrWord("The", 300, 101, 30, 11),
        OcrWord("x = 1", 20, 20, 50, 12),
        OcrWord("PROBLEMS", 1500, 101, 80, 11),  # far-right panel: separate segment
    ]
    lines = group_words_into_lines(words)
    assert lines == ["x = 1", "Program.cs(12,17): error CS0103: The", "PROBLEMS"]


# -- provider -------------------------------------------------------------------------------


class FakeOcr:
    def __init__(self, lines):
        self.lines = lines
        self.calls = 0

    def recognize_lines(self, _image):
        self.calls += 1
        return list(self.lines)


def jetbrains_app(hwnd=42):
    return ForegroundApp(hwnd, 7, "pycharm64.exe", "main.py – PyCharm", KNOWN_EDITORS["pycharm64.exe"], "main.py", "Python")


def make_provider(frames, ocr_lines=()):
    tracker = IssueTracker(debounce_seconds=0, min_confidence=0.8)
    frames = list(frames)
    ocr = FakeOcr(ocr_lines)
    provider = ScreenDetectionProvider(
        capture=lambda _hwnd: frames.pop(0) if len(frames) > 1 else frames[0],
        ocr=ocr,
        window_exists=lambda _hwnd: True,
    )
    provider.tracker = tracker
    tracker.update("screen", "pycharm64.exe:42", [])  # baseline
    return provider, tracker, ocr


def test_squiggles_are_possible_issues_never_counted():
    image, draw = canvas()
    draw_squiggle(draw, 50, 60, 120)
    provider, tracker, _ = make_provider([image])
    provider.scan(jetbrains_app())
    counts = tracker.counts()
    assert counts.errors == 0 and counts.possible == 1 and counts.new_errors == 0


def test_compiler_line_counts_only_after_being_seen_in_two_frames():
    image, _ = canvas()
    provider, tracker, ocr = make_provider([image], ["Main.java:3: error: cannot find symbol"])
    provider.scan(jetbrains_app())
    assert tracker.counts().errors == 0 and tracker.counts().possible == 1
    provider.scan(jetbrains_app())  # same frame again: confirmed without re-running OCR
    assert ocr.calls == 1
    assert tracker.counts().errors == 1 and tracker.counts().new_errors == 1


def test_changed_frame_reanalyses_and_resolves():
    first, draw = canvas()
    draw.rectangle((0, 0, 600, 100), fill=(200, 200, 200))
    second, _ = canvas()
    provider, tracker, ocr = make_provider([first, first, second], ["Main.java:3: error: cannot find symbol"])
    provider.scan(jetbrains_app())
    provider.scan(jetbrains_app())
    assert tracker.counts().errors == 1
    ocr.lines = []  # user fixed it; the screen changed
    provider.scan(jetbrains_app())
    assert ocr.calls == 2
    assert tracker.counts().errors == 0


def test_only_editors_without_integration_are_captured():
    provider, _, _ = make_provider([canvas()[0]])
    vscode = ForegroundApp(1, 2, "Code.exe", "a.py - VS Code", KNOWN_EDITORS["code.exe"])
    browser = ForegroundApp(1, 2, "chrome.exe", "Bank login", None)
    for app in (vscode, browser, None):
        provider.on_context(app, {})
        assert provider._target is None
    provider.on_context(jetbrains_app(), {})
    assert provider._target is not None


def test_closed_window_findings_are_cleared():
    image, draw = canvas()
    draw_squiggle(draw, 50, 60, 120)
    provider, tracker, _ = make_provider([image])
    provider.scan(jetbrains_app())
    provider._window_exists = lambda _hwnd: False
    provider.on_context(None, {})
    assert tracker.current_issues() == []
