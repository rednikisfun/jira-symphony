"""Entry point: python -m jira_symphony"""

from .cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
