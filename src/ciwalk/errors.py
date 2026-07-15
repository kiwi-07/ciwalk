"""Parser errors and shared exceptions."""


class CiwalkError(Exception):
    """Base error for user-facing failures."""


class ParseError(CiwalkError):
    """Workflow YAML could not be parsed into a supported subset."""


class ConfigError(CiwalkError):
    """Invalid CLI options or unsupported workflow configuration."""


class DockerError(CiwalkError):
    """Docker daemon / container lifecycle failure."""
