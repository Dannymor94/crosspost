"""Published RFC vectors and boundary checks; not an end-to-end MFA test."""

import base64

import pytest

from crosspost.auth.totp import generate_backup_codes, generate_secret, hotp, match_step

SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


@pytest.mark.parametrize(
    ("counter", "expected"),
    enumerate(
        [
            "755224",
            "287082",
            "359152",
            "969429",
            "338314",
            "254676",
            "287922",
            "162583",
            "399871",
            "520489",
        ]
    ),
)
def test_rfc4226(counter, expected):
    assert hotp(SECRET, counter) == expected


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_sha1(timestamp, expected):
    assert hotp(SECRET, timestamp // 30, digits=8) == expected


def test_window_and_replay_boundary():
    step = 100
    code = hotp(SECRET, step)
    assert match_step(SECRET, code, now=step * 30) == step
    assert match_step(SECRET, code, now=(step + 1) * 30) == step
    assert match_step(SECRET, code, now=(step - 1) * 30) == step
    assert match_step(SECRET, code, now=(step + 2) * 30) is None
    assert match_step(SECRET, code, now=step * 30, last_step=step) is None
    assert match_step(SECRET, "１２３４５６", now=step * 30) is None


def test_generated_secrets_and_backup_codes():
    a, b = generate_secret(), generate_secret()
    assert a != b and len(base64.b32decode(a)) == 20
    codes = generate_backup_codes()
    assert len(set(codes)) == 8
    assert all(len(code) == 22 for code in codes)
    with pytest.raises(ValueError):
        generate_backup_codes(0)
