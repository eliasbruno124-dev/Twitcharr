"""Twitcharr-owned, per-worker repair for the installed output manager lifecycle.

Install at plugin initialization, before this worker creates managers.  Only the
named Twitcharr output profile is affected; upstream files are not modified.
The Redis owner remains the native worker ID.  A companion random generation
token fences metadata changes, including release, even for the same worker.
Native ensure/stop registry transactions share the manager's pair lock, so a
late caller cannot publish a generation that another caller already replaced.

Buffer indices deliberately survive stop. On the next acquisition, exact old
chunks/timestamp members are retired under the nonce fence, before spawning.
This is a compatibility shim for the inspected manager shape, not a promise of
compatibility with arbitrary future Dispatcharr implementations.
"""

import importlib
import inspect
import logging
import select
import threading
import time
import types
import uuid


PROFILE_NAME = "Twitcharr Emby AAC Video First"
KEY_TTL = 3600
REFRESH_INTERVAL = 60
RETIRE_BATCH_SIZE = 128
MAX_RETIRE_TIMESTAMP_STEPS = 1024
MAX_RETIRE_TAIL_CHUNKS = 8192
_INSTALL_LOCK = threading.RLock()
_MARKER = "_twitcharr_output_lifecycle_guard"
_SERVER_MARKER = "_twitcharr_output_registry_guard"
_AUTO_SERVER = object()


def owner_key(channel_id, profile_id):
    return f"live:channel:{channel_id}:output:mpegts:p{profile_id}:owner"


def nonce_key(channel_id, profile_id):
    """Public key helper for narrowly scoped deployment checks/rollback."""
    return owner_key(channel_id, profile_id) + ":twitcharr-generation"


def state_key(channel_id, profile_id):
    return f"live:channel:{channel_id}:output:mpegts:p{profile_id}:state"


def buffer_index_key(channel_id, profile_id):
    return f"live:channel:{channel_id}:output:mpegts:p{profile_id}:buffer:index"


def chunk_prefix(channel_id, profile_id):
    return f"live:channel:{channel_id}:output:mpegts:p{profile_id}:buffer:chunk:"


def timestamps_key(channel_id, profile_id):
    return f"live:channel:{channel_id}:output:mpegts:p{profile_id}:buffer:chunk_timestamps"


ACQUIRE_LUA = """-- twitcharr-output-acquire-v1
local owner = redis.call('GET', KEYS[1])
if owner then
    if owner == ARGV[1] and redis.call('GET', KEYS[2]) == ARGV[2] then
        return 1
    end
    return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
redis.call('DEL', KEYS[3])
return 1
"""


MUTATE_LUA = """-- twitcharr-output-mutate-v1
if redis.call('GET', KEYS[1]) ~= ARGV[1]
    or redis.call('GET', KEYS[2]) ~= ARGV[2] then
    return 0
end
local action = ARGV[3]
if action == 'state' then
    redis.call('SET', KEYS[3], ARGV[5], 'EX', ARGV[4])
elseif action == 'refresh' then
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    redis.call('EXPIRE', KEYS[2], ARGV[4])
    redis.call('EXPIRE', KEYS[3], ARGV[4])
elseif action == 'release' then
    redis.call('DEL', KEYS[3])
    redis.call('DEL', KEYS[2])
    redis.call('DEL', KEYS[1]) -- native owner is released LAST
elseif action ~= 'check' then
    return redis.error_reply('unsupported lifecycle action')
end
return 1
"""


SNAPSHOT_LUA = """-- twitcharr-output-snapshot-v1
if redis.call('GET', KEYS[1]) ~= ARGV[1]
    or redis.call('GET', KEYS[2]) ~= ARGV[2] then
    return {0}
end
return {1, redis.call('GET', KEYS[3]) or '0'}
"""


RETIRE_LUA = """-- twitcharr-output-retire-v1
if redis.call('GET', KEYS[1]) ~= ARGV[1]
    or redis.call('GET', KEYS[2]) ~= ARGV[2] then
    return 0
end
local cutoff = ARGV[3]
local prefix = ARGV[4]
local function old_index(value)
    if not string.match(value, '^%d+$') then return false end
    local normalized = string.gsub(value, '^0+', '')
    if normalized == '' then normalized = '0' end
    return #normalized < #cutoff
        or (#normalized == #cutoff and normalized <= cutoff)
end
-- Validate every exact key/member BEFORE performing any deletion. String
-- comparison avoids Lua-number rounding for large monotonically growing IDs.
for i = 4, #KEYS do
    local key = KEYS[i]
    if string.sub(key, 1, #prefix) ~= prefix
        or not old_index(string.sub(key, #prefix + 1)) then
        return redis.error_reply('invalid retirement key')
    end
end
for i = 5, #ARGV do
    if not old_index(ARGV[i]) then
        return redis.error_reply('invalid retirement member')
    end
end
for i = 4, #KEYS do redis.call('DEL', KEYS[i]) end
for i = 5, #ARGV do redis.call('ZREM', KEYS[3], ARGV[i]) end
return 1
"""


def _key_text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _canonical_index(value):
    value = _key_text(value)
    if not value or not value.isascii() or not value.isdecimal():
        raise RuntimeError("invalid output buffer index")
    return value.lstrip("0") or "0"


def _old_index(value, cutoff):
    try:
        value = _canonical_index(value)
    except (RuntimeError, UnicodeError):
        return False
    return len(value) < len(cutoff) or (len(value) == len(cutoff) and value <= cutoff)


def _profile_is_owned(manager):
    # Resolve at start, not at import/plugin discovery (Django may not be ready).
    from core.models import OutputProfile

    return OutputProfile.objects.filter(
        pk=manager.profile_id, name=PROFILE_NAME
    ).exists()


def _compatible(cls):
    expected = {
        "__init__": ("self", "channel_id", "profile_id", "command", "ts_buffer", "worker_id"),
        "start": ("self",), "stop": ("self",),
        "_make_buffer": ("self",), "_acquire_owner_lock": ("self",),
        "_release_owner_lock": ("self",), "_set_state": ("self", "state"),
        "_refresh_redis_ttls": ("self",), "_cleanup_redis": ("self",),
        "_writer_loop": ("self",), "_reader_loop": ("self",),
        "_stderr_loop": ("self",),
    }
    try:
        return all(
            tuple(inspect.signature(getattr(cls, method)).parameters) == params
            for method, params in expected.items()
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _server_compatible(cls):
    expected = {
        "ensure_output_profile": ("self", "channel_id", "profile_id", "command"),
        "stop_output_profile": ("self", "channel_id", "profile_id"),
    }
    try:
        return all(
            tuple(inspect.signature(getattr(cls, method)).parameters) == params
            for method, params in expected.items()
        )
    except (AttributeError, TypeError, ValueError):
        return False


def install(manager_class=None, *, server_class=_AUTO_SERVER,
            profile_predicate=None, logger=None, clock=None, wait_readable=None):
    """Install once per class/worker, returning False on an unknown API shape.

    Dependencies are injectable for isolated tests; no Redis/Django access is
    made while installing. With no class injection, the native ProxyServer
    registry transactions are guarded as well. Manager-only injection omits
    the server unless explicitly provided (isolated unit tests).
    The predicate receives a manager or a scope object with profile_id,
    channel_id and worker_id. Unknown scope lookup errors delegate to native
    behavior; an already-managed instance remains guarded without a new lookup.
    """
    log = logger or logging.getLogger(__name__)
    try:
        native_classes = manager_class is None
        if manager_class is None:
            module = importlib.import_module(
                "apps.proxy.live_proxy.output.profile.manager"
            )
            manager_class = module.OutputProfileManager
        if server_class is _AUTO_SERVER:
            server_class = None
            if native_classes:
                module = importlib.import_module("apps.proxy.live_proxy.server")
                server_class = module.ProxyServer
        if not isinstance(manager_class, type) or (
                server_class is not None and not isinstance(server_class, type)):
            raise TypeError("invalid lifecycle class")
        if native_classes and server_class is None:
            raise TypeError("missing native registry class")
    except Exception:
        log.warning("Twitcharr output lifecycle guard unavailable")
        return False

    with _INSTALL_LOCK:
        installed = manager_class.__dict__.get(_MARKER)
        if installed:
            if server_class is None or installed.get("server_class") is server_class:
                return True
            log.warning("Twitcharr output guard already has a different registry scope")
            return False
        # Validate BOTH classes before mutating either, including an unexpected
        # pre-existing registry patch. Partial patching is unsafe at startup.
        if (not _compatible(manager_class)
                or (server_class is not None and (
                    not _server_compatible(server_class)
                    or server_class.__dict__.get(_SERVER_MARKER)))):
            log.warning("Twitcharr output lifecycle guard: incompatible manager/registry API")
            return False

        predicate = profile_predicate or _profile_is_owned
        now = clock or time.monotonic
        ready_to_read = wait_readable or (
            lambda stream: bool(select.select([stream], [], [], 1.0)[0])
        )
        originals = {
            name: getattr(manager_class, name)
            for name in ("start", "stop", "_acquire_owner_lock",
                         "_release_owner_lock", "_set_state",
                         "_refresh_redis_ttls", "_cleanup_redis",
                         "_writer_loop", "_reader_loop")
        }
        # One stable lock per pair. Locks are small and intentionally not evicted
        # while a stopped thread might still be returning through its finally.
        pairs = {}
        pairs_lock = threading.RLock()

        def pair_for_ids(channel_id, profile_id):
            key = (str(channel_id), str(profile_id))
            with pairs_lock:
                pair = pairs.get(key)
                if pair is None:
                    pair = {"lock": threading.RLock(), "owner": None}
                    pairs[key] = pair
                return pair

        def pair_for(manager):
            return pair_for_ids(manager.channel_id, manager.profile_id)

        def managed(manager):
            return getattr(manager, "_tg_managed", False)

        def known_local_owner(channel_id, profile_id):
            with pairs_lock:
                pair = pairs.get((str(channel_id), str(profile_id)))
                previous = pair["owner"] if pair is not None else None
                return previous is not None and managed(previous)

        def keys(manager):
            return (owner_key(manager.channel_id, manager.profile_id),
                    nonce_key(manager.channel_id, manager.profile_id),
                    state_key(manager.channel_id, manager.profile_id))

        def mutate(manager, action, value=""):
            if not getattr(manager, "_tg_owns", False):
                return False
            if not manager._redis:
                return None
            try:
                return bool(manager._redis.eval(
                    MUTATE_LUA, 3, *keys(manager), manager.worker_id,
                    manager._tg_token, action, KEY_TTL, value
                ))
            except Exception:
                log.warning("Twitcharr output lifecycle Redis operation failed")
                # Distinguish an observed token mismatch from an unknown result
                # caused by I/O failure. Only the former proves replacement.
                return None

        def retire_old_data(manager):
            # Installed RedisClient metadata/buffer clients share endpoint+DB;
            # only decode_responses differs. No binary chunk values are read.
            # Thus this connection can atomically fence exact DEL/ZREM calls.
            if manager._tg_retire_cutoff is None:
                result = manager._redis.eval(
                    SNAPSHOT_LUA, 3, *keys(manager)[:2],
                    buffer_index_key(manager.channel_id, manager.profile_id),
                    manager.worker_id, manager._tg_token,
                )
                if not result or result[0] != 1:
                    raise RuntimeError("output retirement lease unavailable")
                manager._tg_retire_cutoff = _canonical_index(result[1])
            cutoff = manager._tg_retire_cutoff
            prefix = chunk_prefix(manager.channel_id, manager.profile_id)
            timestamps = timestamps_key(manager.channel_id, manager.profile_id)

            def delete_batch(chunk_keys=(), members=()):
                if not chunk_keys and not members:
                    return
                if not manager._redis.eval(
                    RETIRE_LUA, 3 + len(chunk_keys), *keys(manager)[:2],
                    timestamps, *chunk_keys, manager.worker_id,
                    manager._tg_token, cutoff, prefix, *members,
                ):
                    raise RuntimeError("output retirement lease unavailable")

            entries_per_batch = RETIRE_BATCH_SIZE // 2
            # The inspected native buffer publishes contiguous local indices;
            # a partially executed SETEX/index/ZADD pipeline can leave only its
            # current head without a timestamp. Catch that head directly. FIFO
            # TTL expiry means a wholly missing descending batch ends the tail.
            # Walk BEFORE deleting stamped chunks, so legacy restarts cannot
            # hide an unstamped old head behind a freshly-created artificial gap.
            # This bounded walk never depends on unrelated Redis key count.
            upper, visited = int(cutoff), 0
            while upper > 0 and visited < MAX_RETIRE_TAIL_CHUNKS:
                if mutate(manager, "check") is not True:
                    raise RuntimeError("output retirement lease unavailable")
                count = min(entries_per_batch, upper, MAX_RETIRE_TAIL_CHUNKS - visited)
                members = [str(index) for index in range(upper, upper - count, -1)]
                chunk_keys = [prefix + member for member in members]
                if not manager._redis.exists(*chunk_keys):
                    break
                delete_batch(chunk_keys=chunk_keys, members=members)
                upper -= count
                visited += count
            else:
                if upper > 0:
                    raise RuntimeError("output retirement tail budget exceeded")

            # Timestamp members identify any remaining exact old chunk keys
            # without a global keyspace SCAN, even if the live tail has gaps.
            cursor = 0
            for _ in range(MAX_RETIRE_TIMESTAMP_STEPS):
                if mutate(manager, "check") is not True:
                    raise RuntimeError("output retirement lease unavailable")
                cursor, found = manager._redis.zscan(
                    timestamps, cursor=cursor, count=entries_per_batch
                )
                eligible = [_key_text(member) for member, _ in found if _old_index(member, cutoff)]
                for offset in range(0, len(eligible), entries_per_batch):
                    members = eligible[offset:offset + entries_per_batch]
                    delete_batch(chunk_keys=[prefix + member for member in members], members=members)
                if int(cursor) == 0:
                    break
            else:
                raise RuntimeError("output retirement timestamp budget exceeded")
            if mutate(manager, "check") is not True:
                raise RuntimeError("output retirement lease unavailable")
            manager._tg_retirement_pending = False

        def finalize(manager, retry_retirement=False):
            pair = pair_for(manager)
            with pair["lock"]:
                if getattr(manager, "_tg_finalized", False):
                    return True
                manager.running = False
                # No thread joins: reader/writer finally calls this same method.
                # Stop local writes before release; the guarded reader also
                # takes this pair lock around add_chunk.
                buffer = getattr(manager, "output_buffer", None)
                buffer_stopped = True
                try:
                    if buffer is not None:
                        buffer.stop()
                except Exception:
                    # Without proof that writes stopped, retain the lease.
                    log.warning("Twitcharr output buffer finalization failed")
                    buffer_stopped = False
                process = getattr(manager, "_process", None)
                try:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                    if process is not None and process.poll() is None:
                        return False
                except Exception:
                    log.warning("Twitcharr output process finalization failed")
                    return False
                if not buffer_stopped:
                    return False
                if (getattr(manager, "_tg_owns", False)
                        and getattr(manager, "_tg_retirement_pending", False)):
                    lease = mutate(manager, "check")
                    if lease is False:
                        # Our unspawned generation was replaced: do not retire
                        # somebody else's data and do not retain a local claim.
                        manager._tg_retirement_pending = False
                    elif lease is True and retry_retirement:
                        try:
                            retire_old_data(manager)
                        except Exception:
                            log.warning("Twitcharr old output retirement failed")
                            return False
                    else:
                        return False  # unknown result: preserve this lease
                try:
                    if process is not None and process.stdin is not None:
                        process.stdin.close()
                except Exception:
                    pass
                if getattr(manager, "_tg_owns", False):
                    # If the lease was already replaced, this is a harmless
                    # no-op; importantly, no index/chunk key is deleted.
                    if mutate(manager, "release") is None:
                        # Unknown Redis outcome must not permit another local
                        # generation. An observed token mismatch is safe: our
                        # process and local writes have already stopped.
                        return False
                manager._tg_owns = False
                manager._tg_finalized = True
                if pair["owner"] is manager:
                    pair["owner"] = None
                return True

        def start(manager):
            try:
                owned = getattr(manager, "_tg_managed", None)
                if owned is None:
                    owned = bool(predicate(manager))
                    manager._tg_managed = owned
            except Exception:
                log.warning("Twitcharr output profile scope could not be verified")
                # An existing local guarded generation is already scope
                # evidence, even while native code has popped its registry.
                owned = known_local_owner(manager.channel_id, manager.profile_id)
                manager._tg_managed = owned
                if not owned:
                    # Unknown scope must not disable unrelated profiles during
                    # a Django failure. This instance remains native thereafter.
                    return originals["start"](manager)
            if not owned:
                return originals["start"](manager)

            pair = pair_for(manager)
            with pair["lock"]:
                if getattr(manager, "_tg_started", False):
                    return bool(manager.running and not manager._tg_finalized)
                previous = pair["owner"]
                if previous is not None and previous is not manager:
                    if previous.worker_id == manager.worker_id:
                        if not finalize(previous, retry_retirement=True):
                            return False
                manager._tg_started = True
                manager._tg_finalized = False
                manager._tg_owns = False
                manager._tg_token = uuid.uuid4().hex
                manager._tg_last_refresh = float("-inf")
                manager._tg_retire_cutoff = None
                manager._tg_retirement_pending = False
                try:
                    started = originals["start"](manager)
                    if not started and manager._tg_owns:
                        finalize(manager)
                    return bool(started)
                except Exception:
                    log.warning("Twitcharr output start failed")
                    finalize(manager)
                    return False

        def stop(manager):
            if not managed(manager):
                return originals["stop"](manager)
            return finalize(manager, retry_retirement=True)

        def acquire(manager):
            if not managed(manager):
                return originals["_acquire_owner_lock"](manager)
            with pair_for(manager)["lock"]:
                if not manager._redis:
                    return False  # no distributed coordination: refuse ownership
                try:
                    acquired = bool(manager._redis.eval(
                        ACQUIRE_LUA, 3, *keys(manager), manager.worker_id,
                        manager._tg_token, KEY_TTL
                    ))
                except Exception:
                    log.warning("Twitcharr output owner acquisition failed")
                    return False
                manager._tg_owns = acquired
                if acquired:
                    pair_for(manager)["owner"] = manager
                    manager._tg_retirement_pending = True
                    try:
                        retire_old_data(manager)
                    except Exception:
                        log.warning("Twitcharr old output retirement failed")
                        # Escape native start before its _make_buffer/spawn;
                        # the wrapper keeps the lease on an unknown result.
                        raise RuntimeError("output retirement incomplete") from None
                return acquired

        def release(manager):
            if not managed(manager):
                return originals["_release_owner_lock"](manager)
            return finalize(manager)

        def set_state(manager, state):
            if not managed(manager):
                return originals["_set_state"](manager, state)
            with pair_for(manager)["lock"]:
                if not mutate(manager, "state", state):
                    raise RuntimeError("output lifecycle lease unavailable")

        def refresh(manager):
            if not managed(manager):
                return originals["_refresh_redis_ttls"](manager)
            with pair_for(manager)["lock"]:
                timestamp = now()
                if timestamp - manager._tg_last_refresh < REFRESH_INTERVAL:
                    return True
                if not mutate(manager, "refresh"):
                    manager.running = False
                    return False
                manager._tg_last_refresh = timestamp
                return True

        def cleanup(manager):
            if not managed(manager):
                return originals["_cleanup_redis"](manager)
            return finalize(manager, retry_retirement=True)

        def writer(manager):
            if not managed(manager):
                return originals["_writer_loop"](manager)
            try:
                return originals["_writer_loop"](manager)
            finally:
                finalize(manager)

        def reader(manager):
            if not managed(manager):
                return originals["_reader_loop"](manager)
            try:
                while manager.running:
                    if not refresh(manager):
                        break
                    if not ready_to_read(manager._process.stdout):
                        if manager._process.poll() is not None:
                            break
                        continue
                    data = manager._process.stdout.read(65536)
                    if not data:
                        break
                    with pair_for(manager)["lock"]:
                        if not manager.running or not mutate(manager, "check"):
                            manager.running = False
                            break
                        manager.output_buffer.add_chunk(data)
            except Exception:
                log.warning("Twitcharr output reader stopped after an error")
            finally:
                finalize(manager)

        if server_class is not None:
            server_originals = {
                name: getattr(server_class, name)
                for name in ("ensure_output_profile", "stop_output_profile")
            }

            def server_owned(server, channel_id, profile_id):
                existing = server.profile_managers.get(channel_id, {}).get(profile_id)
                if ((existing is not None and managed(existing))
                        or known_local_owner(channel_id, profile_id)):
                    return True
                scope = types.SimpleNamespace(
                    channel_id=channel_id, profile_id=profile_id,
                    worker_id=getattr(server, "worker_id", None),
                )
                return bool(predicate(scope))

            def ensure_output_profile(server, channel_id, profile_id, command):
                try:
                    owned = server_owned(server, channel_id, profile_id)
                except Exception:
                    log.warning("Twitcharr output profile scope could not be verified")
                    return server_originals["ensure_output_profile"](
                        server, channel_id, profile_id, command
                    )
                if not owned:
                    return server_originals["ensure_output_profile"](
                        server, channel_id, profile_id, command
                    )
                # Includes lookup, stale-manager stop, start, and publication.
                # Locking start alone permits a late caller to publish dead A
                # after another caller has replaced it with live B.
                with pair_for_ids(channel_id, profile_id)["lock"]:
                    return server_originals["ensure_output_profile"](
                        server, channel_id, profile_id, command
                    )

            def stop_output_profile(server, channel_id, profile_id):
                try:
                    owned = server_owned(server, channel_id, profile_id)
                except Exception:
                    log.warning("Twitcharr output profile scope could not be verified")
                    return server_originals["stop_output_profile"](
                        server, channel_id, profile_id
                    )
                if not owned:
                    return server_originals["stop_output_profile"](
                        server, channel_id, profile_id
                    )
                # Keep native pop/remove/stop in the same registry transaction.
                with pair_for_ids(channel_id, profile_id)["lock"]:
                    return server_originals["stop_output_profile"](
                        server, channel_id, profile_id
                    )

        for name, method in {
            "start": start, "stop": stop, "_acquire_owner_lock": acquire,
            "_release_owner_lock": release, "_set_state": set_state,
            "_refresh_redis_ttls": refresh, "_cleanup_redis": cleanup,
            "_writer_loop": writer, "_reader_loop": reader,
        }.items():
            setattr(manager_class, name, method)
        if server_class is not None:
            server_class.ensure_output_profile = ensure_output_profile
            server_class.stop_output_profile = stop_output_profile
            setattr(server_class, _SERVER_MARKER, {
                "manager_class": manager_class, "originals": server_originals,
            })
        setattr(manager_class, _MARKER, {
            "originals": originals, "server_class": server_class,
        })
        return True
