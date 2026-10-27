"""Regression for Writer's grapheme/character offset mismatch, using a new ODT."""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    import pythoncom
    import win32com.client
    from script_tts.documents import LibreOfficeDocument
    from script_tts.sentences import split_sentences

    pythoncom.CoInitialize()
    doc = None
    temp = tempfile.TemporaryDirectory(prefix='script-tts-range-')
    try:
        service = win32com.client.Dispatch('com.sun.star.ServiceManager')
        desktop = service.createInstance('com.sun.star.frame.Desktop')
        doc = desktop.loadComponentFromURL('private:factory/swriter', '_blank', 0, ())
        body = doc.getText()
        body.setString('')
        cursor = body.createTextCursor()
        for i in range(60):
            cursor.gotoEnd(False)
            body.insertString(cursor, f'Entry {i}: cafe\u0301 and \U0001f469\u200d\U0001f680 records.\nReady. Ready.\nGenerated date: ', False)
            cursor.gotoEnd(False)
            field = doc.createInstance('com.sun.star.text.textfield.DateTime')
            field.setPropertyValue('IsDate', True)
            field.setPropertyValue('IsFixed', True)
            body.insertTextContent(cursor, field, False)
            cursor.gotoEnd(False)
            body.insertString(cursor, '.\n', False)
        cursor.gotoEnd(False)
        body.insertString(cursor, 'Final sentence. Another final sentence. The end.', False)
        text = body.getString()
        doc.storeAsURL((Path(temp.name) / 'range-test.odt').as_uri(), ())
        view = doc.getCurrentController().getViewCursor()
        view.gotoStart(False)
        sentences = split_sentences(text)
        legacy = doc.getText().createTextCursor()
        legacy.gotoStart(False)
        legacy_complete = legacy.goRight(sentences[-1].start, False)
        prefix = doc.getText().createTextCursorByRange(legacy.getStart())
        prefix.gotoStart(True)
        assert not legacy_complete or prefix.getString() != text[:sentences[-1].start], 'Fixture did not reproduce the old offset problem'
        with patch('script_tts.documents.foreground', return_value=0):
            adapter = LibreOfficeDocument(0)
            for sentence in sentences:
                adapter.place(sentence)
                prefix = doc.getText().createTextCursorByRange(view.getStart())
                prefix.gotoStart(True)
                assert prefix.getString() == text[:sentence.start], sentence
            descriptor = doc.createSearchDescriptor()
            descriptor.setSearchString('Final sentence.')
            found = doc.findFirst(descriptor)
            view.gotoRange(found.getStart(), False)
            view.goRight(2, False)
            partial = LibreOfficeDocument(0)
            assert partial.text.startswith('nal sentence.')
            partial_start = text.index('Final sentence.') + 2
            for sentence in split_sentences(partial.text):
                partial.place(sentence)
                prefix = body.createTextCursorByRange(view.getStart())
                prefix.gotoStart(True)
                assert prefix.getString() == text[:partial_start + sentence.start]
        assert doc.getText().getString() == text
        print(f'PASS Writer: old drift reproduced; {len(sentences)} exact ranges verified through EOF, plus restart from a mid-word caret')
    finally:
        if doc is not None:
            doc.close(True)
        temp.cleanup()
        pythoncom.CoUninitialize()


if __name__ == '__main__':
    main()
