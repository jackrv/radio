from shared.capture_profiles import MAC_MIC, PI_WEBCAM, profile_of, title
from shared.error_analysis import analyse


def test_old_records_get_profile_from_their_source():
    assert profile_of({"capture_source": "portaudio"}) == MAC_MIC
    assert profile_of({}) == MAC_MIC, "найстаріші записи Mac без мітки джерела"
    assert profile_of({"capture_source": "legacy"}) == PI_WEBCAM
    assert profile_of(None) == MAC_MIC


def test_stamped_input_wins_and_device_is_the_fallback():
    assert profile_of({"capture_source": "legacy", "input": {"device": "USB Camera", "profile": "pi-cable"}}) == "pi-cable"
    assert profile_of({"capture_source": "legacy", "input": {"device": "USB Camera", "profile": ""}}) == "USB Camera"
    assert profile_of({"capture_source": "legacy", "input": {"device": None, "profile": ""}}) == PI_WEBCAM
    assert title(PI_WEBCAM) == "вебкамера на Pi" and title("pi-cable") == "pi-cable"


def test_conditions_split_no_edits_by_profile():
    rows = [{"id": f"{i:032x}", "reference": "Клен 48, прийом", "raw_text": "",
             "final_text": "Клен 48, прийом" if i % 2 else "клей 48", "profile": MAC_MIC if i < 4 else PI_WEBCAM}
            for i in range(6)]
    groups = {g["profile"]: g for g in analyse(rows, {"клен"})["conditions"]["profile"]}
    assert (groups[MAC_MIC]["records"], groups[MAC_MIC]["no_edits"]) == (4, 2)
    assert (groups[PI_WEBCAM]["records"], groups[PI_WEBCAM]["no_edits"]) == (2, 1)


def test_filter_stats_count_losses_and_verdicts(tmp_path):
    import numpy as np
    from shared.error_analysis import filter_stats, filter_verdict
    from shared.recordings import RecordingStore
    assert filter_verdict(3, 10)[0] == "label"
    assert filter_verdict(12, 8)[0] == "off" and filter_verdict(5, 15)[0] == "relax" and filter_verdict(1, 19)[0] == "keep"
    store = RecordingStore(tmp_path / "rec")
    pcm = np.zeros(1600, "<i2").tobytes()
    ids = [store.save(pcm, 16000, {"status": "notification", "capture_source": "legacy"}) for _ in range(3)]
    store.save(pcm, 16000, {"status": "notification", "capture_source": "portaudio"})
    store.save(pcm, 16000, {"status": "ready", "capture_source": "legacy"})
    store.review(ids[0], "Клен 48", "speech", 0)
    store.review(ids[1], "", "noise", 0)
    rows = {r["status"]: r for r in filter_stats(store, profile=PI_WEBCAM)}
    assert list(rows) == ["notification"]
    assert (rows["notification"]["dropped"], rows["notification"]["lost"], rows["notification"]["caught"],
            rows["notification"]["unlabelled"]) == (3, 1, 1, 1)
