"""Локальна копія/перевірка/відновлення корпусу; без моделей і зміни робочої теки."""
import argparse
import json
from pathlib import Path

import config
from services.archive_backup import create_archive_backup, verify_archive_backup, restore_archive_backup, inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup', help='Копіювати WAV та індекс із перевіркою')
    backup.add_argument('--archive', type=Path, default=config.RECORDINGS_DIR)
    backup.add_argument('--destination', type=Path, default=config.BASE_DIR/'.local-archive-backups')
    verify = commands.add_parser('verify', help='Перевірити manifest, файли й індекс')
    verify.add_argument('source', type=Path)
    restore = commands.add_parser('restore', help='Відновити лише в нову теку; робочий архів не перезаписується')
    restore.add_argument('source', type=Path)
    restore.add_argument('--destination', type=Path, required=True)
    check = commands.add_parser('inventory', help='Показати розбіжності WAV та індексу без очищення')
    check.add_argument('--archive', type=Path, default=config.RECORDINGS_DIR)
    args = parser.parse_args(argv)
    if args.command=='backup': result=create_archive_backup(args.archive,args.destination)
    elif args.command=='verify': result=verify_archive_backup(args.source)
    elif args.command=='restore': result=restore_archive_backup(args.source,args.destination)
    else: result=inventory(args.archive)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
