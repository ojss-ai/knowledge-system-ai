"""Entry shim: `python __main__.py ...` from the tool dir (exit-gate invocation).

The implementation lives in cli.py [plan-fix]: a setuptools console script
(`kb-confluence-sync = "cli:main"`) cannot import from a module named
`__main__` — at runtime that name is the script itself.
"""

from cli import main

if __name__ == "__main__":
    main()
