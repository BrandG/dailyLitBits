"""Display name and the plain-text alternative on every email."""
import config
import dispatch
import security
from plaintext import html_to_text


def capture_smtp(monkeypatch):
    seen = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set_debuglevel(self, *a): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): seen.append(msg)
    monkeypatch.setattr(dispatch.smtplib, "SMTP", FakeSMTP)
    return seen


def test_message_is_alternative_text_then_html_with_display_name(monkeypatch):
    monkeypatch.setattr(config, "FROM_EMAIL", "pages@dailylitbits.com")
    seen = capture_smtp(monkeypatch)
    assert dispatch.send_via_brevo("someone@example.com", "Hi",
                                   '<p>Hello &amp; welcome. <a href="https://x.example/a">Go</a></p>')
    msg = seen[0]
    assert msg["From"] == "dailyLitBits <pages@dailylitbits.com>"
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    text = parts[0].get_payload(decode=True).decode()
    assert "Hello & welcome. Go (https://x.example/a)" in text


def test_sender_with_a_name_already_keeps_just_the_address(monkeypatch):
    monkeypatch.setattr(config, "FROM_EMAIL", "Old Name <pages@dailylitbits.com>")
    seen = capture_smtp(monkeypatch)
    dispatch.send_via_brevo("someone@example.com", "Hi", "<p>x</p>")
    assert seen[0]["From"] == "dailyLitBits <pages@dailylitbits.com>"


def test_converter_drops_head_style_and_hidden_content():
    text = html_to_text("""<html><head><title>T</title><style>.a{color:red}</style></head>
        <body><div style="display: none">preheader</div><h2>Title</h2>
        <p>One<br>Two</p><p>Three</p></body></html>""")
    assert "color" not in text and "preheader" not in text and "T\n" not in text
    assert text == "Title\n\nOne\nTwo\n\nThree\n"


def test_link_whose_text_is_its_url_is_not_doubled():
    assert html_to_text('<a href="https://a.example/">https://a.example/</a>') == "https://a.example/\n"


def test_daily_part_reads_well_as_text(conn, book):
    body = dispatch.format_email_html(
        "Test Book", 3, 4, 10, "First paragraph.\n\nSecond paragraph.",
        security.generate_unsub_token(1), security.generate_next_token(1),
        security.generate_dashboard_token(1), recap="Earlier, things happened.")
    text = html_to_text(body)
    assert "Test Book" in text
    assert "First paragraph.\n\nSecond paragraph." in text
    assert "Previously:" in text and "Earlier, things happened." in text
    assert "Send the next part now (https://dailylitbits.com/next?token=" in text
    assert "Unsubscribe (https://dailylitbits.com/unsubscribe?token=" in text
    assert "\n|\n" not in text and " | Unsubscribe (" in text
    assert "<" not in text and "{" not in text
