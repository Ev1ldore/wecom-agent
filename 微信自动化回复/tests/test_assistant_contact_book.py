"""Manual catalog persists deselected identities and refuses stale saves."""

import json

import pytest

from src.core.assistant.contact_book import ContactBook, contact_id


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "whitelist": [{"conversation": "a", "sender": "alice"}],
                "media": {"keep": True},
            }
        )
    )
    return ContactBook(path)


def test_existing_selection_preserved_then_unselected_contact_stays_visible(book):
    first = book.state()
    assert first["contacts"][0]["selected"] is True
    assert first["contacts"][0]["source"] == "现有白名单配置"
    updated = book.select([], first["revision"])
    assert updated["contacts"][0]["selected"] is False
    assert len(updated["contacts"]) == 1
    assert book.file.read()["media"] == {"keep": True}
    assert ContactBook(book.file.path).state() == updated


def test_new_contact_is_unchecked_and_identity_not_display_name(book):
    result = book.add("同名联系人", "b", "bob", book.state()["revision"])
    assert result["contacts"][-1]["selected"] is False
    result = book.add("同名联系人", "c", "carol", result["revision"])
    assert len({c["id"] for c in result["contacts"]}) == 3
    chosen = result["contacts"][-1]["id"]
    assert book.select([chosen], result["revision"])["contacts"][-1]["selected"]
    assert book.file.read()["whitelist"] == [{"conversation": "c", "sender": "carol"}]


def test_stale_browser_cannot_overwrite_concurrent_cli_change(book):
    version = book.state()["revision"]
    book.file.change("b", "bob")
    with pytest.raises(ValueError, match="其他窗口"):
        book.select([], version)
    assert len(book.file.read()["whitelist"]) == 2


@pytest.mark.parametrize("ids", [["unknown"], [contact_id("a", "alice")] * 2, [1], "*"])
def test_invalid_selection_keeps_file(book, ids):
    before = book.file.path.read_bytes()
    with pytest.raises(ValueError):
        book.select(ids, book.state()["revision"])
    assert book.file.path.read_bytes() == before


def test_duplicate_identity_rejected(book):
    with pytest.raises(ValueError, match="已存在"):
        book.add("another name", "a", "alice", book.state()["revision"])


@pytest.mark.parametrize(
    "contacts", ["invalid", [None], [{"name": "x", "conversation": "a", "sender": "*"}]]
)
def test_invalid_contact_config_rejected(book, contacts):
    cfg = book.file.read()
    cfg["contacts"] = contacts
    book.file.path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        book.state()


def test_empty_catalog_no_demo_contacts(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"whitelist": []}')
    assert ContactBook(path).state()["contacts"] == []
