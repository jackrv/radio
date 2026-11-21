import json

from services.experiments import compare_runs, report
from services.lab_results import summarize
from core.text_metrics import error_rates


def row(key, reference, hypothesis, label="speech"):
    return dict(id=key, label=label, reference=reference, hypothesis=hypothesis,
                score={**error_rates(reference, hypothesis), "reference_has_digits": False},
                reference_recipient=None, reference_sender=None)


def save_run(root, name, rows):
    folder = root / name; folder.mkdir()
    (folder / "params.json").write_text(json.dumps({"records":len(rows),"variant":"pipeline"}))
    (folder / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (folder / "metrics.json").write_text(json.dumps({"summary":summarize(rows)}))


def test_comparison_excludes_changed_references_and_explains_improvements(tmp_path):
    a, b = "20261120T010000Z_q-base", "20261120T020000Z_q-turbo"
    save_run(tmp_path,a,[row("1","Клен прийом","Клен"),row("2","старий еталон","старий еталон"),row("3","так","так")])
    save_run(tmp_path,b,[row("1","Клен прийом","Клен прийом"),row("2","новий еталон","новий еталон"),row("4","так","так")])
    result = compare_runs(tmp_path,a,b)
    assert result["common"] == 1 and result["excluded_reference"] == 1
    assert result["only_a"] == 1 and result["only_b"] == 1
    assert result["better"] == 1 and result["worse"] == 0
    assert result["examples"] == [{"id":"1","delta":-1}]
    data = report(tmp_path,b)
    assert data["baseline"] == a and data["comparison"] == result


def test_report_does_not_invent_zero_metrics_or_compare_disjoint_data(tmp_path):
    a, b = "20261120T010000Z_q-base", "20261120T020000Z_q-turbo"
    save_run(tmp_path,a,[row("1","так","так")])
    save_run(tmp_path,b,[row("2","ні","ні")])
    data = report(tmp_path,b)
    assert data["comparison"]["common"] == 0
    assert "false_text_on_non_speech" not in data["run"]["summary"]
