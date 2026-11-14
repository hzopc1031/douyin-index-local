import json
import sys

if __name__ == "__main__":
    # Windows has different ACL and locking semantics. Reject before importing
    # Unix-only modules until a verified private-storage implementation exists.
    if sys.platform == 'win32':
        print(json.dumps({'exit_code': 5, 'exit_reason': 'windows_comments_not_supported'}))
        raise SystemExit(5)
    from .cli import main
    raise SystemExit(main())
