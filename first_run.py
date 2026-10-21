"""First-run setup: make sure there is a private secret key before the server starts."""
import secrets
from pathlib import Path

_PW = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'


def ensure_env(base_dir):
    """Create / complete `.env` with a random SECRET_KEY (the default one is public, so sessions could be forged).

    Returns {'admin_password': ...} the first time a brand-new install is set up, else None.
    The admin password is only needed by a *new* database; it is not written to `.env`."""
    path = Path(base_dir) / '.env'
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    keys = {line.split('=', 1)[0].strip() for line in text.splitlines() if '=' in line and not line.lstrip().startswith('#')}
    added, first = [], None
    if 'SECRET_KEY' not in keys:
        added.append(f'SECRET_KEY={secrets.token_hex(32)}')
    if not path.exists():
        added += ['HOST=0.0.0.0', 'PORT=8000', 'SESSION_HOURS=12']
        first = {'admin_password': ''.join(secrets.choice(_PW) for _ in range(12))}
    if added:
        with open(path, 'a', encoding='utf-8') as f:
            if text and not text.endswith('\n'):
                f.write('\n')
            f.write('\n'.join(added) + '\n')
    return first
