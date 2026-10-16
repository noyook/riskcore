"""Sandboxed boolean expression evaluator for predicate declarations.

Predicates are DATA (predicates.md): deny_when / warn_when are strings like
"veto_active OR r_milli >= lock_threshold". They are evaluated with the ast
module against a fixed namespace — never with eval().

Allowed grammar (anything else is a load-time/ evaluation-time error):
    expr    := or_expr
    or_expr := and_expr (OR and_expr)*
    and_expr:= not_expr (AND not_expr)*
    not_expr:= NOT not_expr | atom
    atom    := NAME | BOOL | INT | ( expr )
    compare := atom ((==|!=|<|<=|>|>=) atom)+      # chained like Python
Only names present in the provided namespace resolve; unknown names raise.
"""
from __future__ import annotations

import ast
import re
from typing import Mapping


class ExpressionError(ValueError):
    """Raised for disallowed syntax or unresolvable names."""


#: The predicate DSL (predicates.md) declares boolean operators in uppercase
#: ("veto_active OR r_milli >= lock_threshold"). Normalize to Python tokens
#: before parsing; word boundaries keep identifiers untouched.
_DSL_KEYWORDS = {"OR": "or", "AND": "and", "NOT": "not"}
_KEYWORD_RE = re.compile(r"\b(OR|AND|NOT)\b")


def _normalize(expression: str) -> str:
    return _KEYWORD_RE.sub(lambda m: _DSL_KEYWORDS[m.group(0)], expression)


_ALLOWED_CMP = {ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE}


def _eval_node(node: ast.AST, ns: Mapping[str, object]) -> object:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ns)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, ns) for v in node.values]
        if isinstance(node.op, ast.And):
            result: object = True
            for v in values:
                result = v if not v else result
                if not v:
                    return v
            return result
        if isinstance(node.op, ast.Or):
            for v in values:
                if v:
                    return v
            return values[-1] if values else False
        raise ExpressionError(f"unsupported boolean operator: {type(node.op).__name__}")
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, ns)
        raise ExpressionError(f"unsupported unary operator: {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ns)
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _ALLOWED_CMP:
                raise ExpressionError(f"unsupported comparison: {type(op).__name__}")
            right = _eval_node(comparator, ns)
            if not _compare(left, op, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise ExpressionError(f"unknown name in expression: '{node.id}'")
        return ns[node.id]
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or isinstance(node.value, int):
            return node.value
        raise ExpressionError(f"unsupported constant: {node.value!r}")
    raise ExpressionError(f"disallowed syntax: {type(node).__name__}")


def _compare(left: object, op: ast.cmpop, right: object) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right  # type: ignore[operator]
    if isinstance(op, ast.LtE):
        return left <= right  # type: ignore[operator]
    if isinstance(op, ast.Gt):
        return left > right  # type: ignore[operator]
    return left >= right  # type: ignore[operator]


def eval_bool(expression: str, namespace: Mapping[str, object]) -> bool:
    """Evaluate a predicate expression to a bool. Raises ExpressionError."""
    try:
        tree = ast.parse(_normalize(expression), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"invalid expression syntax: {exc}") from exc
    result = _eval_node(tree, namespace)
    if not isinstance(result, bool):
        raise ExpressionError(f"expression did not evaluate to bool: {expression!r}")
    return result
