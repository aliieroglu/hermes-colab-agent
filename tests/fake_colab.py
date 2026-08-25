"""A local stand-in for the browser-side Colab notebook tool surface.

It really executes the helper code this server generates, against a temp
directory, so the tests exercise the actual base64 chunking, checksum
verification, and stage runner rather than a mock of them.
"""

import contextlib
import io
import itertools
import json


class FakeColab:
    """A minimal stand-in for the browser-side notebook tool surface."""

    def __init__(self, *, stdout_chunking: int | None = None):
        self.cells: dict[str, str] = {}
        self.order: list[str] = []
        self.namespace: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1)
        self._stdout_chunking = stdout_chunking

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        if name == "add_code_cell":
            # Colab's browser side requires cellIndex and rejects negatives.
            # Live testing on 2026-08-25 showed every scratch-cell caller
            # failing on this, so the fake enforces the real schema.
            index = arguments.get("cellIndex")
            if not isinstance(index, int):
                raise AssertionError(
                    "add_code_cell called without cellIndex; the browser rejects "
                    'this with "cellIndex Required"'
                )
            if index < 0:
                raise AssertionError(
                    f"add_code_cell called with cellIndex={index}; the browser "
                    "requires a value >= 0"
                )
            cell_id = f"cell-{next(self._ids)}"
            self.cells[cell_id] = arguments.get("code", "")
            self.order.insert(min(index, len(self.order)), cell_id)
            return json.dumps({"newCellId": cell_id})
        if name == "get_cells":
            return json.dumps(
                {
                    "cells": [
                        # The live payload carries no execution metadata at all.
                        {"cell_type": "code", "id": cid, "source": [self.cells[cid]]}
                        for cid in self.order
                    ]
                }
            )
        if name == "update_cell":
            self.cells[arguments["cellId"]] = arguments["content"]
            return "{}"
        if name == "delete_cell":
            self.cells.pop(arguments["cellId"], None)
            if arguments["cellId"] in self.order:
                self.order.remove(arguments["cellId"])
            return "{}"
        if name == "run_code_cell":
            return self._run(self.cells[arguments["cellId"]])
        raise AssertionError(f"unexpected tool call: {name}")

    def _run(self, code: str) -> str:
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                exec(code, self.namespace)
        except Exception as exc:
            return json.dumps(
                {
                    "outputs": [
                        {
                            "output_type": "error",
                            "ename": type(exc).__name__,
                            "evalue": str(exc),
                            "traceback": [f"{type(exc).__name__}: {exc}"],
                        }
                    ]
                }
            )
        text = buffer.getvalue()
        if self._stdout_chunking:
            parts = [
                text[i : i + self._stdout_chunking]
                for i in range(0, len(text), self._stdout_chunking)
            ]
        else:
            parts = [text]
        return json.dumps(
            {"outputs": [{"output_type": "stream", "name": "stdout", "text": parts}]}
        )
