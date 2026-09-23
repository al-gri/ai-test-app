"""Owner-owned acceptance harness, executed ONLY inside the restricted sandbox.

This file is outside the agent's write scope. Assertions inspect behavior of the
committed candidate. They are not evidence of universal application security.
"""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CHILD = '''import json,sys
sys.path.insert(0,'/workspace/app')
from calculator import calculate
try: result={'result':calculate(json.loads(sys.stdin.read()))}
except ValueError: result={'error':'ValueError'}
print(json.dumps(result))
'''

def calculate(expression):
    # A candidate cannot skip unittest via sys.exit/os._exit at import: only
    # its child exits, and an empty/malformed response fails the parent assertion.
    process=subprocess.run([sys.executable,'-c',CHILD],input=json.dumps(expression),
        text=True,capture_output=True,timeout=3)
    if process.returncode != 0: raise AssertionError('Calculator child failed: '+process.stderr[-1500:])
    try: value=json.loads(process.stdout)
    except (ValueError,TypeError): raise AssertionError('Invalid child response') from None
    if value=={'error':'ValueError'}: raise ValueError('Rejected input')
    if set(value)!={'result'} or not isinstance(value['result'],str):
        raise AssertionError('Invalid calculator result')
    return value['result']

class Arithmetic(unittest.TestCase):
    def test_arithmetic(self):
        for expression, expected in [('2+2','4'),('0.1+0.2','0.3'),('2+3*4','14'),
            ('(2+3)*4','20'),('-5+2','-3'),('1--2','3'),('6/4','1.5'),
            (' - ( 2 + 3 ) ','-5'),('2.500+0.5','3'),('-0','0'),('.5*2','1')]:
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), expected)
    def test_rejections(self):
        for expression in ['', ' ', '1/0', 'foo', '2**8', '1;2', '(', '1..2',
            'NaN', '__import__("os")', '('*40+'1'+')'*40, '1'*257, None, 12]:
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError): calculate(expression)

class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen([sys.executable, '/workspace/app/server.py',
            '--host','127.0.0.1','--port','18080'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with socket.create_connection(('127.0.0.1',18080), timeout=.1): return
            except OSError: time.sleep(.05)
        cls.server.kill(); cls.server.wait()
        raise AssertionError('HTTP server did not start')
    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try: cls.server.wait(timeout=3)
        except subprocess.TimeoutExpired: cls.server.kill(); cls.server.wait()
    def test_page_and_assets(self):
        for path, mime in [('/', 'text/html'),('/style.css','text/css'),('/app.js','javascript')]:
            with urlopen('http://127.0.0.1:18080'+path, timeout=2) as r:
                self.assertEqual(r.status,200)
                self.assertIn(mime,r.headers['Content-Type'])
                self.assertGreater(len(r.read()),50)
    def test_api(self):
        request = Request('http://127.0.0.1:18080/api/calculate',
            data=b'{"expression":"0.1+0.2"}',headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=2) as r:
            self.assertEqual(json.load(r), {'result':'0.3'})
    def test_bad_requests(self):
        for data, expected in [(b'{"expression":"1/0"}',400),(b'not-json',400),(b'x'*4097,413)]:
            with self.assertRaises(HTTPError) as error:
                urlopen(Request('http://127.0.0.1:18080/api/calculate', data=data,
                                headers={'Content-Type':'application/json'}),timeout=2)
            self.assertEqual(error.exception.code,expected)
    def test_no_file_listing(self):
        for path in ['/calculator.py','/../etc/passwd','/.git/config']:
            with self.assertRaises(HTTPError) as error:
                urlopen('http://127.0.0.1:18080'+path,timeout=2)
            self.assertEqual(error.exception.code,404)

unittest.main(argv=['trusted-harness'], verbosity=2)
