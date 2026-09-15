"""Normalize complete built-in shell arguments independently of activity display."""

import json

from pydantic_ai.capabilities import AbstractCapability


class ShellArgumentCompatibilityCapability(AbstractCapability[None]):
    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        if call.tool_name not in {"shell_run", "file_create", "file_edit"}:
            return args
        # Decode only a complete wrapper; never repair or execute a truncated script.
        try:
            value = json.loads(args) if isinstance(args, str) else args
            if isinstance(value, dict) and set(value) == {"arguments"}:
                nested = value["arguments"]
                decoded = json.loads(nested) if isinstance(nested, str) else nested
                if isinstance(decoded, dict):
                    return decoded
        except (TypeError, ValueError):
            pass
        return args
