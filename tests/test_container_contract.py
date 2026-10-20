"""Guard the checked-in Streamlit command and configuration security contract."""

import json
import shlex
import tomllib
from pathlib import Path

import pytest


def _disabled_protections(dockerfile_text: str, config_text: str) -> set[str]:
    flags = {"server.enableCORS": "cors", "server.enableXsrfProtection": "xsrf"}
    env_flags = {
        "STREAMLIT_SERVER_ENABLE_CORS": "cors",
        "STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION": "xsrf",
    }
    disabled = set()

    def check(name: str, value: object) -> None:
        text = str(value).casefold()
        assert text in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
            "on",
            "off",
        }, "Unknown protection value requires runtime review"
        if text in {"false", "0", "no", "off"}:
            disabled.add(name)

    command = None
    for line in dockerfile_text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        instruction, body = parts[0], parts[1] if len(parts) > 1 else ""
        instruction = instruction.upper()
        assert (
            instruction != "ENTRYPOINT"
        ), "New entrypoint requires explicit security review"
        if instruction == "ENV":
            pairs = shlex.split(body, comments=True)
            if pairs and "=" not in pairs[0]:
                assert len(pairs) == 2, "Unsupported legacy ENV structure"
                pairs = ["=".join(pairs)]
            for pair in pairs:
                name, separator, value = pair.partition("=")
                assert separator, "Unsupported ENV structure"
                if name in env_flags:
                    check(env_flags[name], value)
        elif instruction == "CMD":
            command = json.loads(body) if body.startswith("[") else ["sh", "-c", body]
    assert isinstance(command, list) and all(
        isinstance(x, str) for x in command
    ), "Missing or unsupported runtime command"
    if command[:2] in (["sh", "-c"], ["/bin/sh", "-c"]):
        assert len(command) == 3, "Unsupported shell arguments"
        # The one supported expansion is quoted as a whole argument, so PORT
        # cannot expand into additional Streamlit flags through shell splitting.
        static = command[2].replace(
            '"--server.port=${PORT:-8501}"', "--server.port=8501"
        )
        assert not any(
            char in static for char in "$`*?[]()\n\r\"'\\"
        ), "Shell expansion requires explicit security review"
        lexer = shlex.shlex(static, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        command = list(lexer)
    assert command[:2] == ["streamlit", "run"], "Unsupported runtime wrapper"
    assert not set(command) & {
        ";",
        "&&",
        "||",
        "|",
        "&",
        ">",
        "<",
    }, "Shell composition requires explicit security review"
    tokens = iter(command[2:])
    for token in tokens:
        name, separator, value = token.removeprefix("--").partition("=")
        if token.startswith("--") and name in flags:
            if not separator:
                value = next(tokens, "")
            check(flags[name], value)
    server = tomllib.loads(config_text).get("server", {})
    for name, label in flags.items():
        key = name.removeprefix("server.")
        if key in server:
            check(label, server[key])
    return disabled


@pytest.mark.parametrize(
    ("command", "environment", "config", "expected"),
    [
        ("streamlit run app.py --server.enableCORS=false", "", "", {"cors"}),
        ("streamlit run app.py --server.enableXsrfProtection false", "", "", {"xsrf"}),
        ("streamlit run app.py --server.enableCORS=FaLsE", "", "", {"cors"}),
        (
            "streamlit run app.py",
            "ENV STREAMLIT_SERVER_ENABLE_CORS=False",
            "",
            {"cors"},
        ),
        (
            "streamlit run app.py",
            "ENV STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION false",
            "",
            {"xsrf"},
        ),
        (
            "streamlit run app.py",
            "ENV\tSTREAMLIT_SERVER_ENABLE_CORS=false",
            "",
            {"cors"},
        ),
        (
            "streamlit run app.py",
            "",
            "[server]\nenableXsrfProtection = false\n",
            {"xsrf"},
        ),
        ("streamlit run app.py", "", "[server]\nenableCORS = false\n", {"cors"}),
        ("streamlit run app.py --server.enableCORS=true", "", "", set()),
        ("streamlit run app.py", "ENV STREAMLIT_SERVER_ENABLE_CORS=True", "", set()),
        ("streamlit run app.py", "", "[server]\nenableCORS = true\n", set()),
        ("streamlit   run app.py  --server.headless=true", "", "", set()),
        (
            "streamlit run app.py",
            "# ENV STREAMLIT_SERVER_ENABLE_CORS=false",
            '# enableCORS = false\n[theme]\nbase = "dark"\n',
            set(),
        ),
    ],
)
def test_detects_disabled_protection_in_supported_sources(
    command: str, environment: str, config: str, expected: set[str]
) -> None:
    """Detect disabled settings regardless of the supported declaration form."""
    dockerfile = environment + "\nCMD " + json.dumps(["sh", "-c", command])
    assert _disabled_protections(dockerfile, config) == expected


@pytest.mark.parametrize(
    "command",
    [
        'CMD ["sh", "-c", "other-command"]',
        'CMD ["sh", "-c", "streamlit run app.py && other-command"]',
        'CMD ["sh", "-c", "streamlit run app.py&other-command"]',
        'CMD ["sh", "-c", "streamlit run app.py --server.enableCORS=${UNSET}"]',
        'CMD ["sh", "-c", "streamlit run app.py $EXTRA_FLAGS"]',
        "CMD streamlit run app.py $EXTRA_FLAGS",
        "CMD streamlit run app.py ${EXTRA_FLAGS}",
        'CMD ["sh", "-c", "streamlit run app.py $(get-flags)"]',
        "CMD streamlit run app.py `get-flags`",
        "CMD streamlit run app.py --server.port=${PORT:-8501}",
        "CMD streamlit run app.py *",
        "CMD "
        + json.dumps(
            [
                "sh",
                "-c",
                "streamlit run app.py\nstreamlit run app.py --server.enableCORS=false",
            ]
        ),
    ],
)
def test_unsupported_commands_do_not_appear_safe(command: str) -> None:
    """Unknown wrappers or dynamic protection values require explicit review."""
    with pytest.raises(AssertionError):
        _disabled_protections(
            'ENV EXTRA_FLAGS="--server.enableCORS=false --server.enableXsrfProtection=false"\n'
            + command,
            "",
        )


def test_repository_keeps_cross_site_protections_enabled() -> None:
    """The actual runtime source must not disable either built-in protection."""
    root = Path(__file__).resolve().parents[1]
    assert (
        _disabled_protections(
            (root / "Dockerfile").read_text(),
            (root / ".streamlit/config.toml").read_text(),
        )
        == set()
    )
