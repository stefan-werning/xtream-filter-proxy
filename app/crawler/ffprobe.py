from __future__ import annotations

import asyncio
import json
import logging
import shutil

logger = logging.getLogger("proxy.ffprobe")


class FfprobeFailedError(Exception):
    """Raised when ffprobe could not be run to completion at all (timeout,
    non-zero exit, unreadable output) -- as opposed to running successfully
    and finding no audio streams. Callers must retry on this instead of
    treating it as a confirmed 'no audio info' result, since a transient
    failure (e.g. the provider rejecting the connection because the
    account's only slot is busy) looks identical to genuinely-no-audio
    unless this distinction is kept.
    """


def ffprobe_available(binary: str = "ffprobe") -> bool:
    return shutil.which(binary) is not None


async def run_ffprobe(url: str, binary: str, timeout_seconds: int) -> dict:
    """Runs ffprobe against a stream URL and returns the parsed JSON.

    Raises FfprobeFailedError on timeout, a non-zero exit code, or
    unparseable output -- never returns None/{} to mean "failed", since
    callers need to tell that apart from "ran fine, no audio streams".
    Always kills the process hard on timeout.
    """
    cmd = [
        binary,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-analyzeduration", "3M",
        "-probesize", "5M",
        url,
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("ffprobe timed out after %ss", timeout_seconds)
            raise FfprobeFailedError(f"timed out after {timeout_seconds}s")
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()[:300]
            raise FfprobeFailedError(f"exit code {proc.returncode}: {err_text}")
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("ffprobe failed: %s", e)
        raise FfprobeFailedError(str(e)) from e
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
