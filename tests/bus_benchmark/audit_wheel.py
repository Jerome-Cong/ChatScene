"""Check exact wheel package inventory against the sole editable source."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('wheel',type=Path)
    parser.add_argument('source',type=Path)
    args=parser.parse_args()
    expected={str(p.relative_to(args.source.parent)):p for p in args.source.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    with zipfile.ZipFile(args.wheel) as archive:
        names=archive.namelist()
        package={n for n in names if n.startswith('bus_benchmark/')}
        assert len(names)==len(set(names)), 'duplicate wheel members'
        assert package==set(expected), {'missing':sorted(set(expected)-package),'extra':sorted(package-set(expected))}
        assert all(n.startswith('bus_benchmark/') or n.startswith('bus_scene_benchmark-') and '.dist-info/' in n for n in names), 'foreign assets in wheel'
        for name,path in expected.items():
            assert archive.read(name)==path.read_bytes(), name
    print(json.dumps({'wheel':args.wheel.name,'package_files':len(expected),'sha256':hashlib.sha256(args.wheel.read_bytes()).hexdigest(),'exact_source_match':True},indent=2))


if __name__=='__main__':
    main()
