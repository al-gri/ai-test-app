"""Loopback-only authenticated FastAPI owner UI, with no worker-facing APIs."""
from __future__ import annotations
import hmac
import json
import secrets
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from ..engine import Team
from ..evolution import attest
from ..llmops.human_requests import HumanRequests
from ..models import PolicyError
from . import queries
from .evidence import EvidenceStore


class Sessions:
    """Single-process UI sessions. Restart logs everybody out; keys are not cookies."""
    def __init__(self, token, clock=time.monotonic):
        if not isinstance(token,str) or len(token)<43:
            raise PolicyError('Use at least 256 bits of random login-token entropy')
        self.token,self.clock=token,clock
        self.lock=threading.RLock();self.items={};self.failures=deque()

    def login(self, token):
        with self.lock:
            now=self.clock()
            self.items={k:v for k,v in self.items.items() if v['expires']>now}
            while self.failures and self.failures[0]<now-60:self.failures.popleft()
            if len(self.failures)>=10:raise PolicyError('Login rate limited; wait one minute')
            if not isinstance(token,str) or not hmac.compare_digest(token,self.token):
                self.failures.append(now);raise PolicyError('Invalid login')
            if len(self.items)>=32:raise PolicyError('Session capacity reached')
            sid=secrets.token_urlsafe(32)
            self.items[sid]={'csrf':secrets.token_urlsafe(32),'expires':now+1800,'views':{}}
            return sid,self.items[sid]['csrf']

    def get(self,sid):
        with self.lock:
            value=self.items.get(sid)
            if not value or value['expires']<=self.clock():
                self.items.pop(sid,None);raise PermissionError('Session expired')
            return value

    def view(self,sid,evidence):
        with self.lock:
            item=self.get(sid);now=self.clock()
            item['views']={k:v for k,v in item['views'].items() if v['expires']>now}
            if len(item['views'])>=256:raise PolicyError('Too many open reviews')
            nonce=secrets.token_urlsafe(24)
            item['views'][nonce]={'expires':now+600, 'evidence':evidence}
            return nonce


def create_app(database, *, login_token, signing_key, actor='local-owner', port=8765, cipher=None):
    if not isinstance(signing_key,bytes) or len(signing_key)<32:
        raise PolicyError('External human signing key must contain at least 32 bytes')
    if type(port) is not int or not 1024<=port<=65535:raise PolicyError('Invalid UI port')
    team=Team(database)
    team.snapshot()  # Refuse a missing/incompatible operational DB at startup.
    evidence=EvidenceStore(team,cipher)
    human=HumanRequests(team,{actor:{'key':signing_key,'permissions':['human_decision']}})
    sessions=Sessions(login_token)
    origin=f'http://127.0.0.1:{port}'
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    app.state.evidence=evidence;app.state.sessions=sessions
    static=Path(__file__).with_name('static')

    @app.middleware('http')
    async def boundary(request,call_next):
        # Do not trust forwarded headers. The supported launch binds loopback and
        # disables proxy processing; exact Host also closes DNS-rebinding paths.
        if request.headers.get('host')!=f'127.0.0.1:{port}':
            response=JSONResponse({'error':'Unexpected host'},400)
        elif request.headers.get('origin') not in (None,origin) or request.headers.get('sec-fetch-site')=='cross-site':
            response=JSONResponse({'error':'Unexpected origin'},403)
        elif request.method not in ('GET','HEAD') and (
                request.headers.get('origin')!=origin or request.headers.get('x-ui-request')!='1'):
            response=JSONResponse({'error':'Same-origin UI request required'},403)
        else:
            try:
                if request.url.path.startswith('/api/') and request.url.path!='/api/login':
                    sid=request.cookies.get('ai_session','')
                    session=sessions.get(sid)
                    if request.method!='GET' and not hmac.compare_digest(
                            request.headers.get('x-csrf-token',''),session['csrf']):
                        raise PermissionError('CSRF validation failed')
                    request.state.sid=sid
                response=await call_next(request)
            except PermissionError:
                response=JSONResponse({'error':'Authentication required'},401)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.exception_handler(PolicyError)
    async def policy_error(request,exc):
        # Do not expose exception text from Git, SQL, crypto or provider adapters.
        return JSONResponse({'error':'Operation rejected. Refresh and check the trusted request/evidence.'},409)

    @app.exception_handler(Exception)
    async def internal_error(request,exc):
        return JSONResponse({'error':'Control plane operation failed; no automatic retry was issued'},503)

    async def body(request):
        if request.headers.get('content-type','').split(';')[0]!='application/json':
            raise PolicyError('JSON required')
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>8192:raise PolicyError('Request too large')
        from ..protocols.jsonrpc import decode, ProtocolError
        try:return decode(bytes(raw),limit=8192)
        except ProtocolError:raise PolicyError('Malformed UI request') from None

    @app.get('/')
    def index():return FileResponse(static/'index.html')

    @app.get('/static/{name}')
    def asset(name:str):
        if name not in {'app.js','style.css'}:return JSONResponse({'error':'Not found'},404)
        return FileResponse(static/name)

    @app.post('/api/login')
    async def login(request:Request):
        data=await body(request)
        if set(data)!={'token'}:raise PolicyError('Invalid login fields')
        sid,csrf=sessions.login(data['token'])
        response=JSONResponse({'csrf':csrf})
        # Secure cannot be used on the supported HTTP loopback endpoint. Never
        # expose this app on a LAN; terminate authenticated HTTPS in a separate design.
        response.set_cookie('ai_session',sid,httponly=True,samesite='strict',max_age=1800,path='/')
        return response

    @app.get('/api/session')
    def session(request:Request):return {'csrf':sessions.get(request.state.sid)['csrf']}

    @app.post('/api/logout')
    def logout(request:Request):
        with sessions.lock:sessions.items.pop(request.state.sid,None)
        response=JSONResponse({'logged_out':True});response.delete_cookie('ai_session');return response

    @app.get('/api/dashboard')
    def graph():return queries.dashboard(database)

    @app.get('/api/ledger')
    def billing(day:str|None=None):return queries.ledger(database,day)

    @app.get('/api/inbox')
    def inbox():return queries.inbox(database)

    @app.get('/api/review/{request_id}')
    def review(request_id:str,request:Request):
        value=evidence.display(request_id)
        nonce=sessions.view(request.state.sid,value)
        return {'view_id':nonce,'evidence':value}

    @app.post('/api/decision')
    async def decide(request:Request):
        data=await body(request)
        if set(data)!={'view_id','decision','comment'}:raise PolicyError('Unexpected decision fields')
        if not isinstance(data['view_id'],str):raise PolicyError('Invalid view')
        with sessions.lock:
            view=sessions.get(request.state.sid)['views'].get(data['view_id'])
            if not view or view['expires']<=sessions.clock():raise PolicyError('Review expired')
            value=view['evidence']
            if data['decision']=='approve' and not value['complete']:
                raise PolicyError('Incomplete evidence cannot be approved')
            answer={k:value[k] for k in ('request_id','binding','subject_digest','display_digest')}
            answer.update(decision=data['decision'],comment=data['comment'])
        # HMAC is deterministic: identical retry gets the original committed
        # response, whereas a conflicting second decision is rejected by Team.
        envelope=attest(answer,actor,signing_key)
        from starlette.concurrency import run_in_threadpool
        from ..observability.otel import operation
        from ..models import digest
        with operation('human.decision',uuid.uuid4().hex,
                {'request_id':value['request_id'],'operation':data['decision']}):
            result=await run_in_threadpool(human.respond,envelope)
        return {'request_id':result['id'],'status':result['status'],
                'response_digest':result['response_digest'],'signed_response':envelope}

    return app
