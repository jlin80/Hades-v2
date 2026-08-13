"""Structural proof that Hades V2 cannot execute a financial operation.

task.md §16 requires the project to be *physically incapable* of trading during
the data-collection phases — not merely configured not to. Configuration can be
flipped; a missing dependency cannot. Hades v1 relied on this same idea and it
was one of the few guarantees that genuinely held: no signer, quote or RPC
adapter was ever wired, so live trading was impossible by construction.

These tests fail the build the moment a signing-capable dependency or a
transaction-submitting code path appears, whatever the intent behind it.
"""

import re
import tomllib
from pathlib import Path

# Packages capable of building, signing or submitting a Solana (or EVM)
# transaction. None of these belong in the project until a trading phase is
# explicitly authorised.
FORBIDDEN_PACKAGES = frozenset(
    {
        "anchorpy",
        "bip32",
        "bip44",
        "ecdsa",
        "eth-account",
        "mnemonic",
        "pynacl",
        "solana",
        "solathon",
        "solders",
        "web3",
    }
)

# Identifiers that only appear in code that moves money.
FORBIDDEN_PATTERNS = (
    r"\bprivate_key\b",
    r"\bsecret_key\b",
    r"\bKeypair\b",
    r"\bsign_transaction\b",
    r"\bsend_transaction\b",
    r"\bsendTransaction\b",
    r"\bsign_and_send\b",
    r"\bfrom_seed\b",
)

_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9_.\-]+)")


def _declared_packages(project_root: Path) -> set[str]:
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    requirements: list[str] = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)

    names: set[str] = set()
    for requirement in requirements:
        match = _REQUIREMENT_NAME.match(requirement.strip())
        if match:
            names.add(match.group(1).lower())
    return names


def test_no_signing_capable_dependency_is_declared(project_root: Path) -> None:
    declared = _declared_packages(project_root)
    violations = declared & FORBIDDEN_PACKAGES
    assert not violations, (
        f"dependency capable of signing/submitting transactions: {sorted(violations)}. "
        "Phases 0-4 must remain incapable of executing a financial operation."
    )


def test_no_transaction_submitting_code_exists(project_root: Path) -> None:
    offenders: list[str] = []
    for source in (project_root / "src").rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, text):
                offenders.append(f"{source.relative_to(project_root)}: {pattern}")

    assert not offenders, f"transaction-capable code found: {offenders}"


def test_no_wallet_or_signer_module_exists(project_root: Path) -> None:
    suspicious = [
        str(path.relative_to(project_root))
        for path in (project_root / "src").rglob("*.py")
        if any(token in path.stem.lower() for token in ("wallet", "signer", "keypair", "executor"))
    ]
    assert not suspicious, f"trading-shaped module present: {suspicious}"
