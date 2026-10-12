import re
import uuid

from producer.ids import article_id_for, new_process_id, new_upload_id

ULID_RE = r"[0-9A-HJKMNP-TV-Z]{26}"


def test_process_and_upload_ids_have_prefix_and_ulid() -> None:
    assert re.fullmatch(rf"prc_{ULID_RE}", new_process_id())
    assert re.fullmatch(rf"upl_{ULID_RE}", new_upload_id())


def test_upload_ids_sort_by_creation() -> None:
    first, second = new_upload_id(), new_upload_id()
    assert first < second


def test_article_id_is_deterministic_for_source_and_external_id() -> None:
    a = article_id_for("news", "doc-1")
    assert a == article_id_for("news", "doc-1")
    assert re.fullmatch(r"art_[0-9a-f]{32}", a)


def test_article_id_differs_by_source_and_external_id() -> None:
    ids = {
        article_id_for("news", "doc-1"),
        article_id_for("blog", "doc-1"),
        article_id_for("news", "doc-2"),
    }
    assert len(ids) == 3


def test_article_id_separator_prevents_concatenation_collisions() -> None:
    assert article_id_for("ab", "c") != article_id_for("a", "bc")


def test_article_id_without_external_id_is_new_each_time() -> None:
    a, b = article_id_for("news", None), article_id_for("news", None)
    assert a != b
    assert re.fullmatch(rf"art_{ULID_RE}", a)


def test_article_id_namespace_is_pinned() -> None:
    # Guards against accidental NAMESPACE changes, which would re-key every article.
    pinned = uuid.UUID("8c26b88c-3e6e-4dda-8b6e-f1b097eb99ad")
    assert article_id_for("news", "doc-1") == "art_" + uuid.uuid5(pinned, "news\x1fdoc-1").hex
