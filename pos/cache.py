"""Rate-limit counters must never be able to take the site down."""
import logging

from django.core.cache.backends.db import DatabaseCache
from django.core.management import call_command
from django.db import DatabaseError

log = logging.getLogger(__name__)
_tried_to_create = set()


class SafeDatabaseCache(DatabaseCache):
    """A database cache that repairs itself.

    The normal DatabaseCache raises an error on every request when its table does not exist
    (you forgot `python manage.py createcachetable`), so login and signup return HTTP 500.
    This version creates the table on first use. If that is impossible (database down, no rights)
    it behaves like an empty cache: the request still succeeds, only the rate limit is skipped.
    """

    def _safely(self, call, fallback):
        try:
            return call()
        except DatabaseError:
            if self._table not in _tried_to_create:
                _tried_to_create.add(self._table)
                try:
                    call_command("createcachetable", self._table, verbosity=0)
                    log.warning("Cache table %r was missing and has been created.", self._table)
                    return call()
                except Exception:
                    log.exception("Cache table %r is missing and could not be created; rate limits are off.", self._table)
            return fallback

    def get_many(self, keys, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).get_many(keys, version), {})

    def set(self, key, value, timeout=None, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).set(key, value, timeout, version), None)

    def add(self, key, value, timeout=None, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).add(key, value, timeout, version), False)

    def touch(self, key, timeout=None, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).touch(key, timeout, version), False)

    def delete(self, key, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).delete(key, version), False)

    def delete_many(self, keys, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).delete_many(keys, version), None)

    def has_key(self, key, version=None):
        return self._safely(lambda: super(SafeDatabaseCache, self).has_key(key, version), False)
