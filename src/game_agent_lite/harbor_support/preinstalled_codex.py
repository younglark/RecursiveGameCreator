"""Loaded by Harbor's own Python interpreter, not imported by the host package."""
import shlex
from harbor.agents.installed.codex import Codex


class PreinstalledCodex(Codex):
    async def install(self, environment):
        if environment.default_user != "runner":
            raise RuntimeError("Harbor task must use runner for private Codex auth ownership")
        result = await self.exec_as_agent(environment, command="codex --version")
        if result.return_code != 0 or (result.stdout or "").strip() != f"codex-cli {self._version}":
            raise RuntimeError("Preinstalled Codex version differs from configured version")

    def build_cli_flags(self):
        return (super().build_cli_flags() + " --output-schema "
                + shlex.quote("/workspace/io/schema.json") + " --output-last-message "
                + shlex.quote("/workspace/io/result.json"))
