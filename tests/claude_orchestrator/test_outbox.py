from pathlib import Path

from claude_orchestrator.outbox import collect_outgoing_files


def test_outbox_ignores_itself_and_never_sends_its_gitignore(tmp_path: Path) -> None:
    """The outbox gets its own `*` .gitignore, which stays in place and is
    never sent; the project's .gitignore is left alone."""
    (tmp_path / ".git").mkdir()
    outbox = tmp_path / "whatsapp-outbox"
    outbox.mkdir()
    (outbox / "report.pdf").write_bytes(b"%PDF")

    first = collect_outgoing_files(tmp_path)
    second = collect_outgoing_files(tmp_path)

    assert [f.path.parent for f in first.sent] == [outbox / "sent"]
    assert second.sent == []
    assert (outbox / ".gitignore").read_text() == "*\n"
    assert not (tmp_path / ".gitignore").exists()


def test_outbox_keeps_an_existing_gitignore(tmp_path: Path) -> None:
    outbox = tmp_path / "whatsapp-outbox"
    outbox.mkdir()
    (outbox / ".gitignore").write_text("*\nkeep-me\n")

    result = collect_outgoing_files(tmp_path)

    assert result.sent == []
    assert (outbox / ".gitignore").read_text() == "*\nkeep-me\n"
