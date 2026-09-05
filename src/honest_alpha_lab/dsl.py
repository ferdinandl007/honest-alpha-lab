"""Constrained symbolic factor DSL; no arbitrary Python execution is permitted."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .contracts import ContractError, canonical_hash

TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*)|(.))")
ALLOWED_FUNCTIONS = {
    "rank",
    "zscore",
    "ts_mean",
    "ts_delta",
    "lag",
    "winsorize",
    "min",
    "max",
}


@dataclass(frozen=True, slots=True)
class Node:
    kind: str
    value: str | float | None = None
    children: tuple[Node, ...] = ()

    @property
    def depth(self) -> int:
        return 1 + max((child.depth for child in self.children), default=0)


class Formula:
    def __init__(self, expression: str, root: Node, max_depth: int = 8) -> None:
        if root.depth > max_depth:
            raise ContractError(f"formula depth exceeds limit {max_depth}")
        self.expression = expression
        self.root = root
        self.max_depth = max_depth

    @property
    def formula_hash(self) -> str:
        # Formatting and commutative operand order are not distinct experiments.
        return canonical_hash({"dsl_version": 2, "ast": _canonical_node(self.root)})

    @property
    def canonical_expression(self) -> str:
        """Parseable canonical spelling for persistent execution deduplication."""
        def render(node):
            if node["kind"] == "constant":
                return format(Decimal(str(node["value"])), "f")
            if node["kind"] == "field":
                return node["value"]
            children = [render(child) for child in node["children"]]
            if node["kind"] == "operator":
                return f"{node['value']} {' '.join(children)}"
            return f"{node['value']}({', '.join(children)})"

        return render(_canonical_node(self.root))

    @property
    def required_fields(self) -> frozenset[str]:
        def collect(node):
            if node.kind == "field":
                return {str(node.value)}
            return set().union(*(collect(child) for child in node.children))

        return frozenset(collect(self.root))

    @classmethod
    def parse(cls, expression: str, max_depth: int = 8) -> Formula:
        if not isinstance(max_depth, int) or not 1 <= max_depth <= 32:
            raise ContractError("max_depth must be between 1 and 32")
        parser = _Parser(expression, max_depth)
        root = parser.parse_expression()
        if parser.peek() is not None:
            raise ContractError(f"unexpected token {parser.peek()!r}")
        return cls(expression, root, max_depth)

    def evaluate(self, context: Mapping[str, Sequence[float]]) -> list[float]:
        """Legacy one-dimensional diagnostic API; use evaluate_panel for mining."""
        return _evaluate(self.root, context)

    def evaluate_panel(self, panel):
        """Evaluate cross-sectional operators by date and temporal operators by asset."""
        from .panel import evaluate_panel

        return evaluate_panel(self, panel)

    def require_scalar(self):
        """Reject operators requiring a cross-section or historical series."""
        def check(node):
            if node.kind == "function" and node.value in {"rank", "zscore", "lag", "ts_mean", "ts_delta"}:
                raise ContractError("feature row formulas require scalar operations; use panel evaluation for rank/history")
            for child in node.children:
                check(child)
        check(self.root)


def _canonical_node(node: Node):
    children = [_canonical_node(child) for child in node.children]
    if node.value in {"+", "*", "min", "max"}:
        children.sort(key=canonical_hash)
    return {"kind": node.kind, "value": node.value, "children": children}


class _Parser:
    def __init__(self, expression: str, max_depth: int) -> None:
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 8192:
            raise ContractError("formula must contain 1 to 8192 characters")
        self.max_depth = max_depth
        self.tokens = []
        for match in TOKEN.finditer(expression.strip()):
            number, identifier, symbol = match.groups()
            self.tokens.append(
                float(number) if number else identifier if identifier else symbol
            )
        if len(self.tokens) > 512:
            raise ContractError("formula exceeds token budget")
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def pop(self):
        token = self.peek()
        if token is None:
            raise ContractError("unexpected end of formula")
        self.index += 1
        return token

    def parse_expression(self, depth: int = 1) -> Node:
        if depth > self.max_depth:
            raise ContractError(f"formula depth exceeds limit {self.max_depth}")
        token = self.pop()
        if isinstance(token, float):
            if not math.isfinite(token):
                raise ContractError("formula constants must be finite")
            return Node("constant", token)
        if token in {"+", "-", "*", "/"}:
            left, right = self.parse_expression(depth + 1), self.parse_expression(depth + 1)
            return Node("operator", token, (left, right))
        if token == "(":
            node = self.parse_expression(depth + 1)
            if self.pop() != ")":
                raise ContractError("expected closing parenthesis")
            return node
        if isinstance(token, str) and self.peek() == "(":
            if token not in ALLOWED_FUNCTIONS:
                raise ContractError(f"function {token!r} is not allowed")
            self.pop()
            children = []
            if self.peek() != ")":
                while True:
                    children.append(self.parse_expression(depth + 1))
                    if self.peek() != ",":
                        break
                    self.pop()
            if self.pop() != ")":
                raise ContractError(
                    f"function {token!r} is missing closing parenthesis"
                )
            if (
                token in {"ts_mean", "ts_delta", "lag", "winsorize"}
                and len(children) != 2
            ):
                raise ContractError(f"{token} requires exactly two arguments")
            if token in {"rank", "zscore"} and len(children) != 1:
                raise ContractError(f"{token} requires exactly one argument")
            if token in {"min", "max"} and len(children) < 2:
                raise ContractError(f"{token} requires at least two arguments")
            if token in {"ts_mean", "ts_delta", "lag", "winsorize"}:
                parameter = children[1]
                if parameter.kind != "constant" or float(parameter.value) <= 0:
                    raise ContractError(f"{token} requires a positive literal parameter")
                if token != "winsorize" and (
                    not float(parameter.value).is_integer() or float(parameter.value) > 2520
                ):
                    raise ContractError("temporal window must be an integer in [1, 2520]")
            return Node("function", token, tuple(children))
        if isinstance(token, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            return Node("field", token)
        raise ContractError(f"invalid token {token!r}")


def _same_length(values: Sequence[Sequence[float]]) -> int:
    if not values or any(len(item) != len(values[0]) for item in values):
        raise ContractError("all formula series must have equal length")
    return len(values[0])


def _evaluate(node: Node, context: Mapping[str, Sequence[float]]) -> list[float]:
    if node.kind == "constant":
        length = len(next(iter(context.values()))) if context else 0
        return [float(node.value)] * length
    if node.kind == "field":
        if node.value not in context:
            raise ContractError(f"unknown field {node.value!r}")
        return [float(value) for value in context[node.value]]
    values = [_evaluate(child, context) for child in node.children]
    length = _same_length(values)
    if node.kind == "operator":
        left, right = values
        if node.value == "/":
            return [a / b if b else math.nan for a, b in zip(left, right)]
        return [
            {"+": a + b, "-": a - b, "*": a * b}[node.value]
            for a, b in zip(left, right)
        ]
    name = str(node.value)
    if name == "min":
        return [min(items) if all(math.isfinite(item) for item in items) else math.nan
                for items in zip(*values)]
    if name == "max":
        return [max(items) if all(math.isfinite(item) for item in items) else math.nan
                for items in zip(*values)]
    if name == "rank":
        order = sorted((i for i in range(length) if math.isfinite(values[0][i])), key=lambda i: values[0][i])
        result = [math.nan] * length
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and values[0][order[end]] == values[0][order[start]]:
                end += 1
            rank = (start + end - 1) / 2 / max(len(order) - 1, 1)
            for index in order[start:end]:
                result[index] = rank
            start = end
        return result
    if name == "zscore":
        mean = sum(values[0]) / max(length, 1)
        variance = sum((value - mean) ** 2 for value in values[0]) / max(length, 1)
        scale = math.sqrt(variance) or 1.0
        return [(value - mean) / scale for value in values[0]]
    if name == "winsorize":
        limit = abs(values[1][0]) if values[1] else 0
        return [max(-limit, min(limit, value))
                if math.isfinite(value) and math.isfinite(limit) else math.nan
                for value in values[0]]
    window = int(values[1][0]) if values[1] else 1
    if window <= 0:
        raise ContractError("time-series window must be positive")
    series = values[0]
    if name == "lag":
        return [0.0] * min(window, length) + list(series[: max(0, length - window)])
    if name == "ts_delta":
        return [
            0.0 if i < window else series[i] - series[i - window] for i in range(length)
        ]
    if name == "ts_mean":
        return [
            sum(series[max(0, i - window + 1) : i + 1])
            / len(series[max(0, i - window + 1) : i + 1])
            for i in range(length)
        ]
    raise ContractError(f"unsupported function {name!r}")
