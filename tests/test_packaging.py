"""What the frozen build must and must not contain.

These read clips-studio.spec as text rather than building anything: a real
build is two and a half hours, so the mistakes worth catching here are the
ones that only surface in an installed copy, hours later, on someone else's
machine.

Both guards below come from bugs that shipped. A development machine has every
Python package lying around, so excluding one from the bundle changes nothing
locally and breaks the release.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "clips-studio.spec"
SDK = ROOT / "sdk" / "python"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))


def _excludes() -> list[str]:
    text = SPEC.read_text(encoding="utf-8")
    block = re.search(r"^excludes = \[(.*?)^\]", text, re.S | re.M)
    assert block, "clips-studio.spec no longer has an excludes list"
    return re.findall(r'"([^"]+)"', block.group(1))


def test_voice_turns_needs_nothing_the_bundle_leaves_out():
    """scipy.cluster is not in the frozen build and sklearn is excluded from
    it; torchaudio and librosa were never there. Any of them imports fine on
    a development machine and fails in an installed copy."""
    source = (SPEC.parent / "analysis" / "voice_turns.py").read_text(encoding="utf-8")
    for package in ("scipy", "sklearn", "torchaudio", "librosa", "torch"):
        assert not re.search(rf"^\s*(?:import|from)\s+{package}\b", source, re.M), (
            f"analysis/voice_turns.py imports {package}, which an installed copy may not have"
        )


def test_script_mode_is_decided_before_the_engine_loads():
    """main.py hands a pipeline's command line to the script host before its
    heavy imports, so a pipeline starts quickly and loads none of the engine."""
    main = (SPEC.parent / "main.py").read_text(encoding="utf-8")
    assert main.index("CLIPSKITTY_SCRIPT_HOST") < main.index("\nimport yaml")


def _stdlib_skip() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    skip = re.search(r"^STDLIB_SKIP = \{(.*?)^\}", text, re.S | re.M)
    assert skip, "clips-studio.spec no longer has STDLIB_SKIP"
    return set(re.findall(r'"([^"]+)"', skip.group(1)))


def _build_installer():
    spec = importlib.util.spec_from_file_location("build_installer_under_test", ROOT / "scripts" / "build_installer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_tests_the_sdk_on_clips_kittys_python():
    """CI's SDK (Windows) job runs the SDK's tests on the app's own Python."""
    yaml = pytest.importorskip("yaml")
    from clipskitty_sdk.host import APP_PYTHON

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = ci["jobs"]["sdk-windows"]
    versions = [step["with"]["python-version"] for step in job["steps"]
                if str(step.get("uses", "")).startswith("actions/setup-python")]
    assert versions == [".".join(map(str, APP_PYTHON))]


