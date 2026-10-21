"""Command-line tasks:  python -m flask --app app backup --out backups --keep 14"""
import os
import click
from .backup import write_backup, prune
from .timeutil import utcnow, to_local


def register(app):
    @app.cli.command('backup')
    @click.option('--out', default='backups', show_default=True, help='Folder to save backups in.')
    @click.option('--keep', default=14, show_default=True, help='How many recent backups to keep.')
    def backup_cmd(out, keep):
        """Save a backup zip (database + uploads) and delete the oldest ones."""
        os.makedirs(out, exist_ok=True)
        target = os.path.join(out, f"baidesk-backup-{to_local(utcnow()).strftime('%Y%m%d-%H%M')}.zip")
        write_backup(app, target)
        removed = prune(out, keep)
        click.echo(f'Backup saved: {target} ({os.path.getsize(target) // 1024} KB). Removed {removed} old backup(s).')
