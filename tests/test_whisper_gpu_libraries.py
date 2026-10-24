"""Whisper on the GPU needs cuBLAS 12, and a PC without it transcribes on the
CPU instead of failing the job (issue #111, transcription/transcriber.py).

faster-whisper's engine is a CUDA 12 build that loads cublas64_12.dll by name
at its first GPU run; PyTorch's CUDA 13 build only has cuBLAS 13. Without
NVIDIA's nvidia-cublas-cu12 wheel (or a CUDA 12 toolkit), every job on an
NVIDIA PC failed at "Transcribing"."""

import os
import sys
import types
from pathlib import Path

import pytest

from transcription import transcriber

CUBLAS = ("cublasLt64_12.dll", "cublas64_12.dll")


# ---- finding and loading cuBLAS 12 (Windows) -------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="the DLL loading is Windows-only")
def test_cublas_12_is_loaded_from_the_first_folder_that_has_both(tmp_path, monkeypatch):
    half, whole = tmp_path / "half", tmp_path / "whole"
    half.mkdir()
    whole.mkdir()
    (half / "cublas64_12.dll").write_bytes(b"")          # its dependency is missing here
    for name in CUBLAS:
        (whole / name).write_bytes(b"")
    loaded = []
    monkeypatch.setenv("PATH", "C:\\elsewhere")
    monkeypatch.setattr(transcriber.os, "add_dll_directory", lambda p: None, raising=False)
    got = transcriber._cuda12_blas([half, whole], load=loaded.append)
    assert got == whole
    assert loaded == [str(whole / "cublasLt64_12.dll"), str(whole / "cublas64_12.dll")]   # dependency first
    assert os.environ["PATH"].split(os.pathsep)[0] == str(whole)   # found by name from now on


@pytest.mark.skipif(os.name != "nt", reason="the DLL loading is Windows-only")
def test_no_cublas_12_anywhere_is_none(tmp_path):
    assert transcriber._cuda12_blas([tmp_path], load=lambda p: pytest.fail("loaded nothing")) is None


@pytest.mark.skipif(os.name != "nt", reason="the DLL loading is Windows-only")
def test_a_copy_that_will_not_load_is_passed_over(tmp_path, monkeypatch):
    bad, good = tmp_path / "bad", tmp_path / "good"
    for folder in (bad, good):
        folder.mkdir()
        for name in CUBLAS:
            (folder / name).write_bytes(b"")

    def load(path):
        if str(bad) in path:
            raise OSError("not a valid Win32 application")

    monkeypatch.setattr(transcriber.os, "add_dll_directory", lambda p: None, raising=False)
    assert transcriber._cuda12_blas([bad, good], load=load) == good


# ---- choosing the device ---------------------------------------------------------------


@pytest.fixture
def whisper(monkeypatch):
    """A stand-in faster_whisper: records every model made."""
    made = []

    class WhisperModel:
        def __init__(self, name, device="cpu", compute_type="auto"):
            made.append(device)
            self.model = types.SimpleNamespace(device=device)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    return made


def test_without_cublas_12_whisper_uses_the_cpu_and_says_why(whisper, monkeypatch, capsys):
    monkeypatch.setattr(transcriber, "_cuda12_blas", lambda: None)
    model = transcriber._load_model("auto", "auto")
    assert whisper == ["cpu"] and not transcriber._on_gpu(model)
    assert "cuBLAS 12" in capsys.readouterr().out


def test_asking_for_the_gpu_by_name_without_cublas_12_says_what_is_missing(whisper, monkeypatch):
    monkeypatch.setattr(transcriber, "_cuda12_blas", lambda: None)
    with pytest.raises(RuntimeError, match=r"cublas64_12.dll"):
        transcriber._load_model("small", "cuda")
    assert whisper == []


def test_with_cublas_12_whisper_goes_on_the_gpu(whisper, monkeypatch):
    monkeypatch.setattr(transcriber, "_cuda12_blas", lambda: Path("."))
    assert transcriber._on_gpu(transcriber._load_model("auto", "auto")) and whisper == ["cuda"]


# ---- a GPU that fails once it starts ---------------------------------------------------


class Segment:
    def __init__(self, start, text):
        self.start, self.end, self.text, self.words = start, start + 1.0, text, []


class Info:
    duration, language = 2.0, "en"


def _model(device, fail=None):
    def run():
        if fail:
            raise RuntimeError(fail)
        yield Segment(0.0, "hello from the " + device)

    return types.SimpleNamespace(model=types.SimpleNamespace(device=device),
                                 transcribe=lambda *a, **k: (run(), Info()))


def _transcribe(tmp_path, monkeypatch, models, device="auto"):
    asked = []

    def load(size, dev):
        asked.append(dev)
        return models.pop(0)

    monkeypatch.setattr(transcriber, "_load_model", load)
    segments = transcriber.transcribe(tmp_path / "v.mp4", "vid", tmp_path, model_size="auto", device=device)
    return asked, [s.text for s in segments]


def test_a_gpu_that_cannot_load_cublas_mid_run_finishes_on_the_cpu(tmp_path, monkeypatch, capsys):
    """The reported error, raised at the first encode, after the model loaded."""
    models = [_model("cuda", fail="Library cublas64_12.dll is not found or cannot be loaded"), _model("cpu")]
    asked, texts = _transcribe(tmp_path, monkeypatch, models)
    assert asked == ["auto", "cpu"] and texts == ["hello from the cpu"]
    assert "transcribing again on the CPU" in capsys.readouterr().out
    assert (tmp_path / "vid.json").exists()                      # cached like any transcript


def test_a_cpu_failure_or_an_unrelated_one_is_not_retried(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="decoder exploded"):
        _transcribe(tmp_path, monkeypatch, [_model("cuda", fail="decoder exploded")])
    with pytest.raises(RuntimeError, match="cublas"):
        _transcribe(tmp_path, monkeypatch, [_model("cpu", fail="cublas oddity")])


def test_the_gpu_asked_for_by_name_is_not_swapped_for_the_cpu(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="cublas64_12"):
        _transcribe(tmp_path, monkeypatch, [_model("cuda", fail="Library cublas64_12.dll is not found")],
                    device="cuda")
