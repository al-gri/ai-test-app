Build a small polished English-language web calculator using Python standard
library only plus HTML/CSS/JavaScript (no CDN, dependencies, or installation).
Return JSON ONLY: {"files":[{"path":"app/calculator.py","content":"..."}, ...]}.
Return ALL FIVE files: app/calculator.py, app/server.py, app/index.html,
app/style.css, app/app.js. Do not return other files or shell commands.

calculator.py exports calculate(expression: str) -> str. Support decimal numbers,
binary + - * /, parentheses, whitespace, unary +/- and normal precedence. Use
decimal.Decimal with precision 28 and a bounded recursive-descent parser.
Do not use eval, exec, compile, ast evaluation, exponentiation or arbitrary names.
Reject non-string or empty input, inputs >256 characters, invalid syntax,
non-finite numbers and division by zero by raising ValueError. Limit nesting to
32. Return normalized plain decimal text without trailing fractional zeroes;
normalize negative zero to '0'.

server.py is runnable as `python app/server.py --host 127.0.0.1 --port 8080`.
Use ThreadingHTTPServer. GET / serves index.html; GET /app.js and /style.css serve
only those assets with appropriate MIME types. Unknown paths return 404, never
serve arbitrary directories or files. POST /api/calculate accepts JSON
{"expression":"2+2"}, returns 200 {"result":"4"}. Invalid input returns 400
{"error":"a concise message"}, request bodies >4096 bytes return 413. No
tracebacks or filesystem details in responses. No logging of request bodies.
Serve from the script's directory, independent of current working directory.

UI: responsive, attractive, accessible labels, keyboard digits/operators,
Enter/= to calculate, Escape to clear, Backspace to delete, visible error state,
decimal point, parentheses, history limited to last 10 in memory. Render all
user inputs/results with textContent, never innerHTML. Fetch local API only.
No inline scripts or external assets. UI should work at 360px width and on desktop.
Never claim to have executed tests; the trusted controller runs them separately.
