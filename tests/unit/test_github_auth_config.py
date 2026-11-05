"""Unit tests for App JWT and repo config (no network)."""

import yaml

from winnow.github.app_auth import TOKEN_REFRESH_S, build_jwt
from winnow.github.repo_config import RepoConfig, parse_repo_config


def test_jwt_shape_and_claims() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    token = build_jwt("5496650", pem, now=1_700_000_000)
    import jwt

    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "5496650"
    assert claims["exp"] - claims["iat"] == 660
    assert TOKEN_REFRESH_S == 3000


def test_repo_config_defaults_and_disable() -> None:
    config, warnings = parse_repo_config("agents:\n  security: false\n")
    assert warnings == []
    assert config.enabled_specialists == ["correctness", "performance", "maintainability"]
    assert config.review.max_inline_comments == 10


def test_repo_config_invalid_field_warns_and_falls_back() -> None:
    config, warnings = parse_repo_config(
        "agents:\n  correctness: true\nbogus_section: 1\nreview:\n  max_inline_comments: ora\n"
    )
    assert any("bogus_section" in w for w in warnings)
    assert any("max_inline_comments" in w for w in warnings)
    assert config.review.max_inline_comments == 10  # default kept


def test_repo_config_garbage_yaml() -> None:
    config, warnings = parse_repo_config("[[[not yaml")
    assert isinstance(config, RepoConfig) and warnings


def test_demo_repo_config_parses() -> None:
    text = open("/Users/adam/Documents/winnow-demo-shop/.winnow.yml", encoding="utf-8").read()
    config, warnings = parse_repo_config(text)
    assert warnings == [] and config.commands.allow_public
    assert yaml.safe_load(text)["version"] == 1
