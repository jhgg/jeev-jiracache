# coding=utf-8
from itertools import izip_longest
import json
import re
from jira.resources import Issue
from redis import Redis

DEFAULT_STOP_WORDS = frozenset(['a', 'an', 'of', 'the'])
_sentinel = object()


class JIRARedisIndex(object):
    def __init__(self, jira_client, prefix='jri', cache_timeout=300, redis_client=None, **conn_kwargs):
        self.prefix = prefix
        self.stop_words = DEFAULT_STOP_WORDS
        self.cache_timeout = cache_timeout
        self.jira_client = jira_client

        self.conn_kwargs = conn_kwargs

        if redis_client:
            self.client = redis_client
        else:
            self.client = Redis(**self.conn_kwargs)

        self.boost_key = '%s:b' % self.prefix
        self.data_key = '%s:d' % self.prefix
        self.smalldata_key = '%s:s' % self.prefix
        self.title_key = '%s:t' % self.prefix
        self.search_key = lambda k: '%s:s:%s' % (self.prefix, k)
        self.key_search_key = lambda k: '%s:k:%s' % (self.prefix, k)
        self.cache_key = lambda pk, bk: '%s:c:%s:%s' % (self.prefix, pk, bk)
        self._offset = 27 ** 20

    def score_key(self, k, max_size=20):
        k_len = len(k)
        a = ord('a') - 2
        score = 0

        for i in range(max_size):
            if i < k_len:
                c = (ord(k[i]) - a)
                if c < 2 or c > 27:
                    c = 1
            else:
                c = 1
            score += c * (27 ** (max_size - i))
        return score

    def clean_phrase(self, phrase):
        phrase = re.sub('[^a-z0-9_\-\s]', '', phrase.lower())
        return [w for w in phrase.split() if w not in self.stop_words]

    def create_key(self, phrase):
        return ' '.join(self.clean_phrase(phrase))

    def autocomplete_keys(self, w):
        for i in range(1, len(w)):
            yield w[:i]
        yield w

    def flush(self, everything=False, batch_size=1000):
        if everything:
            return self.client.flushdb()

        # this could be expensive :-(
        keys = self.client.keys('%s:*' % self.prefix)

        # batch keys
        for i in range(0, len(keys), batch_size):
            self.client.delete(*keys[i:i + batch_size])

    def jira_issue_to_smalldata(self, issue):
        assignee = issue.fields.assignee and dict(
            name=issue.fields.assignee.name,
            display_name=issue.fields.assignee.displayName,
            email=issue.fields.assignee.emailAddress,
            avatar=issue.fields.assignee.raw['avatarUrls']['48x48']
        )

        reporter = issue.fields.reporter and dict(
            name=issue.fields.reporter.name,
            display_name=issue.fields.reporter.displayName,
            email=issue.fields.reporter.emailAddress,
            avatar=issue.fields.reporter.raw['avatarUrls']['48x48']
        )

        return dict(
            id=issue.raw['id'],
            key=issue.key,
            link=issue.permalink(),
            summary=issue.fields.summary,
            assignee=assignee,
            status=dict(
                icon=issue.fields.status.iconUrl,
                name=issue.fields.status.name
            ),
            type=dict(
                name=issue.fields.issuetype.name,
                icon=issue.fields.issuetype.iconUrl
            ),
            reporter=reporter
        )

    def index(self, issue):
        key = issue.key
        if issue.fields.assignee:
            title = '%s %s %s' % (issue.fields.assignee.name, issue.fields.status.name, issue.fields.summary)
        else:
            title = '%s %s' % (issue.fields.status.name, issue.fields.summary)

        if self.is_stored(key):
            stored_title = self.client.hget(self.title_key, key)

            if stored_title == title:
                self.store_issue_data(issue, store_title=False)
                return

            else:
                self.remove(key)

        pipe = self.client.pipeline()
        self.store_issue_data(issue, pipe)

        # Index based on issue key first...
        # Todo: Figure out how to score ¬_¬
        for i, partial_key in enumerate(self.autocomplete_keys(key.lower())):
            pipe.zadd(self.key_search_key(partial_key), key, self._offset - i)
            pipe.zadd(self.search_key(partial_key), key, self._offset * self._offset)

        title_score = self.score_key(self.create_key(title))

        for i, word in enumerate(self.clean_phrase(title)):
            word_score = self.score_key(word) + self._offset
            key_score = (word_score * (i + 1)) + title_score
            for partial_key in self.autocomplete_keys(word):
                pipe.zadd(self.search_key(partial_key), key, key_score)

        pipe.execute()

    def is_stored(self, key):
        return self.client.hexists(self.data_key, key)

    def store_issue_data(self, issue, pipeline=None, store_title=True):
        if not pipeline:
            pipeline = self.client

        if store_title:
            pipeline.hset(self.title_key, issue.key, issue.fields.summary)

        pipeline.hset(self.data_key, issue.key, json.dumps(issue.raw))
        pipeline.hset(self.smalldata_key, issue.key, json.dumps(self.jira_issue_to_smalldata(issue)))

    def remove(self, key):
        title = self.client.hget(self.title_key, key) or ''

        for word in self.clean_phrase(title):
            for partial_key in self.autocomplete_keys(word):
                set_key = self.search_key(partial_key)
                if not self.client.zrange(set_key, 1, 2):
                    self.client.delete(set_key)
                else:
                    self.client.zrem(set_key, key)

        for partial_key in self.autocomplete_keys(key):
            set_key = self.key_search_key(partial_key)

            if not self.client.zrange(set_key, 1, 2):
                self.client.delete(set_key)
            else:
                self.client.zrem(set_key, key)

            set_key = self.search_key(partial_key)

            if not self.client.zrange(set_key, 1, 2):
                self.client.delete(set_key)
            else:
                self.client.zrem(set_key, key)

        self.client.hdel(self.data_key, key)
        self.client.hdel(self.smalldata_key, key)
        self.client.hdel(self.title_key, key)
        self.client.hdel(self.boost_key, key)

    def _chunked(self, iterable, n):
        for group in (list(g) for g in izip_longest(*[iter(iterable)] * n,
                                                    fillvalue=_sentinel)):
            if group[-1] is _sentinel:
                del group[group.index(_sentinel):]
            yield group

    def _load_ids(self, id_list, limit, data_key, raw=True):
        ct = 0
        data = []
        if not id_list:
            return data

        if limit is not None:
            chunks = self._chunked(id_list, limit)
        else:
            chunks = id_list,

        a = data.append
        load_issue_object = not raw and data_key == self.data_key

        for chunk in chunks:
            for raw_data in self.client.hmget(data_key, chunk):
                if not raw_data:
                    continue

                raw_data = json.loads(raw_data)

                if load_issue_object:
                    raw_data = self._issue_from_raw(raw_data)

                a(raw_data)
                ct += 1
                if limit and ct == limit:
                    return data

        return data

    def _issue_from_raw(self, raw):
        return Issue(self.jira_client._options, self.jira_client._session, raw)

    def search_by_key(self, key, data_key=None, limit=None, raw=True):
        if not data_key:
            data_key = self.smalldata_key

        new_key = self.key_search_key(key)
        id_list = self.client.zrange(new_key, 0, limit or -1)
        return self._load_ids(id_list, limit, data_key)

    def get_by_key(self, key, data_key=None, raw=True):
        if not data_key:
            data_key = self.smalldata_key

        result = self.client.hget(data_key, key)
        if result:
            result = json.loads(result)
            if not raw and data_key == self.data_key:
                result = self._issue_from_raw(result)

        return result

    def get_cache_key(self, phrases, boosts):
        if boosts:
            boost_key = '|'.join('%s:%s' % (k, v) for k, v in sorted(boosts.items()))
        else:
            boost_key = ''
        phrase_key = '|'.join(phrases)
        return self.cache_key(phrase_key, boost_key)

    def search(self, phrase, data_key=None, limit=None, boosts=None, autoboost=False, raw=True, return_id_list=False):
        cleaned = self.clean_phrase(phrase)

        if not data_key:
            data_key = self.smalldata_key

        if not cleaned:
            return []

        if autoboost:
            boosts = boosts or {}
            stored = self.client.hgetall(self.boost_key)
            for obj_id in stored:
                if obj_id not in boosts:
                    boosts[obj_id] = float(stored[obj_id])

        if len(cleaned) == 1 and not boosts:
            new_key = self.search_key(cleaned[0])
        else:
            new_key = self.get_cache_key(cleaned, boosts)
            if not self.client.exists(new_key):
                self.client.zinterstore(new_key, map(self.search_key, cleaned))
                self.client.expire(new_key, self.cache_timeout)

        if boosts:
            pipe = self.client.pipeline()
            for raw_id, score in self.client.zrange(new_key, 0, -1, withscores=True):
                orig_score = score
                if raw_id and raw_id in boosts:
                    score *= 1 / boosts[raw_id]

                if orig_score != score:
                    pipe.zadd(new_key, raw_id, score)

            pipe.execute()

        id_list = self.client.zrange(new_key, 0, limit or -1)
        if return_id_list:
            return id_list

        return self._load_ids(id_list, limit, data_key, raw)

    def boost(self, key, multiplier=1.1, negative=False):
        current = self.client.hget(self.boost_key, key)
        current_f = float(current or 1.0)
        if negative:
            multiplier = 1 / multiplier
        self.client.hset(self.boost_key, key, current_f * multiplier)