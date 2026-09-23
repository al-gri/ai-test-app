"""Safe decimal expression evaluator for the web calculator.

Public API: calculate(expression: str) -> str

No eval/exec/compile/ast evaluation. Only + - * / and parentheses with
unary signs are supported, evaluated with decimal.Decimal at 28 digits of
precision using a bounded recursive-descent parser.
"""

from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

MAX_INPUT_LENGTH = 256
MAX_DEPTH = 32
PRECISION = 28


class _ParseError(ValueError):
    """Raised for syntactically invalid expressions."""


class _Lexer:
    """Minimal tokenizer: numbers, operators, parentheses."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._len = len(text)

    def _skip_ws(self) -> None:
        while self._pos < self._len and self._text[self._pos] in " \t\r\n\f\v":
            self._pos += 1

    def peek(self):
        """Return the next token without consuming it, or None at end."""
        saved = self._pos
        token = self.next_token()
        self._pos = saved
        return token

    def next_token(self):
        """Return the next token: (kind, value) or None at end of input."""
        self._skip_ws()
        if self._pos >= self._len:
            return None
        ch = self._text[self._pos]
        if ch in "+-*/()":
            self._pos += 1
            return (ch, ch)
        if ch.isdigit() or ch == ".":
            start = self._pos
            seen_dot = False
            seen_digit = False
            while self._pos < self._len:
                c = self._text[self._pos]
                if c.isdigit():
                    seen_digit = True
                    self._pos += 1
                elif c == ".":
                    if seen_dot:
                        raise _ParseError("Invalid number")
                    seen_dot = True
                    self._pos += 1
                else:
                    break
            if not seen_digit:
                raise _ParseError("Invalid number")
            return ("number", self._text[start:self._pos])
        raise _ParseError("Unexpected character: " + repr(ch))


class _Parser:
    """Recursive-descent parser with bounded nesting depth."""

    TOP_LEVEL_DEPTH = 1

    def __init__(self, text: str) -> None:
        self._lexer = _Lexer(text)
        self._depth = self.TOP_LEVEL_DEPTH

    def parse(self):
        value = self._parse_expr()
        if self._lexer.peek() is not None:
            raise _ParseError("Unexpected trailing input")
        return value

    def _parse_expr(self):
        value = self._parse_term()
        while True:
            token = self._lexer.peek()
            if token is None or token[0] not in ("+", "-"):
                return value
            op = self._lexer.next_token()[0]
            rhs = self._parse_term()
            if op == "+":
                value = value + rhs
            else:
                value = value - rhs

    def _parse_term(self):
        value = self._parse_unary()
        while True:
            token = self._lexer.peek()
            if token is None or token[0] not in ("*", "/"):
                return value
            op = self._lexer.next_token()[0]
            rhs = self._parse_unary()
            if op == "*":
                value = value * rhs
            else:
                if rhs == 0:
                    raise _ParseError("Division by zero")
                value = value / rhs

    def _parse_unary(self):
        token = self._lexer.peek()
        if token is not None and token[0] in ("+", "-"):
            op = self._lexer.next_token()[0]
            operand = self._parse_unary()
            if op == "-":
                return -operand
            return operand
        return self._parse_primary()

    def _parse_primary(self):
        token = self._lexer.peek()
        if token is None:
            raise _ParseError("Unexpected end of input")
        kind = token[0]
        if kind == "number":
            self._lexer.next_token()
            try:
                return Decimal(token[1])
            except InvalidOperation as exc:
                raise _ParseError("Invalid number") from exc
        if kind == "(":
            self._lexer.next_token()
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise _ParseError("Expression is too deeply nested")
            value = self._parse_expr()
            closing = self._lexer.next_token()
            if closing is None or closing[0] != ")":
                raise _ParseError("Missing closing parenthesis")
            self._depth -= 1
            return value
        if kind == ")":
            raise _ParseError("Unexpected closing parenthesis")
        raise _ParseError("Unexpected token: " + repr(kind))


def _normalize(value: Decimal) -> str:
    """Render a Decimal as normalized plain text without trailing zeroes."""
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def calculate(expression: str) -> str:
    """Evaluate a decimal arithmetic expression safely.

    Supports binary + - * /, parentheses, whitespace and unary +/-.
    Raises ValueError for invalid input, invalid syntax, non-finite
    numbers and division by zero.
    """
    if not isinstance(expression, str):
        raise ValueError("Expression must be a string")
    if not expression or not expression.strip():
        raise ValueError("Expression is empty")
    if len(expression) > MAX_INPUT_LENGTH:
        raise ValueError("Expression is too long")

    parser = _Parser(expression)
    with localcontext() as ctx:
        ctx.prec = PRECISION
        try:
            value = parser.parse()
        except InvalidOperation as exc:
            raise ValueError("Invalid number") from exc
        except DivisionByZero as exc:
            raise ValueError("Division by zero") from exc

    if not value.is_finite():
        raise ValueError("Result is not a finite number")
    return _normalize(value)
