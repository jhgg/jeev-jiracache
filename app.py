from geventwebsocket.handler import WebSocketHandler
from gevent.pywsgi import WSGIServer
import json
from flask import Flask, jsonify, request, abort, render_template, Response
import gevent
from redis import Redis
from index import JIRARedisIndex
from jira.client import JIRA
import module

app = Flask(__name__)

@module.loaded
def init():
    module.g.client = JIRA(
        options={
            'server': module.opts['server']
        },
        oauth={
            'access_token': module.opts['oauth_access_token'],
            'access_token_secret': module.opts['oauth_access_token_secret'],
            'consumer_key': module.opts['oauth_consumer_key'],
            'key_cert': module.opts['oauth_key_cert'],
            'resilient': True,
        }
    )
    module.g.redis = Redis()
    module.g.index = JIRARedisIndex(module.g.client, redis_client=module.g.redis)
    module.g.http_server = WSGIServer((module.opts.get('listen_host', '0.0.0.0'),
                                       int(module.opts.get('listen_port', '8085'))), app,
                                      handler_class=WebSocketHandler)
    module.g.http_server.start()
    module.g.connections = Connection


@module.unload
def deinit():
    module.g.redis.connection_pool.disconnect()
    module.g.http_server.stop()
    module.g.connections.stop_all()


@app.route('/search/key/<key>')
def search_key(key):
    data_key = 'full' in request.args and module.g.index.data_key
    try:
        limit = max(1, min(50, int(request.args.get('limit', 50))))

    except:
        limit = 50

    return jsonify(results=module.g.index.search_by_key(key.lower(), data_key, limit=limit))


@app.route('/search/summary')
def search():
    query = request.args.get('q', '')
    data_key = 'full' in request.args and module.g.index.data_key
    response = jsonify(results=module.g.index.search(query, limit=50, data_key=data_key))
    return response


@app.route('/issue/<key>')
def get_by(key):
    data_key = 'full' in request.args and module.g.index.data_key
    issue = module.g.index.get_by_key(key.upper(), data_key=data_key)

    if not issue:
        abort(404)

    return jsonify(**issue)


class Connection(object):
    clients = set()

    def __init__(self, ws):
        self.ws = ws
        self.sent_ids = set()
        self.last_requested_id = None
        self.last_query = None
        self.clients.add(self)

    def stop(self):
        self.clients.discard(self)

    @classmethod
    def stop_all(cls):
        cls.clients.clear()

    def send(self, **kwargs):
        self.ws.send(json.dumps(kwargs))

    def send_raw(self, raw):
        self.ws.send(raw)

    def did_send_key(self, key):
        return key in self.sent_ids

    def is_last_requested_key(self, key):
        return key == self.last_requested_id

    @classmethod
    def publish_updated_issue(cls, raw, small):
        key = raw['key']
        payload = None

        for client in cls.clients:
            if client.did_send_key(key):
                if payload is None:
                    payload = json.dumps(dict(
                        c='update',
                        i=small
                    ))

                client.send_raw(payload)

            if client.is_last_requested_key(key):
                client.send(
                    c='updateraw',
                    i=raw
                )

    def query(self, query, full, seq=None):
        data_key = full and module.g.index.data_key or module.g.index.smalldata_key
        ids = module.g.index.search(query, limit=50, data_key=data_key, return_id_list=True, autoboost=True)
        r = [id for id in ids if id not in self.sent_ids]
        datas = module.g.index._load_ids(r, None, data_key)
        data_dict = {v['key']: v for v in datas}

        r = []

        for id in ids:
            if id in self.sent_ids:
                r.append(id)

            elif id in data_dict:
                r.append(data_dict[id])
                self.sent_ids.add(id)

        self.last_query = query
        if seq is not None:
            self.send(
                r=r,
                s=seq
            )
        else:
            return r

    @classmethod
    def trigger_update(cls):
        for client in cls.clients:
            if not client.last_query:
                continue

            r = client.query(client.last_query, False)

            client.send(
                i=r,
                q=client.last_query,
                c='updatesearch'
            )


@app.route('/ws')
def ws():
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return

    con = Connection(ws)

    try:

        while True:
            message = ws.receive()
            if not message:
                break

            data = json.loads(message)

            cmd = data.get('c')
            seq = data.get('s')

            if cmd == 'query':
                query = data.get('q')
                full = data.get('full')
                con.query(query, full, seq)

            elif cmd == 'get':
                key = data.get('key')
                full = data.get('full')
                data_key = full and module.g.index.data_key
                issue = module.g.index.get_by_key(key.upper(), data_key=data_key)
                con.last_requested_id = key.upper()

                ws.send(json.dumps(dict(
                    r=issue,
                    s=seq
                )))

    finally:
        con.stop()

    return Response("")


@app.route("/")
def idx():
    return render_template("index.html")


@app.route("/webhook", methods=["POST"])
def webhook():
    data = json.loads(request.data)
    key = data['issue']['key']
    gevent.spawn(update_issue, key)
    return Response("")


def update_issue(key):
    issue = module.g.client.issue(key, expand="renderedFields")
    module.g.index.index(issue)
    module.g.index.boost(issue.key)
    module.g.connections.publish_updated_issue(
        raw=issue.raw,
        small=module.g.index.jira_issue_to_smalldata(issue)
    )
    module.g.connections.trigger_update()
