"""Document adapters. Every COM object stays on the reader's STA thread.

No clipboard replacement or simulated editing keystrokes are used.
"""
from __future__ import annotations

import logging
import ctypes
import re
from ctypes import wintypes

from .sentences import Sentence
from .win32 import foreground, process_name, user32

log = logging.getLogger(__name__)
MAX_TEXT = 500_000


class DocumentError(RuntimeError):
    pass


class PositionError(DocumentError):
    """A visual marker failure must not discard already prepared speech."""


def find_exact_range(search, text):
    found = search.FindText(text, False, False)
    if not found:
        return None
    if found.GetText(-1) == text:
        return found
    # Chromium's PDF provider can return a FindText range shifted by one
    # character at an embedded page boundary. Verify against source text and
    # correct only a small, demonstrably exact range, never an unchecked offset.
    corrections = [(0, -1), (0, 1), (-1, -1), (1, 1)]
    corrections += [(start, end) for start in (0, -1, 1, -2, 2) for end in (0, -1, 1, -2, 2) if (start, end) not in corrections and (start, end) != (0, 0)]
    for start_shift, end_shift in corrections:
        candidate = found.Clone()
        if start_shift:
            candidate.MoveEndpointByUnit(0, 0, start_shift)
        if end_shift:
            candidate.MoveEndpointByUnit(1, 0, end_shift)
        if (candidate.GetText(-1) == text and
                candidate.CompareEndpoints(0, search, 0) >= 0 and
                candidate.CompareEndpoints(1, search, 1) <= 0):
            return candidate
    raise DocumentError("Chrome returned inconsistent text positions. Reselect the sentence's first letter and try again.")


class Document:
    text: str
    name: str

    def __init__(self, hwnd: int):
        self.hwnd = hwnd

    def check_foreground(self):
        if foreground() != self.hwnd:
            raise DocumentError("Stopped because you switched windows. Return to the document and press the hotkey to resume.")

    def place(self, sentence: Sentence):
        raise NotImplementedError

    def finish(self):
        """Keep the final sentence marker in place."""


class NativeEditDocument(Document):
    """Classic Notepad's Edit control does not always expose UIA TextPattern."""

    def __init__(self, hwnd: int):
        super().__init__(hwnd)

        class GUIINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]

        info = GUIINFO()
        info.cbSize = ctypes.sizeof(info)
        thread = user32.GetWindowThreadProcessId(hwnd, None)
        if not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
            raise DocumentError("Could not locate the Notepad caret.")
        self.edit = info.hwndFocus
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW(self.edit, name, len(name))
        if name.value.lower() != "edit":
            raise DocumentError("Click inside the Notepad text first.")
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        if user32.GetWindowLongW(self.edit, -16) & 0x20:  # ES_PASSWORD
            raise DocumentError("Password fields cannot be read.")
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.snapshot = self._text()
        if len(self.snapshot) > MAX_TEXT:
            raise DocumentError("This Notepad document is too large (limit: 500,000 characters).")
        self.start, _ = self._selection()
        self.text = self.snapshot.encode("utf-16-le")[self.start * 2:].decode("utf-16-le")
        self.name = "Notepad"

    def _message(self, message, wparam=0, lparam=0):
        result = ctypes.c_size_t()
        if not user32.SendMessageTimeoutW(self.edit, message, wparam, lparam, 2, 1500, ctypes.byref(result)):
            raise DocumentError("Notepad is not responding. Close any open dialogs and try again.")
        return result.value

    def _selection(self):
        start, end = wintypes.DWORD(), wintypes.DWORD()
        self._message(0xB0, ctypes.addressof(start), ctypes.addressof(end))
        return start.value, end.value

    def _text(self):
        length = self._message(0x0E)
        if length > MAX_TEXT * 2:
            raise DocumentError("This Notepad document is too large (limit: 500,000 characters).")
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._message(0x0D, length + 1, ctypes.addressof(buffer))
        return buffer.value

    def place(self, sentence: Sentence):
        self.check_foreground()
        if self._text() != self.snapshot:
            raise DocumentError("The Notepad document changed. Press the hotkey to start again.")
        offset = self.start + len(self.text[:sentence.start].encode("utf-16-le")) // 2
        self._message(0xB1, offset, offset)
        self._message(0xB7)


class LibreOfficeDocument(Document):
    """LibreOffice's local OLE/UNO bridge, including ODT caret positioning."""

    def __init__(self, hwnd: int):
        super().__init__(hwnd)
        import win32com.client

        service = win32com.client.Dispatch("com.sun.star.ServiceManager")
        desktop = service.createInstance("com.sun.star.frame.Desktop")
        self.component = desktop.getCurrentComponent()
        if self.component is None or not self.component.supportsService("com.sun.star.text.TextDocument"):
            raise DocumentError("Click inside the LibreOffice Writer document first.")
        self.controller = self.component.getCurrentController()
        self.view = self.controller.getViewCursor()
        self.container = self.view.getText()
        self.tail = self.container.createTextCursorByRange(self.view.getStart())
        self.tail.gotoEnd(True)
        self.text = str(self.tail.getString())
        if len(self.text) > MAX_TEXT:
            raise DocumentError("Choose a point closer to the end of the document (limit: 500,000 characters per reading).")
        self.name = "LibreOffice Writer"
        self.search_anchor = self.tail.getStart()
        self.previous_end = 0
        self.search_descriptor = self.component.createSearchDescriptor()
        self.search_descriptor.setPropertyValue("SearchCaseSensitive", True)
        self.search_descriptor.setPropertyValue("SearchRegularExpression", False)
        self.search_descriptor.setPropertyValue("SearchBackwards", False)
        self.search_descriptor.setPropertyValue("SearchWords", False)

    def place(self, sentence: Sentence):
        self.check_foreground()
        if self.tail.getString() != self.text:
            raise DocumentError("The document changed. Press the hotkey to start again from the current caret.")
        try:
            self._place_source(sentence)
        except PositionError:
            raise
        except Exception as exc:
            raise PositionError("LibreOffice could not position the cursor for this sentence; speech continues.") from exc

    def _place_source(self, sentence):
        # Writer cursor units differ from expanded field text in getString().
        # Search native ranges instead of
        # accumulating offsets, and check the intervening source text so repeated
        # dialogue cannot accidentally match a later occurrence.
        first_word = re.match(r"\S+", sentence.text)[0]
        queries = [(sentence.text, sentence.end)]
        if first_word != sentence.text:
            # Writer's search does not match through expanded fields. Its first
            # source word still gives a native marker, verified against the
            # exact intervening text, without counting field display characters.
            queries.append((first_word, sentence.start + len(first_word)))
        expected = self.text[self.previous_end:sentence.start]
        for query, source_end in queries:
            self.search_descriptor.setSearchString(query)
            search_at = self.search_anchor
            for _ in range(128):
                found = self.component.findNext(search_at, self.search_descriptor)
                if found is None:
                    break
                prefix = self.container.createTextCursorByRange(self.search_anchor)
                prefix.gotoRange(found.getStart(), True)
                actual = prefix.getString()
                if actual == expected and found.getString() == query:
                    self.view.gotoRange(found.getStart(), False)
                    self.search_anchor = found.getEnd()
                    self.previous_end = source_end
                    return
                if len(actual) > len(expected):
                    break
                search_at = found.getEnd()
        raise PositionError("LibreOffice could not position the cursor for this sentence; speech continues.")


class UIADocument(Document):
    def __init__(self, hwnd: int, automation, module, element, pattern, start, pdf: bool):
        super().__init__(hwnd)
        self.automation, self.module = automation, module
        self.element, self.pattern = element, pattern
        self.pdf = pdf
        self.tail = start.Clone()
        # Collapse to the start of a selection, then extend only the END to EOF.
        self.tail.MoveEndpointByRange(1, start, 0)
        self.tail.MoveEndpointByRange(1, pattern.DocumentRange, 1)
        self.text = self.tail.GetText(MAX_TEXT + 1)
        if len(self.text) > MAX_TEXT:
            raise DocumentError("Choose a point closer to the end (limit: 500,000 characters per reading).")
        if pattern.SupportedTextSelection == 0:
            raise DocumentError("This document does not allow a caret or text selection for tracking.")
        self.search = self.tail.Clone()
        self.previous_end = 0
        self.name = "Chrome PDF" if pdf else "Accessible text"

    def place(self, sentence: Sentence):
        self.check_foreground()
        # Check the text ahead of the last anchor, so edits cannot make FindText
        # silently skip an occurrence and land on a later repeated sentence.
        expected = self.text[self.previous_end:sentence.end]
        if not self.search.GetText(-1).startswith(expected):
            raise DocumentError("The document changed. Press the hotkey to read from the new position.")
        found = find_exact_range(self.search, sentence.text)
        if not found:
            raise DocumentError("This app could not locate the next sentence in its text layer.")
        marker = found.Clone()
        marker.MoveEndpointByRange(1, found, 0)
        if self.pdf:
            # PDF readers have no editing caret: select the first character.
            if marker.MoveEndpointByUnit(1, 0, 1) != 1:
                raise DocumentError("Chrome could not select the first letter of the sentence.")
        marker.Select()
        # Some providers cannot scroll degenerate ranges; the sentence can scroll.
        try:
            # Scroll only the marker, never the full sentence range: a sentence
            # spanning two PDF pages must not scroll its far end into view.
            marker.ScrollIntoView(True)
        except Exception:
            log.debug("Provider did not scroll the sentence", exc_info=True)
        self.search.MoveEndpointByRange(0, found, 1)
        self.previous_end = sentence.end


def _text_patterns(element, uia):
    try:
        pattern = element.GetCurrentPattern(10014).QueryInterface(uia.IUIAutomationTextPattern)
        return pattern
    except Exception:
        return None


def _selected_start(element, pattern, uia, require_selection: bool):
    # PDFs need an actual selection. A document's implicit [0,0] range must
    # never make the reader unexpectedly start at the first page.
    if not require_selection:
        try:
            newer = element.GetCurrentPattern(10024).QueryInterface(uia.IUIAutomationTextPattern2)
            active, caret = newer.GetCaretRange()
            if active and caret:
                return caret
        except Exception:
            pass
    selection = pattern.GetSelection()
    if selection and selection.Length:
        result = selection.GetElement(0)
        if not require_selection or result.CompareEndpoints(0, result, 1) != 0:
            return result
    return None


def capture_document(hwnd: int) -> Document:
    if not hwnd or foreground() != hwnd:
        raise DocumentError("Click in your document and press the hotkey again.")
    app = process_name(hwnd)
    if app == "notepad.exe":
        try:
            return NativeEditDocument(hwnd)
        except DocumentError:
            pass  # Modern Notepad uses UI Automation instead.
    if app in {"soffice.bin", "soffice.exe"}:
        try:
            return LibreOfficeDocument(hwnd)
        except DocumentError:
            raise
        except Exception as exc:
            log.exception("LibreOffice bridge failed")
            raise DocumentError("Could not connect to LibreOffice. Use the same Windows privilege level for Writer and ScriptTTS, then reopen Writer.") from exc

    import comtypes.client

    uia = comtypes.client.GetModule("UIAutomationCore.dll")
    automation = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    focused = automation.GetFocusedElement()
    if not focused or focused.CurrentIsPassword:
        raise DocumentError("Click inside readable document text first.")
    # In Chrome, a selected PDF may leave focus on the containing browser pane.
    # Inspect the enclosing root's Document descendants as well as ancestors.
    candidates = []
    root = automation.ElementFromHandle(hwnd)
    node = focused
    enclosed = False
    for _ in range(12):
        if not node:
            break
        candidates.append(node)
        if automation.CompareElements(node, root):
            enclosed = True
            break
        node = automation.ControlViewWalker.GetParentElement(node)
    if not enclosed:
        candidates = []
    chrome = app in {"chrome.exe", "msedge.exe"}
    if chrome:
        docs = root.FindAll(4, automation.CreatePropertyCondition(30003, 50030))
        # PDF Document is usually nested under Chrome's outer web document.
        for index in reversed(range(min(docs.Length, 200))):
            candidates.append(docs.GetElement(index))
    choices = []
    for element in candidates:
        try:
            pattern = _text_patterns(element, uia)
            if pattern is None:
                continue
            selected = _selected_start(element, pattern, uia, require_selection=chrome)
            if selected is None:
                continue
            # Prefer the largest enclosing text range that contains the selection.
            # Chrome may expose both a paragraph and the complete PDF document.
            size = len(pattern.DocumentRange.GetText(MAX_TEXT + 1))
            choices.append((size, element, pattern, selected))
        except Exception:
            log.debug("Skipping unsupported accessibility element", exc_info=True)
    if not choices:
        if chrome:
            raise DocumentError("Select the first letter in the PDF, then press the hotkey. Chrome must expose a selectable text layer; scanned PDFs need OCR first.")
        raise DocumentError("This app does not expose a readable caret. Click inside the Notepad document and try again.")
    _, element, pattern, start = max(choices, key=lambda item: item[0])
    return UIADocument(hwnd, automation, uia, element, pattern, start, pdf=chrome)
