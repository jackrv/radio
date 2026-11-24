"""Легкий контролер однієї служби: EOF батька → впорядкована зупинка."""

import argparse
import os
import select
import signal
import subprocess
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", type=int, required=True)
    parser.add_argument("--done", type=int, required=True)
    parser.add_argument("--after", type=int)
    parser.add_argument("--lease", type=int)
    parser.add_argument("--grace", type=float, required=True)
    parser.add_argument("--signal", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    # SIGKILL надсилається лише власній новій групі, ніколи групі термінала.
    if os.getpgrp() != os.getpid() or not command:
        return 2
    stopped = threading.Event()
    for signo in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signo, lambda *_: stopped.set())
    child = None
    reader = None
    try:
        # Навіть смерть батька між цією перевіркою й Popen буде виявлена нижче.
        if select.select([args.watch], [], [], 0)[0]:
            return 0
        child = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", close_fds=True,
            pass_fds=tuple(fd for fd in (args.done, args.lease) if fd is not None),
        )

        def drain():
            writable = True
            try:
                for line in child.stdout:
                    if writable:
                        try:
                            print(line, end="", flush=True)
                        except (OSError, ValueError):
                            writable = False
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        parent_gone = False
        while child.poll() is None and not stopped.is_set():
            if select.select([args.watch], [], [], 0.1)[0]:
                parent_gone = True
                break
        if child.poll() is None:
            # Після EOF ngrok чекає завершення radio, API — завершення ngrok.
            while parent_gone and args.after is not None and child.poll() is None:
                if select.select([args.after], [], [], 0.1)[0]:
                    break
            if child.poll() is None:
                child.send_signal(args.signal)
                try:
                    child.wait(timeout=args.grace)
                except subprocess.TimeoutExpired:
                    # Контролер та його нащадки належать одній власній групі.
                    os.killpg(os.getpgrp(), signal.SIGKILL)
        code = child.wait()
        if code < 0:
            # SystemExit(-signal) перетворює -10 на 246. Зберігаємо справжню
            # причину в потоці журналу, а не вгадуємо її з коду оболонки.
            try:
                cause = signal.Signals(-code).name
            except ValueError:
                cause = str(-code)
            print(f"Служба завершилася через сигнал {cause} ({-code})", flush=True)
        return code
    finally:
        if child is not None and child.poll() is None:
            # Не залишати ціль при непередбаченій помилці самого контролера.
            os.killpg(os.getpgrp(), signal.SIGKILL)
        if reader is not None:
            reader.join(timeout=1)
        for fd in (args.watch, args.after, args.done, args.lease):
            if fd is not None:
                os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
