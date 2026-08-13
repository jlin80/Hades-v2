"""Structural guarantees, enforced by AST scan rather than by discipline.

Section 20 of the spec says the code must be *incapable* of executing real
operations. A README promise is not a capability bound; a test that reads the
source and fails the build is.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Import roots that constitute a signing or transaction-submission capability.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "solana",
        "solders",
        "anchorpy",
        "nacl",
        "ecdsa",
        "eth_account",
        "bip_utils",
        "mnemonic",
    }
)

# Identifiers that only appear in code that handles key material or sends
# transactions. Substring match, lowercased.
FORBIDDEN_IDENTIFIERS = (
    "private_key",
    "secret_key",
    "keypair",
    "sign_transaction",
    "send_transaction",
    "wallet_signer",
    "seed_phrase",
)


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_source_tree_is_not_empty() -> None:
    """Guards the two tests below: an empty scan would pass vacuously."""
    assert len(_source_files()) >= 8


def test_no_signing_or_rpc_submission_library_is_imported() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [
                f"{path.relative_to(SRC)}:{node.lineno} imports {root}"
                for root in roots
                if root in FORBIDDEN_IMPORT_ROOTS
            ]
    assert not offenders, "signing capability reached the source tree: " + "; ".join(offenders)


def test_no_key_material_identifiers_exist() -> None:
    offenders: list[str] = []
    for path in _source_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            lowered = line.lower()
            offenders += [
                f"{path.relative_to(SRC)}:{lineno} mentions {needle}"
                for needle in FORBIDDEN_IDENTIFIERS
                if needle in lowered
            ]
    assert not offenders, "key-material identifier found: " + "; ".join(offenders)
