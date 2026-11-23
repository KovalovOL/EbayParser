"""Private, automatically allocated Xvfb display; no TCP listener or desktop service."""
import asyncio
import logging
import os
import tempfile
from contextlib import suppress

from app.parser.errors import BrowserError, ParseError

log = logging.getLogger(__name__)


class VirtualDisplay:
    def __init__(self):
        self.process = None
        self.stderr = None

    async def start(self) -> str:
        read_fd, write_fd = os.pipe()
        reader_pipe = os.fdopen(read_fd, "rb", buffering=0)
        reader_transport = None
        try:
            reader = asyncio.StreamReader(limit=128)
            reader_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), reader_pipe)
            self.stderr = tempfile.TemporaryFile()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    "Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1440x1000x24", "-nolisten", "tcp",
                    "-noreset",
                    pass_fds=(write_fd,), stdout=asyncio.subprocess.DEVNULL, stderr=self.stderr,
                    env={key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG"}},
                )
            except FileNotFoundError:
                raise ParseError("Xvfb is not installed. Use the browser Docker image or set EBAY_BROWSER_HEADLESS=true.") from None
            os.close(write_fd)
            write_fd = None
            try:
                number = (await asyncio.wait_for(reader.readline(), timeout=5)).decode().strip()
            except TimeoutError:
                raise BrowserError("Could not start the browser virtual display.") from None
            if not number.isdigit():
                raise BrowserError("The browser virtual display stopped during startup.")
            # Verify that the allocated socket is accepting connections before
            # launching a browser. -noreset keeps this probe from resetting Xvfb.
            _, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(f"/tmp/.X11-unix/X{number}"), timeout=5)
            writer.close()
            await writer.wait_closed()
            if self.process.returncode is not None:
                raise BrowserError("The browser virtual display exited during startup.")
            return ":" + number
        except (OSError, TimeoutError):
            await self.close()
            raise BrowserError("The browser virtual display is unavailable. It will restart on the next attempt.") from None
        except BaseException:
            await self.close()
            raise
        finally:
            if write_fd is not None:
                os.close(write_fd)
            if reader_transport is not None:
                reader_transport.close()
            else:
                reader_pipe.close()

    async def close(self):
        if self.process is not None and self.process.returncode not in (None, 0) and self.stderr:
            self.stderr.seek(0)
            log.warning("Xvfb exited with code %s: %s", self.process.returncode,
                        self.stderr.read(2000).decode(errors="replace").strip())
        if self.process is not None and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self.process = None
        if self.stderr:
            self.stderr.close()
            self.stderr = None
