from recall.ocr import needs_ocr


def test_typed_pdf_does_not_need_ocr():
    assert not needs_ocr(["A full page of typed text. " * 10, "Another page with plenty of words. " * 5])


def test_scanned_pdf_needs_ocr():
    assert needs_ocr(["", "", ""])
    assert needs_ocr(["  3 ", "", "x"])  # stray characters from page numbers or scan noise
    assert needs_ocr([])
