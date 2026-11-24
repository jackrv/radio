"""Локальний пілот донавчання multilingual tiny; робочі моделі/БД не змінюються.

prepare працює без MLX. train потребує вільного model slot (приймач зупинений).
Контрольний holdout лабораторії ніколи не використовується для градієнтів/вибору ваг.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import time
import wave

from lab.asr_lab import SPLIT_BLOCK_S, load_records, split_of


def partition(created, seed=345):
    if split_of(created) == 'holdout':
        return 'test'
    # Запас біля меж блоку для суміжних фрагментів однієї передачі.
    offset = created % SPLIT_BLOCK_S
    if offset < 60 or offset > SPLIT_BLOCK_S - 60:
        return 'boundary'
    block = int(created // SPLIT_BLOCK_S)
    digest = hashlib.sha256(f'tiny-validation-v1:{seed}:{block}'.encode()).digest()
    return 'validation' if int.from_bytes(digest, 'big') % 5 == 0 else 'train'


def training_text(reference):
    return ' '.join(reference.replace(' / ', ' ').replace('|', ' ').split())


def separate_duplicates(rows):
    """Однаковий PCM не перетинає поділи; суперечлива розмітка виключається."""
    groups = {}
    for r in rows:
        groups.setdefault(r['pcm_sha256'], []).append(r)
    kept, excluded = [], Counter()
    priority = {'test': 0, 'validation': 1, 'boundary': 2, 'train': 3}
    for group in groups.values():
        if len({(r['label'], r['text']) for r in group}) != 1:
            excluded['conflicting_audio'] += len(group)
            continue
        first = min(group, key=lambda r: (priority[r['split']], r['created'], r['id']))
        kept.append(first)
        excluded['duplicate_audio'] += len(group) - 1
    return kept, excluded


def read_pcm(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError('Потрібен WAV mono PCM16 16000 Hz')
        duration = audio.getnframes() / 16000
        if not 0 < duration <= 30:
            raise ValueError('Тривалість поза 0–30 с')
        return audio.readframes(audio.getnframes()), duration


def prepare(archive, output, seed=345):
    archive, output = Path(archive).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('Каталог набору вже існує')
    candidates, rejected = [], Counter()
    for r in load_records(archive, labelled_only=True):
        label = r['label']
        if label not in ('speech', 'noise', 'notification', 'room'):
            continue
        text = training_text(r['reference'] or '') if label == 'speech' else ''
        if label == 'speech' and not text:
            rejected['empty_speech'] += 1
            continue
        source = archive / (r['id'] + '.wav')
        try:
            pcm, duration = read_pcm(source)
        except (OSError, ValueError, wave.Error, EOFError):
            rejected['unusable_wav'] += 1
            continue
        candidates.append({'id': r['id'], 'created': r['created'], 'label': label,
                           'text': text, 'duration_s': duration, 'split': partition(r['created'], seed),
                           'pcm_sha256': hashlib.sha256(pcm).hexdigest(), 'source': str(source)})
    rows, duplicates = separate_duplicates(candidates)
    rejected.update(duplicates)
    rejected['boundary'] = sum(r['split'] == 'boundary' for r in rows)
    selected = []
    rng = random.Random(seed)
    for split in ('train', 'validation', 'test'):
        speech = [r for r in rows if r['split'] == split and r['label'] == 'speech']
        negatives = [r for r in rows if r['split'] == split and r['label'] != 'speech']
        # Test тут — лише список виключення; його не скорочуємо і не копіюємо.
        if split != 'test':
            negatives = rng.sample(negatives, min(len(negatives), len(speech) // 4))
        selected.extend(speech + negatives)
    for split in ('train', 'validation'):
        if not any(r['split'] == split and r['label'] == 'speech' for r in selected):
            raise ValueError('Недостатньо незалежних блоків train/validation')
    output.mkdir(parents=True, mode=0o700)
    (output / 'audio').mkdir(mode=0o700)
    for r in selected:
        if r['split'] == 'test':
            r['audio'] = None
            continue
        target = output / 'audio' / (r['id'] + '.wav')
        shutil.copyfile(r['source'], target)
        target.chmod(0o600)
        pcm, _ = read_pcm(target)
        if hashlib.sha256(pcm).hexdigest() != r['pcm_sha256']:
            raise RuntimeError('Аудіо змінилося під час підготовки')
        r['audio'] = str(target.relative_to(output))
    manifest = output / 'manifest.json'
    manifest.write_text(json.dumps(selected, ensure_ascii=False, indent=2))
    manifest.chmod(0o600)
    summary = {'seed': seed, 'rejected': dict(rejected),
               'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
               'splits': {s: {'labels': dict(Counter(r['label'] for r in selected if r['split'] == s)),
                              'seconds': round(sum(r['duration_s'] for r in selected if r['split'] == s), 2)}
                          for s in ('train', 'validation', 'test')}}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    return summary


def checked_manifest(dataset, seed):
    dataset = Path(dataset)
    raw = (dataset / 'manifest.json').read_bytes()
    summary = json.loads((dataset / 'summary.json').read_text())
    if summary['manifest_sha256'] != hashlib.sha256(raw).hexdigest() or summary['seed'] != seed:
        raise ValueError('Змінено manifest або seed поділу')
    rows = json.loads(raw)
    seen_ids, seen_hashes = set(), set()
    for row in rows:
        if row['split'] != partition(row['created'], seed):
            raise ValueError('Неправильний часовий поділ')
        if row['id'] in seen_ids or row['pcm_sha256'] in seen_hashes:
            raise ValueError('Повтор аудіо або ID у наборі')
        seen_ids.add(row['id']); seen_hashes.add(row['pcm_sha256'])
    return rows


def target_sequence(tokenizer, text, label, max_context):
    if label == 'speech':
        sequence = list(tokenizer.sot_sequence_including_notimestamps) + tokenizer.encode(text) + [tokenizer.eot]
    else:
        sequence = [tokenizer.sot, tokenizer.no_speech, tokenizer.eot]
    if len(sequence) - 1 > max_context:
        raise ValueError('Еталон не вміщується у контекст; текст не обрізається')
    return sequence[:-1], sequence[1:]


def train(args):
    import config
    from services.model_exclusion import lab_model_slot
    # До імпорту MLX/виділення Metal-пам'яті перевіряємо взаємне виключення.
    with lab_model_slot(config.DB_PATH):
        return _train(args)


def _train(args):
    import numpy as np
    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as optim
    from mlx.utils import tree_flatten, tree_map
    from mlx_whisper.load_models import load_model
    from mlx_whisper.tokenizer import get_tokenizer
    from mlx_whisper.audio import log_mel_spectrogram, pad_or_trim, N_SAMPLES, N_FRAMES
    from core.audio import prepare_audio
    from lab.asr_lab import keep_awake

    base, dataset, output = Path(args.base_model).resolve(), Path(args.dataset).resolve(), Path(args.output).resolve()
    dimensions = json.loads((base / 'config.json').read_text())
    if (dimensions.get('n_audio_state'), dimensions.get('n_audio_layer'), dimensions.get('n_text_layer')) != (384, 4, 4) or dimensions.get('n_vocab', 0) < 51865 or dimensions.get('quantization'):
        raise ValueError('Пілот підтримує лише не квантовану multilingual tiny')
    if output.exists():
        raise FileExistsError('Каталог результату вже існує')
    output.mkdir(parents=True, mode=0o700)
    keep_awake()
    mx.random.seed(args.seed)
    model = load_model(str(base))
    model.set_dtype(mx.float32)
    model.freeze(keys=['alignment_heads'], recurse=False)
    model.train()
    tokenizer = get_tokenizer(multilingual=True, num_languages=model.num_languages, language='uk', task='transcribe')
    rows = checked_manifest(dataset, args.seed)
    token_rows, too_long = [], 0
    for row in rows:
        if row['split'] not in ('train', 'validation'):
            continue
        try:
            row['inputs'], row['targets'] = target_sequence(tokenizer, row['text'], row['label'], model.dims.n_text_ctx)
        except ValueError:
            too_long += 1
            continue
        token_rows.append(row)
    training = [r for r in token_rows if r['split'] == 'train']
    validation = [r for r in token_rows if r['split'] == 'validation']
    rng = random.Random(args.seed)
    validation = rng.sample(validation, min(args.val_limit, len(validation)))
    if not training or not validation:
        raise ValueError('Порожній train або validation')
    provenance = {**vars(args), 'train_records': len(training), 'validation_probe': len(validation),
                  'over_context_skipped': too_long, 'parameters': sum(p.size for _, p in tree_flatten(model.trainable_parameters())),
                  'manifest_sha256': hashlib.sha256((dataset / 'manifest.json').read_bytes()).hexdigest()}
    (output / 'params.json').write_text(json.dumps(provenance, indent=2))
    optimizer = optim.AdamW(learning_rate=args.learning_rate, weight_decay=0.01)

    def example(row):
        path = (dataset / row['audio']).resolve()
        if not path.is_relative_to(dataset):
            raise ValueError('Шлях аудіо за межами набору')
        pcm, _ = read_pcm(path)
        if hashlib.sha256(pcm).hexdigest() != row['pcm_sha256']:
            raise ValueError('Змінено аудіо набору')
        audio, _ = prepare_audio(pcm, 0, True)
        mel = pad_or_trim(log_mel_spectrogram(audio, n_mels=80, padding=N_SAMPLES)[:N_FRAMES], N_FRAMES, axis=0)
        return mel[None], mx.array([row['inputs']]), mx.array([row['targets']])

    def loss_fn(m, mel, inputs, targets):
        return nn.losses.cross_entropy(m(mel, inputs), targets, reduction='mean')

    value_grad = nn.value_and_grad(model, loss_fn)

    def validation_loss():
        model.eval()
        numerator = denominator = 0
        for row in validation:
            inputs = example(row)
            loss = float(loss_fn(model, *inputs).item())
            numerator += loss * len(row['targets'])
            denominator += len(row['targets'])
        model.train()
        value = numerator / denominator
        if not math.isfinite(value):
            raise RuntimeError('Нечисловий validation loss')
        return value

    def export(name, step):
        target = output / name
        target.mkdir(exist_ok=True)
        tensors = {k: v.astype(mx.float16) if mx.issubdtype(v.dtype, mx.floating) else v for k, v in tree_flatten(model.parameters())}
        temp = target / 'pending.safetensors'
        mx.save_safetensors(str(temp), tensors)
        os.replace(temp, target / 'weights.safetensors')
        shutil.copyfile(base / 'config.json', target / 'config.json')
        (target / 'step.json').write_text(json.dumps({'step': step}))

    started = time.monotonic()
    best, step, completed_steps, status = validation_loss(), 0, 0, 'running'
    export('best', 0)
    log = (output / 'metrics.jsonl').open('w')
    log.write(json.dumps({'step': 0, 'validation_loss': best}) + '\n'); log.flush()
    print(json.dumps({'stage': 'initial_validation', 'loss': best, **{k: provenance[k] for k in ('train_records', 'validation_probe', 'parameters')}}), flush=True)
    try:
        for step in range(1, args.steps + 1):
            if time.monotonic() - started >= args.max_seconds:
                step -= 1
                status = 'time_budget'
                break
            tick = time.monotonic()
            accumulated, losses = None, []
            for micro in range(args.accumulate):
                index = (step - 1) * args.accumulate + micro
                epoch, offset = divmod(index, len(training))
                order = np.random.default_rng(args.seed + epoch).permutation(len(training))
                loss, grads = value_grad(model, *example(training[int(order[offset])]))
                mx.eval(loss, grads)
                value = float(loss.item())
                if not math.isfinite(value):
                    raise RuntimeError('Нечисловий train loss')
                losses.append(value)
                accumulated = grads if accumulated is None else tree_map(lambda a, b: a + b, accumulated, grads)
                mx.eval(accumulated)
            grads = tree_map(lambda g: g / args.accumulate, accumulated)
            grads, norm = optim.clip_grad_norm(grads, 1.0)
            mx.eval(norm)
            if not math.isfinite(float(norm.item())):
                raise RuntimeError('Нечисловий градієнт')
            optimizer.learning_rate = args.learning_rate * min(1.0, step / 10)
            optimizer.update(model, grads)
            mx.eval(model.parameters(), optimizer.state)
            completed_steps = step
            peak = mx.get_peak_memory() / 1024**3
            if peak > args.max_memory_gb:
                raise RuntimeError('Перевищено ліміт MLX-пам’яті')
            metric = {'step': step, 'train_loss': sum(losses) / len(losses), 'gradient_norm': float(norm.item()), 'step_s': time.monotonic() - tick, 'peak_gb': peak}
            if step % args.validate_every == 0 or step == args.steps:
                metric['validation_loss'] = validation_loss()
                if metric['validation_loss'] < best:
                    best = metric['validation_loss']
                    export('best', step)
            log.write(json.dumps(metric) + '\n'); log.flush()
            if step % 10 == 0 or 'validation_loss' in metric:
                print(json.dumps(metric), flush=True)
        else:
            status = 'completed'
    except BaseException:
        status = 'interrupted_or_failed'
        raise
    finally:
        log.close()
        export('last', completed_steps)
        (output / 'status.json').write_text(json.dumps({'status': status, 'step': completed_steps, 'best_validation_loss': best, 'elapsed_s': time.monotonic() - started}, indent=2))


def main():
    import config
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    prep = subs.add_parser('prepare')
    prep.add_argument('--archive', default=str(config.RECORDINGS_DIR))
    prep.add_argument('--output', required=True)
    prep.add_argument('--seed', type=int, default=345)
    run = subs.add_parser('train')
    run.add_argument('--dataset', required=True)
    run.add_argument('--base-model', required=True)
    run.add_argument('--output', required=True)
    run.add_argument('--steps', type=int, default=300)
    run.add_argument('--accumulate', type=int, default=2)
    run.add_argument('--learning-rate', type=float, default=1e-5)
    run.add_argument('--max-seconds', type=float, default=900)
    run.add_argument('--max-memory-gb', type=float, default=6)
    run.add_argument('--validate-every', type=int, default=25)
    run.add_argument('--val-limit', type=int, default=32)
    run.add_argument('--seed', type=int, default=345)
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.archive, args.output, args.seed), indent=2))
    else:
        for key in ('steps', 'accumulate', 'validate_every', 'val_limit', 'max_seconds', 'max_memory_gb', 'learning_rate'):
            value = getattr(args, key)
            if not math.isfinite(value) or value <= 0:
                parser.error(f'{key}: потрібне скінченне додатне значення')
        train(args)


if __name__ == '__main__':
    main()
