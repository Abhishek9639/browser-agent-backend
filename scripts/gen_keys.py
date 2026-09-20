#!/usr/bin/env python3
"""Generate the RS256 signing keypair.

    python scripts/gen_keys.py

Writes ``keys/jwt_private.pem`` and ``keys/jwt_public.pem`` and prints the env
lines to paste into ``.env``. ``keys/`` is gitignored: a private key in version
control is a compromised private key.

Refuses to overwrite an existing key unless ``--force`` is passed, because
replacing the key invalidates every token already issued.
"""

import argparse
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY_SIZE = 2048  # RS256 minimum for RFC 7518; 4096 is available via --size
DEFAULT_DIR = Path("keys")


def generate(directory: Path, *, key_size: int, force: bool) -> tuple[Path, Path]:
    private_path = directory / "jwt_private.pem"
    public_path = directory / "jwt_public.pem"

    existing = [p for p in (private_path, public_path) if p.exists()]
    if existing and not force:
        joined = ", ".join(str(p) for p in existing)
        raise SystemExit(
            f"Refusing to overwrite {joined}.\n"
            "Replacing the key invalidates every token already issued. "
            "Pass --force if that is what you want."
        )

    directory.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_path.write_bytes(private_pem)
    # Owner read/write only. The public half is public by definition.
    private_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    public_path.write_bytes(public_pem)

    return private_path, public_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="output directory")
    parser.add_argument("--size", type=int, default=KEY_SIZE, help="RSA key size in bits")
    parser.add_argument("--force", action="store_true", help="overwrite existing keys")
    parser.add_argument("--kid", default="browser-agent-key-1", help="key id for the JWKS")
    args = parser.parse_args()

    if args.size < KEY_SIZE:
        raise SystemExit(f"RS256 requires at least {KEY_SIZE} bits.")

    private_path, public_path = generate(args.dir, key_size=args.size, force=args.force)

    print(f"Wrote {private_path} (0600) and {public_path}\n")  # noqa: T201
    print("Add to .env:\n")  # noqa: T201
    print(f"JWT_PRIVATE_KEY_PATH={private_path}")  # noqa: T201
    print(f"JWT_PUBLIC_KEY_PATH={public_path}")  # noqa: T201
    print(f"JWT_KID={args.kid}")  # noqa: T201
    print("\nkeys/ is gitignored. Never commit the private key.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
