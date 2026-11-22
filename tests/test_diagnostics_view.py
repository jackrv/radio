from api.panel.common import diagnostics_view


def test_dictionary_revision_fingerprint_is_kept_but_free_text_is_not():
    view = diagnostics_view({"gauges": {
        "dictionaries.revision": "0123456789ab",
        "text_lists.revision": 4,
        "free.text": "довільний текст",
    }})
    assert view["gauges"]["dictionaries.revision"] == "0123456789ab"
    assert view["gauges"]["text_lists.revision"] == 4
    assert view["gauges"]["free.text"] is None
