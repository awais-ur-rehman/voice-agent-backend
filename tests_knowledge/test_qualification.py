"""Qualification gate tests use vectors only as control fixtures, never model evidence."""

import json
import os
import subprocess
import sys

import pytest

from scripts.qualify_knowledge import evaluate


def test_qualification_requires_explicit_opt_in_before_asset_access(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.qualify_knowledge", "--assets", str(tmp_path / "missing")],
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "opt_in_required"
    assert not result.stderr


def test_qualification_rejects_wrong_match_and_false_positive():
    corpus = {
        "documents": [{"id": "a", "text": "a"}, {"id": "b", "text": "b"}],
        "queries": [
            {"id": "positive", "text": "yes", "expected": "b", "direction": "en-ur"},
            {"id": "negative", "text": "no", "expected": None, "direction": "en-en"},
        ],
    }
    result = evaluate(
        corpus, lambda value: ([0.0, 1.0] if value == "b" else [1.0, 0.0]) + [0.0] * 1022
    )
    assert result["passed"] is False
    assert [row["passed"] for row in result["cases"]] == [False, False]


def test_qualification_validates_real_boundary_vectors():
    corpus = {"documents": [{"id": "a", "text": "a"}], "queries": []}
    with pytest.raises(Exception, match="invalid_vectors"):
        evaluate(corpus, lambda value: [0.0] * 1024)
