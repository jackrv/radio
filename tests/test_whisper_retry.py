"""Другий шанс Whisper без підказки, коли з підказкою модель зациклилася."""
from unittest.mock import Mock

from core.recognition import WhisperRecognizer, is_looped

LOOP = {"segments": [{"text": " Грозу, Грозу, Грозу", "compression_ratio": 19.9, "avg_logprob": -0.1, "no_speech_prob": 0.0}]}
GOOD = {"segments": [{"text": " Клен 15, прийнято, прийом", "compression_ratio": 1.2, "avg_logprob": -0.2, "no_speech_prob": 0.0}]}


def worker(results, *, retry=True):
    from tests.test_blacklist_safety import make_worker
    w, processor = make_worker("")
    recognizer = WhisperRecognizer("model", "uk", 16000, "клен, дуб", Mock())
    recognizer.transcribe_fn = Mock(side_effect=results)
    w.recognizer, w.retry_without_prompt = recognizer, retry
    return w, processor, recognizer


def pcm():
    return (10000).to_bytes(2, "little", signed=True) * 16000


def test_looped_empty_result_is_retried_without_prompt():
    w, processor, recognizer = worker([LOOP, GOOD])
    w.process_segment(pcm())
    prompts = [call.kwargs["initial_prompt"] for call in recognizer.transcribe_fn.call_args_list]
    assert prompts == ["клен, дуб", None]
    assert processor.process.call_args.args[0] == "Клен 15, прийнято, прийом"


def test_no_retry_when_text_exists_flag_is_off_or_result_is_not_a_loop():
    w, _, recognizer = worker([GOOD])
    w.process_segment(pcm())
    assert recognizer.transcribe_fn.call_count == 1
    w, processor, recognizer = worker([LOOP], retry=False)
    w.process_segment(pcm())
    assert recognizer.transcribe_fn.call_count == 1 and not processor.process.called
    assert not is_looped({"segments": []}) and not is_looped(Mock())
