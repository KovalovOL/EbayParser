"""Private, automatically allocated Xvfb display; no TCP listener or desktop service."""
import asyncio
import os
from contextlib import suppress

from app.parser.errors import ParseError


class VirtualDisplay:
    def __init__(self):
        self.process = None

    async def start(self) -> str:
        read_fd, write_fd = os.pipe()
        try:
            try:
                self.process = await asyncio.create_subprocess_exec(
                    "Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1440x1000x24", "-nolisten", "tcp",
                    pass_fds=(write_fd,), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    env={key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG"}},
                )
            except FileNotFoundError:
                raise ParseError("Не встановлено Xvfb. Використайте браузерний Docker-образ або EBAY_BROWSER_HEADLESS=true.") from None
            os.close(write_fd)
            write_fd = None
            try:
                number = (await asyncio.wait_for(asyncio.to_thread(os.read, read_fd, 32), timeout=5)).decode().strip()
            except TimeoutError:
                raise ParseError("Не вдалося запустити віртуальний екран Chromium") from None
            if not number.isdigit():
                raise ParseError("Віртуальний екран Chromium завершився під час запуску")
            return ":" + number
        except BaseException:
            await self.close()
            raise
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)

    async def close(self):
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
