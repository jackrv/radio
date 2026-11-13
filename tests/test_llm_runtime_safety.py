import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from core.correction import build_llm_corrector


def make_corrector(monkeypatch, response="Ліра 68", fail=False):
    fake_module = ModuleType("mlx_lm")

    tokenizer = Mock()
    tokenizer.apply_chat_template.return_value = "TEST_PROMPT"

    fake_module.load = Mock(
        return_value=(object(), tokenizer)
    )

    limits = []

    def fake_generate(
        model, tokenizer, *,
        prompt, verbose, max_tokens
    ):
        limits.append(max_tokens)

        if fail:
            raise RuntimeError("Test generation failure")

        return response

    fake_module.generate = fake_generate

    monkeypatch.setitem(
        sys.modules,
        "mlx_lm",
        fake_module,
    )

    vocab = SimpleNamespace(
        names={"ліра", "наган"}
    )

    log = Mock()

    correct = build_llm_corrector(
        True,
        "test-model",
        vocab,
        vocab,
        log,
    )

    assert callable(correct)

    return correct, limits, log, tokenizer


def test_generation_limit_scales_with_message(monkeypatch):
    correct, limits, _, _ = make_corrector(monkeypatch)

    assert correct("Ліра 68") == "Ліра 68"
    assert correct("слово " * 30) == "Ліра 68"
    assert correct("слово " * 200) == "Ліра 68"

    assert limits[0] == 64
    assert 64 < limits[1] < 512
    assert limits[2] == 512


def test_generation_error_returns_original(monkeypatch):
    correct, _, log, _ = make_corrector(
        monkeypatch,
        fail=True,
    )

    original = "Ліпка 700, прийом"

    assert correct(original) == original

    log.exception.assert_called_once()


def test_empty_llm_response_returns_original(monkeypatch):
    correct, _, _, _ = make_corrector(
        monkeypatch,
        response="   ",
    )

    original = "Буря 011"

    assert correct(original) == original


def test_tokenizer_error_returns_original(monkeypatch):
    correct, _, log, tokenizer = make_corrector(
        monkeypatch
    )

    tokenizer.apply_chat_template.side_effect = RuntimeError(
        "Tokenizer failure"
    )

    original = "Наган 110"

    assert correct(original) == original

    log.exception.assert_called_once()
