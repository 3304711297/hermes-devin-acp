from __future__ import annotations

import json
import sys

for raw in sys.stdin:
    req = json.loads(raw)
    method = req.get('method')
    rid = req.get('id')
    if method == 'initialize':
        assert req['params']['clientInfo']['name'] == 'windsurf'
        assert req['params']['clientCapabilities']['terminal'] is True
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'protocolVersion':1,'agentInfo':{'name':'devin','title':'Devin','version':'test'},'agentCapabilities':{}}}), flush=True)
    elif method == 'session/new':
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{
            'sessionId':'test-session',
            'configOptions':[{'id':'model','type':'select','currentValue':'swe','options':[{'value':'swe','name':'SWE'},{'value':'swe-2','name':'SWE-2'}]}, {'id':'mode','type':'select','currentValue':'accept-edits','options':[{'value':'accept-edits','name':'Accept edits'},{'value':'bypass','name':'Bypass'}]}]
        }}), flush=True)
    elif method == 'session/set_config_option':
        assert req['params']['configId'] in {'model','mode'}
        assert (req['params']['configId'] == 'model' and req['params']['value'] in {'swe','swe-2'}) or (req['params']['configId'] == 'mode' and req['params']['value'] in {'accept-edits','bypass'})
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'configOptions':[]}}), flush=True)
    elif method == 'session/prompt':
        # Ask the client to execute a terminal command, exercising the full ACP
        # client-host direction that a real Devin coding turn needs.
        print(json.dumps({'jsonrpc':'2.0','id':90,'method':'terminal/create','params':{
            'sessionId':'test-session','command':sys.executable,'args':['-c','print(\"terminal-ok\")'],'outputByteLimit':100000
        }}), flush=True)
        terminal_id = None
        while True:
            reply = json.loads(sys.stdin.readline())
            if reply.get('id') == 90:
                terminal_id = reply['result']['terminalId']
                break
        print(json.dumps({'jsonrpc':'2.0','id':92,'method':'terminal/wait_for_exit','params':{'terminalId':terminal_id}}), flush=True)
        wait_reply = json.loads(sys.stdin.readline())
        assert wait_reply['result']['exitCode'] == 0
        print(json.dumps({'jsonrpc':'2.0','id':91,'method':'terminal/output','params':{'terminalId':terminal_id}}), flush=True)
        output_reply = json.loads(sys.stdin.readline())
        assert 'terminal-ok' in output_reply['result']['output']
        print(json.dumps({'jsonrpc':'2.0','id':93,'method':'terminal/release','params':{'terminalId':terminal_id}}), flush=True)
        json.loads(sys.stdin.readline())
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'test-session','update':{'sessionUpdate':'agent_thought_chunk','content':{'text':'thinking'}}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'test-session','update':{'sessionUpdate':'agent_message_chunk','content':{'text':'hello from SWE-2'}}}}), flush=True)
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'stopReason':'end_turn'}}), flush=True)
    elif method == 'terminal/create':
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'terminalId':'t1'}}), flush=True)
    elif method == 'terminal/output':
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'output':'ok','truncated':False,'exitStatus':{'exitCode':0}}}), flush=True)
    elif method == 'terminal/wait_for_exit':
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{'exitCode':0,'signal':None}}), flush=True)
    elif method in {'terminal/kill','terminal/release'}:
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':None}), flush=True)
    else:
        print(json.dumps({'jsonrpc':'2.0','id':rid,'result':{}}), flush=True)
