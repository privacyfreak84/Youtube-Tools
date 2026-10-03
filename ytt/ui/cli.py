"""Command line entry point. The only layer that prints or prompts."""
import argparse
import sys

from ytt import __version__


def build_parser():
    ap = argparse.ArgumentParser(prog="ytt", description="Research YouTube and make compilations.")
    ap.add_argument("--version", action="version", version=f"ytt {__version__}")
    return ap


def main(argv=None):
    ap = build_parser()
    ap.parse_args(argv)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
