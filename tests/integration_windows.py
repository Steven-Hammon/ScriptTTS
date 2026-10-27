r"""Opt-in adapter checks using disposable test documents, with no model download.

Run: .venv\Scripts\python.exe tests\integration_windows.py libreoffice
These tests exercise the installed application; run outside a restricted sandbox.
"""
from __future__ import annotations

import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def libreoffice():
    import pythoncom
    import win32com.client
    from script_tts.documents import LibreOfficeDocument
    from script_tts.sentences import split_sentences
    from script_tts.win32 import foreground, process_name

    pythoncom.CoInitialize()
    document = None
    temp = tempfile.TemporaryDirectory(prefix='script-tts-')
    try:
        service = win32com.client.Dispatch('com.sun.star.ServiceManager')
        desktop = service.createInstance('com.sun.star.frame.Desktop')
        document = desktop.loadComponentFromURL('private:factory/swriter', '_blank', 0, ())
        text = 'First \U0001f600 sentence. Second sentence.\nThird paragraph.'
        document.getText().setString(text)
        document.storeAsURL((Path(temp.name) / 'caret-test.odt').as_uri(), ())
        with patch('script_tts.documents.foreground', return_value=0):
            _check_libreoffice(document, text, LibreOfficeDocument, split_sentences, foreground, process_name)
        print('PASS LibreOffice ODT: caret tracked 3 sentences across an emoji and paragraph, text unchanged')
    finally:
        if document is not None:
            document.close(True)
        temp.cleanup()
        pythoncom.CoUninitialize()


def _check_libreoffice(document, text, LibreOfficeDocument, split_sentences, foreground, process_name):
        view = document.getCurrentController().getViewCursor()
        view.gotoStart(False)
        adapter = LibreOfficeDocument(0)
        assert adapter.text == text, 'Did not capture the disposable Writer document'
        sentences = split_sentences(adapter.text)
        for sentence in sentences:
            adapter.place(sentence)
            before = document.getText().createTextCursorByRange(view.getStart())
            before.gotoStart(True)
            actual = before.getString()
            assert actual == text[:sentence.start], (sentence.text, repr(actual))
        assert document.getText().getString() == text


def notepad():
    import subprocess
    import comtypes.client
    import win32gui
    from script_tts.documents import capture_document, NativeEditDocument
    from script_tts.sentences import split_sentences
    from script_tts.win32 import foreground

    uia = comtypes.client.GetModule('UIAutomationCore.dll')
    automation = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    hwnd = None
    with tempfile.TemporaryDirectory(prefix='script-tts-') as temp:
        path = Path(temp) / f'script-tts-test-{uuid.uuid4().hex[:8]}.txt'
        text = 'First \U0001f600 sentence. Second sentence.\r\nThird paragraph.'
        path.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8'))
        subprocess.Popen(['notepad.exe', str(path)])
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                found = []
                win32gui.EnumWindows(lambda h, _: found.append(h) if path.stem in win32gui.GetWindowText(h) else None, None)
                if len(found) == 1:
                    hwnd = found[0]
                    break
                time.sleep(0.1)
            assert hwnd is not None, 'Notepad test window did not open'
            root = automation.ElementFromHandle(hwnd)
            controls = root.FindAll(4, automation.CreateTrueCondition())
            edit, pattern = None, None
            for i in range(controls.Length):
                element = controls.GetElement(i)
                try:
                    candidate = element.GetCurrentPattern(10014).QueryInterface(uia.IUIAutomationTextPattern)
                    if candidate.DocumentRange.GetText(-1).startswith('First'):
                        edit, pattern = element, candidate
                        break
                except Exception:
                    pass
            if pattern is not None:
                edit.SetFocus()
                marker = pattern.DocumentRange.Clone()
                marker.MoveEndpointByRange(1, marker, 0)
                marker.Select()
            with patch('script_tts.documents.foreground', return_value=hwnd):
                adapter = capture_document(hwnd)
                assert adapter.text.startswith('First \U0001f600 sentence.')
                for sentence in split_sentences(adapter.text):
                    adapter.place(sentence)
                    if isinstance(adapter, NativeEditDocument):
                        offset, end = adapter._selection()
                        assert offset == end
                        assert adapter.snapshot.encode('utf-16-le')[offset * 2:].decode('utf-16-le').startswith(sentence.text)
                    else:
                        selected = pattern.GetSelection().GetElement(0)
                        tail = selected.Clone()
                        tail.MoveEndpointByRange(1, pattern.DocumentRange, 1)
                        assert tail.GetText(-1).startswith(sentence.text), sentence.text
            if isinstance(adapter, NativeEditDocument):
                assert adapter._text() == adapter.snapshot
            else:
                assert pattern.DocumentRange.GetText(-1) == adapter.text
            print('PASS Notepad TXT: captured caret and tracked 3 sentences, text unchanged')
        finally:
            if hwnd is not None:
                win32gui.PostMessage(hwnd, 0x0010, 0, 0)
                deadline = time.monotonic() + 3
                while win32gui.IsWindow(hwnd) and time.monotonic() < deadline:
                    time.sleep(0.05)


def _pdf(path):
    pages = [b'First sentence. Second sentence. Third sentence.',
             b'Repeated sentence. Repeated sentence.', b'Last sentence.']
    kids = ' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>'.encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for i, text in enumerate(pages):
        content = 5 + 2 * i
        stream = b'BT /F1 16 Tf 72 700 Td (' + text + b') Tj ET'
        objects.extend([
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>'.encode(),
            b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream'])
    result = b'%PDF-1.4\n'
    offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(result))
        result += f'{index} 0 obj\n'.encode() + item + b'\nendobj\n'
    xref = len(result)
    result += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    result += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    result += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(result)


def chrome():
    import subprocess
    import comtypes.client
    import win32gui
    from script_tts.documents import capture_document, find_exact_range
    from script_tts.sentences import split_sentences

    uia = comtypes.client.GetModule('UIAutomationCore.dll')
    automation = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    hwnd = None
    with tempfile.TemporaryDirectory(prefix='script-tts-', ignore_cleanup_errors=True) as temp:
        path = Path(temp) / f'script-tts-pdf-test-{uuid.uuid4().hex[:8]}.pdf'
        _pdf(path)
        process = subprocess.Popen([r'C:\Program Files\Google\Chrome\Application\chrome.exe', '--user-data-dir=' + str(Path(temp) / 'profile'), '--force-renderer-accessibility', '--no-first-run', '--no-default-browser-check', '--new-window', path.as_uri()])
        try:
            deadline = time.monotonic() + 12
            pattern = None
            while time.monotonic() < deadline:
                windows = []
                win32gui.EnumWindows(lambda h, _: windows.append(h) if path.stem in win32gui.GetWindowText(h) else None, None)
                if len(windows) != 1:
                    time.sleep(0.2)
                    continue
                hwnd = windows[0]
                root = automation.ElementFromHandle(hwnd)
                controls = root.FindAll(4, automation.CreatePropertyCondition(30003, 50030))
                for i in reversed(range(controls.Length)):
                    element = controls.GetElement(i)
                    try:
                        candidate = element.GetCurrentPattern(10014).QueryInterface(uia.IUIAutomationTextPattern)
                        if 'First sentence.' in candidate.DocumentRange.GetText(-1):
                            pattern = candidate
                            break
                    except Exception:
                        pass
                if pattern is not None:
                    break
                time.sleep(0.3)
            assert pattern is not None, 'Chrome PDF did not expose a document TextPattern'
            first = find_exact_range(pattern.DocumentRange, 'First sentence.')
            original = first.Clone()
            first.MoveEndpointByRange(1, original, 0)
            first.MoveEndpointByUnit(1, 0, 1)
            first.Select()
            # Chromium may rebuild its accessibility tree after PDF startup.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                selected_ranges = pattern.GetSelection()
                if selected_ranges and selected_ranges.Length:
                    break
                first.Select()
                time.sleep(0.1)
            with patch('script_tts.documents.foreground', return_value=hwnd):
                adapter = capture_document(hwnd)
                assert adapter.text.startswith('First sentence.'), repr(adapter.text[:160])
                pattern = adapter.pattern
                sentences = split_sentences(adapter.text, line_breaks=False)
                assert len(sentences) == 6, [sentence.text for sentence in sentences]
                for sentence in sentences:
                    try:
                        adapter.place(sentence)
                    except Exception:
                        print('Failed sentence:', repr(sentence.text))
                        print('Remaining text:', repr(adapter.search.GetText(-1)[:140]))
                        match = adapter.search.FindText(sentence.text, False, False)
                        print('FindText result:', repr(match.GetText(-1)) if match else None)
                        raise
                    time.sleep(0.15)
                    selected = pattern.GetSelection().GetElement(0)
                    assert selected.GetText(-1) == sentence.text[0], (sentence.text, selected.GetText(-1))
            print('PASS Chrome PDF: selected first letter of 6 sentences across 3 pages, including repeated text')
        finally:
            if hwnd is not None:
                win32gui.PostMessage(hwnd, 0x0010, 0, 0)
                deadline = time.monotonic() + 3
                while win32gui.IsWindow(hwnd) and time.monotonic() < deadline:
                    time.sleep(0.05)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    if sys.argv[1:] == ['libreoffice']:
        libreoffice()
    elif sys.argv[1:] == ['notepad']:
        notepad()
    elif sys.argv[1:] == ['chrome']:
        chrome()
    else:
        raise SystemExit('Specify libreoffice')
