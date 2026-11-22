"""Звірка WAV з індексом аудіоархіву (лише читання): .venv/bin/python -m tools.archive_inventory"""
import argparse
import json

import config
from services.archive_inventory import inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', default=str(config.RECORDINGS_DIR))
    args = parser.parse_args(argv)
    print(json.dumps(inventory(args.archive), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
