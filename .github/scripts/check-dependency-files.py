"""Check that pip constraint files are also discoverable by Dependabot."""
from pathlib import Path
import shlex


def check(root):
    errors = []
    for manifest in sorted((root / 'services').glob('*/requirements.txt')):
        for line in manifest.read_text().splitlines():
            words = shlex.split(line, comments=True)
            if not words or words[0] not in ('-c', '--constraint', '-r', '--requirement'):
                continue
            if len(words) != 2:
                errors.append(f'{manifest}: malformed requirement/constraint reference')
                continue
            referenced = manifest.parent / words[1]
            if not referenced.is_file():
                errors.append(f'{manifest}: missing {words[1]}')
            elif referenced.suffix not in ('.txt', '.in'):
                errors.append(f'{manifest}: Dependabot cannot stage {words[1]}; use .txt or .in')
    return errors


if __name__ == '__main__':
    failures = check(Path(__file__).resolve().parents[2])
    if failures:
        raise SystemExit('\n'.join(failures))
    print('Dependency file references are compatible with pip and Dependabot.')
