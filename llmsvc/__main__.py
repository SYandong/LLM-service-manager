# Generated-By: Codex / gpt-6-astra
"""Package entry point. The read-only scheduler arrives in milestone M1."""

import argparse

from llmsvc import __version__


def main():
    parser = argparse.ArgumentParser(description="llmsvc control-plane scaffold")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
