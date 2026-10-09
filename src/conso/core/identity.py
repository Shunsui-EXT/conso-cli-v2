"""
Identity provisioning.

The extension authenticates users with Google (or X) OAuth, but the Supabase
project also has email/password auth enabled with open signup, so accounts can
be provisioned without an OAuth provider. This module generates the identity
material (email, password, display name, consoname) and wires it into the
client.

Verification strategy is pluggable: if the mailer does not auto-confirm, an
EmailVerifier implementation (IMAP / catch-all / TempMail API) supplies the
confirmation link or OTP. See ``conso.ops.verifiers``.
"""

from __future__ import annotations

import random
import secrets
import string
from dataclasses import dataclass

from .config import Settings

_ADJECTIVES = [
    "swift", "quiet", "bright", "bold", "calm", "keen", "lucid", "noble",
    "rapid", "vivid", "prime", "solid", "sharp", "clever", "steady", "agile",
]
_NOUNS = [
    "falcon", "otter", "comet", "harbor", "ember", "quartz", "vector", "lumen",
    "cipher", "atlas", "nova", "raven", "delta", "onyx", "pixel", "zenith",
]
_ALPHABET = string.ascii_lowercase + string.digits


@dataclass
class Identity:
    email: str
    password: str
    display_name: str
    consoname: str


def _rand_suffix(rng: random.Random, length: int = 8) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(length))


def generate_password(rng: random.Random, length: int) -> str:
    """URL-safe random password with guaranteed character classes."""
    core = [rng.choice(string.ascii_uppercase), rng.choice(string.ascii_lowercase),
            rng.choice(string.digits), rng.choice("!@#$%^&*-_")]
    core += [secrets.choice(string.ascii_letters + string.digits) for _ in range(max(0, length - 4))]
    rng.shuffle(core)
    return "".join(core)


def generate_consoname(rng: random.Random) -> str:
    return f"{rng.choice(_ADJECTIVES)}{rng.choice(_NOUNS)}{rng.randint(10, 99)}"


def build_identity(settings: Settings, *, rng: random.Random | None = None, index: int = 0) -> Identity:
    rng = rng or random.Random()
    if not settings.email_domain:
        raise ValueError("EMAIL_DOMAIN is required to generate identities (e.g. your catch-all domain).")
    local = f"{settings.display_name_prefix}{_rand_suffix(rng, 10)}"
    email = f"{local}@{settings.email_domain}"
    return Identity(
        email=email,
        password=generate_password(rng, settings.password_length),
        display_name=f"{rng.choice(_ADJECTIVES).capitalize()}{rng.choice(_NOUNS).capitalize()}",
        consoname=generate_consoname(rng),
    )
