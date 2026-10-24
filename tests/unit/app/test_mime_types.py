import mimetypes

from lantai.app.mime_types import init_browser_mime_types


def test_init_browser_mime_types_skips_system_knownfiles(monkeypatch):
    reads = []

    def fake_read(self, filename, strict=True):
        _ = self, strict
        reads.append(filename)
        raise PermissionError(filename)

    monkeypatch.setattr(mimetypes, "knownfiles", ["/etc/apache2/mime.types"])
    monkeypatch.setattr(mimetypes.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(mimetypes.MimeTypes, "read", fake_read)
    monkeypatch.setattr(mimetypes, "_db", None)

    init_browser_mime_types()

    assert reads == []
    assert mimetypes.knownfiles == []
    assert mimetypes.guess_type("index.js")[0] == "application/javascript"
    assert mimetypes.guess_type("styles.css")[0] == "text/css"
