"""Keep large tool output in the conversation sandbox and expose bounded reads."""

import base64
import hashlib
import json
import shlex
from dataclasses import dataclass, replace
from uuid import uuid4

from pydantic_ai import FunctionToolset
from pydantic_ai.messages import ToolReturn
from pydantic_ai_harness.tool_output_limits import Band, Spill, ToolOutputLimits, Truncate

from dify_agent.layers.shell.layer import DifyShellLayer
from dify_agent.layers.workbench_activity import tool_result_failed


@dataclass
class WorkbenchOverflowStore:
    shell: DifyShellLayer

    async def operation(self, operation: str, handle: str, **values):
        payload = base64.b64encode(json.dumps({"operation": operation, "handle": handle, **values}).encode()).decode()
        result = await self.shell.run_remote_script_complete(
            f"python3 -c {shlex.quote(_PROGRAM)} {shlex.quote(payload)}",
            timeout=30,
            max_output_bytes=128 * 1024,
        )
        if result.exit_code != 0 or not result.output_complete:
            raise OSError("Stored tool output is unavailable")
        data = json.loads(result.output)
        if not isinstance(data, dict) or data.get("error"):
            raise OSError("Stored tool output is unavailable")
        return data

    async def write(self, key: str, data: bytes) -> str:
        # Each spill is independent, including concurrent retries of the same
        # tool. The handle grants access only inside the current conversation.
        handle = hashlib.sha256(f"{key}:{uuid4()}".encode()).hexdigest()
        # Leave room for JSON/base64 and the helper itself under command-line
        # limits, including the Windows interpreter used by local verification.
        for offset in range(0, max(1, len(data)), 12 * 1024):
            await self.operation(
                "write",
                handle,
                offset=offset,
                data=base64.b64encode(data[offset : offset + 12 * 1024]).decode(),
            )
        await self.operation("commit", handle, size=len(data), digest=hashlib.sha256(data).hexdigest())
        return handle

    async def read(self, handle: str) -> bytes:
        # Implements the standard OverflowStore protocol without exceeding the
        # sandbox command output limit. Model reads use read_slice instead.
        chunks, offset = [], 0
        while True:
            result = await self.operation("read", handle, offset=offset)
            chunk = base64.b64decode(result["data"])
            chunks.append(chunk)
            offset += len(chunk)
            if offset >= result["size"]:
                return b"".join(chunks)
            if not chunk:
                raise OSError("Stored tool output ended unexpectedly")

    async def read_slice(self, handle: str, **values) -> str:
        return (await self.operation("slice", handle, **values))["text"]


class WorkbenchToolOutputLimits(ToolOutputLimits):
    def __init__(self, shell: DifyShellLayer, input_budget: int):
        self.workbench_store = WorkbenchOverflowStore(shell)
        self.read_chars = max(256, min(12_000, input_budget))
        super().__init__(
            store=self.workbench_store,
            over_tokens=True,
            bands=[Band(over=max(1, input_budget // 4), action=Spill(then=Truncate(max_chars=self.read_chars - 160)))],
        )

    def get_toolset(self):
        async def read_tool_result(
            handle: str,
            offset: int = 0,
            limit: int = 200,
            from_end: bool = False,
            pattern: str | None = None,
            char_offset: int = 0,
        ) -> str:
            """Read stored output from this conversation without repeating the original operation.

            offset/limit select lines; pattern is a literal filter. For a long
            single line, use the returned next_char_offset with the same line
            selection. The full payload also remains in .cache/workbench-tool-results.
            """
            if offset < 0 or limit < 1 or char_offset < 0:
                return '{"error":"offset and char_offset must be non-negative; limit must be positive"}'
            try:
                return await self.workbench_store.read_slice(
                    handle,
                    offset=offset,
                    limit=min(limit, 1_000),
                    from_end=from_end,
                    pattern=pattern,
                    char_offset=char_offset,
                    max_chars=self.read_chars,
                )
            except OSError:
                return '{"error":"Stored result unavailable. Verify the handle and existing state before repeating any external write."}'

        return FunctionToolset([read_tool_result])

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        value = result.return_value if isinstance(result, ToolReturn) else result
        reduced = await super().after_tool_execute(ctx, call=call, tool_def=tool_def, args=args, result=result)
        # Reduction must not turn a large business failure into an apparent
        # success and reset the five-failure budget.
        if reduced is not result and tool_result_failed(call.tool_name, value):
            if isinstance(reduced, ToolReturn):
                return replace(reduced, metadata={**(reduced.metadata or {}), "is_error": True})
            return ToolReturn(return_value=reduced, metadata={"is_error": True})
        return reduced


_PROGRAM = r"""
import base64, hashlib, json, os, re, sys
from pathlib import Path

def line_parts(target):
    # TextIO handles UTF-8 boundaries and universal CR/LF newlines. Preserve the
    # remaining str.splitlines separators without buffering an entire long line.
    unfinished = False
    with target.open('r', encoding='utf-8', errors='replace') as stream:
        while chunk := stream.read(64 * 1024):
            start = 0
            for boundary in re.finditer(r'[\n\v\f\x1c-\x1e\x85\u2028\u2029]', chunk):
                yield chunk[start:boundary.start()], True
                start = boundary.end()
                unfinished = False
            if start < len(chunk):
                yield chunk[start:], False
                unfinished = True
        if unfinished:
            yield '', True

def scan(target, args, start=None, end=None):
    count, line_length, total, selected = 0, 0, 0, 0
    pattern = args['pattern']
    found, tail = not pattern, ''
    output, candidate = [], []
    first, last = args['char_offset'], args['char_offset'] + args['max_chars']
    for part, finished in line_parts(target):
        if not found:
            search = tail + part
            found = pattern in search
            tail = search[-(len(pattern) - 1):] if len(pattern) > 1 else ''
        wanted = start is not None and start <= count < end
        if wanted:
            position = total + bool(selected) + line_length
            left, right = max(0, first - position), min(len(part), last - position)
            if left < right:
                candidate.append(part[left:right])
        line_length += len(part)
        if finished:
            if found:
                if wanted:
                    if selected and first <= total < last:
                        output.append('\n')
                    output.extend(candidate)
                    total += bool(selected) + line_length
                    selected += 1
                count += 1
            if end is not None and count >= end:
                break
            found, tail, line_length, candidate = not pattern, '', 0, []
    return count, ''.join(output), total

try:
    args = json.loads(base64.b64decode(sys.argv[1]))
    handle = args['handle']
    if not isinstance(handle, str) or not re.fullmatch(r'[0-9a-f]{64}', handle):
        raise ValueError('Invalid tool result handle')
    workspace = Path.cwd().resolve()
    directory = workspace / '.cache' / 'workbench-tool-results'
    if not directory.resolve().is_relative_to(workspace):
        raise ValueError('Tool result storage escapes the conversation')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / handle
    temporary = directory / (handle + '.partial')
    if target.is_symlink() or temporary.is_symlink():
        raise ValueError('Tool result storage must not be a symlink')
    operation = args['operation']
    result = {}
    if operation == 'write':
        offset = args['offset']
        with temporary.open('wb' if offset == 0 else 'r+b') as stream:
            stream.seek(offset)
            stream.write(base64.b64decode(args['data']))
    elif operation == 'commit':
        digest, size = hashlib.sha256(), 0
        with temporary.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        if size != args['size'] or digest.hexdigest() != args['digest']:
            raise ValueError('Incomplete stored output')
        os.replace(temporary, target)
    elif operation == 'read':
        with target.open('rb') as stream:
            stream.seek(args['offset'])
            result = {'data': base64.b64encode(stream.read(32 * 1024)).decode(), 'size': target.stat().st_size}
    elif operation == 'slice':
        matching, _, _ = scan(target, args)
        offset, limit = args['offset'], args['limit']
        end = max(0, matching - offset) if args['from_end'] else min(matching, offset + limit)
        start = max(0, end - limit) if args['from_end'] else offset
        _, text, length = scan(target, args, start, end) if start < end else (0, '', 0)
        first = args['char_offset']
        last = min(length, first + args['max_chars'])
        result = {'text': json.dumps({
            'handle': handle, 'matching_lines': matching, 'line_offset': start,
            'next_char_offset': last if last < length else None,
            'text': text,
        }, ensure_ascii=False)}
    else:
        raise ValueError('Invalid stored output operation')
    print(json.dumps(result, ensure_ascii=False))
except Exception:
    print(json.dumps({'error': 'Stored tool output is unavailable'}))
    sys.exit(1)
"""
