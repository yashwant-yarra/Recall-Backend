"""Run with LibreOffice installed (included in the worker Docker image)."""
import shutil
import subprocess
from zipfile import ZipFile

import pytest

from recall.extract import OFFICE_TYPES, extract_text

pytestmark = pytest.mark.skipif(not shutil.which("libreoffice"), reason="LibreOffice is not installed")


@pytest.mark.parametrize("font,replacement", [("Calibri", "Carlito"), ("Cambria", "Caladea")])
def test_office_font_substitution(font, replacement):
    result = subprocess.run(["fc-match", font], check=True, capture_output=True, text=True, timeout=10)
    assert replacement in result.stdout


@pytest.mark.parametrize("mime,extension", OFFICE_TYPES.items())
def test_extract_real_office_files(tmp_path, mime, extension):
    if extension == "doc":
        body = '<office:text><text:p>Recall Word test</text:p></office:text>'
        source_extension, kind = "odt", "text"
        expected = ["Recall Word test"]
    else:
        expected = ["Recall first slide", "Recall second slide"]
        slides = "".join(
            f'<draw:page draw:name="Slide {i}" draw:style-name="{"hidden" if i == 1 else "visible"}">'
            '<draw:frame svg:width="20cm" svg:height="5cm" '
            f'svg:x="1cm" svg:y="1cm"><draw:text-box><text:p>{text}</text:p>'
            '</draw:text-box></draw:frame></draw:page>'
            for i, text in enumerate(expected, 1)
        )
        body = f"<office:presentation>{slides}</office:presentation>"
        source_extension, kind = "odp", "presentation"
    source = tmp_path / f"sample.{source_extension}"
    content = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
        f'office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.{kind}">'
        '<office:automatic-styles><style:style style:name="hidden" style:family="drawing-page">'
        '<style:drawing-page-properties presentation:visibility="hidden"/>'
        '</style:style></office:automatic-styles>'
        f'<office:body>{body}</office:body></office:document-content>'
    )
    with ZipFile(source, "w") as archive:
        archive.writestr("mimetype", f"application/vnd.oasis.opendocument.{kind}")
        archive.writestr("content.xml", content)
        archive.writestr("META-INF/manifest.xml",
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            f'<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.{kind}"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
            '</manifest:manifest>')
    result = subprocess.run(
        ["libreoffice", f"-env:UserInstallation={(tmp_path / 'profile').as_uri()}",
         "--headless", "--convert-to", extension, "--outdir", str(tmp_path), str(source)],
        check=True, capture_output=True, timeout=60,
    )
    assert source.with_suffix(f".{extension}").is_file(), (result.stdout, result.stderr)
    data = source.with_suffix(f".{extension}").read_bytes()
    pages = extract_text(data, mime)
    assert len(pages) == len(expected)
    for page, text in zip(pages, expected):
        assert text in page
