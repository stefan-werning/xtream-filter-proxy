import pytest

from app.crawler.ffprobe import FfprobeFailedError, run_ffprobe


@pytest.mark.asyncio
async def test_run_ffprobe_raises_on_nonzero_exit(monkeypatch, tmp_path):
    fake_binary = tmp_path / "fake_ffprobe.sh"
    fake_binary.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    fake_binary.chmod(0o755)

    with pytest.raises(FfprobeFailedError, match="exit code 1"):
        await run_ffprobe(url="http://example/x.mkv", binary=str(fake_binary), timeout_seconds=5)


@pytest.mark.asyncio
async def test_run_ffprobe_raises_on_timeout(tmp_path):
    fake_binary = tmp_path / "fake_ffprobe.sh"
    fake_binary.write_text("#!/bin/sh\nsleep 5\n")
    fake_binary.chmod(0o755)

    with pytest.raises(FfprobeFailedError, match="timed out"):
        await run_ffprobe(url="http://example/x.mkv", binary=str(fake_binary), timeout_seconds=0.2)


@pytest.mark.asyncio
async def test_run_ffprobe_raises_on_invalid_json(tmp_path):
    fake_binary = tmp_path / "fake_ffprobe.sh"
    fake_binary.write_text("#!/bin/sh\necho 'not json'\nexit 0\n")
    fake_binary.chmod(0o755)

    with pytest.raises(FfprobeFailedError):
        await run_ffprobe(url="http://example/x.mkv", binary=str(fake_binary), timeout_seconds=5)


@pytest.mark.asyncio
async def test_run_ffprobe_returns_dict_on_success(tmp_path):
    fake_binary = tmp_path / "fake_ffprobe.sh"
    fake_binary.write_text('#!/bin/sh\necho \'{"streams": []}\'\nexit 0\n')
    fake_binary.chmod(0o755)

    result = await run_ffprobe(url="http://example/x.mkv", binary=str(fake_binary), timeout_seconds=5)
    assert result == {"streams": []}
